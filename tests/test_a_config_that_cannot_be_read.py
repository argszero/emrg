"""The three ways a config file can be unreadable, through every reader of it.

Why this file exists
--------------------
`~/.emrg/config.toml` can be unreadable in three shapes, and they are one class
because no caller can tell them apart by asking "could I read the config?":

* **no such file** (or a directory, or a permission bit) — an `OSError`
* **bytes that are not TOML** — `tomllib.TOMLDecodeError`
* **bytes that are not UTF-8** — `UnicodeDecodeError`, which is a `ValueError`
  and **not** an `OSError`

Measured 2026-10-03 (`cyc20261003-211427`), before the fix, on the daemon that
`python -m emrg.server` runs (child process, HOME pinned to a scratch tree, so
nothing here reads or writes the host's own config):

    config        child rc  emrgd-exit.log   what the host was told
    not UTF-8     1         NO RECORD        nothing but the PATH line
    corrupt TOML  1         NO RECORD        nothing but the PATH line
    missing       1         NO RECORD        nothing but the PATH line
    valid         143       A RECORD         the exit record, reason named

Two readers were wrong in two different ways. `emrg/config.py`'s two section
loaders spelled `except (OSError, tomllib.TOMLDecodeError)` — two of the three —
so a non-UTF-8 file raised out of both of them while its two siblings returned
defaults: one input class, two answers. And `emrg/server/__main__.py` called
`load_config()` *above* the try that produces the durable exit record, so a config
failure reached neither the daemon's log nor the record — and the traceback went
to `emrgd-crash.log`, which the client's failed-start explainer deliberately does
not read (`emrg/client/daemon_manager.py::_log_path`: it reads `emrgd.log` on
purpose). The host was shown a failed start and no cause.

Both halves are pinned here, each against the opposite direction, and the one
behaviour this file cannot show is named at the bottom.

⚠️ Nothing here starts, stops or restarts a daemon: `run_server` is replaced by a
stub that raises `SystemExit(97)` without binding anything, and the entry's two
file-touching effects (`_configure_logging`, `_redirect_std_streams`) are replaced
by no-ops, so no test in this file can write to the host's `~/.emrg`.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import emrg.config as cfg_mod  # noqa: E402
import emrg.server.daemon as srv  # noqa: E402
from emrg.server import __main__ as entry  # noqa: E402

#: The unreadable shapes, as the bytes to put on disk (`None` means "no file").
#: `not-utf-8` is the member the shipped tuple missed; it is listed with the rest
#: so a future narrowing has to drop it visibly.
SHAPES = {
    "missing": None,
    "not-utf-8": b'[llm]\nmodel = "\xff\xfebad"\n',
    "corrupt-toml": b"[llm\nmodel = ",
    "is-a-directory": "DIR",
}

GOOD_CONFIG = b'[llm]\nbase_url = "https://example.invalid/v1"\nmodel = "m"\n'


def _write_shape(cfg_path: Path, payload) -> None:
    """Put one shape on disk at `cfg_path`, replacing whatever was there."""
    if cfg_path.exists():
        if cfg_path.is_dir():
            cfg_path.rmdir()
        else:
            cfg_path.unlink()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if payload == "DIR":
        cfg_path.mkdir()
    elif payload is not None:
        cfg_path.write_bytes(payload)


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    """A config path inside `tmp_path`, for both the module and the entry."""
    path = tmp_path / ".emrg" / "config.toml"
    monkeypatch.setattr(cfg_mod, "config_path", lambda: path)
    return path


# ── the two section loaders: one input class, one answer ─────────────────────


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_an_unreadable_config_gives_a_sandbox_loader_its_defaults(cfg_path, shape) -> None:
    """`load_sandbox_config` tolerates *every* unreadable shape, not two of them."""
    _write_shape(cfg_path, SHAPES[shape])
    got = cfg_mod.load_sandbox_config()
    assert got == cfg_mod.SandboxConfig(), (
        f"{shape}: the sandbox loader answered something other than its defaults — the "
        "three shapes are one input class, so a reader that answers two of them and "
        "raises on the third is answering the same question two ways"
    )


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_an_unreadable_config_gives_the_update_loader_its_defaults(cfg_path, shape) -> None:
    """The same for `load_update_config`, which the UpgradeManager is built from."""
    _write_shape(cfg_path, SHAPES[shape])
    got = cfg_mod.load_update_config()
    assert got == cfg_mod.UpdateConfig(), (
        f"{shape}: the update loader answered something other than its defaults"
    )


def test_the_sandbox_loader_really_reads_a_config_it_can_read(cfg_path) -> None:
    """The control: "returns the defaults" must not be the answer for every input.

    Without this, a loader that ignored the file entirely would pass the two tests
    above — the defect they exist for would be replaced by a bigger one.
    """
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text('[sandbox]\npwsh_path = "/opt/pwsh"\n', encoding="utf-8")
    assert cfg_mod.load_sandbox_config().pwsh_path == "/opt/pwsh"


def test_the_update_loader_really_reads_a_config_it_can_read(cfg_path) -> None:
    """And the same control for the other loader."""
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("[update]\nenabled = false\n", encoding="utf-8")
    assert cfg_mod.load_update_config().enabled is False


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_home_names_the_error_each_shape_actually_raises(cfg_path, shape) -> None:
    """`CONFIG_READ_ERRORS` is the home, and it holds the shape that raises it.

    Read by *doing* the read, not by inspecting the tuple: each shape's real
    exception has to be a member, so narrowing the tuple (dropping
    `UnicodeDecodeError`, as the two loaders had) fails here rather than in
    production. This is the pin the old spelling would have failed.
    """
    _write_shape(cfg_path, SHAPES[shape])
    with pytest.raises(Exception) as excinfo:  # noqa: B017 - the type is the subject
        cfg_mod.load_config(cfg_path)
    assert isinstance(excinfo.value, cfg_mod.CONFIG_READ_ERRORS), (
        f"{shape} raised {type(excinfo.value).__name__}, which is not in "
        f"CONFIG_READ_ERRORS — a caller catching that tuple would let it escape"
    )


def test_the_home_is_not_every_exception() -> None:
    """The control: a tuple that caught everything would pass the test above."""
    assert not isinstance(RuntimeError("x"), cfg_mod.CONFIG_READ_ERRORS)


def test_the_exception_the_old_tuple_missed_is_a_value_error_not_an_os_error() -> None:
    """Why the miss happened, pinned where an editor will see it.

    `UnicodeDecodeError` inherits from `ValueError`; it is not an `OSError`, so the
    spelling `(OSError, tomllib.TOMLDecodeError)` reads like "every I/O or parse
    failure" and is not. If this ever stops being true the tuple is still correct,
    so this is a note rather than a guard — but it is the sentence the next
    narrowing will need.
    """
    assert issubclass(UnicodeDecodeError, ValueError)
    assert not issubclass(UnicodeDecodeError, OSError)


# ── the daemon entry: the stop path that reached neither log nor record ──────


def _drive_entry(monkeypatch, cfg_path: Path, record_path: Path, calls: list):
    """Run `emrg.server.__main__.main()` with every real effect replaced.

    The stub `run_server` returns a sentinel `DaemonExit` rather than raising: the
    entry maps **any** `BaseException` escaping the server to one of its own stop
    reasons (`SystemExit` → `sigterm`, everything else → `crash`), so an exception
    would be re-labelled before it could be read as a marker. Returning the object
    the real `run_server` returns is also the honest shape of that call. Nothing is
    spawned, no port is touched, and neither `_configure_logging` nor
    `_redirect_std_streams` runs, so no file outside `tmp_path` is written.
    """
    monkeypatch.setattr(entry, "_configure_logging", lambda: None)
    monkeypatch.setattr(entry, "_redirect_std_streams", lambda: None)
    monkeypatch.setattr(entry, "ensure_tool_dirs", lambda: None)
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)
    monkeypatch.setattr(srv, "_EXIT_RECORD_PATH", record_path)

    async def fake_run_server(llm):
        calls.append(llm)
        return srv.DaemonExit("test-stub", 97, None)

    monkeypatch.setattr(entry, "run_server", fake_run_server)
    with pytest.raises(SystemExit) as excinfo:
        entry.main()
    return excinfo.value.code


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_config_the_entry_cannot_read_leaves_a_record_and_no_server(
    cfg_path, tmp_path, monkeypatch, shape, caplog
) -> None:
    """Every unreadable shape: exit 1, a named cause, a durable record, no server."""
    _write_shape(cfg_path, SHAPES[shape])
    record_path = tmp_path / "emrgd-exit.log"
    calls: list = []

    with caplog.at_level(logging.CRITICAL, logger="emrg.server"):
        code = _drive_entry(monkeypatch, cfg_path, record_path, calls)

    assert code == 1, f"{shape}: a daemon that cannot read its config starts nothing"
    assert calls == [], f"{shape}: run_server was reached with a config that could not be read"
    assert record_path.exists(), (
        f"{shape}: no exit record. The record's own contract is 'every daemon stop, "
        "normal or abnormal, must be attributable' — and the client's failed-start "
        "explainer reads emrgd.log, so a cause outside the logging system is a cause "
        "the host is not shown"
    )
    record = json.loads(record_path.read_text(encoding="utf-8").splitlines()[-1])
    # `crash` is the landed contract for a startup failure before the event loop
    # (#1836, `cyc20261003-222355` resolved this branch onto it): the exit record's
    # `reason` is one word for the *kind* of stop, and the cause lives in the
    # traceback beside it. This branch's own version had written `config: <cause>`
    # here; the landing kept the word and moved the cause, which is the reading the
    # host's failed-start report already parses.
    assert record["reason"] == "crash", (
        f"{shape}: the record's reason is the word every pre-loop startup failure gets, "
        f"and the cause belongs in the traceback: {record}"
    )
    assert "Error" in (record.get("traceback") or ""), (
        f"{shape}: the record has to name the cause, not just that something crashed: "
        f"{record}"
    )
    assert record["exit_code"] == 1
    assert str(cfg_path) in caplog.text, (
        f"{shape}: the log line has to name the file — the host has to be able to "
        f"find it without guessing, and the traceback carries the line and column but "
        f"not the path: {caplog.text!r}"
    )


def test_a_config_the_entry_can_read_still_starts_the_server(
    cfg_path, tmp_path, monkeypatch
) -> None:
    """The control, and the point: the guard must not swallow the normal path.

    `run_server` is reached, its own exit code comes back, and the record says
    what that path was — not a config failure.
    """
    _write_shape(cfg_path, GOOD_CONFIG)
    record_path = tmp_path / "emrgd-exit.log"
    calls: list = []

    code = _drive_entry(monkeypatch, cfg_path, record_path, calls)

    assert code == 97, "the entry exited on its own instead of calling run_server"
    assert calls, "run_server was never reached with a config that loads"
    written = json.loads(record_path.read_text(encoding="utf-8").splitlines()[-1])
    assert written["reason"] == "test-stub", (
        "the entry wrote a config-failure record for a config it read fine — the "
        f"record has to say what happened, not merely that something did: {written}"
    )


def test_the_entry_reads_the_same_three_shapes_the_home_names() -> None:
    """One home, read by the entry — not a second spelling of the tuple.

    A tuple re-spelled at the call site is a tuple free to drift from
    `emrg.config.CONFIG_READ_ERRORS`, which is how the two section loaders came to
    miss the third shape in the first place.
    """
    source = Path(entry.__file__).read_text(encoding="utf-8")
    assert "except CONFIG_READ_ERRORS as exc:" in source, (
        "the entry no longer catches the shared tuple by name — a literal tuple here "
        "is the second home this file exists to prevent"
    )
    assert "except (OSError" not in source and "except (FileNotFoundError" not in source, (
        "the entry spells a config-error tuple of its own again"
    )
