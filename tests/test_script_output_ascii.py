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

Both guards here, deliberately different in kind:

* **static** — every string literal a script can print or write to
  stdout/stderr is ASCII-only. This covers output paths a behavioural test
  does not drive (the "what does the new entry path bypass?" gap that let the
  original defect past three reviews).
* **behavioural** — ``check_nonlocal.py`` runs as a *subprocess* with raw byte
  capture under two hostile codecs, in the positive (pass) and negative
  (fail) state, asserting the exit code, the verdict text and ASCII output.

Boundary (stated, not implied): these pin literal output. Data-driven output —
say a CJK path interpolated into a message — is not covered, and neither are
shell scripts: bash writes bytes straight to the fd, so a legacy console shows
mojibake instead of raising, which is a different (and non-fatal) failure.
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
    """Every string literal reachable by print()/stdout.write()/stderr.write().

    Only the literal *segments* are returned: an f-string's interpolations are
    values, not text the author chose, so a name or path holding non-ASCII is
    out of scope here (see the module docstring's boundary note).
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
