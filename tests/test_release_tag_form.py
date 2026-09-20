"""A release tag's *form* is part of the release, and the instruction is what produced it.

Measured defect (2026-09-20): `v0.2.97` reached GitHub as a **lightweight** tag while every
release since `v0.2.94` is **annotated** — it was the only tag in the series with no `^{}`
dereference line (`git ls-remote --tags origin`). The build that followed was not broken *by
the tag form*: it failed for an unrelated reason (run `35479263507`, real-daemon GUI tests in
the Windows leg, fixed by `#1457`, with `release` skipped), and the green run is the re-tag run
`35481341873`. The artifact simply did not match the line it belonged to. The command that
produced it was the one this repository taught: `Agent.md` §Releasing step 2 and both next-step
messages in `scripts/bump-version.py` said `git tag v<x.y.z>`, which is exactly a lightweight
tag.

Two halves, both mechanised here rather than left as prose:

* the **instruction** carries the annotated form (this file), so it cannot drift back to the
  bare spelling without a red test;
* the **artifact** is checked where it arrives — `build-release.yml`'s `verify-tag` job reads
  the tag object's type from the API before any platform builds. That check's discriminating
  signal was measured in both states against real tags, because a test cannot make that call
  offline: `git/ref/tags/<tag>` → `object.type` is `tag` for `v0.2.97`/`v0.2.96` (annotated)
  and `commit` for `v0.2.92`/`v0.2.93` (lightweight).

The pin on that job is written three ways on purpose, because each way is blind where the
others see — measured 2026-09-20 by a mutation battery over the workflow text (eleven edits
that each keep the file parseable and leave every other assertion green):

* what the job **reads** (a regex on the API call), because four edits keep the read and
  neutralise the *verdict* — `if false`, an inverted comparison, a comparison against a
  literal, and a dropped `exit 1` — and none of them is visible in the text;
* the **wiring** it must keep (job presence, `needs`, the ref gate, the dependents' `if`), each
  read from the parsed job and not from the document, because two of those spellings occur in
  more than one job here and a document-level match cannot tell which job lost the line;
* the **verdict** itself, executed in both states with `gh` stubbed — the only arm that sees a
  weakened condition, and the only one that would notice the job simply never asking.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.is_file(), f"{rel} is missing — the guard cannot measure what it guards"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{rel} is empty — a vacuous read must not read as a pass"
    return text


def _release_step_two() -> str:
    """The `2. **Tag**` line of `Agent.md`'s Releasing section, or a loud failure.

    A missing line must fail here rather than yield `""` and let every `in` assertion below
    pass on nothing — the deletion of the step is the largest drift this guard exists to catch.
    """
    text = _read("Agent.md")
    section = re.search(r"^## Releasing\n(.*?)(?=^## )", text, re.S | re.M)
    assert section, "Agent.md has no `## Releasing` section"
    lines = [ln for ln in section.group(1).splitlines() if ln.startswith("2. **Tag**")]
    assert len(lines) == 1, f"expected exactly one `2. **Tag**` step, got {len(lines)}"
    return lines[0]


def _jobs() -> dict:
    """`build-release.yml`'s jobs, parsed — wiring is read from a job, never from the file.

    Both wiring spellings asserted below occur in more than one job
    (`startsWith(github.ref, 'refs/tags/')` gates `release` as well), so a document-level match
    stays green when the job it names loses the line: measured by arm, dropping `verify-tag`'s
    gate left a substring pin green because `release` still carried it. (PyYAML reads a bare
    `on:` key as True — only `jobs` is read here.)
    """
    jobs = yaml.safe_load(_read(".github/workflows/build-release.yml")).get("jobs")
    assert isinstance(jobs, dict) and jobs, "build-release.yml carries no `jobs` mapping"
    return jobs


def test_agent_md_teaches_the_annotated_tag() -> None:
    step = _release_step_two()
    # The **taught command** is the thing that produced the defect, so pin that and not the
    # whole line: the line legitimately mentions the bare spelling while naming it as wrong,
    # and a global negative would make explaining the defect impossible.
    taught = re.search(r"`([^`]*git tag[^`]*)`", step)
    assert taught, f"the tag step carries no backticked command: {step}"
    command = taught.group(1)
    assert command.startswith("git tag -a v<x.y.z> -m "), (
        f"Agent.md's taught tag command is not annotated: {command}"
    )
    assert "git push origin v<x.y.z>" in command, f"the taught step does not push: {command}"


def test_bump_version_prints_the_annotated_tag_in_both_sites() -> None:
    text = _read("scripts/bump-version.py")
    # The module docstring's numbered flow and the message printed after a real bump are two
    # separate sites; a fix applied to one of them is the shape this asserts against.
    assert 'git tag -a vX.Y.Z -m "emrg vX.Y.Z"' in text, "the docstring's step 3 is not annotated"
    assert "git tag -a v{args.version} -m" in text, (
        "the printed next-step message is not annotated"
    )
    assert "git tag vX.Y.Z &&" not in text, "the docstring still teaches the lightweight form"
    assert "git tag v{args.version} &&" not in text, "the printed message still does"


def test_build_release_checks_the_tag_form_before_building() -> None:
    wf = _read(".github/workflows/build-release.yml")
    jobs = _jobs()
    assert "verify-tag" in jobs, "build-release.yml has no `verify-tag` job"
    # Pin the *field read*, not the word: `object.type` also appears in the job echo /
    # `::error::` message and in the comment above it, so a substring assert stays green
    # even when the API read itself is replaced by a constant — measured by arm on this
    # tree (substring: pass, this assertion: fail). Raised by an external review of this PR.
    assert re.search(r"gh api [^\n]*--jq '\.object\.type'", wf), (
        "the verify-tag job does not read the tag object type from the API: the word "
        "`object.type` in the job echo/::error:: message, or in a comment above it, "
        "would satisfy a substring check"
    )
    needs = jobs.get("build", {}).get("needs")
    assert needs in ("verify-tag", ["verify-tag"]), (
        f"`build` does not depend on `verify-tag` (needs={needs!r}) — the check is orphaned "
        "and cannot fail a build"
    )
    gate = str(jobs["verify-tag"].get("if") or "")
    assert "startsWith(github.ref, 'refs/tags/')" in gate, (
        f"`verify-tag` is not gated on a tag ref (if={gate!r}) — a branch dispatch would fail "
        "it. Read from the job: the same spelling gates `release`, so a document-level match "
        "cannot say which job lost the line."
    )
    # The check is scoped to tag refs, so on a branch dispatch this job is **skipped** — and
    # `build` mirrors that skip. Measured 2026-09-20 (run 35484455980, a throwaway probe
    # carrying these exact shapes): a job whose `needs` target was skipped concludes
    # **skipped** itself, so a branch dispatch would report `success` having built nothing.
    # `if: !failure() && !cancelled()` is the remedy, and the probe's failure arm showed it
    # still skips when the dependency *fails*, so the gate keeps its power. Read from the
    # parsed job rather than by an anchored regex: the regex form walks the rest of the file,
    # so it was blind to exactly the regression it existed to catch (measured by arm).
    condition = str(jobs["build"].get("if") or "")
    assert "!failure()" in condition and "!cancelled()" in condition, (
        f"`build` does not carry `if: ${{{{ !failure() && !cancelled() }}}}` (if={condition!r}) "
        "— a skipped `verify-tag` skips it too, and a branch dispatch would go green having "
        "built nothing"
    )


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the step body is a POSIX shell script: what this arm measures is the job's own "
        "verdict, so it is gated to the platform that can run one."
    ),
)
def test_the_tag_check_refuses_a_lightweight_tag(tmp_path) -> None:
    """The step's verdict, executed in both states instead of read.

    The assertions above bind *what the job reads*; measured 2026-09-20 (an external review's
    mutation battery over this file, reproduced here) they are blind to every edit that keeps
    the read and neutralises the verdict — `if false; then`, an inverted comparison, a
    comparison against a literal, and a dropped `exit 1` — which are the shapes a future edit
    takes when it weakens this job without touching the line the regex names. So the step's
    `run:` body is taken from the parsed workflow and **executed** with `gh` stubbed as a shell
    function: no network, no real API call, and the stub records the arguments it was called
    with, because a stub that ignored its own argv would itself be blind to a dropped
    `--jq '.object.type'`.
    """
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")
    jobs = _jobs()
    steps = (jobs.get("verify-tag") or {}).get("steps") or []
    bodies = [step.get("run") for step in steps if isinstance(step, dict) and step.get("run")]
    assert len(bodies) == 1, (
        f"expected exactly one `run:` step in `verify-tag`, got {len(bodies)} — a job that "
        "carries no script, or more than one, is not what the two runs below measure"
    )
    assert isinstance(bodies[0], str), f"the step's `run:` is not a script: {bodies[0]!r}"
    argv_log = tmp_path / "argv.log"
    argv_log.write_text("", encoding="utf-8")
    stub = rf'gh() {{ printf "%s\n" "$*" >> "{argv_log}"; printf "%s" "$STUB_KIND"; }}' + "\n"
    base_env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_REPOSITORY": "argszero/emrg",
        "GITHUB_REF_NAME": "v9.9.9",
    }

    def run(kind: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [shell, "-c", stub + bodies[0]],
            env={**base_env, "STUB_KIND": kind},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    annotated, lightweight = run("tag"), run("commit")
    assert annotated.returncode == 0, (
        f"the job fails an annotated tag (object.type=tag): rc={annotated.returncode} "
        f"{annotated.stdout}{annotated.stderr}"
    )
    assert lightweight.returncode != 0, (
        "the job accepts a lightweight tag (object.type=commit) — it no longer refuses what it "
        f"exists to refuse: rc={lightweight.returncode} {lightweight.stdout}"
    )
    called = argv_log.read_text(encoding="utf-8")
    assert "git/ref/tags/v9.9.9" in called, (
        f"the job never asked the API about this ref; it asked: {called.strip()!r}"
    )
    assert "--jq .object.type" in called, (
        "the job asked for the ref but not for `object.type`, so the verdict rests on the whole "
        f"object rather than on the field both runs above are built from: {called.strip()!r}"
    )
