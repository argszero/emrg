"""`config.toml` hot reload (rant 2026-09-17T16:52:57) — measured, not asserted.

The host's complaint: editing `~/.emrg/config.toml` had no effect on the running
daemon; the only path was a client SIGKILLing it, which drops every connection
and rebuilds every scheduled handler. These tests pin the replacement — a
daemon-side stat + in-place apply, with `model` riding the `/model` path.

Two rules these tests obey, both from the project's own safety lines:

* **They never read or write the host's real `~/.emrg/config.toml`.** Every
  case builds its own file under `tmp_path` and hands it to
  `ConfigReloader(..., path=tmp)`. `load_config`'s new optional `path` is what
  makes that possible; a test that resolved `config_path()` would be editing
  the host's runtime state.
* **They never start, stop or restart a daemon.** `EmrgServer` is constructed
  in-process (as `tests/test_daemon.py::_make_server` already does) and one
  revision is driven through `_reload_config_once()` by hand — no `_run()`, no
  loop, no sleep, no socket.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

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
    server._config_reloader = ConfigReloader(live, path=cfg_path)
    return server, cfg_path


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
