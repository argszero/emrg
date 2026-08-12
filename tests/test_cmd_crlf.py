"""Guard: Windows batch/PowerShell files MUST be CRLF (rant 2026-08-12T12:30:41).

Root cause of the 0.2.25/0.2.26/0.2.27 installer "exit code 1" series: the
repo's bin/*.cmd were LF-only, so cmd.exe misparsed them (whole file joined
serially → @echo off ignored → arbitrary commands executed → non-zero exit
→ installer aborted). .gitattributes (`*.cmd/*.bat/*.ps1 text eol=crlf`)
enforces CRLF at checkout; this test verifies the working tree never
regresses (a future PR that adds a LF-only .cmd fails CI).
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WIN_SCRIPT_EXTS = (".cmd", ".bat", ".ps1")


def _win_scripts():
    out = subprocess.check_output(["git", "ls-files"], cwd=str(REPO_ROOT), text=True)
    for rel in out.splitlines():
        if rel.lower().endswith(WIN_SCRIPT_EXTS):
            yield REPO_ROOT / rel


def test_windows_scripts_are_crlf():
    files = list(_win_scripts())
    assert files, "expected at least one tracked .cmd/.bat/.ps1 file"
    bad = []
    for p in files:
        data = p.read_bytes()
        crlf = data.count(b"\r\n")
        lf = data.count(b"\n")
        if crlf != lf:  # bare-LF lines present (LF-only or mixed endings)
            bad.append(f"{p.relative_to(REPO_ROOT)}: {lf - crlf} bare-LF line(s)")
    assert not bad, (
        "Windows scripts must use CRLF line endings (LF-only .cmd files are "
        "misparsed by cmd.exe → installer 'exit code 1'; see rant "
        "2026-08-12T12:30:41):\n" + "\n".join(bad)
    )
