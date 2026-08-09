"""Auto update-check prompt tests (rant 2026-08-10T07:12:12).

Covers the host-specified acceptance items:
- version comparison (0.2.17 < 0.2.18)
- TTL logic (not expired → no check)
- prompt idempotency (same version not repeated)
- silent failure on network/API errors (no raise, next TTL retries)
- [update] check=false disables the daemon loop
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from emrg.update_check import (
    DEFAULT_TTL_SECONDS,
    check_latest_version,
    is_newer,
    load_state,
    mark_prompted,
    parse_version,
    run_update_check_once,
    save_state,
    should_check,
    should_prompt,
    state_path,
)


# ── version comparison ────────────────────────────────────────────────────

def test_parse_version_basic():
    assert parse_version("v0.2.18") == (0, 2, 18)
    assert parse_version("0.2.17") == (0, 2, 17)
    assert parse_version("v1.0") == (1, 0)
    assert parse_version("") == ()


def test_parse_version_drops_prerelease_suffix():
    # releases/latest never returns prereleases, but the parser is defensive:
    # a prerelease tag terminates at the non-numeric piece → truncated tuple
    # can never compare as NEWER than a released version (safe semantic).
    assert parse_version("v0.2.17-beta1") == (0, 2)
    assert parse_version("0.2.18-rc.2") == (0, 2)
    # prerelease must never trigger a "new version" prompt
    assert is_newer(parse_version("0.2.18-rc.2"), parse_version("0.2.17")) is False


def test_is_newer():
    assert is_newer((0, 2, 18), (0, 2, 17)) is True
    assert is_newer((0, 2, 17), (0, 2, 18)) is False
    assert is_newer((0, 2, 17), (0, 2, 17)) is False
    assert is_newer((), (0, 2, 17)) is False  # unparseable never newer


def test_should_prompt_acceptance():
    # 0.2.17 current, latest 0.2.18, not yet prompted → prompt
    state = {}
    assert should_prompt(state, "0.2.18", "0.2.17") is True
    # same version already prompted → no repeat (idempotency)
    state = {"prompted_version": "0.2.18"}
    assert should_prompt(state, "0.2.18", "0.2.17") is False
    # up to date → no prompt
    assert should_prompt({}, "0.2.17", "0.2.17") is False
    # no latest → no prompt
    assert should_prompt({}, "", "0.2.17") is False


# ── TTL logic ─────────────────────────────────────────────────────────────

def test_should_check_missing_state_returns_true():
    assert should_check({}) is True


def test_should_check_fresh_state_returns_false():
    state = {"checked_at": time.time()}
    assert should_check(state, DEFAULT_TTL_SECONDS) is False


def test_should_check_stale_state_returns_true():
    state = {"checked_at": time.time() - DEFAULT_TTL_SECONDS - 1}
    assert should_check(state, DEFAULT_TTL_SECONDS) is True


def test_should_check_custom_ttl():
    state = {"checked_at": time.time() - 5000}
    assert should_check(state, 3600) is True
    assert should_check(state, 10000) is False


# ── state file ────────────────────────────────────────────────────────────

def test_state_roundtrip(tmp_path, monkeypatch):
    from emrg import config as config_mod

    monkeypatch.setattr(config_mod, "config_dir", lambda: tmp_path)
    # re-read the module-level state_path binding
    from emrg import update_check as uc

    monkeypatch.setattr(uc, "state_path", lambda: tmp_path / ".last_update_check.json")
    save_state({"checked_at": 123.0, "latest_version": "0.2.18"})
    assert load_state() == {"checked_at": 123.0, "latest_version": "0.2.18"}


def test_load_state_missing_file(tmp_path, monkeypatch):
    from emrg import update_check as uc

    monkeypatch.setattr(uc, "state_path", lambda: tmp_path / "nope.json")
    assert load_state() == {}


def test_load_state_corrupt_file(tmp_path, monkeypatch):
    from emrg import update_check as uc

    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(uc, "state_path", lambda: p)
    assert load_state() == {}


def test_mark_prompted_persists(tmp_path, monkeypatch):
    from emrg import update_check as uc

    p = tmp_path / "state.json"
    monkeypatch.setattr(uc, "state_path", lambda: p)
    state = {"latest_version": "0.2.18"}
    out = mark_prompted(state, "0.2.18")
    assert out["prompted_version"] == "0.2.18"
    assert json.loads(p.read_text(encoding="utf-8"))["prompted_version"] == "0.2.18"


# ── network check ─────────────────────────────────────────────────────────

def test_check_latest_version_success():
    async def run():
        class _Resp:
            status_code = 200

            def json(self):
                return {"tag_name": "v0.2.18"}

        client = AsyncMock()
        client.get = AsyncMock(return_value=_Resp())
        client.__aenter__ = AsyncMock(return_value=client)
        with patch(
            "emrg.update_check.httpx.AsyncClient",
            return_value=client,
        ):
            return await check_latest_version()

    assert asyncio.run(run()) == "0.2.18"


def test_check_latest_version_http_error_returns_none():
    async def run():
        class _Resp:
            status_code = 500

            def json(self):
                return {}

        client = AsyncMock()
        client.get = AsyncMock(return_value=_Resp())
        client.__aenter__ = AsyncMock(return_value=client)
        with patch(
            "emrg.update_check.httpx.AsyncClient",
            return_value=client,
        ):
            return await check_latest_version()

    assert asyncio.run(run()) is None  # silent, no raise


def test_check_latest_version_network_error_returns_none():
    async def run():
        with patch(
            "emrg.update_check.httpx.AsyncClient",
            side_effect=OSError("network unreachable"),
        ):
            return await check_latest_version()

    assert asyncio.run(run()) is None  # silent, no raise


def test_run_update_check_once_failure_preserves_state(tmp_path, monkeypatch):
    from emrg import update_check as uc

    p = tmp_path / "state.json"
    monkeypatch.setattr(uc, "state_path", lambda: p)
    with patch.object(uc, "check_latest_version", AsyncMock(return_value=None)):
        result = asyncio.run(uc.run_update_check_once({}))
    assert result["checked"] is False
    assert result["latest_version"] is None
    # failure must NOT persist a checked_at (next TTL retries immediately)
    assert not p.exists()


def test_run_update_check_once_success_persists(tmp_path, monkeypatch):
    from emrg import update_check as uc

    p = tmp_path / "state.json"
    monkeypatch.setattr(uc, "state_path", lambda: p)
    with patch.object(uc, "check_latest_version", AsyncMock(return_value="0.2.18")):
        result = asyncio.run(uc.run_update_check_once({}))
    assert result["checked"] is True
    assert result["latest_version"] == "0.2.18"
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["latest_version"] == "0.2.18"
    assert "checked_at" in saved


# ── daemon loop disable (config [update] check=false) ─────────────────────

def test_update_check_loop_returns_when_disabled():
    """config [update] check=false → loop exits immediately (no network)."""
    import emrg.server.daemon as daemon_mod

    async def run():
        server = daemon_mod.EmrgServer.__new__(daemon_mod.EmrgServer)
        with patch("emrg.config.load_update_config") as mock_cfg:
            mock_cfg.return_value.check = False
            await server._update_check_loop()
        return True

    assert asyncio.run(run()) is True
