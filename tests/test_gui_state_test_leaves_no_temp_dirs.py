"""The GUI state test must hand back the temp directories it creates.

`emrg/gui/test/gui-state.test.js` calls `fs.mkdtempSync` once per calling test and — before
this guard's companion fix — never removed the result. `mkdtemp()` never reuses a name, so
the directories accumulated for good: measured on this host, three per run (36 → 39 → 42
across three `npm test` runs), and a longer-lived checkout grows without bound.

Why the check lives here rather than in the JS suite
----------------------------------------------------
A process cannot observe its own litter from the inside: deleting the cleanup hook takes
the assertion that watches it with it, so a guard in the same file would be green in both
worlds. The observation therefore has to come from a second process — this one. It is a
pytest test rather than a `emrg/gui/test/*.test.js` file because the Node harness counts its
own files (`scripts/check-node-test-count.py`), and a guard about cleanup should not move
that number.

The measurement is a private temp root
--------------------------------------
`os.tmpdir()` is redirected to a directory this test creates — `TMPDIR` for POSIX, `TMP` and
`TEMP` for Windows, all three set, so no platform is special-cased and the test does not
have to skip on one. The question asked is therefore exact: *did the file under test leave
anything at all in the temp root it was given?* A directory left anywhere else would be a
different defect and is not claimed here.

Both directions are asserted on the same run: the file's own tests must pass (otherwise an
empty temp root would only prove the file never ran), and the temp root must be empty.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUI_DIR = REPO_ROOT / "emrg" / "gui"
SUBJECT = GUI_DIR / "test" / "gui-state.test.js"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "no `node` on PATH: this guard runs the suite file in its own process to read "
            "what it leaves behind, and cannot do that without the runner"
        )
    return node


def _run_in_private_temp_root(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the subject with `os.tmpdir()` redirected to `root`."""
    env = dict(os.environ)
    for var in ("TMPDIR", "TMP", "TEMP"):
        env[var] = str(root)
    return subprocess.run(
        [_node(), "--test", str(SUBJECT.relative_to(GUI_DIR))],
        cwd=GUI_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def test_the_subject_exists_and_still_creates_a_temp_home_per_test():
    """An empty subject would make the guard vacuous, so the subject is named first.

    The call, not a mention: `mkdtempSync` has to appear as a call in this file for the run
    below to be measuring anything at all.
    """
    assert SUBJECT.exists(), f"the subject of this guard is gone: {SUBJECT}"
    source = SUBJECT.read_text(encoding="utf-8")
    assert re.search(r"\bmkdtempSync\s*\(", source), (
        f"{SUBJECT.name} no longer calls mkdtempSync, so this guard is watching a file that "
        "creates no temp directory - which is a failure to measure rather than a pass"
    )


def test_the_gui_state_file_leaves_nothing_in_the_temp_root_it_is_given(tmp_path):
    root = tmp_path / "tmpdir"
    root.mkdir()

    result = _run_in_private_temp_root(root)

    # The summary line's prefix depends on the reporter `node --test` picks — TAP emits
    # `# pass 7`, the spec reporter emits `ℹ pass 7` — so both are accepted rather than
    # pinning the one this host happens to print.
    passed = re.search(r"^[#ℹ]\s*pass (\d+)\s*$", result.stdout, re.MULTILINE)
    failed = re.search(r"^[#ℹ]\s*fail (\d+)\s*$", result.stdout, re.MULTILINE)
    assert passed and failed, (
        "node --test did not print its summary, so this run measured nothing:\n"
        f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
    )
    # Positive control: the file has to have really run and really passed, or an empty temp
    # root proves only that nothing happened.
    assert int(failed.group(1)) == 0, f"{failed.group(1)} of the file's tests failed:\n{result.stdout[-2000:]}"
    assert int(passed.group(1)) > 0, "the file ran no tests at all - nothing was measured"

    leftovers = sorted(p.name for p in root.iterdir())
    assert leftovers == [], (
        f"{SUBJECT.name} left {len(leftovers)} entr(y/ies) in the temp root it was given: "
        f"{leftovers}. mkdtemp() never reuses a name, so each of these is permanent litter "
        "on every machine that runs the harness (issue #1916)."
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the redirection below is read back through `os.tmpdir()` in the child; on "
    "Windows that is TEMP/TMP, which the guard sets, but the subject's own `os.tmpdir()` "
    "call is the one that decides - measured on this arm only where the child is the same "
    "interpreter family as the runner, i.e. POSIX",
)
def test_the_temp_root_redirection_is_real_and_not_assumed(tmp_path):
    """The guard's premise, asserted rather than believed.

    If the redirection did not take effect in the child, the emptiness assertion above
    would be about a directory nothing ever wrote to - vacuously green, and it would stay
    green after the fix was removed. This drives the same redirection with a child that
    prints its own `os.tmpdir()`, so the premise is measured.
    """
    root = tmp_path / "tmpdir"
    root.mkdir()
    env = dict(os.environ)
    for var in ("TMPDIR", "TMP", "TEMP"):
        env[var] = str(root)
    out = subprocess.run(
        [_node(), "-e", "process.stdout.write(require('os').tmpdir())"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert out.returncode == 0, f"could not ask node for its temp dir: {out.stderr[-500:]}"
    assert Path(out.stdout.strip()) == root.resolve(), (
        f"the child resolved its temp dir to {out.stdout.strip()!r}, not the directory this "
        f"test created ({root.resolve()}), so the emptiness assertion above is not about "
        "the directory the subject was handed"
    )
