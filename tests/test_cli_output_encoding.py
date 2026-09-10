"""The `emrg` CLI must not die when its output cannot be encoded.

Background (cycle 2026-09-10, same class as the `scripts/` defect on #1121)
----------------------------------------------------------------------------
With stdout redirected, Python encodes using the *locale* codec rather than the
console's: ASCII under ``LANG=C``/POSIX, ``cp1252`` on older Windows, GBK on
zh-CN hosts. `emrg --help` prints an em dash, which the ASCII codec cannot
represent, so the print raised ``UnicodeEncodeError`` mid-write:

    $ PYTHONIOENCODING=ascii python -m emrg --help > log.txt
    Traceback (most recent call last):
      ...
    UnicodeEncodeError: 'ascii' codec can't encode character '\\u2014'
    $ echo $?
    1

The command exited 1 and printed *nothing* -- `--help` failing at a point where
a caller reads "the CLI is broken". ``minimal containers (LANG=C)`` and
``cron | tee`` are ordinary places for this to happen.

The fix degrades unencodable characters instead of aborting, and applies only
to non-interactive streams (see ``emrg.__main__._harden_redirected_output``),
so an interactive console keeps its full typography.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from emrg.__main__ import _harden_redirected_output

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args: list[str], codec: str) -> subprocess.CompletedProcess[bytes]:
    """Run the CLI with a forced stdout codec, capturing raw bytes.

    Byte capture (no ``text=True``) is deliberate: a text-mode capture decodes
    in the *parent*, which hides the child's own failure.
    """
    import os

    env = dict(os.environ, PYTHONIOENCODING=codec)
    return subprocess.run(
        [sys.executable, "-m", "emrg", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("codec", ["ascii", "cp1252"])
def test_cli_help_survives_a_legacy_stdout(codec: str) -> None:
    """`--help` must print and exit 0 under a codec that cannot encode its text.

    ASCII is the reported failure (LANG=C / POSIX); cp1252 is the older Windows
    default. Both must yield usable output rather than a traceback.
    """
    proc = _run_cli(["--help"], codec)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert b"Traceback" not in proc.stderr, proc.stderr
    assert b"UnicodeEncodeError" not in proc.stderr, proc.stderr
    assert proc.stdout.strip(), "help text must still be printed"
    assert b"usage:" in proc.stdout

    # Everything emitted must be round-trippable by the codec that produced it.
    # The two codecs reach that differently, and the difference is the point:
    # cp1252 *can* encode the em dash (0x97), so it passes through untouched,
    # while ASCII cannot, so that character degrades to "?" rather than raising.
    assert proc.stdout.decode(codec)
    if codec == "ascii":
        assert proc.stdout.isascii()
        assert b"EMRG ?" in proc.stdout, "the unencodable char should degrade to '?'"


def test_cli_help_keeps_its_typography_on_a_utf8_stdout() -> None:
    """The degradation must not apply where the text *is* encodable.

    Positive control for the fix: hardening redirected streams must not be a
    blanket ASCII-ification of the CLI's output.
    """
    proc = _run_cli(["--help"], "utf-8")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert b"usage:" in proc.stdout
    assert "\u2014".encode("utf-8") in proc.stdout, "em dash should survive UTF-8"


class _FakeStream:
    """Minimal stdout stand-in recording reconfigure() calls."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty
        self.calls: list[dict] = []

    def isatty(self) -> bool:
        return self._tty

    def reconfigure(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _BareStream:
    """A stream object with no reconfigure() (e.g. a wrapper)."""

    def isatty(self) -> bool:
        return False


def test_interactive_streams_are_left_alone(monkeypatch) -> None:
    """A terminal can encode the text; the TUI's streams must not be rewritten."""
    out, err = _FakeStream(tty=True), _FakeStream(tty=True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    _harden_redirected_output()

    assert out.calls == []
    assert err.calls == []


def test_redirected_streams_degrade_instead_of_aborting(monkeypatch) -> None:
    """A pipe/file stream gets errors="replace" so an unencodable char cannot raise."""
    out, err = _FakeStream(tty=False), _FakeStream(tty=False)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    _harden_redirected_output()

    assert out.calls == [{"errors": "replace"}]
    assert err.calls == [{"errors": "replace"}]


def test_streams_without_reconfigure_are_tolerated(monkeypatch) -> None:
    """Streams that cannot be reconfigured must not turn the hardening into a crash."""
    monkeypatch.setattr(sys, "stdout", _BareStream())
    monkeypatch.setattr(sys, "stderr", _BareStream())

    _harden_redirected_output()  # must not raise
