"""`scripts/check-install-drift.py`: install-tree content no commit of the checkout holds.

Why this file exists
--------------------
The daemon runs the **installed** package: `_resolve_task_template` returns
`Path(__file__).parent / <template>`, and that `__file__` is under
`~/.emrg/install/source/`. The install tree is not a git clone, so a file edited in place
there lives in no history and the next install/upgrade replaces it whole.

Measured 2026-10-07 (`cyc20261007-130240`, `cyc20261007-151625`) on this host: the install
tree's `competition_prompt.md` is the `v0.3.7` bytes **plus** a hand-appended block, every
other prompt file in the tree is byte-identical to `v0.3.7`, and the rule that block
carries appears in no ref of the repository. The tag the release chain is part-way through
(`v0.3.8`) carries none of it, so the upgrade would delete the host's mandate rather than
ship it.

Every case here builds its own checkout and its own install tree under `tmp_path`: the
subject is a pair of directories, and a test that read this host's real install tree would
be measuring the host rather than the tool. Nothing here touches `~/.emrg/install`, and
nothing here reads `~/.emrg/install/version.txt` - that path is named by a permanent red
line and this tool does not need it (see the docstring's "Membership is asked of
`git rev-list --all --objects`").

Exit codes, at the tool's own process boundary
----------------------------------------------
The tool is driven as a **subprocess**, not in-process, so what is asserted is what a
caller really reads: the code, and the text on stdout. `0` no drift, `1` drift, `2` not
measurable.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "check-install-drift.py"

#: The file both trees carry, at a path both call the same thing - the mapping is
#: positional, which is why an install-relative path has to be a tracked path here.
SHARED = "emrg/server/prompt.md"
RELEASED = "released content for the prompt template\n"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(
        "install_drift_" + path.stem.replace("-", "_"), path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A one-commit repository holding `SHARED` with the released bytes."""
    root = tmp_path / "checkout"
    (root / "emrg" / "server").mkdir(parents=True)
    (root / SHARED).write_text(RELEASED, encoding="utf-8")
    (root / "README.md").write_text("# readme\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "released")
    return root


@pytest.fixture
def install_dir(tmp_path: Path, checkout: Path) -> Path:
    """An install tree holding what the release shipped, at the same paths."""
    tree = tmp_path / "install" / "source"
    (tree / "emrg" / "server").mkdir(parents=True)
    shutil.copyfile(checkout / SHARED, tree / SHARED)
    shutil.copyfile(checkout / "README.md", tree / "README.md")
    return tree


def _run(install: Path | str, root: Path | str, codec: str | None = None) -> subprocess.CompletedProcess:
    env = None
    if codec is not None:
        import os

        env = {**os.environ, "PYTHONIOENCODING": codec}
    return subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--install-dir",
            str(install),
            "--root",
            str(root),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


def test_the_hash_is_the_one_git_computes(tmp_path: Path) -> None:
    """The instrument's control: the shortcut stands in for `git hash-object`.

    The scan hashes every shared file in-process instead of spawning `hash-object` per
    file, so the construction it uses has to be checked against the command it replaces -
    including the byte that matters, the NUL ending the header.
    """
    module = _load(TOOL)
    for name, payload in (
        ("plain.txt", b"abc"),
        ("empty.txt", b""),
        ("with-nul.txt", b"a\x00b"),
        ("utf8.txt", "prompt \u2014 unicode\n".encode("utf-8")),
        ("crlf.txt", b"a\r\nb\r\n"),
    ):
        path = tmp_path / name
        path.write_bytes(payload)
        expected = subprocess.run(
            ["git", "hash-object", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout.strip()
        assert module._git_blob_sha(path) == expected, (
            f"{name}: the in-process hash disagrees with `git hash-object`, so every "
            "membership test below is asking about the wrong id"
        )


def test_a_clean_install_tree_answers_zero(install_dir: Path, checkout: Path) -> None:
    """The positive half: a tree holding exactly the released bytes is not drift."""
    proc = _run(install_dir, checkout)
    assert proc.returncode == 0, f"a clean install tree was reported as drift: {proc.stdout}"
    assert "2 file(s) checked, 0 skipped" in proc.stdout, (
        f"the count of what was checked is what keeps a green verdict from being a "
        f"reading over nothing: {proc.stdout!r}"
    )


def test_an_edited_file_answers_one_and_names_the_path(
    install_dir: Path, checkout: Path
) -> None:
    """The negative half: an in-place edit is named, with the stack it belongs in."""
    edited = install_dir / SHARED
    edited.write_text(RELEASED + "\n### a rule added in the install tree\n", encoding="utf-8")

    proc = _run(install_dir, checkout)
    assert proc.returncode == 1, f"an edited install file was not reported: {proc.stdout}"
    assert SHARED in proc.stdout, f"the drifted path is not named: {proc.stdout!r}"
    assert "1 file(s) hold content no commit has; 2 checked" in proc.stdout, (
        f"the summary must name how many of how many: {proc.stdout!r}"
    )
    assert "the next one destroys it" in proc.stdout, (
        "the verdict does not say what the consequence is, which is the whole reason to "
        f"report it: {proc.stdout!r}"
    )
    assert (checkout / SHARED).read_text(encoding="utf-8") == RELEASED, (
        "the tool wrote to the checkout: it is a reading, and it may not repair anything"
    )


def test_the_verdict_flips_on_a_single_added_byte(
    install_dir: Path, checkout: Path
) -> None:
    """The discriminating control: the two verdicts differ only by the edit.

    Both runs share the checkout, the install tree and every other file; the only change
    between them is one appended line. Without this pair, a tool that always answered `1`
    would pass the case above, and one that always answered `0` would pass the case before
    it.
    """
    before = _run(install_dir, checkout)
    assert before.returncode == 0
    with (install_dir / SHARED).open("a", encoding="utf-8") as handle:
        handle.write("\n")
    after = _run(install_dir, checkout)
    assert after.returncode == 1, (
        "appending one newline to the install copy is exactly the edit class this tool "
        f"exists for: {after.stdout!r}"
    )


def test_an_install_file_the_checkout_does_not_track_is_skipped_not_reported(
    install_dir: Path, checkout: Path
) -> None:
    """A file only the install has is a version difference, not an edit.

    Packaging writes files this repository does not track (`py.typed` is the measured
    one), and the repository also moves files between releases; reporting either as an
    in-place edit would make the tool noise, which is how a reading stops being read. It
    is counted and named as skipped rather than silently dropped.
    """
    (install_dir / "packaging_only.txt").write_text("not in the repository\n", encoding="utf-8")

    proc = _run(install_dir, checkout)
    assert proc.returncode == 0, (
        f"an untracked install file was reported as drift: {proc.stdout!r}"
    )
    assert "packaging_only.txt" not in proc.stdout.split("could not measure")[0]
    assert "1 skipped" in proc.stdout, (
        f"the skipped file is not counted, so a reader cannot tell it was considered: "
        f"{proc.stdout!r}"
    )


def test_the_report_names_both_trees_before_the_verdict(
    install_dir: Path, checkout: Path
) -> None:
    """Which pair answered, printed first - the family's rule for a tree-reading guard.

    This tool is the family's only member whose subject is a *pair*, and both halves can
    be pointed somewhere else: a verdict that did not say which install tree and which
    checkout produced it would read the same for this host and for a tmp directory.
    `check-merge-sequence.py` reads sibling reports and refuses one that does not name its
    tree, so the line is a contract and not a courtesy.
    """
    proc = _run(install_dir, checkout)
    lines = proc.stdout.splitlines()
    assert lines[0] == f"tree: {checkout}", f"the checkout is not named first: {lines[:2]}"
    assert lines[1] == f"install: {install_dir}", (
        f"the install tree is not named second, so the pair is not identified: {lines[:2]}"
    )


def test_a_missing_install_tree_is_unmeasurable(tmp_path: Path, checkout: Path) -> None:
    """No install tree is not a clean install tree."""
    proc = _run(tmp_path / "nowhere", checkout)
    assert proc.returncode == 2, f"a missing subject answered a verdict: {proc.stdout!r}"
    assert "could not measure" in proc.stderr


def test_a_root_that_is_not_a_checkout_is_unmeasurable(
    install_dir: Path, tmp_path: Path
) -> None:
    """Nothing can say whether content was ever committed without a history."""
    plain = tmp_path / "not_a_checkout"
    plain.mkdir()
    proc = _run(install_dir, plain)
    assert proc.returncode == 2, f"a root with no history answered a verdict: {proc.stdout!r}"
    assert "not a git checkout" in proc.stderr


def test_a_tree_sharing_no_tracked_path_is_unmeasurable(
    tmp_path: Path, checkout: Path
) -> None:
    """The empty-set refusal: nothing checked may not read as nothing found.

    The check is per shared path, so a tree sharing none with the checkout has had no
    question put to it at all - and `0` would say the files were checked. This is the
    class fixed across the family on 2026-10-06 (`cyc20261006-165503`).
    """
    unrelated = tmp_path / "unrelated"
    (unrelated / "somewhere_else").mkdir(parents=True)
    (unrelated / "somewhere_else" / "file.txt").write_text("x\n", encoding="utf-8")

    proc = _run(unrelated, checkout)
    assert proc.returncode == 2, (
        f"a tree sharing no path with the checkout answered a verdict: {proc.stdout!r}"
    )
    assert "share no tracked path" in proc.stderr, proc.stderr


def test_the_default_install_tree_is_the_one_the_daemon_runs() -> None:
    """The default subject is the install tree, not this checkout.

    `_resolve_task_template` renders a built-in template from `Path(__file__).parent`, and
    for a running daemon that is the installed copy. A default pointed anywhere else would
    answer about a tree no session reads, in the same words.
    """
    module = _load(TOOL)
    assert module.DEFAULT_INSTALL_DIR == "~/.emrg/install/source", (
        "the default install tree moved away from the one the daemon renders from"
    )


def test_the_help_and_both_verdicts_survive_an_ascii_console(
    install_dir: Path, checkout: Path
) -> None:
    """A script's own output must not turn a verdict into a traceback.

    `tests/test_script_output_ascii.py` covers this statically for every script in
    `scripts/`; this drives the paths that matter here through a hostile codec, because a
    crash on the drift path would be read as `1` - the code that means "drift found".
    """
    help_proc = subprocess.run(
        [sys.executable, str(TOOL), "--help"],
        capture_output=True,
        text=True,
        encoding="ascii",
        errors="replace",
        env={"PYTHONIOENCODING": "ascii", "PATH": "/usr/bin:/bin"},
        timeout=120,
    )
    assert help_proc.returncode == 0, f"--help under an ascii codec: {help_proc.stderr!r}"
    assert "could not measure" not in help_proc.stderr

    with (install_dir / SHARED).open("a", encoding="utf-8") as handle:
        handle.write("\n")
    drift = _run(install_dir, checkout, codec="ascii")
    assert drift.returncode == 1, (
        f"the drift verdict under an ascii codec came back as {drift.returncode}: "
        f"{drift.stderr!r}"
    )
    assert drift.stdout.encode("ascii"), "the report is not ascii-clean"
