"""`scripts/check-install-drift.py`: install-tree content no shipped commit of the checkout holds.

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
line and this tool does not need it (see the docstring's "Membership is asked of the
objects reachable from the refs a release is built from").

Exit codes, at the tool's own process boundary
----------------------------------------------
The tool is driven as a **subprocess**, not in-process, so what is asserted is what a
caller really reads: the code, and the text on stdout. `0` no drift, `1` drift, `2` not
measurable.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import stat
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


@pytest.fixture(autouse=True)
def _hermetic_git_config(tmp_path_factory, monkeypatch) -> None:
    """This module's fixtures must not inherit the **host's** git configuration.

    Every case here builds a repository and asserts what git stores for a path, so a
    setting that changes what git stores changes the expected answer - and the settings
    that do it are exactly the ones a host varies: `core.autocrlf` is unset on this
    development host and **true** on GitHub's `windows-2025` runner. Measured 2026-10-07
    (`cyc20261007-203559`): with a global `autocrlf=true` injected, every line-ending
    transformation applies to *every* path, so a fixture that relied on which paths a rule
    matches stopped exercising what it claimed, and `test-windows` went red on run
    `37608201387` for a test asserting exactly that. The assertion was right; the fixture
    was reading the host.

    Both the fixture's own git calls and the tool subprocess inherit this, because they
    have to agree on what git stores - pinning only one side would compare two
    conventions, which is the defect the tool itself was just fixed for.
    """
    empty = tmp_path_factory.mktemp("gitconfig") / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


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


def test_the_id_is_the_one_git_stores_for_that_path(tmp_path: Path) -> None:
    """The convention, pinned in both directions on real bytes.

    The scan asks git for the id rather than computing one in-process, and the reason is
    the second half of this test: a raw `sha1("blob <len>\\0" + bytes)` is a *different
    function* from `hash-object` as soon as a filter applies, and this repository's own
    `.gitattributes` (`*.cmd`/`*.bat`/`*.ps1 text eol=crlf`) and any host's
    `core.autocrlf=true` both make one apply.

    So both halves are asserted: for content no filter touches the two agree (the function
    really is a content hash), and for a CRLF file under `core.autocrlf=true` they
    deliberately do NOT - and it is git's answer, not the raw one, that the tool must use.
    """
    module = _load(TOOL)
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")

    def stored(path: Path, relative: str) -> str:
        return module._git_stored_id(root, path, relative)

    # Half one: with nothing filtering, the id is the plain content hash - including the
    # byte that makes the construction a blob and not a bare digest.
    for name, payload in (
        ("plain.txt", b"abc"),
        ("empty.txt", b""),
        ("with-nul.txt", b"a\x00b"),
        ("utf8.txt", "prompt \u2014 unicode\n".encode("utf-8")),
    ):
        path = tmp_path / name
        path.write_bytes(payload)
        raw = hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()
        assert stored(path, f"sub/{name}") == raw, (
            f"{name}: the id this tool compares is not a content hash of the file at all"
        )

    # Half two: the case the first version of this tool got wrong, deterministic on every
    # platform because the filter is configured here rather than inherited from the host.
    _git(root, "config", "core.autocrlf", "true")
    committed = root / "sub" / "file.txt"
    committed.write_bytes(b"a\nb\n")
    _git(root, "add", "sub/file.txt")
    _git(root, "commit", "-q", "-m", "lf blob")
    blob = _git(root, "rev-parse", "HEAD:sub/file.txt").stdout.strip()

    crlf = tmp_path / "install" / "sub" / "file.txt"
    crlf.parent.mkdir(parents=True)
    crlf.write_bytes(b"a\r\nb\r\n")
    raw = hashlib.sha1(b"blob %d\x00" % len(b"a\r\nb\r\n") + b"a\r\nb\r\n").hexdigest()

    assert stored(crlf, "sub/file.txt") == blob, (
        "a CRLF file whose committed blob is LF did not answer the committed id: the "
        "comparison has left git's own convention, which is exactly what made "
        "`test-windows` red (run 37588646750)"
    )
    assert raw != blob, (
        "the raw construction agrees here, so this fixture no longer exercises the "
        "divergence it exists to pin - the filter is not being applied"
    )


def test_a_crlf_install_file_is_not_called_drift(tmp_path: Path) -> None:
    """The regression itself, end to end through the tool's process boundary.

    A clean install tree whose files carry CRLF, in a checkout configured to normalize line
    endings, must answer `0`. Under the raw construction every such file is reported as an
    edit - on the Windows leg that was five failing tests, and on a host whose attributes
    transform a whole file type it would be a reading nobody could use.

    The control is in the same test: changing a byte of that same file must still answer
    `1`, so this is not a tool that stopped seeing drift.
    """
    root = tmp_path / "checkout"
    (root / "emrg").mkdir(parents=True)
    (root / "emrg" / "mod.py").write_bytes(b"x = 1\ny = 2\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "true")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "lf blob")

    install = tmp_path / "install" / "source"
    (install / "emrg").mkdir(parents=True)
    (install / "emrg" / "mod.py").write_bytes(b"x = 1\r\ny = 2\r\n")

    clean = _run(install, root)
    assert clean.returncode == 0, (
        "an install file differing from the committed blob only in line endings was "
        f"reported as drift: {clean.stdout!r}"
    )

    (install / "emrg" / "mod.py").write_bytes(b"x = 1\r\ny = 3\r\n")
    edited = _run(install, root)
    assert edited.returncode == 1, (
        f"a real edit of the same file was not reported: {edited.stdout!r}"
    )


def test_the_repo_relative_path_is_what_decides_the_conversion(tmp_path: Path) -> None:
    """`--path` is load-bearing, and only a **path-keyed** attribute shows it.

    Attributes are matched against the file's repo-relative path, so the path has to be
    supplied: the install copy lives outside the checkout, and git cannot derive one. A
    basename-only rule (`*.cmd`) hides this, because the file's own name matches it either
    way - found by a mutation arm that dropped the flag and **survived** (cycle
    `cyc20261007-182502`). With a directory-keyed rule the difference is measurable:

        .gitattributes:  sub/*.cmd text eol=crlf
        hash-object <file>                -> the raw CRLF id, which no commit holds
        hash-object --path=sub/a.cmd <f>  -> the committed LF blob

    So a clean install copy of such a file is reported as an edit unless the path is given,
    which is the whole reason this tool passes it.
    """
    root = tmp_path / "checkout"
    (root / "sub").mkdir(parents=True)
    (root / ".gitattributes").write_text("sub/*.cmd text eol=crlf\n", encoding="utf-8")
    (root / "sub" / "a.cmd").write_text("echo hi\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "lf blob")
    blob = _git(root, "rev-parse", "HEAD:sub/a.cmd").stdout.strip()

    install = tmp_path / "install" / "source"
    (install / "sub").mkdir(parents=True)
    crlf = install / "sub" / "a.cmd"
    crlf.write_bytes(b"echo hi\r\n")

    module = _load(TOOL)
    assert module._git_stored_id(root, crlf, "sub/a.cmd") == blob, (
        "the tool did not answer the committed blob for a CRLF file whose attribute rule "
        "is keyed on its directory"
    )
    without_path = _git(root, "hash-object", str(crlf)).stdout.strip()
    assert without_path != blob, (
        "this fixture no longer exercises the flag: without `--path` git answered the "
        "committed blob, so the rule is not keyed on the path here"
    )

    proc = _run(install, root)
    assert proc.returncode == 0, (
        f"a clean install copy under a path-keyed attribute rule was reported as drift: "
        f"{proc.stdout!r}"
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
    assert "1 file(s) hold content no shipped commit has; 2 checked" in proc.stdout, (
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


def test_content_only_a_bookkeeping_ref_holds_is_reported_and_that_ref_named(
    tmp_path: Path,
) -> None:
    """A recovery snapshot is not history a release ships, so an edit it alone holds is drift.

    `refs/emrg/rescue/*` is where `scripts/recover-worktree.py` pins a dirty tree's work
    precisely because it exists nowhere else - which is the point: a snapshot no release
    builds from cannot make a live install-tree edit safe. Counting it as history is how
    the install tree's `vibe_check.j2` sat unreported while the tag comparison found it
    (#1898), so membership is asked of `refs/heads/*`, `refs/remotes/*` and `refs/tags/*`.

    The ref is not discarded from the report either: it is where the bytes still are, and
    "an upgrade will delete this" is only actionable somewhere to recover them from. The
    control runs the same pair with the ref deleted, so the naming is exercised in both
    directions instead of asserted once.
    """
    root = tmp_path / "checkout"
    (root / SHARED).parent.mkdir(parents=True)
    (root / SHARED).write_text(RELEASED, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "released")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    # The edit, committed on a scratch branch and pinned into the bookkeeping namespace,
    # then that branch deleted: the bytes now reach nothing a release is built from.
    edited = "an edit that reaches no shipped ref\n"
    _git(root, "checkout", "-q", "-b", "scratch")
    (root / SHARED).write_text(edited, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the edit")
    pinned = _git(root, "rev-parse", "HEAD").stdout.strip()
    rescue = "refs/emrg/rescue/20260101T000000Z"
    _git(root, "update-ref", rescue, pinned)
    _git(root, "checkout", "-q", branch)
    _git(root, "branch", "-D", "scratch")

    install = tmp_path / "install" / "source"
    (install / SHARED).parent.mkdir(parents=True)
    (install / SHARED).write_text(edited, encoding="utf-8")

    proc = _run(install, root)
    assert proc.returncode == 1, (
        "content reachable only from a recovery snapshot was read as shipped history, "
        f"which is the whole defect of #1898: {proc.stdout!r}"
    )
    assert SHARED in proc.stdout, f"the drifted path is not named: {proc.stdout!r}"
    assert rescue in proc.stdout, (
        "the report does not name the ref the bytes are still recoverable from, so it says "
        f"'an upgrade destroys this' without saying where it lives: {proc.stdout!r}"
    )

    # The control: with the snapshot gone the file is drift like any other, so the naming
    # above is about the ref and not a string the tool always prints.
    _git(root, "update-ref", "-d", rescue)
    after = _run(install, root)
    assert after.returncode == 1, after.stdout
    assert rescue not in after.stdout, f"the ref is named after it was deleted: {after.stdout!r}"


def test_an_absent_object_is_tolerated_rather_than_making_the_reading_unmeasurable(
    tmp_path: Path,
) -> None:
    """A clone whose history has a hole must still answer, and must say the hole is there.

    Not hypothetical: measured 2026-10-08 (`cyc20261008-001036`) on the host this tool
    serves, `git rev-list --branches --tags --remotes --objects` exits 128 on `bad tree
    object 219ff46e...`, and so does every narrower walk - the hole is in the *history*,
    which is exactly what a membership question is about. Left intolerant the tool answers
    `2` on the very host it exists for, and a reading that can only say "could not measure"
    there measures nothing.

    The hole is forged rather than waited for: the branch's own tree object is removed from
    the store, so the walk meets an object it does not have - the same condition, on a
    repository built for the case. Tolerating it can only *remove* a candidate, so the files
    under the missing tree read as drift: the safe direction, and the count is printed so a
    narrowed set is never read as the whole one.
    """
    root = tmp_path / "checkout"
    (root / "emrg").mkdir(parents=True)
    (root / "emrg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "released")

    install = tmp_path / "install" / "source"
    (install / "emrg").mkdir(parents=True)
    (install / "emrg" / "mod.py").write_text("x = 1\n", encoding="utf-8")

    intact = _run(install, root)
    assert intact.returncode == 0, (
        f"an intact history did not answer cleanly, so this is not a control: {intact.stdout!r}"
    )

    tree = _git(root, "rev-parse", "HEAD^{tree}").stdout.strip()
    loose = root / ".git" / "objects" / tree[:2] / tree[2:]
    assert loose.exists(), (
        "this fixture needs a freshly written, still-loose object to forge the hole; "
        f"{tree} is not loose, so the case would not exercise anything"
    )
    os.chmod(loose, stat.S_IWRITE)
    loose.unlink()

    intolerant = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--branches", "--tags", "--remotes", "--objects"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert intolerant.returncode != 0, (
        "the forgery did not create the condition: the intolerant walk still finished, so "
        "this case would pass with or without the tolerance"
    )

    proc = _run(install, root)
    assert proc.returncode == 1, (
        "content under a missing tree is not reachable from the refs a release is built "
        f"from, so it is drift in the safe direction: {proc.stdout!r} {proc.stderr!r}"
    )
    assert "membership: 1 object(s)" in proc.stdout, (
        "the report does not say how much of history it walked, so a narrowed membership "
        f"set reads as the whole one: {proc.stdout!r}"
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


def test_the_module_does_not_inherit_the_hosts_git_config(tmp_path: Path) -> None:
    """The condition `test-windows` runs under, reproducible on any host.

    The failure this pins was not a wrong assertion - it was a fixture reading the host.
    GitHub's `windows-2025` runner has `core.autocrlf=true` globally, this development host
    has it unset, and a case that asserts which paths a conversion rule matches therefore
    answered differently in the two places (run `37608201387`, `1 failed, 3963 passed`).
    A green local suite could not have said so, which is the whole cost of the class.

    So the module is run under a deliberately hostile global config with the test deselected
    (it would otherwise recurse), and every other case must still pass. That makes the
    platform condition a *measurement* rather than a place-only observation: delete the
    autouse pinning fixture and this test fails on Linux and macOS alike.
    """
    hostile = tmp_path / "hostile.gitconfig"
    hostile.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")

    import os

    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(hostile),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(Path(__file__).resolve()),
            "-q",
            "-p",
            "no:cacheprovider",
            "-k",
            f"not {test_the_module_does_not_inherit_the_hosts_git_config.__name__}",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=600,
    )
    assert proc.returncode == 0, (
        "with the Windows runner's `autocrlf=true` in force, this module's own fixtures "
        "stopped agreeing with their assertions:\n"
        f"{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}"
    )
