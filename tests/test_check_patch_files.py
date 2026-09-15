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

def _quote(token: str) -> str:
    """git quotes a *whole* header token - `"a/my file.md"`, never `a/"my file.md"`.

    It quotes when the token contains whitespace, a double quote or a backslash,
    so a fixture that emits such a path unquoted is not a patch git would ever
    write, and the parser is entitled to misread it.
    """
    if any(ch in token for ch in ' \t"\\'):
        return '"' + token.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return token


def _file_section(path: str, hunks: int = 1, *, new: bool = False, deleted: bool = False) -> str:
    a, b = _quote(f"a/{path}"), _quote(f"b/{path}")
    out = f"diff --git {a} {b}\n"
    if new:
        out += "new file mode 100644\n"
    if deleted:
        out += "deleted file mode 100644\n"
    out += f"--- {a}\n+++ {b}\n"
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


def test_a_quoted_path_with_a_space_is_parsed(tmp_path, mod, capsys):
    """git quotes a path containing whitespace; the header must survive it."""
    old = _write(tmp_path, "old.patch", _file_section("docs/my file.md"))
    new = _write(tmp_path, "new.patch", _file_section("docs/my file.md", 3))
    assert '"a/docs/my file.md" "b/docs/my file.md"' in (
        tmp_path / "new.patch"
    ).read_text(encoding="utf-8"), "the fixture must be a patch git would actually write"

    assert mod.main([old, new]) == 0
    out = capsys.readouterr().out
    assert "docs/my file.md" in out
    assert "file.md (3 hunks)" in out, "the whole quoted path is one file, not two"


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
