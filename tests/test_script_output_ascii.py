"""Host scripts must not crash when stdout cannot encode their own output.

Background (cycle 2026-09-10, the `--check` codec defect on #1119)
----------------------------------------------------------------
`scripts/bump-version.py` printed ``✓``/``✗`` verdicts, so any invocation whose
stdout was a pipe or a file rather than a UTF-8 console raised
``UnicodeEncodeError`` on that print and exited 1 — a *consistent* tree
reporting the exact "drift found" its pre-push self-check exists to report.
The suite could not see it: every CLI test drove ``main()`` in-process, where
stdout is a Python buffer and no codec is involved.

The same class was then reproduced in ``scripts/check_nonlocal.py``: its
success line ``✅ nonlocal integrity check passed`` turns a passing check into
``rc=1`` plus a traceback under ``PYTHONIOENCODING=gbk`` or ``ascii``.

Three guards here, deliberately different in kind:

* **static, printed literals** — every string literal a script can print or
  write to stdout/stderr is ASCII-only. This covers output paths a behavioural
  test does not drive (the "what does the new entry path bypass?" gap that let
  the original defect past three reviews).
* **static, help text** — a script's module docstring, when the script prints
  it, and every ``description=``/``epilog=``/``help=`` literal are ASCII-only.
  Docstrings are not exempt: ``argparse`` prints ``description=__doc__``, so a
  docstring is ordinary output on the ``--help`` path.
* **behavioural** — ``check_nonlocal.py`` runs as a *subprocess* with raw byte
  capture under two hostile codecs, in the positive (pass) and negative (fail)
  state, asserting the exit code, the verdict text and ASCII output; and every
  argparse script's ``--help`` is run the same way.

A third instance of the class, found while reviewing this very change: three of
the five scripts below pass ``description=__doc__`` and their docstrings kept
the em dash this repo's prose uses, so ``--help`` exited 1 with a traceback
under ``PYTHONIOENCODING=ascii``. Fixing the *printed* literals had left the
docstring half of the same output path unguarded — the first version of this
file asserted the opposite ("docstrings cannot crash a caller"), which is true
of a docstring nobody prints and false of one argparse prints.

Boundary (stated, not implied): these pin literal output. Data-driven output —
say a CJK path interpolated into a message — is not covered, and neither are
shell scripts: bash writes bytes straight to the fd, so a legacy console shows
mojibake instead of raising, which is a different (and non-fatal) failure.
Comments are exempt: no Python path prints one, and where a traceback echoes a
source line the interpreter escapes the unencodable character (verified: a
trailing ``# ... \u2014 ...`` on a failing line prints as ``\\u2014`` under
``PYTHONIOENCODING=ascii`` with exit 1, not ``UnicodeEncodeError``).
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _printed_literals(path: Path) -> list[tuple[int, str]]:
    """Every string literal written at a print()/stdout.write()/stderr.write() call.

    Only the literal *segments* are returned: an f-string's interpolations are
    values, not text the author chose, so a name or path holding non-ASCII is
    out of scope here (see the module docstring's boundary note). Also out of
    scope: a literal defined elsewhere and printed by name (``MSG = "...";
    print(MSG)``) — this rule reads the call site, not the dataflow. The
    behavioural tests are what cover indirection.
    """
    found: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_print = isinstance(func, ast.Name) and func.id == "print"
        is_write = (
            isinstance(func, ast.Attribute)
            and func.attr == "write"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr in ("stdout", "stderr")
        )
        if not (is_print or is_write):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant):
                segments = [arg]
            elif isinstance(arg, ast.JoinedStr):
                segments = [v for v in arg.values if isinstance(v, ast.Constant)]
            else:
                continue
            for seg in segments:
                if isinstance(seg.value, str):
                    found.append((seg.lineno, seg.value))
    return found


def test_script_printed_literals_are_ascii() -> None:
    """No host script may print a character a legacy codec cannot encode.

    Verified incident (this repo, cycle 2026-09-10): ``check_nonlocal.py``
    exited 1 with a traceback on a *passing* check because its success line
    carried U+2705. A console codec of ``gbk``/``cp1252``/``ascii`` cannot
    encode the check marks and emoji this repo's prose uses freely, so a
    scripted caller (`script | tee`, `subprocess.run(capture_output=True)`,
    `> log.txt`) saw a false failure. Keep verdict output ASCII; prose inside
    the script can keep its typography.
    """
    scripts = sorted(SCRIPTS.glob("*.py"))
    assert scripts, f"no scripts found under {SCRIPTS}"

    offenders: list[str] = []
    for script in scripts:
        for lineno, text in _printed_literals(script):
            bad = sorted({ch for ch in text if ord(ch) > 127})
            if bad:
                codes = " ".join(f"U+{ord(ch):04X}" for ch in bad)
                offenders.append(f"{script.name}:{lineno}: {codes} in {text[:60]!r}")
    assert not offenders, (
        "host scripts must print ASCII-only literals so their output is "
        "encodable by any console codec; found non-ASCII in printed/written "
        "strings:\n  " + "\n  ".join(offenders)
    )


def _help_text_literals(path: Path) -> list[tuple[str, str]]:
    """Literals argparse writes to stdout on ``--help``.

    ``description`` (conventionally ``__doc__``), ``epilog`` and every
    ``help=`` are printed by argparse, so they reach a caller's console exactly
    like a ``print()`` does. The module docstring is included when the script
    references ``__doc__``, which is the usual way it becomes help text.
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    found: list[tuple[str, str]] = []
    if "__doc__" in src:
        doc = ast.get_docstring(tree, clean=False)
        if doc:
            found.append(("module docstring (printed as __doc__)", doc))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        for kw in node.keywords:
            if kw.arg not in ("description", "epilog", "help"):
                continue
            value = kw.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.append((f"{kw.arg}= on line {node.lineno}", value.value))
            elif isinstance(value, ast.JoinedStr):
                for seg in value.values:
                    if isinstance(seg, ast.Constant) and isinstance(seg.value, str):
                        found.append((f"{kw.arg}= on line {node.lineno}", seg.value))
    return found


def test_script_help_text_is_ascii() -> None:
    """``--help`` output must be encodable too — argparse prints docstrings.

    Verified incident (this repo, cycle 2026-09-10, found by review of the
    sibling test): ``scripts/reader_fix_latency.py`` and two others build their
    parser with ``description=__doc__``; the docstrings carried U+2014, so
    ``PYTHONIOENCODING=ascii script --help`` exited 1 with
    ``UnicodeEncodeError`` — on the one command a user runs to learn how to
    call the script. A docstring is output, not a comment.
    """
    scripts = sorted(SCRIPTS.glob("*.py"))
    assert scripts, f"no scripts found under {SCRIPTS}"

    offenders: list[str] = []
    for script in scripts:
        for where, text in _help_text_literals(script):
            bad = sorted({ch for ch in text if ord(ch) > 127})
            if bad:
                codes = " ".join(f"U+{ord(ch):04X}" for ch in bad)
                offenders.append(f"{script.name}: {where}: {codes}")
    assert not offenders, (
        "help text and docstrings printed by argparse must be ASCII-only so "
        "`--help` survives any console codec; found non-ASCII in:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# behavioural: the reported crash, through a real subprocess
# --------------------------------------------------------------------------


def _tool_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Materialise the script with its own ``emrg/client/app.py`` beside it.

    The script resolves the file it audits from ``__file__``
    (``scripts/../emrg/client/app.py``), so a subprocess needs a self-contained
    tree — patching anything in-process cannot reach the child.
    """
    tool = tmp_path / "scripts" / "check_nonlocal.py"
    tool.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SCRIPTS / "check_nonlocal.py", tool)
    return tool, tmp_path / "emrg" / "client" / "app.py"


PASSING_APP = """\
    async def interactive():
        state_var = False

        async def handle_key(data):
            nonlocal state_var
            state_var = True
"""

FAILING_APP = """\
    async def interactive():
        state_var = False

        async def handle_key(data):
            state_var = True
"""


def _run(tool: Path, codec: str) -> subprocess.CompletedProcess[bytes]:
    """Run the script with a non-UTF-8 stdout/stderr, capturing raw bytes.

    Byte capture (no ``text=True``) is deliberate: a text-mode capture makes
    the *parent* decode the child's output, which hides the defect — the
    child's own traceback is what a host sees.
    """
    env = dict(os.environ, PYTHONIOENCODING=codec)
    return subprocess.run(
        [sys.executable, str(tool)],
        cwd=tool.parent.parent,
        env=env,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("codec", ["ascii", "gbk"])
def test_check_nonlocal_verdict_survives_a_non_utf8_stdout(tmp_path, codec) -> None:
    """A passing check must report rc=0 with readable output under any codec.

    Before the fix, the passing case raised ``UnicodeEncodeError`` on the
    U+2705 success line and exited 1 — indistinguishable, to a scripted
    caller, from "the check failed".
    """
    tool, app_py = _tool_tree(tmp_path)

    app_py.parent.mkdir(parents=True, exist_ok=True)
    app_py.write_text(textwrap.dedent(PASSING_APP), encoding="utf-8")

    passed = _run(tool, codec)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert passed.stdout.isascii(), passed.stdout
    assert passed.stderr == b"", passed.stderr
    assert b"OK: nonlocal integrity check passed" in passed.stdout

    # Negative state: the same codec must not cost the diagnosis either.
    app_py.write_text(textwrap.dedent(FAILING_APP), encoding="utf-8")

    failed = _run(tool, codec)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert failed.stderr.isascii(), failed.stderr
    assert b"state_var" in failed.stderr, "the missing declaration must be named"
    assert b"Traceback" not in failed.stderr, failed.stderr


def _argparse_scripts() -> list[Path]:
    """Scripts that answer ``--help`` (their own source imports argparse)."""
    return [
        script
        for script in sorted(SCRIPTS.glob("*.py"))
        if "argparse" in script.read_text(encoding="utf-8")
    ]


@pytest.mark.parametrize("codec", ["ascii", "gbk"])
def test_script_help_survives_a_non_utf8_stdout(codec: str) -> None:
    """Every script's ``--help`` must exit 0 with ASCII output under any codec.

    This is the behavioural half of the review finding: the static guard names
    the offending literal, this one proves the crash is gone through a real
    subprocess, and neither can be satisfied by accident — a passing run needs
    the whole help text (and only that) to be ASCII.
    """
    scripts = _argparse_scripts()
    assert scripts, f"no argparse scripts found under {SCRIPTS}"

    failures: list[str] = []
    for script in scripts:
        proc = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=REPO_ROOT,
            env=dict(os.environ, PYTHONIOENCODING=codec),
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0 or b"Traceback" in proc.stderr:
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            failures.append(
                f"{script.name}: rc={proc.returncode} {tail[-1] if tail else '(no stderr)'}"
            )
        elif not proc.stdout.isascii():
            failures.append(f"{script.name}: rc=0 but --help output is not ASCII")
    assert not failures, (
        f"`--help` must survive PYTHONIOENCODING={codec}; failures:\n  "
        + "\n  ".join(failures)
    )
