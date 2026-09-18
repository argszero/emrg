"""`config.toml` hot reload (rant 2026-09-17T16:52:57) — measured, not asserted.

The host's complaint: editing `~/.emrg/config.toml` had no effect on the running
daemon; the only path was a client SIGKILLing it, which drops every connection
and rebuilds every scheduled handler. These tests pin the replacement — a
daemon-side fingerprint + in-place apply, with `model` riding the `/model` path.

Two rules these tests obey, both from the project's own safety lines:

* **They never read or write the host's real `~/.emrg/config.toml`.** Every
  case builds its own file under `tmp_path` and hands it to
  `ConfigReloader(..., path=tmp)`. `load_config`'s new optional `path` is what
  makes that possible; a test that resolved `config_path()` would be editing
  the host's runtime state. The *implicit* resolutions are pinned by the
  autouse fixture below rather than by each test remembering to pass `path=`.
* **They never start, stop or restart a daemon.** `EmrgServer` is constructed
  in-process (as `tests/test_daemon.py::_make_server` already does) and one
  revision is driven through `_reload_config_once()` by hand — no `_run()`, no
  loop, no sleep, no socket.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from pathlib import Path

import pytest

from emrg import config as cfg_mod
from emrg.config import LlmConfig, load_config
from emrg.server import config_reload as cr
from emrg.server.config_reload import ConfigReloader, describe
from emrg.server.daemon import EmrgServer

BASE = """[llm]
base_url = "http://localhost:1/v1"
api_key = "sk-test"
model = "model-a"
max_tokens = 100
temperature = 0.5
context_window = 1000
"""


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _live_from(cfg_path: Path) -> LlmConfig:
    """The live config as the daemon would hold it: loaded from that file.

    Building it by hand instead is how a fixture drifts from its file — the
    first draft of these tests left `temperature` at its default while the file
    said 0.5, so every reload reported a `temperature` change that the test was
    not expecting. The daemon's own semantics are "this file was read at
    startup", so the fixture reads it too.
    """
    return load_config(cfg_path).llm


def _server(tmp_path: Path, body: str = BASE) -> tuple[EmrgServer, Path]:
    """A server whose live config and reload path both point at `tmp_path`."""
    cfg_path = tmp_path / "config.toml"
    _write(cfg_path, body)
    live = _live_from(cfg_path)
    server = EmrgServer(live)
    # The `[update]` half (issue #1356), isolated the same way: the object the
    # daemon hands its UpgradeManager, loaded from this tmp file instead of the
    # host's real config.toml. No test here calls `tick()`.
    server._update_config = load_config(cfg_path).update
    server._config_reloader = ConfigReloader(
        live, path=cfg_path, live_update=server._update_config
    )
    return server, cfg_path


@pytest.fixture(autouse=True)
def _the_implicit_config_path_is_never_the_hosts(tmp_path, monkeypatch):
    """`config_path()` must resolve inside `tmp_path` for every test in this file.

    The module docstring has claimed this from the start, and one caller broke
    it invisibly: `EmrgServer.__init__` calls `load_update_config()`, which
    resolves `config_path()` — so merely *constructing* a server read the host's
    real `~/.emrg/config.toml`, and the result was discarded a line later by
    `_server`. Nothing was mis-asserted, which is why it survived: a read of host
    state that no expectation depends on stays invisible until a later edit makes
    an expectation depend on it (the same shape as the `tests/test_ws_e2e.py`
    patch that stopped reaching the daemon, 2026-09-18).

    Both modules are re-pointed because neither name is the other's alias:
    `emrg.config.config_path` is what `load_update_config` calls (it is defined
    in that module), while `config_reload.py` imported the name by value — a
    fixture that patched only one would leave the other reading the host.

    Pinned to the same file name the tests build themselves, so an implicit
    resolution and an explicit `path=` argument agree on the file.
    """
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)
    monkeypatch.setattr(cr, "config_path", lambda: cfg_path)


# ── the decision half ────────────────────────────────────────────────


def test_the_type_table_covers_every_reloadable_field():
    """The table is a second source of truth — this is the step that keeps it honest.

    A field added to `LlmConfig` and silently missing from `_TYPES` would be
    reloaded with no type check at all (`validate` skips it), which is the
    "unfinished enumeration" failure mode: the list quietly stops covering the
    thing it exists to cover.
    """
    assert set(cr._TYPES) == set(cr.reloadable_fields())
    assert "model" not in cr.reloadable_fields()


def test_an_unchanged_file_is_not_an_outcome(tmp_path):
    reloader = ConfigReloader(LlmConfig(), path=_no_file(tmp_path))
    # Missing file: nothing to do, and no crash.
    assert reloader.poll() is None


def _no_file(tmp_path: Path) -> Path:
    return tmp_path / "absent.toml"


def test_a_valid_edit_is_applied_in_place(tmp_path):
    cfg_path = tmp_path / "config.toml"
    _write(cfg_path, BASE)
    live = _live_from(cfg_path)
    reloader = ConfigReloader(live, path=cfg_path)

    # Same bytes → no revision.
    assert reloader.poll() is None

    _write(cfg_path, BASE.replace("max_tokens = 100", "max_tokens = 4096")
                     .replace("temperature = 0.5", "temperature = 1.0"))
    outcome = reloader.poll()
    assert outcome is not None and outcome.error is None
    assert sorted(outcome.applied) == ["max_tokens", "temperature"]
    assert live.max_tokens == 4096 and live.temperature == 1.0
    # Fields the file did not move stay where they were.
    assert live.context_window == 1000 and live.api_key == "sk-test"
    assert describe(outcome) == "changed=max_tokens,temperature"

    # Polling again with no further write is not a second revision.
    assert reloader.poll() is None


def test_a_broken_revision_keeps_the_previous_config_and_is_retried(tmp_path):
    cfg_path = tmp_path / "config.toml"
    _write(cfg_path, BASE)
    live = _live_from(cfg_path)
    reloader = ConfigReloader(live, path=cfg_path)

    # A half-written file (the case the rant names): unparseable TOML.
    _write(cfg_path, "[llm\nmodel = ")
    outcome = reloader.poll()
    assert outcome is not None
    assert outcome.error is not None and "TOMLDecodeError" in outcome.error
    assert outcome.applied == [] and outcome.model is None
    assert live.max_tokens == 100 and live.model == "model-a"
    assert describe(outcome).startswith("rejected (previous config kept)")

    # The same broken revision is not re-reported on every tick…
    assert reloader.poll() is None
    # …and the host's next write is a new revision, applied normally.
    _write(cfg_path, BASE.replace("max_tokens = 100", "max_tokens = 200"))
    retry = reloader.poll()
    assert retry is not None and retry.error is None
    assert retry.applied == ["max_tokens"] and live.max_tokens == 200


def test_a_wrongly_typed_field_rejects_the_whole_revision(tmp_path):
    """Atomicity: not one field of a bad revision may land.

    The tempting implementation assigns as it walks the fields, so the good
    keys of a bad revision take effect and the daemon runs a configuration
    that is half the old file and half the new one — which reads exactly like
    a successful reload. `max_tokens` here is valid in the same revision and
    must still not move.
    """
    cfg_path = tmp_path / "config.toml"
    _write(cfg_path, BASE)
    live = _live_from(cfg_path)
    reloader = ConfigReloader(live, path=cfg_path)

    _write(cfg_path, BASE.replace("model = \"model-a\"", "model = \"model-a\"\nvision = \"yes\"")
                     .replace("max_tokens = 100", "max_tokens = 4096"))
    outcome = reloader.poll()
    assert outcome is not None
    assert outcome.error is not None and "vision" in outcome.error
    assert live.max_tokens == 100, "a good key of a rejected revision must not land"
    assert live.vision is False

    # `bool` is a subclass of `int`: without the strict check this passes.
    _write(cfg_path, BASE.replace("max_tokens = 100", "max_tokens = true"))
    strict = reloader.poll()
    assert strict is not None and strict.error is not None
    assert live.max_tokens == 100


def test_a_model_change_is_reported_not_assigned(tmp_path):
    """`model` is the one field that must not be a plain assignment.

    A real API-model change invalidates the usage anchors and re-resolves
    `context_window` — that is the `/model` path (Dev.to 3dh3g). The reloader
    therefore reports the requested model and leaves `live.model` alone.
    """
    cfg_path = tmp_path / "config.toml"
    _write(cfg_path, BASE)
    live = _live_from(cfg_path)
    reloader = ConfigReloader(live, path=cfg_path)

    _write(cfg_path, BASE.replace('model = "model-a"', 'model = "model-b"'))
    outcome = reloader.poll()
    assert outcome is not None
    assert outcome.model == "model-b"
    assert outcome.applied == []
    assert live.model == "model-a", "the reloader must not assign the model itself"


def test_a_same_size_edit_inside_the_timestamp_granule_is_still_a_revision(tmp_path):
    """The change detector must not depend on the filesystem's timestamp resolution.

    Measured on the `windows-2025` leg (2026-09-17, run for head `ef38270e`):
    `test_a_model_change_is_reported_not_assigned` failed there with
    `assert None is not None` - two writes milliseconds apart share a timestamp, and
    `model-a` → `model-b` keeps the size, so `(st_mtime_ns, st_size)` was *identical*
    and the edit was never applied. That is the rant's own complaint - "I edited it and
    nothing happened" - surviving on one platform, silently, with no log line.

    The Windows shape is replayed here by restoring the first write's mtime with
    `os.utime`, so the premise (equal `st_mtime_ns`, equal size, different bytes) holds
    as a *file system fact* rather than a patched `stat`: a detector that consults
    only `(mtime_ns, size)` reports "nothing changed" for this file on any platform.
    """
    cfg_path = tmp_path / "config.toml"
    _write(cfg_path, BASE)
    live = _live_from(cfg_path)
    reloader = ConfigReloader(live, path=cfg_path)
    before = cfg_path.stat()

    _write(cfg_path, BASE.replace('model = "model-a"', 'model = "model-b"'))
    os.utime(cfg_path, ns=(before.st_atime_ns, before.st_mtime_ns))

    after = cfg_path.stat()
    assert after.st_size == before.st_size, "the edit must keep the size for this shape"
    assert after.st_mtime_ns == before.st_mtime_ns, "and the timestamp"

    outcome = reloader.poll()
    assert outcome is not None, "the same-size edit inside the tick was not detected"
    assert outcome.model == "model-b"


# ── the daemon's act half ────────────────────────────────────────────


def test_the_daemon_applies_reloads_and_mirrors_max_tool_rounds(tmp_path):
    server, cfg_path = _server(tmp_path)

    _write(cfg_path, BASE.replace("max_tokens = 100", "max_tokens = 777")
                     .replace('model = "model-a"', 'model = "model-a"\nmax_tool_rounds = 42'))
    outcome = asyncio.run(server._reload_config_once())
    assert outcome is not None and outcome.error is None
    assert "max_tool_rounds" in outcome.applied
    assert server.llm.config.max_tool_rounds == 42
    # The daemon snapshots this at construction; a reload that forgot it would
    # leave the tool loop running on the old limit while the config says 42.
    assert server._max_tool_rounds == 42
    assert server.llm.config.max_tokens == 777
    # Nothing to do when the file has not moved again.
    assert asyncio.run(server._reload_config_once()) is None


def test_the_loop_applies_a_revision_and_survives_a_broken_one(tmp_path, monkeypatch):
    """The loop itself: sleeps, applies, and cannot be killed by a bad file.

    Every other test here drives `_reload_config_once()` by hand, which would
    leave the actual timer — the part that makes the feature exist — untested:
    a loop that never called it, or that died on the first parse error, would
    pass all of them. The interval is patched to zero so the test needs one
    event-loop yield instead of two real seconds.
    """
    from emrg.server import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "POLL_INTERVAL_SECONDS", 0.0)
    server, cfg_path = _server(tmp_path)

    async def drive() -> tuple[bool, int]:
        server._running = True
        task = asyncio.create_task(server._config_reload_loop())
        await asyncio.sleep(0)
        _write(cfg_path, BASE.replace("max_tokens = 100", "max_tokens = 1234"))
        applied = False
        for _ in range(200):
            await asyncio.sleep(0)
            if server.llm.config.max_tokens == 1234:
                applied = True
                break
        # A revision the loop cannot use must not end the loop.
        _write(cfg_path, "not toml [")
        for _ in range(100):
            await asyncio.sleep(0)
        alive = not task.done()
        kept = server.llm.config.max_tokens
        server._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alive and applied, kept

    survived, kept = asyncio.run(drive())
    assert survived, "the loop did not apply the revision, or died on a bad one"
    assert kept == 1234, "a broken revision must leave the previous config in force"


def test_the_daemon_routes_a_model_change_through_the_switch_path(tmp_path):
    server, cfg_path = _server(tmp_path)
    # Two anchors exist: a plain assignment would leave them in place, and the
    # next round would mix the old provider's real tokens with the new
    # estimate delta (the #946 failure mode the switch path exists to stop).
    server._usage_anchors["s1"] = (1000, 900)

    # The entry lives in the file, not in the live config: the revision is
    # applied field by field (including `models`, the list /model picks from)
    # *before* the switch runs, so the switch must resolve the file's entry —
    # an assignment made before the write would be overwritten by the reload.
    _write(cfg_path, BASE.replace('model = "model-a"', 'model = "model-b"') + """
[[llm.models]]
name = "model-b"
model = "api-b"
context_window = 4242
vision = true
""")
    outcome = asyncio.run(server._reload_config_once())
    assert outcome is not None and outcome.model == "model-b"
    assert server.llm.config.model == "api-b", "the entry's API model, not the display name"
    assert server.llm.config.context_window == 4242
    assert server.llm.config.vision is True
    assert server._usage_anchors == {}, "anchors must be invalidated by a model change"
    assert "s1" in server._usage_anchor_dropped_by_switch


def test_a_rejected_revision_never_raises_out_of_the_tick(tmp_path):
    """A bad config must not be able to stop the daemon it is reloading into."""
    server, cfg_path = _server(tmp_path)
    _write(cfg_path, "this is not toml [")
    outcome = asyncio.run(server._reload_config_once())
    assert outcome is not None and outcome.error is not None
    assert server.llm.config.max_tokens == 100


# ── a revision the clients display has to reach them (issue #1374) ────


def _recorder(server) -> list[dict]:
    """Replace the daemon's broadcast with a recording sink, and return the log.

    The sink is the seam the claim is about — "a frame carrying the effective value
    is sent to the connected clients" — so recording there is measuring the daemon
    rather than asserting that a function exists. `test_ws_e2e`'s
    `test_a_config_reload_broadcasts_the_effective_vision` covers the other half
    (that the frame really crosses a socket).
    """
    sent: list[dict] = []

    async def record(data, exclude=None):
        sent.append(data)

    server._broadcast_all = record
    return sent


def test_a_vision_revision_reaches_the_clients_that_display_it(tmp_path):
    """The measured defect: the value moved in the daemon, and only the log knew.

    Before this arm, `vision = true` edited into the file left every connected
    client showing the previous answer until a reconnect or a `/model`, because the
    only frames that carry the effective vision are a `pong` and a `model_set`.
    """
    server, cfg_path = _server(tmp_path)
    assert server.llm.config.vision is False
    sent = _recorder(server)

    _write(cfg_path, BASE + "vision = true\n")
    outcome = asyncio.run(server._reload_config_once())

    assert outcome is not None and "vision" in outcome.applied, (
        "the reload itself must still apply — this arm reports it, it does not replace it"
    )
    assert server.llm.config.vision is True
    assert len(sent) == 1, f"expected exactly one frame, got {sent}"
    frame = sent[0]
    assert frame["type"] == "config_applied"
    assert frame["vision"] is True, "the frame carries the live value, not the file's key"
    assert frame["context_window"] == 1000
    assert frame["applied"] == outcome.applied
    # A reload resolves nothing, so it must not name a resolution: `vision_source`'s
    # two legal values both describe a `/model` switch (rant 2026-09-17T16:53:02),
    # and a third spelling invented here would be a value no resolution can produce.
    assert "vision_source" not in frame


def test_the_other_direction_moves_the_clients_too(tmp_path):
    """`true → false` is the half a one-directional test misses.

    It is the dangerous direction: a client that keeps showing `images: yes` while
    the daemon has stopped accepting images is the failure the badge exists to
    prevent, so the frame must move here as well.
    """
    server, cfg_path = _server(tmp_path, BASE + "vision = true\n")
    assert server.llm.config.vision is True
    sent = _recorder(server)

    _write(cfg_path, BASE + "vision = false\n")
    outcome = asyncio.run(server._reload_config_once())

    assert outcome is not None and "vision" in outcome.applied
    assert server.llm.config.vision is False
    assert [f["vision"] for f in sent] == [False]


def test_a_revision_no_client_displays_is_not_broadcast(tmp_path):
    """The control that makes the two tests above discriminating.

    Without it, "broadcast on every applied revision" passes both — and puts a
    frame on the wire for every keystroke the host makes in a file that has nothing
    to do with what a client shows. `max_tokens` is daemon-local: nothing outside
    the daemon reads it.
    """
    server, cfg_path = _server(tmp_path)
    sent = _recorder(server)

    _write(cfg_path, BASE.replace("max_tokens = 100", "max_tokens = 1234"))
    outcome = asyncio.run(server._reload_config_once())

    assert outcome is not None and outcome.applied == ["max_tokens"]
    assert server.llm.config.max_tokens == 1234
    assert sent == [], "a value no client displays must not produce a frame"


def test_a_revision_that_moves_the_model_and_the_vision_is_reported_once(tmp_path):
    """One revision, one frame — the two arms are exclusive, not cumulative.

    A model revision already travels `_apply_model_switch`, whose frame carries the
    resolved `vision`. If the new arm were a second `if` rather than an `elif`, this
    revision would send two frames saying the same thing and the client would print
    two lines for one edit.
    """
    server, cfg_path = _server(tmp_path)
    sent = _recorder(server)

    _write(
        cfg_path,
        BASE.replace('model = "model-a"', 'model = "model-b"') + "vision = true\n",
    )
    outcome = asyncio.run(server._reload_config_once())

    assert outcome is not None and outcome.model == "model-b"
    assert "vision" in outcome.applied, "the fixture must exercise both arms at once"
    assert [f["type"] for f in sent] == ["model_set"]
    assert sent[0]["vision"] is True, "the switch frame already reports the effective value"


# ── the `[update]` section (issue #1356) ─────────────────────────────


def test_the_update_type_table_covers_every_reloadable_field():
    """The same second-source-of-truth rule as `_TYPES`, one section over.

    A field added to `UpdateConfig` and absent from `_UPDATE_TYPES` would be
    applied with no check at all (`validate` skips it), and the section is the
    one whose whole defect was being silently out of scope.
    """
    assert set(cr._UPDATE_TYPES) == set(cr.update_reloadable_fields())
    assert cr.update_reloadable_fields() == ("enabled", "delay_minutes")


def test_an_update_section_edit_is_applied_in_place(tmp_path):
    """The measured defect: `load_update_config()` ran once, in the tick loop.

    So `enabled = false` in the file did nothing to a running daemon — and after
    the client's mtime-restart was removed (PR #1355) nothing at all covered it.
    """
    server, cfg_path = _server(tmp_path)
    live_update = server._update_config
    assert live_update.enabled is True and live_update.delay_minutes == 1440

    _write(cfg_path, BASE + "\n[update]\nenabled = false\ndelay_minutes = 60\n")
    outcome = asyncio.run(server._reload_config_once())
    assert outcome is not None and outcome.error is None
    assert outcome.update_applied == ["enabled", "delay_minutes"]
    assert outcome.applied == [], "nothing under [llm] moved"
    # In place, on the object itself — which is what makes the manager's next
    # tick read the new values without being rebuilt.
    assert live_update.enabled is False and live_update.delay_minutes == 60
    assert describe(outcome) == "[update] changed=enabled,delay_minutes"
    # A second poll with no further write is not a second revision.
    assert asyncio.run(server._reload_config_once()) is None


def test_a_wrongly_typed_update_field_rejects_the_whole_revision(tmp_path):
    """Atomicity is per *revision*, not per section.

    If the `[llm]` half were applied while `[update]` was rejected, the daemon
    would run a configuration that is half the old file and half the new one —
    which reads exactly like a successful reload, the failure mode `validate`
    exists to prevent.
    """
    server, cfg_path = _server(tmp_path)
    _write(
        cfg_path,
        BASE.replace("max_tokens = 100", "max_tokens = 4096")
        + '\n[update]\ndelay_minutes = "soon"\n',
    )
    outcome = asyncio.run(server._reload_config_once())
    assert outcome is not None and outcome.error is not None
    assert outcome.error.startswith("[update] delay_minutes is str")
    assert server.llm.config.max_tokens == 100, "the [llm] half leaked through"
    assert server._update_config.delay_minutes == 1440
    # The rejected revision is retried as soon as the file moves again.
    _write(cfg_path, BASE.replace("max_tokens = 100", "max_tokens = 4096"))
    again = asyncio.run(server._reload_config_once())
    assert again is not None and again.applied == ["max_tokens"]


def test_the_upgrade_tick_loop_hands_the_manager_the_shared_object(tmp_path, monkeypatch):
    """The seam between the two halves, **driven** rather than asserted.

    The manager captures its `UpdateConfig` at construction and reads it once per
    5-minute tick, so the reload can only reach it if the loop hands it *this*
    object. Asserting that `UpgradeManager` keeps the object it is given would
    test the constructor and not the daemon — it passes even while the loop
    builds the manager from its own `load_update_config()` copy, which is exactly
    the state this change removes (measured: that arm left this test green).

    The loop therefore runs against a **recording stub**, one tick deep. The real
    manager is never constructed, `tick()` here is a local no-op, and no releases
    API, `version.txt` or `emrg-upgrade` session is reachable from this test.
    """
    from emrg.server import upgrade as upgrade_mod

    seen: dict[str, object] = {}

    class RecordingManager:
        def __init__(self, config, run_session_cb):
            seen["config"] = config

        async def tick(self) -> None:
            seen["ticked"] = True

    monkeypatch.setattr(upgrade_mod, "UpgradeManager", RecordingManager)
    monkeypatch.setattr(upgrade_mod, "TICK_INTERVAL", 0)

    server, cfg_path = _server(tmp_path)

    async def drive() -> None:
        task = asyncio.create_task(server._upgrade_tick_loop())
        for _ in range(50):
            await asyncio.sleep(0)
            if seen.get("ticked"):
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert seen["ticked"], "the loop did not reach a tick"
    assert seen["config"] is server._update_config

    # And the object the loop is ticking on is the one a reload moves.
    _write(cfg_path, BASE + "\n[update]\nenabled = false\n")
    outcome = asyncio.run(server._reload_config_once())
    assert outcome is not None and outcome.update_applied == ["enabled"]
    assert seen["config"].enabled is False, "what tick() reads first"


def test_a_reloader_without_the_update_object_does_not_own_that_section(tmp_path):
    """A section is owned by the object it is given, and no object means no claim.

    The alternative — validating `[update]` anyway — would let a mistyped value in
    a section this reloader cannot apply reject the `[llm]` half as well, which is
    over-reach in the other direction.
    """
    cfg_path = tmp_path / "config.toml"
    _write(cfg_path, BASE + '\n[update]\ndelay_minutes = "soon"\n')
    live = _live_from(cfg_path)
    reloader = ConfigReloader(live, path=cfg_path)

    _write(
        cfg_path,
        BASE.replace("max_tokens = 100", "max_tokens = 4096")
        + '\n[update]\ndelay_minutes = "soon"\n',
    )
    outcome = reloader.poll()
    assert outcome is not None and outcome.error is None
    assert outcome.applied == ["max_tokens"] and outcome.update_applied == []
    assert live.max_tokens == 4096


def test_the_daemons_own_reloader_owns_the_update_section(tmp_path):
    """The wiring in `__init__`, driven through the daemon's **own** reloader.

    Every other test here drives a `ConfigReloader` this file built — `_server()`
    *replaces* the daemon's. So the construction that actually ships,
    `ConfigReloader(llm_config, live_update=self._update_config)`, was never
    exercised: dropping `live_update=` leaves this whole file green (measured
    while reviewing this PR), i.e. the `[update]` section would be hot-reloadable
    in appearance only — the silent state issue #1356 exists to end, reproduced
    one level up. `_server()` cannot pin it, because replacing that object is the
    thing it does; the daemon's own reloader is only reachable by letting the
    fixture point the implicit `config_path()` at this file and constructing the
    server with no replacement.

    The other seam has its own driven test
    (`test_the_upgrade_tick_loop_hands_the_manager_the_shared_object`): that the
    manager *reads* this object, and that this object *is* the one the reloader
    owns, are two claims — one test cannot cover both.
    """
    cfg_path = tmp_path / "config.toml"
    _write(cfg_path, BASE + "\n[update]\nenabled = true\ndelay_minutes = 5\n")
    server = EmrgServer(_live_from(cfg_path))

    assert server._config_reloader.live_update is server._update_config, (
        "the daemon must hand its own `[update]` object to the reloader it "
        "builds — a reloader without it neither checks nor applies that section"
    )
    assert server._update_config.delay_minutes == 5, (
        "the daemon's baseline must come from the file, not from defaults or "
        "the host's config — the fixture resolves `config_path()` to this file"
    )

    _write(cfg_path, BASE + "\n[update]\nenabled = false\ndelay_minutes = 30\n")
    outcome = asyncio.run(server._reload_config_once())
    assert outcome is not None and outcome.error is None
    assert outcome.update_applied == ["enabled", "delay_minutes"]
    assert server._update_config.enabled is False
    assert server._update_config.delay_minutes == 30


def test_every_live_field_is_actually_reloadable(tmp_path):
    """The applied set is derived from the dataclass, so it cannot go stale.

    Pinned by name rather than by count: this asserts the *exclusion* of
    `model` and that a newly added field is reloadable by default, which is
    the property that keeps the feature from silently covering less over time.
    """
    a = LlmConfig(**{f.name: f.default for f in dataclasses.fields(LlmConfig)})
    fields = set(cr.reloadable_fields()) | {"model"}
    assert fields == {f.name for f in dataclasses.fields(LlmConfig)}
    assert cr.reloadable_fields() == tuple(
        f.name for f in dataclasses.fields(LlmConfig) if f.name != "model"
    )
    assert a.model == LlmConfig().model
