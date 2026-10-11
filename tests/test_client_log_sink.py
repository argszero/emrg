"""The client's own log sink asks its subject's kind before opening it (issue #2123).

`emrg/__main__.py::_configure_client_logging` builds a `RotatingFileHandler` on
``<cwd>/.emrg/emrg-client.log``, and `RotatingFileHandler` opens its subject at
**construction** — so this is an open on the client's startup path, before the TUI exists.
A FIFO there blocks for ever (`open()` raises nothing, so no `except OSError` beside it can
fire) and a directory there raises straight out of `_run_client`. Measured on master
`23bec8dc` (2026-10-12, `cyc20261012-060054`) with the block replicated in a child, one
child per subject kind under a 10 s cap: FIFO **did not return**, directory raised
`IsADirectoryError` in 0.08 s, regular file returned in 0.22 s.

The site was reachable only by starting the TUI, which is why it survived the sweep that
fixed its three siblings (#2108, #2117, #2118) — so the first thing this file pins is that
the setup is a named module-level helper at all, and the rest is the family's pair: the
FIFO arm is POSIX-gated, and its FIFO-free counterpart on a **directory** runs on both legs
(`tests/test_config.py:323` states the convention; the merged siblings keep it —
`tests/test_app_logs_live_under_logs_dir.py`, `tests/test_daemon.py`).

What is deliberately not here: `log_dir.mkdir(parents=True, exist_ok=True)` is unguarded,
so a **file** at ``<cwd>/.emrg`` raises `FileExistsError`. That is the boundary the daemon's
three sinks share and a different defect class (a directory that is not a directory), so it
is named rather than silently left, and it is not what this issue is about. No test here
starts a client.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg import __main__ as emrg_main  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SINK = Path(".emrg") / "emrg-client.log"
BOUND_SECONDS = 30


@contextlib.contextmanager
def _root_handlers_cleared():
    """Let `logging.basicConfig` install a handler while this block runs.

    `basicConfig` is a no-op when the root logger already has handlers, and pytest's
    logging plugin puts one there for every test — so without this the writer under test
    would compute its path, install nothing, and report green while having written no file
    at all.
    """
    saved_handlers = logging.root.handlers[:]
    saved_level = logging.root.level
    logging.root.handlers = []
    try:
        yield
    finally:
        for handler in logging.root.handlers:
            with contextlib.suppress(Exception):
                handler.close()
        logging.root.handlers = saved_handlers
        logging.root.level = saved_level


def _rotating_handlers() -> list[logging.Handler]:
    from logging.handlers import RotatingFileHandler

    return [h for h in logging.root.handlers if isinstance(h, RotatingFileHandler)]


def _plant(workspace: Path, kind: str) -> Path:
    """Plant `kind` at ``<workspace>/.emrg/emrg-client.log``; "nothing yet" plants nothing."""
    sink = workspace / SINK
    sink.parent.mkdir(parents=True, exist_ok=True)
    if kind == "a FIFO":
        os.mkfifo(sink)
    elif kind == "a directory":
        sink.mkdir()
    elif kind == "a regular file":
        sink.write_text("", encoding="utf-8")
    elif kind != "nothing yet":
        raise AssertionError(f"unhandled subject kind: {kind}")
    return sink


def _setup_in_a_child(workspace: Path, kind: str):
    """Run the real setup in a child whose cwd holds `kind` at the client log path.

    A child, not a call in-process: a blocked `open` cannot be interrupted here, so a
    reverted guard would hang the whole suite (a cancelled CI job reads as `no_verdict`)
    instead of reddening one test. `_configure_client_logging` does not re-point this
    interpreter's streams, so in-process would be safe on a working tree — the child is
    what keeps it safe on a broken one.
    """
    try:
        return subprocess.run(
            [sys.executable, "-c",
             "from emrg.__main__ import _configure_client_logging as f; print('reached', f().name)"],
            cwd=workspace,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=BOUND_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"the client's log setup did not return within {BOUND_SECONDS}s with {kind} at "
            f"{SINK} — a log sink was opened without asking its kind first (a FIFO blocks at "
            f"the open and raises nothing, so an `except OSError` beside it can never fire)"
        )


# ── the shape the fix needs: a named helper, not a block inside the client ──


def test_the_log_setup_is_reachable_without_starting_a_client():
    """Why the site survived the sweep: `_run_client` itself is not callable from a test.

    Pinned as a property rather than by reading the source — the callable is the thing that
    makes every arm below possible at all.
    """
    assert callable(emrg_main._configure_client_logging)
    assert emrg_main._configure_client_logging is not emrg_main._run_client


# ── behaviour: a regular file, and the absent case ─────────────────────────


def test_a_regular_file_at_the_client_log_takes_a_rotating_handler(tmp_path, monkeypatch, capsys):
    """The normal case, and the half that must not change: the log is still written."""
    sink = _plant(tmp_path, "a regular file")
    monkeypatch.chdir(tmp_path)

    with _root_handlers_cleared():
        returned = emrg_main._configure_client_logging()
        installed = _rotating_handlers()

    assert returned.resolve() == sink.resolve()
    assert installed, "no rotating handler was installed on a regular file"
    assert Path(installed[0].baseFilename).resolve() == returned.resolve()
    assert "not a regular file" not in capsys.readouterr().err, (
        "a regular file was reported as a non-regular subject"
    )


def test_an_absent_client_log_is_created_rather_than_refused(tmp_path, monkeypatch, capsys):
    """Absence is not a refusal: `non_regular_kind` *stats*, so a missing file raises.

    The guard reads that `OSError` as "open it", and this is the arm that discriminates the
    branch — a guard that returns whenever the stat failed, or that refuses whenever it has
    no `None` kind, passes every other arm here and fails this one.
    """
    sink = _plant(tmp_path, "nothing yet")
    monkeypatch.chdir(tmp_path)

    with _root_handlers_cleared():
        returned = emrg_main._configure_client_logging()
        installed = _rotating_handlers()

    assert returned.resolve() == sink.resolve()
    assert sink.exists(), "the absent log file was not created"
    assert installed, "an absent log file was refused instead of created"
    assert not sink.is_dir()
    assert "not a regular file" not in capsys.readouterr().err, (
        "an absent log file was reported as a non-regular subject"
    )


# ── the pair: a directory on both legs, a FIFO where the platform has one ──


def test_a_directory_at_the_client_log_names_its_kind_and_the_client_still_starts(
    tmp_path, monkeypatch, capsys
):
    """The FIFO-free half, and the one the Windows leg runs.

    A FIFO is the only shape that *blocks*, so the arm below is POSIX-gated and reverting
    the guard there is undetectable on Windows. A directory is the shape both legs have,
    and at this site it is the failure that takes the client down: unguarded,
    `RotatingFileHandler` raises `IsADirectoryError` out of `_run_client` (measured
    `cyc20261012-060054`), so no TUI is ever reached. Guarded, the call returns and says
    which kind it found — the message, not the return value, is the discriminator.
    """
    sink = _plant(tmp_path, "a directory")
    monkeypatch.chdir(tmp_path)

    with _root_handlers_cleared():
        returned = emrg_main._configure_client_logging()
        installed = _rotating_handlers()

    err = capsys.readouterr().err
    assert returned.resolve() == sink.resolve()
    assert "a directory" in err, f"the skip must name the kind it found: {err!r}"
    assert "not a regular file" in err, f"the message states the boundary: {err!r}"
    assert not installed, "a rotating handler was installed on a subject that is a directory"
    assert sink.is_dir(), "the directory was replaced by an opened file"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
def test_a_fifo_at_the_client_log_does_not_wedge_the_startup(tmp_path):
    """`RotatingFileHandler` opens at construction, so this is the startup path itself."""
    sink = _plant(tmp_path, "a FIFO")

    proc = _setup_in_a_child(tmp_path, "a FIFO")

    assert proc.returncode == 0, proc.stderr
    assert "reached" in proc.stdout, (
        f"the client's log setup did not finish; the client would never start: {proc.stderr!r}"
    )
    assert "not a regular file" in proc.stderr, (
        "the skip was silent — why no file log was installed must be readable: "
        f"{proc.stderr!r}"
    )
    assert stat.S_ISFIFO(os.stat(sink).st_mode), "the client log was written into the pipe"
    assert os.stat(sink).st_size == 0, "something wrote into the FIFO"
