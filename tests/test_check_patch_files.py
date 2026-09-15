"""Tests for scripts/check-patch-files.py - the file-set comparison.

Background
----------
A patch that drops a file is invisible to every check a maintainer naturally
runs: it applies cleanly, and the suite is green, because the file is not there
to fail. This repo hit exactly that - a fix was published whose only test file
had been dropped during a rebuild - and the free check that catches it is a set
comparison of the two patches' file lists.

The tests below pin the properties that make the verdict trustworthy, in both
directions, because a guard that fires on everything is as useless as one that
never fires:

* a drop is reported with the file named, and the exit code is 1;
* a gain is reported and the exit code stays 0, and a file the patch *deletes*
  still counts as present (the tool compares file sets, not live files);
* the patches are compared pairwise, so a drop between the second and the third
  is not missed while the second is compared against the first;
* a patch that parses to **zero** files exits 2 rather than 0. This is the
  property that keeps the tool honest: a parser that matched nothing agrees with
  every input, so "no file disappeared" would then be a statement about the
  parser instead of about the patches;
* everything whose answer cannot be measured (no patches, one patch, an
  unreadable path) exits 2, never 0;
* the output is ASCII and the exit codes survive a console codec that cannot
  encode anything else.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-patch-files.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_patch_files", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


# ── synthetic patches ────────────────────────────────────────────────────────

def _needs_quoting(name: str) -> bool:
    """Does git have to C-quote this name? Measured, not assumed.

    git quotes when the name holds a byte it cannot print plainly - a non-ASCII
    byte, a control character, `"` or `\\`. A **space does not quote it**, which
    is the whole reason this file's fixtures were rebuilt: the previous version
    of this suite emitted `"a/my file.md" "b/my file.md"`, a header git never
    writes, and so tested the belief instead of the format.
    """
    return any(
        chr(b) == '"' or chr(b) == "\\" or not (0x20 <= b < 0x7F) for b in name.encode("utf-8")
    )


def _c_quote(name: str) -> str:
    """git's quoting of a whole name: escapes for `"` `\\` tab, octal for other bytes."""
    out: list[str] = []
    for byte in name.encode("utf-8"):
        char = chr(byte)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\t":
            out.append("\\t")
        elif 0x20 <= byte < 0x7F:
            out.append(char)
        else:
            out.append("\\%03o" % byte)
    return '"' + "".join(out) + '"'


def _header_token(prefix: str, name: str) -> str:
    token = prefix + name
    return _c_quote(token) if _needs_quoting(token) else token


def _name_line(marker: str, name: str) -> str:
    """A `--- a/<name>` / `+++ b/<name>` line, spelled the way git spells it.

    Measured on a real repository: a name git can print plainly is written bare,
    **with a tab appended when it contains a space** (`+++ b/a b.txt<TAB>`), and a
    name it cannot print plainly is C-quoted instead. That terminator is why the
    path can be read off one of these lines unambiguously.
    """
    token = marker + name
    if _needs_quoting(token):
        return _c_quote(token)
    return token + ("\t" if " " in token else "")


def _file_section(path: str, hunks: int = 1, *, new: bool = False, deleted: bool = False) -> str:
    out = f"diff --git {_header_token('a/', path)} {_header_token('b/', path)}\n"
    if new:
        out += "new file mode 100644\n"
    if deleted:
        out += "deleted file mode 100644\n"
    # git writes /dev/null on the side the file does not exist on.
    out += f"{'--- /dev/null' if new else _name_line('--- a/', path)}\n"
    out += f"{'+++ /dev/null' if deleted else _name_line('+++ b/', path)}\n"
    out += "@@ -1,1 +1,1 @@\n" * hunks
    return out


def _write(tmp_path: Path, name: str, *sections: str) -> str:
    path = tmp_path / name
    path.write_text("".join(sections), encoding="utf-8")
    return str(path)


# ── the verdict ──────────────────────────────────────────────────────────────

def test_no_drop_is_ok(tmp_path, mod, capsys):
    old = _write(tmp_path, "old.patch", _file_section("a.py"), _file_section("b.py"))
    new = _write(tmp_path, "new.patch", _file_section("a.py", 2), _file_section("b.py"))

    assert mod.main([old, new]) == 0
    out = capsys.readouterr().out
    assert "OK: no file the previous patch carried is missing" in out


def test_a_dropped_file_is_named_and_fails(tmp_path, mod, capsys):
    old = _write(tmp_path, "old.patch", _file_section("a.py"), _file_section("tests/t.py", 5))
    new = _write(tmp_path, "new.patch", _file_section("a.py"))

    assert mod.main([old, new]) == 1
    out = capsys.readouterr().out
    assert "tests/t.py" in out
    assert "5 hunks" in out, "the dropped file's hunk count is what makes the loss legible"
    assert "FAIL" in out


def test_a_gained_file_is_reported_but_passes(tmp_path, mod, capsys):
    old = _write(tmp_path, "old.patch", _file_section("a.py"))
    new = _write(tmp_path, "new.patch", _file_section("a.py"), _file_section("c.py"))

    assert mod.main([old, new]) == 0
    out = capsys.readouterr().out
    assert "gained" in out and "c.py" in out


def test_a_file_the_patch_deletes_is_still_present(tmp_path, mod):
    """The tool compares file *sets*, not live files: a deletion is not a drop."""
    old = _write(tmp_path, "old.patch", _file_section("a.py"), _file_section("gone.py"))
    new = _write(tmp_path, "new.patch", _file_section("a.py"), _file_section("gone.py", deleted=True))

    assert mod.main([old, new]) == 0


def test_a_new_file_is_marked_new(tmp_path, mod, capsys):
    p = _write(tmp_path, "p.patch", _file_section("fresh.py", new=True))
    assert mod.main(["--list", p]) == 0
    assert "new file fresh.py" in capsys.readouterr().out


def test_three_patches_are_compared_pairwise(tmp_path, mod, capsys):
    a = _write(tmp_path, "a.patch", _file_section("x.py"), _file_section("keep.py"))
    b = _write(tmp_path, "b.patch", _file_section("x.py"), _file_section("keep.py"))
    c = _write(tmp_path, "c.patch", _file_section("x.py"))

    # The drop is between b and c; comparing only a against b would miss it.
    assert mod.main([a, b, c]) == 1
    assert "keep.py" in capsys.readouterr().out


def test_a_space_in_a_path_is_written_unquoted_and_still_parsed(tmp_path, mod, capsys):
    """The shape git actually writes for a space, and the header must survive it.

    Measured on a real repository: a path whose only unusual character is a space
    is written **unquoted** (`diff --git a/a b.txt b/a b.txt`) with a tab
    terminator on the `---`/`+++` lines. The previous version of this test
    asserted the *quoted* form and passed, because its fixture and the parser
    shared the same wrong belief about the format.
    """
    old = _write(tmp_path, "old.patch", _file_section("docs/my file.md"))
    new = _write(tmp_path, "new.patch", _file_section("docs/my file.md", 3))
    written = (tmp_path / "new.patch").read_text(encoding="utf-8")
    assert written.startswith("diff --git a/docs/my file.md b/docs/my file.md\n"), written
    assert "+++ b/docs/my file.md\t\n" in written, "git terminates a space-bearing name with a tab"

    assert mod.main([old, new]) == 0
    out = capsys.readouterr().out
    assert "docs/my file.md" in out
    assert "file.md (3 hunks)" in out, "the whole path is one file, not two"


def test_a_truncated_name_that_collides_is_no_longer_a_false_pass(tmp_path, mod, capsys):
    """The verdict this tool must never get wrong (reported on PR #1246).

    Splitting the header on whitespace read `a b.txt` as `b.txt`, so a previous
    patch carrying {a b.txt, b.txt, keep.txt} and a rebuilt one carrying only
    {b.txt, keep.txt} had the *same* parsed set: the loss of `a b.txt` was
    invisible and the tool printed OK. A wrong name is invisible in both patches,
    which is why this case is a false pass and not merely a cosmetic mistake.
    """
    old = _write(
        tmp_path, "old.patch",
        _file_section("a b.txt"), _file_section("b.txt"), _file_section("keep.txt"),
    )
    new = _write(tmp_path, "new.patch", _file_section("b.txt"), _file_section("keep.txt"))

    assert mod.main([old, new]) == 1, "the drop must be caught, not silently agreed with"
    out = capsys.readouterr().out
    assert "a b.txt" in out
    assert "OK: no file" not in out


def test_a_c_quoted_name_is_reported_as_its_real_name(tmp_path, mod, capsys):
    """git escapes a name it cannot print plainly; the report shows the name, not the escapes."""
    name = "docs/w\u00edth-\u00fcn\u00efcode\tq.txt"
    patch = _write(tmp_path, "p.patch", _file_section(name, 2))
    written = (tmp_path / "p.patch").read_text(encoding="utf-8")
    assert '\\303\\255' in written, "the fixture must carry git's octal escape, not the character"

    assert mod.main(["--list", patch]) == 0
    out = capsys.readouterr().out
    assert f"edits {name} (2 hunks)" in out
    assert "\\303" not in out, "the escaped spelling must not reach the report"


def test_a_mode_only_change_is_named_from_the_header(tmp_path, mod, capsys):
    """A mode-only section has no `---`/`+++` line at all: the header is the only source."""
    section = "diff --git a/a b.txt b/a b.txt\nold mode 100644\nnew mode 100755\n"
    patch = _write(tmp_path, "p.patch", section)
    assert "+++ " not in section and "--- " not in section

    assert mod.main(["--list", patch]) == 0
    assert "edits a b.txt" in capsys.readouterr().out


def test_a_binary_section_with_no_name_lines_is_named(tmp_path, mod, capsys):
    """A binary change carries no hunks and no name line either."""
    section = (
        "diff --git a/blob.bin b/blob.bin\nindex c94be36..e38375f 100644\n"
        "Binary files a/blob.bin and b/blob.bin differ\n"
    )
    patch = _write(tmp_path, "p.patch", section)
    assert mod.main(["--list", patch]) == 0
    assert "edits blob.bin" in capsys.readouterr().out


def test_a_pure_rename_is_named_by_its_new_path(tmp_path, mod, capsys):
    """A 100%-similarity rename has no name lines; `rename to` is the unambiguous source."""
    section = (
        "diff --git a/a b.txt b/c d.txt\nsimilarity index 100%\n"
        "rename from a b.txt\nrename to c d.txt\n"
    )
    patch = _write(tmp_path, "p.patch", section)
    assert mod.main(["--list", patch]) == 0
    out = capsys.readouterr().out
    assert "edits c d.txt" in out, "the post-image path is the one compared"
    assert "a b.txt" not in out


def test_a_section_whose_file_cannot_be_named_makes_the_run_unmeasurable(tmp_path, mod, capsys):
    """A file the parser cannot name must stop the comparison, not shrink it.

    Skipping the section would leave the *other* file in both patches, so the
    comparison would agree with itself and print OK - the same false pass as a
    wrong name, arrived at from the other side. The question is unanswerable, and
    an unanswerable question is exit 2 here, never a pass.
    """
    old = _write(tmp_path, "old.patch", _file_section("keep.py"), "diff --git unreadable\n")
    new = _write(tmp_path, "new.patch", _file_section("keep.py"))

    assert mod.main([old, new]) == 2
    out = capsys.readouterr().out
    assert "unmeasurable" in out and "cannot name the file" in out
    assert "OK:" not in out


def test_a_real_git_patch_agrees_with_gits_own_file_list(tmp_path, mod, capsys):
    """The ground truth is git's own answer, not a fixture's idea of it.

    Two staged trees, the second rebuilt without the space-bearing file: the
    parsed names must equal `git diff --cached --name-only`, and the drop must be
    the tool's verdict.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is not reachable")

    def run(*args):
        return subprocess.run(
            args, cwd=tmp_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    def stage(names):
        run("git", "rm", "-r", "--cached", "-q", ".")
        for path in tmp_path.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                path.unlink()
        for name in names:
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("one\ntwo\n", encoding="utf-8")
        run("git", "add", "-A")
        names_git = [n for n in run("git", "diff", "--cached", "--name-only").stdout.splitlines() if n]
        return run("git", "diff", "--cached").stdout, sorted(names_git)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "probe")

    prev_text, prev_names = stage(["keep.txt", "b.txt", "a b.txt"])
    cur_text, cur_names = stage(["keep.txt", "b.txt"])
    assert prev_names == ["a b.txt", "b.txt", "keep.txt"], "git's own list is the ground truth"

    assert sorted(mod.parse_patch(prev_text).keys()) == prev_names
    assert sorted(mod.parse_patch(cur_text).keys()) == cur_names

    old = tmp_path / "old.patch"
    new = tmp_path / "new.patch"
    old.write_text(prev_text, encoding="utf-8")
    new.write_text(cur_text, encoding="utf-8")
    assert mod.main([str(old), str(new)]) == 1
    assert "a b.txt" in capsys.readouterr().out


# ── unmeasurable must never read as a pass ───────────────────────────────────

def test_a_patch_with_no_diff_headers_is_unmeasurable(tmp_path, mod, capsys):
    """The property that keeps the tool honest.

    A patch this tool cannot read yields an empty file set, which would agree
    with every other patch and print OK. Zero files is a statement about the
    parser, so it must exit 2.
    """
    old = _write(tmp_path, "old.patch", _file_section("a.py"))
    empty = _write(tmp_path, "empty.patch", "this is not a diff\n")

    assert mod.main([old, empty]) == 2
    out = capsys.readouterr().out
    assert "unmeasurable" in out and "parsed to 0 files" in out
    assert "OK:" not in out, "an unreadable patch must not produce a pass verdict"


def test_a_missing_patch_file_is_unmeasurable(tmp_path, mod, capsys):
    old = _write(tmp_path, "old.patch", _file_section("a.py"))
    assert mod.main([old, str(tmp_path / "nope.patch")]) == 2
    assert "unmeasurable" in capsys.readouterr().out


def test_one_patch_without_list_is_unmeasurable(tmp_path, mod, capsys):
    old = _write(tmp_path, "old.patch", _file_section("a.py"))
    assert mod.main([old]) == 2
    assert "nothing to compare it against" in capsys.readouterr().out


def test_no_patches_is_unmeasurable(mod, capsys):
    assert mod.main([]) == 2
    assert "usage" in capsys.readouterr().out.lower()


def test_the_measured_patches_are_named(tmp_path, mod, capsys):
    """A reader must be able to tell what was read, not just what was concluded."""
    old = _write(tmp_path, "old.patch", _file_section("a.py"))
    new = _write(tmp_path, "new.patch", _file_section("a.py"))
    mod.main([old, new])
    out = capsys.readouterr().out
    assert "patches read:" in out and "old.patch" in out and "new.patch" in out


# ── as a subprocess: exit codes and ASCII under a hostile codec ──────────────

def _run(args: list[str], codec: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        check=False,
        env=dict(os.environ, PYTHONIOENCODING=codec),
    )


@pytest.mark.parametrize("codec", ["ascii", "gbk", "cp1252"])
def test_verdict_reaches_a_console_that_cannot_encode_typography(tmp_path, codec):
    old = _write(tmp_path, "old.patch", _file_section("a.py"), _file_section("t.py"))
    new = _write(tmp_path, "new.patch", _file_section("a.py"))

    proc = _run([old, new], codec)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert b"t.py" in proc.stdout
    assert proc.stdout == proc.stdout.decode("ascii", errors="ignore").encode("ascii"), (
        "the verdict line must be ASCII: a caller piping the output to a file or a "
        "captured pipe must not turn a verdict into a UnicodeEncodeError"
    )


def test_help_exits_zero_and_is_ascii(mod):
    proc = _run(["--help"], "ascii")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert b"check-patch-files.py" in proc.stdout
    assert proc.stdout.decode("ascii")  # raises if any byte is non-ASCII
