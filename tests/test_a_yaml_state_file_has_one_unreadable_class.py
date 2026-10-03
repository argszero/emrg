"""A YAML state file has ONE unreadable class, and every reader must answer it once.

Why this file exists
--------------------
`projects.yml` and `tasks.yml` are YAML state files the host edits by hand. Reading
one of them can fail in three shapes, and they are one class because a reader cannot
tell them apart by asking "could I read it?":

* **`OSError`** — no such file, a directory where the file should be, a permission bit
* **`yaml.YAMLError`** — bytes that are not YAML
* **`UnicodeDecodeError`** — bytes that are not UTF-8, which is a **`ValueError`
  and not an `OSError`**, which is why the tuple `(yaml.YAMLError, OSError)` catches
  two of the three and lets this one escape

Measured 2026-10-03 (`cyc20261003-213507`, this file) before the fix, driving each
reader with a corrupt file and then with a non-UTF-8 file: **eight readers spelled
that two-shape tuple**, all eight let the third shape escape, and the escape did not
merely change a message — it changed the *answer*, in both directions:

    reader                              corrupt YAML            not UTF-8
    tasks.yml read_table                TableUnreadable         raw UnicodeDecodeError
    _resolve_project_path               None                    raw UnicodeDecodeError
    _load_project_config                {}                      raw UnicodeDecodeError
    _ensure_emrg_project_entry          keeps going             raw UnicodeDecodeError
    _rebuild_sessions_index             index rebuilt (no paths) index NOT rebuilt at all
    _touch_project                      entry rewritten         nothing recorded
    _handle_list_projects               a projects_list frame   connection dropped
    _handle_remove_project              a project_removed frame connection dropped

`read_table`'s own docstring is the sharpest case: it declares its refusal type —
"Parse failure, or a root that is not a list ⇒ `TableUnreadable`. Never `[]`" — and
one shape of an unreadable file bypasses that type entirely, so the three call sites
that catch `TableUnreadable` never see it.

Three readers already had the class right and are the control: `submit_rant_tool`'s
`_registered_project_names`, and the two `except Exception` sites in `daemon.py`.

The rule lives in one place — `emrg.server.atomic.YAML_READ_ERRORS` — and this file
pins both halves: the behaviour of each reader, and the fact that each one *names
that home* rather than respelling a tuple of its own.

⚠️ Nothing here writes the host's `~/.emrg`: every path is a `tmp_path`, and the two
readers that resolve `config_dir()` have it monkeypatched first.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import emrg.sessions_index as sidx  # noqa: E402
import emrg.server.daemon as daemon_mod  # noqa: E402
import emrg.server.scheduler as sched_mod  # noqa: E402
from emrg.config import LlmConfig  # noqa: E402
from emrg.server.atomic import YAML_READ_ERRORS  # noqa: E402

#: The two *unreadable* shapes whose answers this file compares. `corrupt-yaml` is the
#: shape the shipped tuples caught; `not-utf-8` is the one they missed. They are
#: compared rather than each pinned to a literal, because the defect is precisely that
#: the same input class got two different answers.
UNREADABLE = {
    "corrupt-yaml": b"- name: [unclosed\n  path: /tmp/x\n",
    "not-utf-8": b"- name: \xff\xfe-project\n  path: /tmp/x\n",
}

GOOD_PROJECTS = b"- name: alpha\n  path: /tmp/alpha\n"


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


# ── the home itself ─────────────────────────────────────────────────────────


def test_the_home_names_the_third_shape_and_it_is_not_an_os_error() -> None:
    """The pin the shipped tuple would have failed, written where an editor sees it.

    `UnicodeDecodeError` inherits from `ValueError`, so a tuple that reads like "every
    I/O or parse failure" is not one. This is deliberately about the *home* rather than
    about each reader: the readers' own tests below are the behaviour half.
    """
    assert issubclass(UnicodeDecodeError, ValueError)
    assert not issubclass(UnicodeDecodeError, OSError)
    assert UnicodeDecodeError in YAML_READ_ERRORS
    assert OSError in YAML_READ_ERRORS
    assert yaml.YAMLError in YAML_READ_ERRORS


def test_the_home_is_not_every_exception() -> None:
    """The control: a tuple that caught everything would pass every test below."""
    assert not isinstance(RuntimeError("x"), YAML_READ_ERRORS)


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_the_home_catches_what_each_shape_actually_raises(tmp_path, shape) -> None:
    """Read by *doing* the read, so a future narrowing fails here and not in production."""
    path = _write(tmp_path / "projects.yml", UNREADABLE[shape])
    with pytest.raises(Exception) as excinfo:  # noqa: B017 - the type is the subject
        yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(excinfo.value, YAML_READ_ERRORS), (
        f"{shape} raised {type(excinfo.value).__name__}, which is not in "
        f"YAML_READ_ERRORS — a reader catching that tuple would let it escape"
    )


# ── reader 1: tasks.yml, whose refusal type is declared ─────────────────────


def test_read_table_refuses_a_corrupt_table_with_its_own_type(tmp_path) -> None:
    """The shape the shipped tuple caught — pinned so the pair below has a baseline."""
    from emrg.server.scheduler import TableUnreadable, read_table

    path = _write(tmp_path / "tasks.yml", UNREADABLE["corrupt-yaml"])
    with pytest.raises(TableUnreadable):
        read_table(path)


def test_read_table_refuses_a_non_utf8_table_with_its_own_type(tmp_path) -> None:
    """The same table, unreadable for a different byte — so the same refusal type.

    `TableUnreadable` exists because "no tasks" retires every handler while
    "unreadable" must change nothing; a raw `UnicodeDecodeError` is neither, and the
    three call sites that catch `TableUnreadable` do not catch it.
    """
    from emrg.server.scheduler import TableUnreadable, read_table

    path = _write(tmp_path / "tasks.yml", UNREADABLE["not-utf-8"])
    with pytest.raises(TableUnreadable):
        read_table(path)


def test_read_table_really_reads_a_table_it_can_read(tmp_path) -> None:
    """The control: "refuses" must not become the answer for every input."""
    from emrg.server.scheduler import read_table

    path = _write(
        tmp_path / "tasks.yml",
        b"- name: auto1\n  type: evolution\n  path: /tmp/a1\n  interval: 600\n  enabled: true\n",
    )
    assert [r["name"] for r in read_table(path)] == ["auto1"]


# ── readers 2 and 3: the two projects.yml resolvers ─────────────────────────


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_resolve_project_path_answers_both_shapes_with_none(tmp_path, monkeypatch, shape) -> None:
    """A project name is unresolvable from a file that cannot be read — either way."""
    monkeypatch.setattr(sched_mod, "config_dir", lambda: tmp_path)
    _write(tmp_path / "projects.yml", UNREADABLE[shape])
    assert sched_mod._resolve_project_path("alpha") is None


def test_resolve_project_path_really_resolves(tmp_path, monkeypatch) -> None:
    """The control, and the point: the resolver is not simply answering None."""
    monkeypatch.setattr(sched_mod, "config_dir", lambda: tmp_path)
    _write(tmp_path / "projects.yml", GOOD_PROJECTS)
    assert sched_mod._resolve_project_path("alpha") == "/tmp/alpha"


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_load_project_config_answers_both_shapes_with_empty(tmp_path, monkeypatch, shape) -> None:
    """Same class, same answer: `{}` means "no entry", which is what an unreadable file gives."""
    monkeypatch.setattr(sched_mod, "config_dir", lambda: tmp_path)
    _write(tmp_path / "projects.yml", UNREADABLE[shape])
    assert sched_mod._load_project_config("alpha", "/tmp/alpha") == {}


def test_load_project_config_really_loads(tmp_path, monkeypatch) -> None:
    """The control."""
    monkeypatch.setattr(sched_mod, "config_dir", lambda: tmp_path)
    _write(tmp_path / "projects.yml", GOOD_PROJECTS)
    assert sched_mod._load_project_config("alpha", "/tmp/alpha")["path"] == "/tmp/alpha"


# ── reader 4: the scheduler's self-heal ─────────────────────────────────────


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_the_self_heal_survives_an_unreadable_projects_file(tmp_path, monkeypatch, shape) -> None:
    """Self-heal is a startup repair: an unreadable file must not take the scheduler down.

    Measured before the fix: a corrupt file was logged and repaired past, and a
    non-UTF-8 file raised out of `_ensure_emrg_project_entry` — the same input class
    answered two ways, one of which is a crash on a path whose whole job is to repair.
    """
    monkeypatch.setattr(sched_mod, "config_dir", lambda: tmp_path)
    _write(tmp_path / "projects.yml", UNREADABLE[shape])
    scheduler = sched_mod.TaskScheduler.__new__(sched_mod.TaskScheduler)
    scheduler._ensure_emrg_project_entry()


# ── reader 5: the startup session-index backfill ────────────────────────────


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_the_index_rebuild_runs_for_either_unreadable_shape(tmp_path, monkeypatch, shape) -> None:
    """The index is rebuilt (degraded) for corrupt YAML and skipped entirely for non-UTF-8.

    `_rebuild_sessions_index` has an inner guard that leaves `project_paths` empty and an
    outer `except Exception` that abandons the rebuild. The third shape reaches the
    *outer* one, so the two shapes differ by "an index built without the project paths"
    versus "no index built at all" — and neither is observable in the log, which is
    written at debug level and discarded.
    """
    calls: list[list[str]] = []

    def fake_rebuild(config_root, project_paths=None):
        calls.append(list(project_paths or []))
        return 0

    monkeypatch.setattr(sidx, "rebuild_sessions_index", fake_rebuild)
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    server = daemon_mod.EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = _write(tmp_path / "projects.yml", UNREADABLE[shape])

    server._rebuild_sessions_index()

    assert calls, (
        f"{shape}: the index rebuild never ran. The projects.yml paths are an input to it, "
        "so a file that cannot be read has to degrade to the same call the corrupt shape makes"
    )


def test_the_index_rebuild_passes_the_registered_paths(tmp_path, monkeypatch) -> None:
    """The control: the rebuild is reached *with* the paths a readable file lists."""
    calls: list[list[str]] = []

    def fake_rebuild(config_root, project_paths=None):
        calls.append(list(project_paths or []))
        return 0

    monkeypatch.setattr(sidx, "rebuild_sessions_index", fake_rebuild)
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    server = daemon_mod.EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = _write(tmp_path / "projects.yml", GOOD_PROJECTS)

    server._rebuild_sessions_index()

    assert calls == [["/tmp/alpha"]]


# ── reader 6: project registration ──────────────────────────────────────────


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_touch_project_records_the_project_for_either_shape(tmp_path, monkeypatch, shape) -> None:
    """Registration must succeed for both: the file is rewritten from what could be read.

    Measured before the fix: corrupt YAML logged and rebuilt the file (losing the
    entries it could not read), and non-UTF-8 raised — so the project was not recorded
    at all, on the path the evolution cycle uses to discover active projects.
    """
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    server = daemon_mod.EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    projects_file = _write(tmp_path / "projects.yml", UNREADABLE[shape])
    server._projects_log = projects_file
    work = tmp_path / "work" / "alpha"
    work.mkdir(parents=True)

    server._touch_project(str(work))

    written = yaml.safe_load(projects_file.read_text(encoding="utf-8"))
    assert isinstance(written, list) and written, f"{shape}: nothing was recorded"
    assert str(work) in [e.get("path") for e in written if isinstance(e, dict)]


def test_touch_project_keeps_the_entries_it_can_read(tmp_path, monkeypatch) -> None:
    """The control: a readable file's other entries survive the registration."""
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    server = daemon_mod.EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    projects_file = _write(
        tmp_path / "projects.yml",
        b"- name: alpha\n  path: /tmp/alpha\n  last_active: '2026-01-01T00:00:00'\n",
    )
    server._projects_log = projects_file
    work = tmp_path / "work" / "beta"
    work.mkdir(parents=True)

    server._touch_project(str(work))

    written = yaml.safe_load(projects_file.read_text(encoding="utf-8"))
    assert {e["path"] for e in written} == {"/tmp/alpha", str(work)}


def test_touch_project_still_survives_an_entry_whose_path_is_not_a_string(
    tmp_path, monkeypatch
) -> None:
    """`TypeError` was caught here for a *different* failure and must stay caught.

    A record whose `path` is a list reaches `os.path.realpath` and raises `TypeError`
    — a wrongly-typed entry, not a read failure, which is why this reader's tuple
    carries a member the others do not. Its tuple was rewritten in this cycle to name
    the shared home, so this is the pin that the rewrite did not drop what it was not
    about: measured by mutating that line back to the home alone (arm A6), which
    leaves this test as the only thing that fails.
    """
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    server = daemon_mod.EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    projects_file = _write(
        tmp_path / "projects.yml", b"- name: broken\n  path:\n    - not\n    - a path\n"
    )
    server._projects_log = projects_file
    work = tmp_path / "work" / "beta"
    work.mkdir(parents=True)

    server._touch_project(str(work))

    written = yaml.safe_load(projects_file.read_text(encoding="utf-8"))
    assert [e["path"] for e in written] == [str(work)]


# ── readers 7 and 8: the two project commands ───────────────────────────────


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_list_projects_replies_for_either_shape(tmp_path, monkeypatch, shape) -> None:
    """The command answers the client for both shapes; it does not drop the connection.

    Measured before the fix: the corrupt shape logged and replied `projects_list` with
    an empty list, and the non-UTF-8 shape raised out of the handler. A handler that
    raises is a handler whose client loses its connection, which the client cannot
    tell apart from a network drop.
    """
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    server = daemon_mod.EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = _write(tmp_path / "projects.yml", UNREADABLE[shape])
    writer = _FakeWriter()

    asyncio.run(server._handle_list_projects(writer))

    frames = writer.decoded()
    assert len(frames) == 1 and frames[0]["type"] == "projects_list"
    assert frames[0]["projects"] == [], f"{shape}: an unreadable file lists no projects"


def test_list_projects_really_lists(tmp_path, monkeypatch) -> None:
    """The control."""
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    server = daemon_mod.EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = _write(tmp_path / "projects.yml", GOOD_PROJECTS)
    writer = _FakeWriter()

    asyncio.run(server._handle_list_projects(writer))

    assert [p["path"] for p in writer.decoded()[0]["projects"]] == ["/tmp/alpha"]


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
def test_remove_project_replies_for_either_shape(tmp_path, monkeypatch, shape) -> None:
    """Removal answers with a refusal frame for both — the shape its siblings already use.

    The corrupt shape produced `project_removed: removed=false, error="failed to read
    projects.yml"`; the non-UTF-8 shape produced a raised handler instead, so the same
    unreadable file had two different protocols.
    """
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    server = daemon_mod.EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = _write(tmp_path / "projects.yml", UNREADABLE[shape])
    writer = _FakeWriter()

    asyncio.run(server._handle_remove_project("alpha", writer))

    frames = writer.decoded()
    assert len(frames) == 1 and frames[0]["type"] == "project_removed"
    assert frames[0]["removed"] is False


def test_remove_project_really_removes(tmp_path, monkeypatch) -> None:
    """The control."""
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    server = daemon_mod.EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = _write(tmp_path / "projects.yml", GOOD_PROJECTS)
    writer = _FakeWriter()

    asyncio.run(server._handle_remove_project("alpha", writer))

    assert writer.decoded()[0] == {"type": "project_removed", "removed": True, "name": "alpha"}


# ── the second half: every reader names the one home ────────────────────────


def test_every_yaml_state_reader_catches_the_shared_home() -> None:
    """A tuple re-spelled at a call site is a tuple free to drift from the home.

    Eight sites spelling `(yaml.YAMLError, OSError)` by hand is how the third shape came
    to be missed in eight places at once. The readers must name
    `emrg.server.atomic.YAML_READ_ERRORS`, and no reader may spell a YAML tuple of its
    own — the same pin `tests/test_a_config_that_cannot_be_read.py` puts on
    `CONFIG_READ_ERRORS`.
    """
    offenders: list[str] = []
    for path in (Path(sched_mod.__file__), Path(daemon_mod.__file__)):
        source = path.read_text(encoding="utf-8")
        # Every YAML guard in these two modules is a tuple naming YAMLError.
        for lineno, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("except "):
                continue
            if "YAMLError" in stripped and "YAML_READ_ERRORS" not in stripped:
                offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert offenders == [], (
        "these readers spell a YAML error tuple of their own instead of naming "
        f"emrg.server.atomic.YAML_READ_ERRORS: {offenders}"
    )
    for path in (Path(sched_mod.__file__), Path(daemon_mod.__file__)):
        source = path.read_text(encoding="utf-8")
        assert "YAML_READ_ERRORS" in source, f"{path.name} never names the shared home"


def test_the_shared_home_is_reachable_from_both_modules() -> None:
    """The import edge exists, so the pin above is about naming rather than availability."""
    assert sched_mod.YAML_READ_ERRORS is YAML_READ_ERRORS
    assert daemon_mod.YAML_READ_ERRORS is YAML_READ_ERRORS
