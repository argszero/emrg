"""A release step that locates its leg's product must refuse when the product is absent.

Rant 2026-10-08 (`2026-10-08T09:27:29`, issue #1913). `build-release.yml`'s three macOS
steps each begin by locating the pkg and **skip silently** when it is missing, while the
Windows step in the same job refuses:

    - name: Zip installer (Windows only)          # refuses
        EXE="$(find . -maxdepth 1 -name 'EMRG-*-windows-x64.exe' | head -1)"
        if [ -z "$EXE" ]; then
          echo "::error::no EMRG-*-windows-x64.exe found in dist/artifacts"
          exit 1
        fi

    - name: Sign pkg (macOS only)                 # used to skip
        PKG="$(find dist/artifacts -maxdepth 1 -name 'EMRG-*-macos-*.pkg' | head -1)"
        if [ -z "$PKG" ]; then echo "no pkg found, skipping"; exit 0; fi

So a leg that runs and produces **nothing** exits 0 three times, and the release job's
completeness assertion cannot notice, because its expected set is derived from the same
files the build produced: a platform absent from the build is absent from both sides of
that comparison. The v0.3.1 incident that assertion was written for — 9 built, 8
published — is a **truncated upload**; this is a product that was never built.

## What this guard does

It derives its subjects from the **parsed** workflow rather than naming the steps, so a
step that locates a product by pattern becomes a subject the moment it exists. For each
subject it runs the step's **own shell text** — the discovery assignment plus the
emptiness check, and the `cd` above them — under the same flags the runner uses
(`bash --noprofile --norc -eo pipefail`), in both directions:

    absent  -> non-zero exit, an `::error::`, and the step's own glob in the message
    present -> exit 0, and no `::error::`

Only the prefix through the emptiness check is executed, so the run never reaches the
signing, notarizing or stapling commands — the guard cannot sign, notarize or publish
anything, and needs no stubbed toolchain. The Windows step is a **positive control** in
every run: it already refuses, so a guard that reports failure for it is measuring itself.

The other half of #1913 — the release job's self-referential expected set — is deliberately
not guarded here: deriving it from the downloaded artifacts is a documented choice, and
refusing an absent product at the source is what makes that derivation trustworthy.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = ".github/workflows/build-release.yml"

# The runner's flags for `shell: bash`. Spelled once, because a friendlier interpreter
# default would not exercise the same failure modes the runner has.
RUNNER_SHELL = ["--noprofile", "--norc", "-eo", "pipefail"]

# `VAR="$(find <path> ... -name '<glob>' ...)"` — how a step asks "is my product here?".
# Derived, never a list of step names: a regex over the step bodies is what makes a future
# leg covered without editing this file.
_DISCOVERY = re.compile(
    r"""^\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)="\$\(\s*find\s+(?P<path>\S+)(?P<rest>.*?)"""
    r"""-name\s+'(?P<pattern>[^']+)'"""
)

# `if [ -z "$VAR" ]; then` — the emptiness check that decides what an absent product means.
_EMPTY_CHECK = re.compile(
    r'^\s*if\s+\[\s+-z\s+"\$(?P<var>[A-Za-z_][A-Za-z0-9_]*)"\s*\]\s*;\s*then\b'
)

_CD = re.compile(r"^\s*cd\s+(?P<dir>\S+)\s*$")


@dataclass(frozen=True)
class Subject:
    """One step that locates its leg's product by pattern."""

    job: str
    step: str
    var: str
    pattern: str
    product_dir: str
    fragment: str

    def __str__(self) -> str:  # pytest ids
        return f"{self.step} [{self.pattern}]"


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.is_file(), f"{rel} is missing — the guard cannot measure what it guards"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{rel} is empty — a vacuous read must not read as a pass"
    return text


def _body(step: dict) -> list[str]:
    """The step's shell lines, comments dropped — a read is a read, not a mention of one."""
    return [
        ln for ln in str(step.get("run") or "").splitlines() if not ln.strip().startswith("#")
    ]


def _discovery_var(line: str) -> str | None:
    m = _DISCOVERY.match(line)
    return m.group("var") if m else None


def _fragment(lines: list[str], start: int) -> str | None:
    """From the discovery line, the step's own text through the end of the emptiness check.

    Both shapes the file has carried: the one-line `if [ -z "$X" ]; then ...; fi`, and the
    block form the refusal needs. Returning None (rather than a guess) when neither shape is
    found keeps "could not read this step" from passing as "this step is fine".
    """
    for i in range(start + 1, len(lines)):
        m = _EMPTY_CHECK.match(lines[i])
        if not m or m.group("var") != _discovery_var(lines[start]):
            continue
        if re.search(r";\s*fi\s*$", lines[i]):
            return "\n".join(lines[start : i + 1]) + "\n"
        for j in range(i + 1, len(lines)):
            if lines[j].strip() == "fi":
                return "\n".join(lines[start : j + 1]) + "\n"
        return None
    return None


def _subjects(workflow_text: str) -> list[Subject]:
    """Every step that locates its leg's product, derived from the workflow."""
    doc = yaml.safe_load(workflow_text)
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict) and jobs, "the workflow declares no jobs"

    found: list[Subject] = []
    for job_name, job in jobs.items():
        steps = job.get("steps") if isinstance(job, dict) else None
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            lines = _body(step)
            for i, line in enumerate(lines):
                m = _DISCOVERY.match(line)
                if not m:
                    continue
                fragment = _fragment(lines, i)
                if fragment is None:
                    continue
                # The `cd` above the discovery line is part of the step's own text and is a
                # pure directory change; it is carried so `find .` resolves where the step
                # means it to, and nothing else from the preamble is.
                preamble = [ln for ln in lines[:i] if _CD.match(ln)]
                found.append(
                    Subject(
                        job=job_name,
                        step=str(step.get("name") or "(unnamed step)"),
                        var=m.group("var"),
                        pattern=m.group("pattern"),
                        product_dir=os.path.normpath(
                            os.path.join(*(_cd_dir(preamble) or ["."]), m.group("path"))
                        ),
                        fragment="\n".join(preamble + [fragment]) if preamble else fragment,
                    )
                )
    return found


def _cd_dir(preamble: list[str]) -> list[str] | None:
    for line in reversed(preamble):
        m = _CD.match(line)
        if m:
            return [m.group("dir")]
    return None


# The body is a POSIX shell script and the arms below execute it. On the Windows runner
# `bash` resolves to the WSL launcher, which answers "no installed distributions" and exits
# 1 — so a run there measures the shell's absence, not the step (measured on the v0.3.8
# re-run, 36959438654). The macOS steps this guards only ever run on macOS.
_posix_shell_only = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the step body is a POSIX shell script and this arm executes it; without a real "
        "bash the run measures the shell's absence, not the step"
    ),
)


def _run(subject: Subject, root: Path, *, present: bool) -> subprocess.CompletedProcess:
    """Run the subject's own text under the runner's shell, with the product present or not.

    The shell is resolved rather than spelled `bash` for the reason above. No toolchain stub
    is needed: the executed text stops at the emptiness check, so it cannot reach a real
    `productsign`, `notarytool` or `stapler`.
    """
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")

    product_dir = root / subject.product_dir
    product_dir.mkdir(parents=True, exist_ok=True)
    if present:
        # Instantiate the step's own glob: every `*` becomes a character, so the name the
        # step searches for really matches. Derived from the pattern, never hand-written.
        (product_dir / subject.pattern.replace("*", "x")).write_bytes(b"product")

    script = root / "step.sh"
    script.write_text(subject.fragment, encoding="utf-8")
    return subprocess.run(
        [shell, *RUNNER_SHELL, str(script)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _combined(result: subprocess.CompletedProcess) -> str:
    return f"{result.stdout}{result.stderr}"


def test_the_subject_set_is_derived_from_the_workflow_and_not_empty() -> None:
    """A guard with no subjects passes on any workflow, so the set is asserted first."""
    subjects = _subjects(_read(WORKFLOW))
    assert subjects, (
        f"no step in {WORKFLOW} locates its leg's product by pattern — the guard has "
        "nothing to measure, which is not the same as measuring a clean tree"
    )
    # Every subject was read from a parsed step and carries that step's own text; a subject
    # whose fragment is not a prefix of its step would be a copy, and a copy can drift.
    for s in subjects:
        assert s.fragment.strip(), f"{s.step}: empty fragment"
        assert s.var and s.pattern, f"{s.step}: the discovery line was not read"


def test_a_step_added_later_becomes_a_subject_without_editing_this_file() -> None:
    """The derivation itself, proved on a workflow that does not exist in the repo.

    Without this arm the guard could be a list of names in disguise: it would keep passing
    after a new leg step appeared, and the defect would return through the step nobody
    guarded.
    """
    synthetic = """
jobs:
  build:
    steps:
      - name: A leg nobody has written yet
        run: |
          LI="$(find dist/artifacts -maxdepth 1 -name 'EMRG-*-linux-*.run' | head -1)"
          if [ -z "$LI" ]; then
            echo "::error::no EMRG-*-linux-*.run found in dist/artifacts"
            exit 1
          fi
"""
    subjects = _subjects(synthetic)
    assert [s.step for s in subjects] == ["A leg nobody has written yet"], (
        "a step that locates a product by pattern was not taken as a subject — the subjects "
        f"are not derived from the workflow: {subjects!r}"
    )
    assert subjects[0].pattern == "EMRG-*-linux-*.run"
    assert subjects[0].product_dir == "dist/artifacts"


@_posix_shell_only
@pytest.mark.parametrize("subject", _subjects(_read(WORKFLOW)), ids=str)
def test_an_absent_product_is_refused_loudly(subject: Subject, tmp_path) -> None:
    """The defect itself: absent product -> non-zero exit and an `::error::` naming it.

    All three assertions are needed and none implies another: exiting non-zero without a
    message leaves the log saying nothing about what was missing, and a message that names
    some other pattern sends the reader after the wrong file.
    """
    result = _run(subject, tmp_path, present=False)
    combined = _combined(result)
    assert result.returncode != 0, (
        f"{subject.step} exited 0 with no {subject.pattern} present. A leg that ran and "
        "produced nothing would then report success, and the release job cannot notice: its "
        f"expected asset set is derived from the same build.\noutput={combined!r}"
    )
    assert "::error::" in combined, (
        f"{subject.step} failed without an `::error::` annotation, so GitHub reports only a "
        f"non-zero exit and the log never says what was missing.\noutput={combined!r}"
    )
    assert subject.pattern in combined, (
        f"{subject.step}'s refusal does not name the glob it searched for "
        f"({subject.pattern!r}), so the reader has to guess which product was absent.\n"
        f"output={combined!r}"
    )


@_posix_shell_only
@pytest.mark.parametrize("subject", _subjects(_read(WORKFLOW)), ids=str)
def test_a_present_product_is_not_refused(subject: Subject, tmp_path) -> None:
    """The other direction, so the guard is a check and not a rule that always fires.

    A step that refused unconditionally would satisfy the arm above while breaking every
    real release.
    """
    result = _run(subject, tmp_path, present=True)
    combined = _combined(result)
    assert result.returncode == 0, (
        f"{subject.step} refused although {subject.pattern} was present in "
        f"{subject.product_dir!r} — the guard above would pass on a step that can never "
        f"release.\noutput={combined!r}"
    )
    assert "::error::" not in combined, (
        f"{subject.step} reported an error although its product was present.\n"
        f"output={combined!r}"
    )
