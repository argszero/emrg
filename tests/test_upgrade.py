"""UpgradeManager tests (rant 2026-08-20T12:33:59 — 自动升级重构).

Covers the host-specified acceptance items:
- [update] new config fields (enabled/delay_minutes; defaults true/1440)
- tick: delay-filter → newest eligible tag ≠ local version → trigger once
- in-flight guard: no re-trigger while an upgrade session runs
- enabled=false / local==target / network failure → no trigger
- daemon integration: tick fires the session callback, in-flight resets
- no residual references to the removed update_check mechanism
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from emrg.config import UpdateConfig
from emrg.server.upgrade import (
    RELEASES_URL,
    SESSION_ID,
    UpgradeManager,
    is_newer,
    parse_version,
)

# ── parse_version / is_newer (migrated from update_check.py) ──────────────


def test_parse_version_basic():
    assert parse_version("v0.2.18") == (0, 2, 18)
    assert parse_version("0.2.18") == (0, 2, 18)
    assert parse_version("v0.2.57") == (0, 2, 57)


def test_parse_version_prerelease_suffix_stops_parsing():
    # prerelease/build suffixes must never parse as a full version
    assert parse_version("v0.2.18-beta1") == (0, 2)
    assert parse_version("v0.2.18-rc.2") == (0, 2)
    assert parse_version("") == ()
    assert parse_version("garbage") == ()


def test_is_newer():
    assert is_newer((0, 2, 57), (0, 2, 56))
    assert not is_newer((0, 2, 56), (0, 2, 57))
    assert not is_newer((0, 2, 18), (0, 2, 18))
    assert not is_newer((), (0, 2, 57))  # unparseable never newer


# ── delay filter: takes the NEWEST tag within the eligibility window ──────


def _release(tag: str, age_seconds: int) -> dict:
    """A release dict published `age_seconds` before now."""
    published = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_seconds)
    )
    return {"tag_name": tag, "published_at": published}


async def _tick_with_releases(monkeypatch, releases, delay_minutes=1440, enabled=True):
    """Run one tick with a stubbed releases API response; return trigger calls."""
    calls = []

    async def fake_run_session(session_id, cwd, prompt):
        calls.append({"session_id": session_id, "cwd": cwd, "prompt": prompt})

    mgr = UpgradeManager(
        UpdateConfig(enabled=enabled, delay_minutes=delay_minutes), fake_run_session
    )

    async def fake_get(url):
        class _Resp:
            status_code = 200

            def json(self):
                return releases

        return _Resp()

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return await fake_get(url)

    import emrg.server.upgrade as up

    monkeypatch.setattr(up.httpx, "AsyncClient", _FakeClient)
    await mgr.tick()
    return calls


def test_tick_delay_filter_takes_newest_eligible(monkeypatch, tmp_path):
    # Two eligible (older than 1 day) + one too-recent (must be excluded)
    releases = [
        _release("v0.2.56", 60 * 60 * 24 * 5),
        _release("v0.2.57", 60 * 60 * 24 * 2),
        _release("v0.2.99", 60 * 5),  # too recent — delay window not elapsed
    ]
    monkeypatch.setattr(
        "emrg.server.upgrade.VERSION_FILE", tmp_path / "version.txt"
    )
    (tmp_path / "version.txt").write_text("0.2.55\n", encoding="utf-8")
    calls = asyncio.run(_tick_with_releases(monkeypatch, releases, delay_minutes=1440))
    assert len(calls) == 1
    assert calls[0]["session_id"] == SESSION_ID
    assert "v0.2.57" in calls[0]["prompt"]  # newest ELIGIBLE tag (0.2.99 excluded)


def test_tick_no_trigger_when_local_matches_target(monkeypatch, tmp_path):
    releases = [_release("v0.2.57", 60 * 60 * 24 * 2)]
    monkeypatch.setattr(
        "emrg.server.upgrade.VERSION_FILE", tmp_path / "version.txt"
    )
    (tmp_path / "version.txt").write_text("0.2.57\n", encoding="utf-8")
    calls = asyncio.run(_tick_with_releases(monkeypatch, releases))
    assert calls == [], "local == target → no trigger"


def test_tick_disabled_by_config(monkeypatch, tmp_path):
    releases = [_release("v0.2.57", 60 * 60 * 24 * 2)]
    monkeypatch.setattr(
        "emrg.server.upgrade.VERSION_FILE", tmp_path / "version.txt"
    )
    (tmp_path / "version.txt").write_text("0.2.55\n", encoding="utf-8")
    calls = asyncio.run(_tick_with_releases(monkeypatch, releases, enabled=False))
    assert calls == [], "enabled=false → no trigger at all"


def test_tick_network_failure_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "emrg.server.upgrade.VERSION_FILE", tmp_path / "version.txt"
    )
    (tmp_path / "version.txt").write_text("0.2.55\n", encoding="utf-8")

    class _FailClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise Exception("network down")

    import emrg.server.upgrade as up

    monkeypatch.setattr(up.httpx, "AsyncClient", _FailClient)
    mgr = UpgradeManager(UpdateConfig(), lambda **kw: asyncio.sleep(0))
    asyncio.run(mgr.tick())  # must not raise


def test_inflight_guard_skips_retrigger(monkeypatch, tmp_path):
    """While an upgrade session is running, tick() must not re-trigger."""
    releases = [_release("v0.2.57", 60 * 60 * 24 * 2)]
    monkeypatch.setattr(
        "emrg.server.upgrade.VERSION_FILE", tmp_path / "version.txt"
    )
    (tmp_path / "version.txt").write_text("0.2.55\n", encoding="utf-8")

    async def scenario():
        calls = []
        session_done = asyncio.Event()

        async def slow_session(session_id, cwd, prompt):
            calls.append(session_id)
            await session_done.wait()

        mgr = UpgradeManager(UpdateConfig(), slow_session)

        class _Resp:
            status_code = 200

            def json(self):
                return releases

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return _Resp()

        import emrg.server.upgrade as up

        monkeypatch.setattr(up.httpx, "AsyncClient", _FakeClient)

        # First tick triggers the session (blocked on the event).
        t1 = asyncio.create_task(mgr.tick())
        await asyncio.sleep(0.05)
        assert mgr._inflight is True, "session start → in-flight"
        assert len(calls) == 1

        # Second tick while in-flight → skipped.
        await mgr.tick()
        assert len(calls) == 1, "in-flight → no re-trigger"

        # Session finishes → in-flight resets.
        session_done.set()
        await t1
        assert mgr._inflight is False, "session end → in-flight reset"

    asyncio.run(scenario())


# ── config: new [update] fields ───────────────────────────────────────────


def test_update_config_defaults():
    cfg = UpdateConfig()
    assert cfg.enabled is True
    assert cfg.delay_minutes == 1440
    assert not hasattr(cfg, "check"), "old [update] check field must be gone"
    assert not hasattr(cfg, "ttl_hours"), "old [update] ttl_hours field must be gone"
    assert not hasattr(cfg, "auto_download"), "old auto_download field must be gone"


def test_load_update_config_new_fields(tmp_path, monkeypatch):
    from emrg import config as cfg_mod

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[update]\nenabled = false\ndelay_minutes = 1\n", encoding="utf-8"
    )
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)
    cfg = cfg_mod.load_update_config()
    assert cfg.enabled is False
    assert cfg.delay_minutes == 1


def test_load_config_full_new_fields(tmp_path, monkeypatch):
    from emrg import config as cfg_mod

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[llm]\nbase_url = 'x'\napi_key = 'k'\n"
        "[update]\nenabled = false\ndelay_minutes = 5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)
    cfg = cfg_mod.load_config()
    assert cfg.update.enabled is False
    assert cfg.update.delay_minutes == 5


# ── daemon integration: tick → run_session_cb (public runner) ─────────────


def test_daemon_upgrade_session_runner(monkeypatch, tmp_path):
    """The daemon's _run_upgrade_session executes the prompt as an agent
    session: session created, busy lock set and released, tool loop invoked."""
    from tests.test_daemon import _make_server  # reuse the daemon test helper

    server = _make_server()
    monkeypatch.setattr(server, "_max_tool_rounds", 3)
    # ⛔ Red line (host 2026-08-21T10:35:57): tests must never create/write the
    # real emrg-upgrade session — isolate the session factory (the conftest
    # autouse guard raises on the real one for SESSION_ID).
    monkeypatch.setattr(server, "_get_or_create_session", lambda sid, cwd: object())
    ran = []

    async def fake_loop(req, ws, session, cancel_event, allow_tools=True):
        ran.append((req.session_id, req.cwd, req.prompt, allow_tools))
        # mirror the real _run_tool_loop_locked finally: release the busy lock
        server._session_busy[req.session_id] = False

    monkeypatch.setattr(server, "_run_tool_loop_locked", fake_loop)

    asyncio.run(server._run_upgrade_session("emrg-upgrade", str(tmp_path), "PROMPT"))

    assert len(ran) == 1
    assert ran[0][0] == "emrg-upgrade"
    assert ran[0][1] == str(tmp_path)
    assert ran[0][2] == "PROMPT"
    assert ran[0][3] is True, "upgrade sessions run with tools"
    assert server._session_busy.get("emrg-upgrade") is False, "busy lock released"


def test_upgrade_chain_hermeticity_guards():
    """⛔ Red line (host 2026-08-21T10:35:57): the conftest autouse guard must
    block the real auto-upgrade chain by default — no real GitHub releases
    request, no real install/version.txt access. A long-running pytest session
    really executed the upgrade chain every 5 minutes (PID 72994, 21h).
    """
    from pathlib import Path

    import emrg.server.upgrade as up

    # 1. Network: the upgrade module's httpx.AsyncClient raises by default.
    with pytest.raises(AssertionError, match="red-line"):
        up.httpx.AsyncClient()

    # 2. Version file: not the real ~/.emrg/install/version.txt.
    assert up.VERSION_FILE != Path.home() / ".emrg" / "install" / "version.txt"


# ── no residual references to the removed mechanism ───────────────────────


def test_no_residual_update_check_references():
    """The old update_check mechanism must be fully removed (host §7/§8):
    emrg/update_check.py gone; no references to the module, its state file,
    or the removed [update] fields outside upgrade.py's own docstring."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).parent.parent
    files = [
        "emrg/server/daemon.py",
        "emrg/server/upgrade.py",
        "emrg/config.py",
        "emrg/client/app.py",
        "emrg/gui/main.js",
        "emrg/gui/preload.js",
    ]
    assert not (repo / "emrg/update_check.py").exists(), "update_check.py must be deleted"
    assert not (repo / "tests/test_update_check.py").exists(), "test_update_check.py must be deleted"
    for rel in files:
        text = (repo / rel).read_text(encoding="utf-8")
        # allow the upgrade.py docstring itself + config.py removal note to
        # mention the old names; everything else must be clean
        if rel == "emrg/server/upgrade.py":
            continue
        if rel == "emrg/config.py" and "removed" in text:
            continue
        # daemon.py legitimately keeps the SKILLS updater's run_update_check_once
        # (emrg.skills.installer — a separate skills mechanism, not the removed
        # auto-upgrade module); strip those lines before asserting.
        if rel == "emrg/server/daemon.py":
            text = "\n".join(
                ln for ln in text.split("\n") if "run_update_check_once" not in ln
            )
        assert "update_check" not in text, f"{rel} still references update_check"
        assert "ttl_hours" not in text, f"{rel} still references ttl_hours"
        assert "auto_download" not in text, f"{rel} still references auto_download"
        assert ".last_update_check" not in text, f"{rel} still references the state file"
