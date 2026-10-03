"""A JSON state file has ONE unreadable class, and every reader must answer it once.

Why this file exists
--------------------
The JSON half of the family `tests/test_a_config_that_cannot_be_read.py` (config.toml)
and `tests/test_a_yaml_state_file_has_one_unreadable_class.py` (projects.yml,
tasks.yml) each measured. Reading a JSON state file can fail in three shapes, and they
are one class because a reader cannot tell them apart by asking "could I read it?":

* **`OSError`** — no such file, a directory, a permission bit
* **`json.JSONDecodeError`** — bytes that are not JSON
* **`UnicodeDecodeError`** — bytes that are not UTF-8, which is a **`ValueError` and
  not an `OSError`**, which is why `(json.JSONDecodeError, OSError)` catches two of the
  three and lets this one escape

Measured 2026-10-03 (`cyc20261003-215322`) on the **twelve** readers that spelled that
two-shape tuple. For every one, the third shape did not change a message — it broke a
**declared contract**, and the declarations are quoted here because each one is the
thing that is false:

    reader                                   its own declaration
    sessions_index._load                     "corrupt/missing file yields an empty
                                              dict (never raises)"
    sessions_index._read_meta_session_id      "None if missing/corrupt"
    Session.title                            (a property: returns the fallback)
    Session.list_sessions                    "corrupt meta.json, skipping"
    Session._save_meta_with_title             (must preserve the existing title)
    Session.create_with_id                   (the index write is "never raises")
    git_utils._cached_tool_path               "if present"
    git_utils._cache_tool_paths               "degrades to an empty dict instead of
                                              raising JSONDecodeError"
    daemon._evolution_count                   "corrupt/partial write — don't count"
    daemon evolution_summary                  (skip the file, keep the rest)
    daemon._handle_resume_session             (meta unreadable ⇒ {} ⇒ ghost check)
    daemon list_rants                         "failed to read … " and answer
    daemon._count_drill_drift_events           (count what is readable)

and the answers it produced instead, which is the part that makes it a defect rather
than a style note — a **single** unreadable file cost more than itself:

    * `Session.list_sessions` returned [] (today: **RAISED**), so one bad `meta.json`
      hid every session of the project from both clients
    * `Session.create_with_id` **RAISED**, so a non-UTF-8 sessions index broke *every
      session save* on the host — the index is read on each write to upsert into it
    * the two project/message handlers raised mid-handler, which a client cannot tell
      apart from a dropped connection

The class now has one home — `emrg.read_errors`, a leaf both `emrg/` and
`emrg/server/` import downward — and this file pins both halves: the behaviour of each
reader, and the fact that the others' constants are **derived** from the shared read
half rather than respelled beside it.

Three readers already had the class right and are the control:
`skills.registry.read_state`, and the two `except Exception` sites in `daemon.py`.

⚠️ Nothing here writes the host's `~/.emrg`: every path is a `tmp_path`, and the two
readers that resolve `config_dir()` have it monkeypatched first.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import emrg.server.daemon as daemon_mod  # noqa: E402
import emrg.session as session_mod  # noqa: E402
import emrg.server.git_utils as gu  # noqa: E402
import emrg.sessions_index as sidx  # noqa: E402
from emrg.config import CONFIG_READ_ERRORS, LlmConfig  # noqa: E402
from emrg.read_errors import FILE_READ_ERRORS, JSON_READ_ERRORS  # noqa: E402
from emrg.server.atomic import YAML_READ_ERRORS  # noqa: E402
from emrg.session import Session  # noqa: E402

#: The two *unreadable* shapes whose answers this file compares. `corrupt-json` is the
#: shape the shipped tuples caught; `not-utf-8` is the one they missed. They are
#: compared rather than each pinned to a literal, because the defect is precisely that
#: the same input class got two different answers.
UNREADABLE = {
    "corrupt-json": b'{"session_id": "s1", ',
    "not-utf-8": b'{"session_id": "\xff\xfe"}\n',
}

GOOD_META = b'{"session_id": "s1", "title": "kept", "message_count": 2}'


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class _FakeWriter:
    """Minimal WebSocket stand-in — the shape `tests/test_daemon.py` uses."""

    def __init__(self) -> None:
        self._frames: list[str] = []

    async def send(self, data) -> None:
        self._frames.append(data.decode() if isinstance(data, bytes) else data)

    async def close(self) -> None:
        pass

    def decoded(self) -> list[dict]:
        return [json.loads(f) for f in self._frames]


def _drive(server, msg: dict) -> list[dict]:
    """One `_process_message` call, returning the frames it sent."""
    writer = _FakeWriter()
    asyncio.run(server._process_message(msg, writer))  # type: ignore[arg-type]
    return writer.decoded()


def _server(tmp_path: Path, monkeypatch) -> daemon_mod.EmrgServer:
    """A server whose `config_dir()` is a scratch tree, so nothing touches ~/.emrg."""
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    return daemon_mod.EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))


# ── the home, and the derivation the other two formats share it through ─────


def test_the_home_names_the_third_shape_and_it_is_not_an_os_error() -> None:
    """The pin the shipped tuple would have failed, where an editor will see it."""
    assert issubclass(UnicodeDecodeError, ValueError)
    assert not issubclass(UnicodeDecodeError, OSError)
    assert UnicodeDecodeError in FILE_READ_ERRORS
    assert JSON_READ_ERRORS == (*FILE_READ_ERRORS, json.JSONDecodeError)


def test_the_home_is_not_every_exception() -> None:
    """The control: a tuple that caught everything would pass every test below."""
    assert not isinstance(RuntimeError("x"), JSON_READ_ERRORS)


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_the_home_catches_what_each_shape_actually_raises(tmp_path, shape) -> None:
    """Read by *doing* the read, so a future narrowing fails here, not in production."""
    path = _write(tmp_path / "meta.json", UNREADABLE[shape])
    with pytest.raises(Exception) as excinfo:  # noqa: B017 - the type is the subject
        json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(excinfo.value, JSON_READ_ERRORS), (
        f"{shape} raised {type(excinfo.value).__name__}, which is not in "
        f"JSON_READ_ERRORS — a reader catching that tuple would let it escape"
    )


def test_the_other_two_formats_derive_the_same_read_half() -> None:
    """One input class, one read half: the three constants cannot drift apart.

    `CONFIG_READ_ERRORS` and `YAML_READ_ERRORS` are built from `FILE_READ_ERRORS`
    rather than each spelling `UnicodeDecodeError` themselves — which is exactly how
    three families came to miss it in three separate sweeps.
    """
    for name, errors in (("CONFIG_READ_ERRORS", CONFIG_READ_ERRORS), ("YAML_READ_ERRORS", YAML_READ_ERRORS)):
        assert set(FILE_READ_ERRORS) <= set(errors), f"{name} does not carry the shared read half"
    assert CONFIG_READ_ERRORS == (*FILE_READ_ERRORS, CONFIG_READ_ERRORS[-1])
    assert YAML_READ_ERRORS == (*FILE_READ_ERRORS, YAML_READ_ERRORS[-1])


# ── the sessions index: its docstring promises it never raises ──────────────


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_load_answers_both_shapes_with_an_empty_index(tmp_path, shape) -> None:
    """`_load`: "corrupt/missing file yields an empty dict (never raises)"."""
    path = _write(tmp_path / "sessions_index.json", UNREADABLE[shape])
    assert sidx._load(path) == {}


def test_load_really_loads(tmp_path) -> None:
    """The control: "empty" must not be the answer for every input."""
    path = _write(tmp_path / "sessions_index.json", b'{"s1": "/tmp/p"}')
    assert sidx._load(path) == {"s1": "/tmp/p"}


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_read_meta_session_id_answers_both_shapes_with_none(tmp_path, shape) -> None:
    """`_read_meta_session_id`: "None if missing/corrupt"."""
    path = _write(tmp_path / "meta.json", UNREADABLE[shape])
    assert sidx._read_meta_session_id(path) is None


def test_read_meta_session_id_really_reads(tmp_path) -> None:
    """The control."""
    path = _write(tmp_path / "meta.json", GOOD_META)
    assert sidx._read_meta_session_id(path) == "s1"


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_an_unreadable_index_does_not_break_a_session_save(tmp_path, monkeypatch, shape) -> None:
    """The index is read on every upsert, so an unreadable one breaks *every* save.

    Measured before the fix: `Session.create_with_id` raised `UnicodeDecodeError` for
    the non-UTF-8 shape while the corrupt shape created the session and reset the
    index — one unreadable file, two different verdicts on session creation.
    """
    index = _write(tmp_path / "sessions_index.json", UNREADABLE[shape])
    monkeypatch.setattr(sidx, "sessions_index_path", lambda: index)
    session = Session.create_with_id("s_new", tmp_path)
    assert session.session_id == "s_new"
    # …and the index is usable afterwards, which is what "resetting" means.
    assert isinstance(sidx._load(index), dict)


# ── Session: the three readers that touch a session's own meta.json ─────────


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_a_session_title_falls_back_for_either_shape(tmp_path, shape) -> None:
    """`title` is a property the clients read on every list; it must not raise."""
    session = Session.create_with_id("s_t", tmp_path)
    _write(session._meta_path, UNREADABLE[shape])
    assert session.title == "s_t"


def test_a_session_title_really_reads_the_file(tmp_path) -> None:
    """The control, and the point: the fallback is not simply the answer."""
    session = Session.create_with_id("s_t", tmp_path)
    session.rename("a real title")
    assert session.title == "a real title"


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_rename_survives_an_unreadable_meta(tmp_path, shape) -> None:
    """Renaming reads the old title first; an unreadable file must cost the title only."""
    session = Session.create_with_id("s_r", tmp_path)
    _write(session._meta_path, UNREADABLE[shape])
    session.rename("renamed")
    meta = json.loads(session._meta_path.read_text(encoding="utf-8"))
    assert meta["title"] == "renamed"


def test_rename_preserves_the_title_it_can_read(tmp_path) -> None:
    """The control: the read is not skipped, it just cannot fail the write."""
    session = Session.create_with_id("s_r", tmp_path)
    session.rename("first")
    session._save_meta_with_title(None)  # the "preserve" branch
    assert json.loads(session._meta_path.read_text(encoding="utf-8"))["title"] == "first"


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_list_sessions_skips_the_one_it_cannot_read(tmp_path, shape) -> None:
    """One unreadable `meta.json` costs that row, not the listing.

    Measured before the fix: the corrupt shape logged "corrupt meta.json, skipping"
    and returned the other rows; the non-UTF-8 shape raised out of the loop, so both
    clients showed **no sessions at all** for a project that had them.
    """
    root = tmp_path / "proj"
    for sid in ("s_good1", "s_bad", "s_good2"):
        meta = root / ".emrg" / "sessions" / sid / "meta.json"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_bytes(UNREADABLE[shape] if sid == "s_bad" else GOOD_META)

    listed = Session.list_sessions(root)

    assert len(listed) == 2, f"{shape}: the one unreadable meta cost the other rows"


def test_list_sessions_really_lists(tmp_path) -> None:
    """The control."""
    root = tmp_path / "proj"
    for sid in ("s_a", "s_b"):
        meta = root / ".emrg" / "sessions" / sid / "meta.json"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_bytes(GOOD_META)
    assert len(Session.list_sessions(root)) == 2


# ── git_utils: the install-info cache ───────────────────────────────────────


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_the_tool_path_cache_reads_both_shapes_as_absent(tmp_path, monkeypatch, shape) -> None:
    """`_cached_tool_path`: a cache it cannot read means "not cached", never a crash.

    This one is on the hot path of every `git_cmd()` — `resolve_git_gh` consults it
    first — so a raise here is a raise inside the daemon's own tool resolution.
    """
    monkeypatch.setattr(gu, "INSTALL_INFO", _write(tmp_path / "install-info.json", UNREADABLE[shape]))
    assert gu._cached_tool_path("git") is None


def test_the_tool_path_cache_really_reads(tmp_path, monkeypatch) -> None:
    """The control."""
    monkeypatch.setattr(
        gu, "INSTALL_INFO", _write(tmp_path / "install-info.json", b'{"git_path": "/opt/git"}')
    )
    assert gu._cached_tool_path("git") == "/opt/git"


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_the_cache_write_survives_an_unreadable_cache(tmp_path, monkeypatch, shape) -> None:
    """"degrades to an empty dict instead of raising" — for both shapes.

    The write reads the cache first to merge into it. Before the fix the non-UTF-8
    shape raised out of `_cache_tool_paths`, so the cache was never written and the
    next resolution paid a full re-probe.
    """
    info = _write(tmp_path / "install-info.json", UNREADABLE[shape])
    monkeypatch.setattr(gu, "INSTALL_INFO", info)
    gu._cache_tool_paths("/opt/git", "/opt/gh")
    written = json.loads(info.read_text(encoding="utf-8"))
    assert written["git_path"] == "/opt/git" and written["gh_path"] == "/opt/gh"


def test_the_cache_write_really_merges(tmp_path, monkeypatch) -> None:
    """The control: a readable cache's other keys survive the write.

    `repo` is not the key to check — the writer sets that one unconditionally, so it
    would pass for a write that read nothing at all. An unrelated key is the reading
    that discriminates.
    """
    info = _write(
        tmp_path / "install-info.json",
        b'{"git_path": "/old/git", "kept_from_before": "yes"}',
    )
    monkeypatch.setattr(gu, "INSTALL_INFO", info)
    gu._cache_tool_paths("/opt/git", "/opt/gh")
    written = json.loads(info.read_text(encoding="utf-8"))
    assert written["kept_from_before"] == "yes" and written["git_path"] == "/opt/git"


# ── daemon: evolution logs, resume, the rant panel, the drill counter ───────


def _evolution_log(tmp_path: Path, name: str, payload: bytes) -> Path:
    return _write(tmp_path / "logs" / name, payload)


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_evolution_count_skips_the_log_it_cannot_read(tmp_path, monkeypatch, shape) -> None:
    """`_evolution_count`: "corrupt/partial write — don't count" means don't *stop*."""
    server = _server(tmp_path, monkeypatch)
    _evolution_log(tmp_path, "evolution-2026.json", UNREADABLE[shape])
    _evolution_log(tmp_path, "evolution-2027.json", b'{"timestamp": "2027-01-01"}')

    assert server._evolution_count() == 1, f"{shape}: the readable log was not counted"


def test_evolution_summary_replies_for_either_unreadable_log(tmp_path, monkeypatch) -> None:
    """The `evolution_summary` handler: skip the file, keep the rest of the answer."""
    for shape in sorted(UNREADABLE):
        scratch = tmp_path / shape
        scratch.mkdir()
        server = _server(scratch, monkeypatch)
        _evolution_log(scratch, "evolution-2026.json", UNREADABLE[shape])
        _evolution_log(scratch, "evolution-2027.json", b'{"timestamp": "2027-01-01"}')

        frames = _drive(server, {"type": "evolution_summary"})

        assert len(frames) == 1 and frames[0]["type"] == "evolution_summary"
        assert len(frames[0]["recent"]) == 1, f"{shape}: the readable log was dropped"


def test_evolution_summary_really_reads(tmp_path, monkeypatch) -> None:
    """The control."""
    server = _server(tmp_path, monkeypatch)
    _evolution_log(tmp_path, "evolution-2026.json", b'{"timestamp": "2026-01-01"}')
    frames = _drive(server, {"type": "evolution_summary"})
    assert len(frames[0]["recent"]) == 1


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_resume_session_replies_for_either_unreadable_meta(tmp_path, monkeypatch, shape) -> None:
    """`_handle_resume_session` reads meta to validate; it must answer, not drop.

    A handler that raises is a handler whose client loses its connection, which the
    client cannot tell apart from a network drop.
    """
    server = _server(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    _write(project / ".emrg" / "sessions" / "s1" / "meta.json", UNREADABLE[shape])
    writer = _FakeWriter()

    asyncio.run(server._handle_resume_session("s1", project, writer))

    frames = writer.decoded()
    assert frames and frames[0]["type"] == "resume_result", f"{shape}: no reply was sent"


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_list_rants_replies_for_either_unreadable_ledger(tmp_path, monkeypatch, shape) -> None:
    """The rant panel names the failure in a frame; for both shapes the same way."""
    server = _server(tmp_path, monkeypatch)
    server._rants_log = _write(tmp_path / "rants.jsonl", UNREADABLE[shape])

    frames = _drive(server, {"type": "list_rants"})

    assert len(frames) == 1 and frames[0]["type"] == "rants_list"
    assert frames[0]["rants"] == []


def test_list_rants_really_lists(tmp_path, monkeypatch) -> None:
    """The control."""
    server = _server(tmp_path, monkeypatch)
    server._rants_log = _write(
        tmp_path / "rants.jsonl",
        b'{"timestamp": "2026-01-01T00:00:00+08:00", "project": "emrg", "status": "pending"}\n',
    )
    frames = _drive(server, {"type": "list_rants"})
    assert len(frames[0]["rants"]) == 1


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_the_drill_counter_skips_the_line_it_cannot_read(tmp_path, monkeypatch, shape) -> None:
    """`_count_drill_drift_events`: a line it cannot read is a line it cannot count."""
    server = _server(tmp_path, monkeypatch)
    stats = _write(tmp_path / "usage-anchor.jsonl", UNREADABLE[shape])
    monkeypatch.setattr(daemon_mod, "_USAGE_ANCHOR_STATS_PATH", stats)

    assert server._count_drill_drift_events() == 0


def test_the_drill_counter_really_counts(tmp_path, monkeypatch) -> None:
    """The control: the counter is not simply answering 0."""
    server = _server(tmp_path, monkeypatch)
    line = json.dumps(
        {"type": "anchor_provider_drift", "session": daemon_mod._PLANTED_FIRE_DRILL_SESSION}
    ).encode()
    monkeypatch.setattr(
        daemon_mod, "_USAGE_ANCHOR_STATS_PATH", _write(tmp_path / "usage-anchor.jsonl", line + b"\n")
    )
    assert server._count_drill_drift_events() == 1


# ── the second half: every reader names the one home ────────────────────────


def test_every_json_reader_binds_the_shared_home() -> None:
    """One home, read by every reader — not a tuple re-spelled at each call site.

    Twelve sites spelling `(json.JSONDecodeError, OSError)` is how the third shape came
    to be missed in twelve places at once. The rule for the *source* is the structural
    one (`scripts/check_read_parse_guards.py`, pinned by
    `tests/test_check_read_parse_guards.py`): a `try` that reads a file text and parses
    it must name a decode error, however it spells it. This is the narrower half that
    says *which* spelling these four modules use, so a future call site that decides to
    spell its own tuple has to fail the structural guard rather than sneak past a
    pin that only counted lines starting with `except`.

    (An earlier version of this test did exactly that — matched every line containing
    `JSONDecodeError` — and reported nine false positives, because parsing a websocket
    frame or a string is not reading a file and needs no decode error at all.)
    """
    for module in (session_mod, sidx, gu, daemon_mod):
        assert getattr(module, "JSON_READ_ERRORS", None) is JSON_READ_ERRORS, (
            f"{Path(module.__file__).name} does not bind emrg.read_errors.JSON_READ_ERRORS"
        )
