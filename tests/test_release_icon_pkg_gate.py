"""The release's icon acceptance must be wired into CI, not left to a cycle's hands.

Issue #2283. The `Generate icon assets` step of `build-release.yml` asserts the generated
`.icns` is **non-empty** (`test -s`), and a ring baked into the render is non-empty: it
passes every gate this repo runs, and the host sees the white border after installing
(rant 2026-10-13T17:45:42). The tool that answers the question has existed since
`cyc20261014-125204` (`scripts/check-icon-transparency.py`, documented in
`DEVELOPMENT.md`), but nothing ran it: the hand-run was attempted twice and completed zero
times, and CI would not have noticed either way.

Measured 2026-10-14 (`cyc20261015-072903`) on the **published** v0.3.12 pkg (sha256
`fdc0c380e2b1f273e0c41391568dbf6409c81b232a5655343c955097a0323de3`, 141895388 bytes):
`pkgutil --expand` yields `EMRG-component.pkg/Payload`, the payload holds exactly one icon
(`emrg-gui/EMRG.app/Contents/Resources/icon.icns`, 612824 bytes, no AppleDouble sibling),
and the tool reads it `clear` on all 8 PNG representations -- rc 0. A synthetic pkg whose
app icon is opaque reads `FAULT`, rc 1. So the acceptance is satisfiable here, on the
artifact, before upload.

These arms read the step from the **parsed** workflow (never by a text search: the sign,
notarize and staple steps all locate the pkg with the same `find`, so a document-level
match cannot say which step was read) and execute its body the way the runner runs it,
with `pkgutil`, `gunzip`, `cpio` and `uv` stubbed. The tool's *verdicts* are pinned by
`tests/test_check_icon_transparency.py` on fixtures; what is pinned here is the **wiring**:
which tree the step hands the tool, and that both of the tool's failure codes -- and a
missing pkg -- end the step red instead of green.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

WORKFLOW = ".github/workflows/build-release.yml"

STEP_NAME = "Assert the pkg's icon kept its transparency (macOS only)"

TOOL = "scripts/check-icon-transparency.py"

# The runner's default flags for `shell: bash` -- `-e` is what turns a failed call into a
# red step, so the arms must run under them and not under a friendlier interpreter default.
RUNNER_SHELL = ["--noprofile", "--norc", "-eo", "pipefail"]

PKG_NAME = "EMRG-0.3.13-macos-arm64.pkg"

# What the extraction has to leave on disk for the tool to have a subject: the app bundle
# path inside the payload, exactly as the published pkg carries it.
APP_ICON = "emrg-gui/EMRG.app/Contents/Resources/icon.icns"

_PKGUTIL_STUB = '''#!/usr/bin/env python3
"""Stand in for pkgutil: `--expand PKG DIR` writes the layout the real one writes."""
import os, sys

argv = sys.argv[1:]
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write("pkgutil " + " ".join(argv) + "\\n")
if argv[:1] != ["--expand"] or len(argv) != 3:
    sys.exit(f"stub only speaks `--expand PKG DIR`, got {argv!r}")
component = os.path.join(argv[2], "EMRG-component.pkg")
os.makedirs(component, exist_ok=True)
if os.environ.get("STUB_PAYLOAD", "1") == "1":
    with open(os.path.join(component, "Payload"), "wb") as fh:
        fh.write(b"a gzipped cpio payload, in the stub\\n")
sys.exit(0)
'''

_GUNZIP_STUB = '''#!/usr/bin/env python3
"""Stand in for gunzip -dc: pass the payload through, and record the read."""
import os, sys

with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write("gunzip " + " ".join(sys.argv[1:]) + "\\n")
sys.stdout.write("payload bytes\\n")
sys.exit(0)
'''

_CPIO_STUB = '''#!/usr/bin/env python3
"""Stand in for `cpio -i -d`: consume the archive and lay the app down where it belongs.

`cpio` extracts into its **current** directory, which is what the step's subshell sets, so
the stub writes the same relative tree rather than an absolute one handed to it.
"""
import os, sys

with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write("cpio " + " ".join(sys.argv[1:]) + "\\n")
sys.stdin.read()
target = os.environ["STUB_APP_ICON"]
os.makedirs(os.path.dirname(target), exist_ok=True)
with open(target, "wb") as fh:
    fh.write(b"icns fixture")
sys.exit(0)
'''

_UV_STUB = '''#!/usr/bin/env python3
"""Stand in for the tool's interpreter: record the argv, answer the scenario's code."""
import os, sys

with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write("uv " + " ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ["TOOL_RC"]))
'''


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.is_file(), f"{rel} is missing -- the guard cannot measure what it guards"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{rel} is empty -- a vacuous read must not read as a pass"
    return text


def _icon_step() -> dict:
    jobs = yaml.safe_load(_read(WORKFLOW)).get("jobs")
    assert isinstance(jobs, dict) and "build" in jobs, "build-release.yml has no `build` job"
    steps = jobs["build"].get("steps")
    assert isinstance(steps, list) and steps, "the `build` job carries no steps"
    named = [s for s in steps if isinstance(s, dict) and s.get("name") == STEP_NAME]
    assert len(named) == 1, f"expected exactly one {STEP_NAME!r} step, got {len(named)}"
    step = named[0]
    assert step.get("run"), f"the {STEP_NAME!r} step carries no `run:` body"
    return step


def _body(step: dict) -> str:
    """The body with comment lines dropped -- a read must be a read, not a mention of one."""
    body = "\n".join(
        ln for ln in str(step["run"]).splitlines() if not ln.strip().startswith("#")
    )
    assert body.strip(), "the icon step body is nothing but comments"
    return body


_posix_shell_only = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the step body is a POSIX shell script and this arm executes it; without a real "
        "bash the run measures the shell's absence, not the step. Gated to the platforms "
        "that can run one -- the step itself is macOS-only."
    ),
)


def _run_step(tmp_path, tool_rc: int, with_pkg: bool = True, with_payload: bool = True):
    """Run the step body as the runner runs it, with its four external commands stubbed.

    `TMPDIR` is pinned to the arm's own directory so the tree the step extracts stays
    readable: the arm asserts *what* the tool was pointed at, which is the whole question
    this file exists to answer.
    """
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, text in (
        ("pkgutil", _PKGUTIL_STUB),
        ("gunzip", _GUNZIP_STUB),
        ("cpio", _CPIO_STUB),
        ("uv", _UV_STUB),
    ):
        stub = bindir / name
        stub.write_text(text, encoding="utf-8")
        stub.chmod(0o755)

    artifacts = tmp_path / "dist" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    if with_pkg:
        (artifacts / PKG_NAME).write_bytes(b"pkg")

    calls = tmp_path / "calls.txt"
    calls.write_text("", encoding="utf-8")

    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(exist_ok=True)

    script = tmp_path / "step.sh"
    script.write_text(_body(_icon_step()), encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["STUB_CALLS"] = str(calls)
    env["STUB_PAYLOAD"] = "1" if with_payload else "0"
    env["TOOL_RC"] = str(tool_rc)
    # The path the extraction stub writes is **relative**: `cpio` extracts into its own cwd.
    env["STUB_APP_ICON"] = APP_ICON
    # `mktemp` is pinned through the standard variable the step honours, so the tree it
    # extracts lands inside this arm's directory instead of the shared temp area.
    env["TMPDIR"] = str(tmpdir)

    result = subprocess.run(
        [shell, *RUNNER_SHELL, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    result.calls = calls.read_text(encoding="utf-8").splitlines()  # type: ignore[attr-defined]
    result.tmpdir = tmpdir  # type: ignore[attr-defined]
    return result


def _tool_call(calls: list[str]) -> str | None:
    return next((ln for ln in calls if TOOL in ln), None)


# ── the wiring ───────────────────────────────────────────────────────────────

@_posix_shell_only
def test_the_step_reads_the_icon_out_of_the_tree_it_extracted(tmp_path) -> None:
    """The one direction that must stay green: the tool gets the payload, not a path guess.

    The arm asserts *what* the tool was pointed at -- a directory that really holds the app
    bundle's icon after the extraction step -- because a step that ran the tool against
    `packaging/assets/icon.icns` (the gitignored, stale local product the first hand-run
    measured) would satisfy every count of "the tool runs here" and answer about nothing
    the release ships.
    """
    result = _run_step(tmp_path, tool_rc=0)
    assert result.returncode == 0, (
        "the step reddened with the tool answering rc 0"
        f"\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    call = _tool_call(result.calls)
    assert call is not None, (
        f"the step never ran {TOOL} -- recorded calls: {result.calls!r}"
    )
    # The subject is taken from the call itself: the arm must not assume where the step put
    # its scratch dir, only that the tool was pointed at the tree the extraction produced.
    subject = Path(call.rsplit(None, 1)[-1])
    assert subject.is_dir() and subject.name == "payload", (
        f"the tool was pointed at something other than the extracted payload: {call!r}"
    )
    assert subject.resolve().is_relative_to(result.tmpdir.resolve()), (
        f"the reading is about a tree outside this arm's own temp root: {subject}"
    )
    assert (subject / APP_ICON).is_file(), (
        "the tool was pointed at a directory that does not hold the app's icon -- the "
        "extraction stub's own relative tree, so this arm is measuring the step's shell "
        "rather than the stubs"
    )
    assert "pkgutil --expand" in " ".join(result.calls), (
        f"the step read no payload out of the pkg: {result.calls!r}"
    )


def test_the_step_is_macos_only_and_not_allowed_to_fail_silently() -> None:
    """`if:` and `continue-on-error` are the wiring, and the parsed YAML is where they live.

    An unwired tool is a step that silently stops running: a missing `if:` would run the
    macOS-only `pkgutil` on the Windows and Linux legs, and a `continue-on-error: true`
    would report a release with an opaque icon as a success.
    """
    step = _icon_step()
    assert step.get("if") == "runner.os == 'macOS'", (
        f"the icon step's guard is {step.get('if')!r} -- `pkgutil`, `gunzip` and `cpio` are "
        f"POSIX commands and this reading is about the macOS pkg"
    )
    assert "continue-on-error" not in step, (
        "the icon step may not be allowed to fail quietly: its whole purpose is to redden a "
        "release that ships an opaque icon"
    )
    body = _body(step)
    assert TOOL in body, f"the step body does not run the repository's own tool ({TOOL})"
    # Both of the tool's failure codes are answered for, and the answers differ: 1 is a
    # verdict about the artifact, 2 is "nothing was read", and a reader must not be sent to
    # the wrong cause for either.
    assert "::error::" in body, "the step has no error path"
    assert "opaque" in body and "not a pass" in body, (
        "the step does not distinguish the tool's two failure codes in its own words"
    )


# ── both failure codes, and the two ways the subject can be missing ──────────

@_posix_shell_only
def test_an_opaque_icon_fails_the_step(tmp_path) -> None:
    """rc 1 must redden the job rather than being reported as "the tool complained"."""
    result = _run_step(tmp_path, tool_rc=1)
    assert result.returncode != 0, (
        "the step PASSED with the tool reporting an opaque icon"
        f"\nstdout={result.stdout!r}"
    )
    assert "opaque" in result.stdout, (
        f"the step failed without naming the cause it read.\nstdout={result.stdout!r}"
    )


@_posix_shell_only
def test_an_unreadable_subject_fails_the_step(tmp_path) -> None:
    """rc 2 is the other failure, and it is not the same one: nothing was read at all."""
    result = _run_step(tmp_path, tool_rc=2)
    assert result.returncode != 0, (
        "the step PASSED with the tool reporting that it could not measure the icon"
        f"\nstdout={result.stdout!r}"
    )
    assert "not a pass" in result.stdout and "opaque" not in result.stdout, (
        "an unread subject was reported as an artifact verdict (or as nothing at all): the "
        f"two answers have different remedies.\nstdout={result.stdout!r}"
    )


@_posix_shell_only
def test_a_release_with_no_macos_pkg_fails_this_step(tmp_path) -> None:
    """The step's subject IS the pkg (issue #1913): with none built, exit 0 stamps it.

    Nothing is expanded either -- the guard sits above the extraction, so this arm also
    proves the reading was not moved inside a branch it never reaches.
    """
    result = _run_step(tmp_path, tool_rc=0, with_pkg=False)
    assert result.returncode != 0, "a release with no macOS pkg passed its own icon step"
    assert "no EMRG-*-macos-*.pkg found" in result.stdout, (
        f"the guard failed without naming what it could not find.\nstdout={result.stdout!r}"
    )
    assert not any(ln.startswith("pkgutil") for ln in result.calls), (
        f"the step expanded a pkg it never found: {result.calls!r}"
    )


@_posix_shell_only
def test_a_pkg_with_no_payload_is_not_a_pass(tmp_path) -> None:
    """An expanded pkg that carries no Payload has no icon to read, and that is not rc 0."""
    result = _run_step(tmp_path, tool_rc=0, with_payload=False)
    assert result.returncode != 0, (
        "the step passed with a pkg it could not get a payload out of"
    )
    assert "no component Payload" in result.stdout, (
        f"the step failed without naming the shape it expected.\nstdout={result.stdout!r}"
    )
    assert _tool_call(result.calls) is None, (
        f"the tool was run on an empty extraction: {result.calls!r}"
    )
