"""What `emrg server` says when the config file itself cannot be loaded.

`load_config()` has three ways to fail, and this path answered exactly one of them.
Measured 2026-10-03 (`cyc20261003-184104`) on an isolated HOME, with the config file
of each shape:

    missing   -> exit 1, `Error: config not found at <path> — create it with [llm] section`
    corrupt   -> exit 1, a raw traceback: `tomllib.TOMLDecodeError: Expected '=' after a
                 key in a key/value pair (at line 3, column 6)` under seven `tomllib`
                 internal frames
    not UTF-8 -> exit 1, the same shape with `UnicodeDecodeError`

So one failure mode was reported and two leaked a traceback, in the same function, for
the same class of failure. The daemon's own entry (`emrg/server/__main__.py`) reports all
three — startup there is inside the guard that turns a crash into a record (issue #1835,
PR #1836) — and `emrg server` is that path's foreground twin: the command the CLI's own
start-failure message tells a host to run ("please run 'emrg server' manually").

The rule is that a failure names its cause where the reader is looking, and it is why the
*parser's* words are kept — they carry the file, the line and the column — while the
traceback's frames are not: naming `tomllib`'s internals tells a host nothing they can act
on. `UnicodeDecodeError` is a different fact from a syntax error, so it gets its own
sentence rather than borrowing the TOML one.

⚠️ Nothing here starts, stops or restarts a daemon (MANIFESTO 第四条附则二). `_run_daemon`
is driven with its side effects stubbed — `ensure_tool_dirs` (which would mutate
`os.environ['PATH']`), the server itself, and `asyncio.run` — so no process is spawned and
no port is touched, and the one test that uses the **real** loader writes its corrupt file
at the path `conftest._redirect_the_config_path` already points `config_path()` at (a
`tmp_path` scratch tree), so no host file is read or written. `_run_daemon` never calls
`_configure_logging`, so no log file is opened either.

⚠️ **The scratch path is the suite's `config_path()`, not `Path.home()`** — measured
2026-10-03 while writing this file. `_redirect_the_config_path` patches the *function*
(`emrg.config.config_path`), not `Path.home`, and its docstring says so ("a test that wants
a config file of its own patches `config_path` after this fixture"). A version of the
integration test below that instead pointed `Path.home` at a scratch tree got
`FileNotFoundError` — the loader was still looking at the fixture's path, which was empty —
and its assertion read that as the *missing* branch. Write the file where the loader
already looks.
"""

from __future__ import annotations

import sys
import tomllib
import types
from pathlib import Path

import pytest

from emrg import __main__ as cli


@pytest.fixture()
def daemon_entry(monkeypatch):
    """`_run_daemon` with its side effects stubbed, and what it reached recorded.

    Returns a callable: `run(load)`, where `load` is a zero-argument callable standing
    in for `load_config()`. The returned dict says whether the server was reached — the
    control that keeps the new clauses from swallowing a *successful* start.
    """
    reached: dict = {"server": False}

    monkeypatch.setattr("emrg.tool_path.ensure_tool_dirs", lambda: [])
    monkeypatch.setattr(
        "emrg.server.daemon.run_server", lambda _llm: reached.__setitem__("server", True)
    )
    monkeypatch.setattr(cli, "asyncio", types.SimpleNamespace(run=lambda _coro: None))

    def run(load):
        monkeypatch.setattr("emrg.config.load_config", load)
        try:
            cli._run_daemon()
        except SystemExit as e:
            return e.code, reached
        return None, reached

    return run


def test_a_missing_config_keeps_the_loaders_own_sentence(daemon_entry, capsys):
    """The control: the failure mode that was already reported must not change."""
    message = "config not found at /nowhere/config.toml — create it with [llm] section"

    def load():
        raise FileNotFoundError(message)

    code, reached = daemon_entry(load)
    assert code == 1, "a config that cannot be loaded is a failed start"
    assert message in capsys.readouterr().err, "the loader's own sentence is the report"


@pytest.mark.parametrize(
    "exc,spoken",
    [
        (
            tomllib.TOMLDecodeError(
                "Expected '=' after a key in a key/value pair (at line 48, column 6)"
            ),
            "is not valid TOML",
        ),
        (
            UnicodeDecodeError("utf-8", b"\xff", 15, 16, "invalid start byte"),
            "is not UTF-8 text",
        ),
    ],
)
def test_the_two_modes_that_leaked_a_traceback_now_say_what_happened(
    daemon_entry, capsys, exc, spoken
):
    """The defect, one assertion per mode.

    The parser's own words have to survive: `(at line 48, column 6)` is the actionable
    half of the corrupt-config case, and a sentence without it would be a nicer-looking
    report that places the host no closer to the edit.
    """
    def load():
        raise exc

    code, reached = daemon_entry(load)
    err = capsys.readouterr().err

    assert code == 1, "both modes are failed starts"
    assert spoken in err, err
    assert str(exc) in err, "the parser's own words, line and column included, are kept"
    assert "Traceback (most recent call last)" not in err, (
        "a config that cannot be loaded is reported as a sentence now — a traceback's "
        f"frames name tomllib's internals, which a host cannot act on: {err!r}"
    )
    assert not reached["server"], "a config that cannot be loaded must not reach the server"


def test_every_message_names_the_file_it_could_not_load(daemon_entry, capsys):
    """A failure that does not name the file leaves the host to guess which one it read.

    The path is `emrg.config.config_path()` — what the loader itself resolves — so the
    sentence and the loader cannot disagree about which file was read.
    """
    from emrg.config import config_path

    def load():
        raise tomllib.TOMLDecodeError("Expected '=' after a key in a key/value pair (at line 3, column 6)")

    daemon_entry(load)
    err = capsys.readouterr().err
    assert str(config_path()) in err, err


def test_a_loadable_config_still_starts_the_server(daemon_entry):
    """The control the whole file rests on: the new clauses do not swallow a good start.

    Without this, three `except` clauses added for failure modes would pass every test
    above while making `emrg server` unable to run at all.
    """
    code, reached = daemon_entry(lambda: types.SimpleNamespace(llm=object()))
    assert code is None, "a loadable config does not exit"
    assert reached["server"], "the server was never reached"


def test_the_real_loader_reaches_the_corrupt_branch(monkeypatch, capsys):
    """The integration, with the **real** `load_config` — no stub raising for me.

    A stubbed loader proves the clause handles an object of that class; it cannot show
    that the class is the one the loader raises. Here the file really is invalid TOML and
    really is read by `emrg.config.load_config`.

    The file goes where `_redirect_the_config_path` already points `config_path()` — this
    test's `tmp_path` scratch tree — rather than at a `Path.home()` of its own: the loader
    resolves `config_path()`, so a second notion of "the" path is a test that reads an
    empty file and calls the resulting `FileNotFoundError` the branch it meant to reach
    (measured while writing this file, 2026-10-03).
    """
    import emrg.config as cfg_mod

    cfg = cfg_mod.config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[llm]\nmodel = "x"\nprobe\n', encoding="utf-8")
    monkeypatch.setattr("emrg.tool_path.ensure_tool_dirs", lambda: [])

    with pytest.raises(SystemExit) as exit_info:
        cli._run_daemon()

    err = capsys.readouterr().err
    assert exit_info.value.code == 1
    assert "is not valid TOML" in err, err
    assert "line 3" in err, "the real parser's position is what makes this actionable"
    assert str(cfg) in err, "the sentence names the file the loader actually read"
    assert "Traceback (most recent call last)" not in err, err


def test_the_config_path_the_message_names_is_the_one_the_loader_reads(monkeypatch, tmp_path):
    """Two readers of one path must not disagree — the message is derived, not spelled.

    `config_path()` is imported into `_run_daemon` and called for the message, so a later
    edit cannot point the sentence at a hard-coded `~/.emrg/config.toml` while the loader
    reads somewhere else. Asserted on the source, because that is the property: the
    message is built from the same function the loader uses.
    """
    src = Path(cli.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "from emrg.config import config_path, load_config" in src
    assert "{config_path()} is not valid TOML" in src
    assert "{config_path()} is not UTF-8 text" in src
