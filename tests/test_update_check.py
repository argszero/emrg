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
    asset_sha256,
    check_latest_version,
    download_release_asset,
    is_newer,
    load_state,
    mark_prompted,
    parse_version,
    platform_asset_name,
    release_asset_url,
    run_update_check_once,
    save_state,
    sha256_file,
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


# ── auto-download (rant 2026-08-12T12:10:12) ──────────────────────────────

def test_platform_asset_name_windows(monkeypatch):
    monkeypatch.setattr("emrg.update_check.platform.system", lambda: "Windows")
    assert platform_asset_name("v0.2.27") == "EMRG-0.2.27-windows-x64.exe"


def test_platform_asset_name_macos_arm64(monkeypatch):
    monkeypatch.setattr("emrg.update_check.platform.system", lambda: "Darwin")
    monkeypatch.setattr("emrg.update_check.platform.machine", lambda: "arm64")
    assert platform_asset_name("0.2.27") == "EMRG-0.2.27-macos-arm64.pkg"


def test_platform_asset_name_macos_x64(monkeypatch):
    monkeypatch.setattr("emrg.update_check.platform.system", lambda: "Darwin")
    monkeypatch.setattr("emrg.update_check.platform.machine", lambda: "x86_64")
    assert platform_asset_name("0.2.27") == "EMRG-0.2.27-macos-x64.pkg"


def test_platform_asset_name_linux(monkeypatch):
    monkeypatch.setattr("emrg.update_check.platform.system", lambda: "Linux")
    monkeypatch.setattr("emrg.update_check.platform.machine", lambda: "x86_64")
    assert platform_asset_name("0.2.27") == "EMRG-0.2.27-linux-x86_64.AppImage"
    monkeypatch.setattr("emrg.update_check.platform.machine", lambda: "aarch64")
    assert platform_asset_name("0.2.27") == "EMRG-0.2.27-linux-aarch64.AppImage"


def test_platform_asset_name_unsupported(monkeypatch):
    monkeypatch.setattr("emrg.update_check.platform.system", lambda: "Plan9")
    assert platform_asset_name("0.2.27") is None
    assert platform_asset_name("") is None


def test_release_asset_url():
    assert release_asset_url("v0.2.27", "EMRG-0.2.27-windows-x64.exe") == (
        "https://github.com/argszero/emrg/releases/download/v0.2.27/"
        "EMRG-0.2.27-windows-x64.exe"
    )


def test_asset_sha256_extracts_digest():
    release = {"assets": [
        {"name": "EMRG-0.2.27-windows-x64.exe",
         "digest": "sha256:ffa9c7cc906e049a61e0a2ff7fd0d8365521d1e225af34de8a9bc022d76c11b7"},
        {"name": "other.txt", "digest": "sha256:beef"},
    ]}
    assert asset_sha256(release, "EMRG-0.2.27-windows-x64.exe") == (
        "ffa9c7cc906e049a61e0a2ff7fd0d8365521d1e225af34de8a9bc022d76c11b7"
    )


def test_asset_sha256_missing_digest_field():
    # asset found but no digest → None (caller skips verification, never blocks)
    release = {"assets": [{"name": "x.pkg"}]}
    assert asset_sha256(release, "x.pkg") is None
    # asset not in metadata → None
    assert asset_sha256(release, "nope.pkg") is None
    assert asset_sha256(None, "x.pkg") is None


def test_sha256_file(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello world")
    import hashlib
    assert sha256_file(p) == hashlib.sha256(b"hello world").hexdigest()


class _FakeStream:
    """Async context manager mimicking httpx.Response inside client.stream()."""

    def __init__(self, status_code, chunks):
        self.status_code = status_code
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


_ASSET = "EMRG-0.2.99-windows-x64.exe"  # pinned via platform_asset_name patch


def _release_json(digest=None, asset_name=_ASSET):
    assets = [{"name": asset_name, "digest": f"sha256:{digest}" if digest else None}]
    return {"tag_name": "v0.2.99", "assets": assets}


def _patch_download(tmp_path, monkeypatch, stream_resp, release=None):
    """Wire httpx.AsyncClient mocks so download_release_asset is hermetic.

    Returns (client_mock, capture_dict) — capture["headers"] holds the Range
    header the download attempted, for resume assertions.
    """
    from unittest.mock import MagicMock

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    release = release if release is not None else _release_json(digest="abcd")
    client.get = AsyncMock(return_value=_Resp200(release))
    client.stream = MagicMock(return_value=stream_resp)
    capture = {}

    orig_stream = client.stream

    def _wrapped_stream(*args, **kwargs):
        capture["headers"] = kwargs.get("headers") or {}
        return orig_stream(*args, **kwargs)

    client.stream = _wrapped_stream

    monkeypatch.setattr("emrg.update_check.httpx.AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr("emrg.update_check.updates_dir", lambda: tmp_path)
    # Pin the platform asset name — tests must not depend on the host platform.
    monkeypatch.setattr("emrg.update_check.platform_asset_name", lambda v: _ASSET)
    return client, capture


class _Resp200:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_download_success_verify_skipped_when_no_digest(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    client, capture = _patch_download(
        tmp_path, monkeypatch, _FakeStream(200, [b"PK\x03\x04", b"DATA"]),
        release=_release_json(digest=None),  # no digest → skip verify
    )
    result = asyncio.run(download_release_asset("0.2.99"))
    assert result["downloaded_version"] == "0.2.99"
    assert result["downloaded_path"] == str(tmp_path / "EMRG-0.2.99-windows-x64.exe")
    assert result["downloaded_sha"] == sha256_file(tmp_path / "EMRG-0.2.99-windows-x64.exe")
    # .part consumed → only the final file remains
    assert not (tmp_path / "EMRG-0.2.99-windows-x64.exe.part").exists()
    assert capture["headers"] == {}, "no Range header on a fresh download"


def test_download_verify_failure_deletes_part(tmp_path, monkeypatch):
    client, capture = _patch_download(
        tmp_path, monkeypatch, _FakeStream(200, [b"tampered-bytes"]),
        release=_release_json(digest="0" * 64),  # wrong digest
    )
    result = asyncio.run(download_release_asset("0.2.99"))
    assert result == {}, "verification failure → {} (retry next TTL)"
    assert not (tmp_path / "EMRG-0.2.99-windows-x64.exe").exists()
    assert not (tmp_path / "EMRG-0.2.99-windows-x64.exe.part").exists()


def test_download_resume_sends_range_and_appends(tmp_path, monkeypatch):
    part = tmp_path / "EMRG-0.2.99-windows-x64.exe.part"
    part.write_bytes(b"0123456789")
    client, capture = _patch_download(
        tmp_path, monkeypatch, _FakeStream(206, [b"abcdef"]),
        release=_release_json(digest=None),
    )
    result = asyncio.run(download_release_asset("0.2.99"))
    assert capture["headers"] == {"Range": "bytes=10-"}, "resume sends Range from .part size"
    final = tmp_path / "EMRG-0.2.99-windows-x64.exe"
    assert final.read_bytes() == b"0123456789abcdef", "206 appends to the partial file"


def test_download_already_verified_skips_network(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    dest = tmp_path / "EMRG-0.2.99-windows-x64.exe"
    dest.write_bytes(b"good-bytes")
    digest = sha256_file(dest)
    client, capture = _patch_download(
        tmp_path, monkeypatch, _FakeStream(200, [b"never-used"]),
        release=_release_json(digest=digest),
    )
    result = asyncio.run(download_release_asset("0.2.99"))
    assert result["downloaded_version"] == "0.2.99"
    assert result["downloaded_sha"] == digest
    assert capture == {}, "no network call when the file is already verified"


def test_download_http_error_silent(tmp_path, monkeypatch):
    client, capture = _patch_download(
        tmp_path, monkeypatch, _FakeStream(404, [b""]),
        release=_release_json(digest=None),
    )
    result = asyncio.run(download_release_asset("0.2.99"))
    assert result == {}
    assert not (tmp_path / "EMRG-0.2.99-windows-x64.exe").exists()


def test_download_network_error_silent_keeps_part(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    part = tmp_path / "EMRG-0.2.99-windows-x64.exe.part"
    part.write_bytes(b"partial")

    def _boom(*a, **k):
        raise OSError("connection reset")

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.get = AsyncMock(return_value=_Resp200(_release_json(digest=None)))
    client.stream = MagicMock(side_effect=_boom)
    monkeypatch.setattr("emrg.update_check.httpx.AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr("emrg.update_check.updates_dir", lambda: tmp_path)

    result = asyncio.run(download_release_asset("0.2.99"))
    assert result == {}, "network failure → {} (silent)"
    assert part.exists(), ".part kept so the next TTL resumes"


def test_download_unsupported_platform_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "emrg.update_check.platform_asset_name", lambda v: None
    )
    result = asyncio.run(download_release_asset("0.2.99"))
    assert result == {}


def test_download_release_fetch_failure_silent(tmp_path, monkeypatch):
    from unittest.mock import patch as mpatch

    with mpatch(
        "emrg.update_check.fetch_latest_release",
        AsyncMock(return_value=None),
    ):
        result = asyncio.run(download_release_asset("0.2.99"))
    assert result == {}


# ── config defaults (rant 2026-08-12T12:10:12: ttl 24h → 1h + auto_download) ─

def test_update_config_defaults():
    from emrg.config import UpdateConfig

    cfg = UpdateConfig()
    assert cfg.check is True
    assert cfg.ttl_hours == 1, "default TTL 24h → 1h (rant 2026-08-12T12:10:12)"
    assert cfg.auto_download is True


def test_load_update_config_defaults_missing_file(tmp_path, monkeypatch):
    from emrg import config as config_mod

    monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "missing.toml")
    cfg = config_mod.load_update_config()
    assert cfg.ttl_hours == 1
    assert cfg.auto_download is True


def test_load_update_config_parses_auto_download(tmp_path, monkeypatch):
    from emrg import config as config_mod

    p = tmp_path / "config.toml"
    p.write_text("[update]\ncheck = false\nauto_download = false\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "config_path", lambda: p)
    cfg = config_mod.load_update_config()
    assert cfg.check is False
    assert cfg.auto_download is False
    assert cfg.ttl_hours == 1, "unset ttl_hours falls back to the new 1h default"


# ── daemon: _maybe_auto_download gating (rant 2026-08-12T12:10:12) ─────────

def test_maybe_auto_download_disabled_by_config():
    import emrg.server.daemon as daemon_mod

    async def run():
        server = daemon_mod.EmrgServer.__new__(daemon_mod.EmrgServer)
        with patch("emrg.server.daemon.asyncio.create_task") as m_ct:
            await server._maybe_auto_download("0.2.99", False)
        return m_ct.call_count

    assert asyncio.run(run()) == 0, "auto_download=false → no download task"


def test_maybe_auto_download_skips_when_not_newer():
    import emrg.server.daemon as daemon_mod

    async def run():
        server = daemon_mod.EmrgServer.__new__(daemon_mod.EmrgServer)
        with patch("emrg.server.daemon.asyncio.create_task") as m_ct:
            # running version is 0.2.27 (emrg.__version__); "0.2.20" is older
            await server._maybe_auto_download("0.2.20", True)
        return m_ct.call_count

    assert asyncio.run(run()) == 0


def test_maybe_auto_download_skips_when_already_downloaded():
    import emrg.server.daemon as daemon_mod

    async def run():
        server = daemon_mod.EmrgServer.__new__(daemon_mod.EmrgServer)
        with patch("emrg.update_check.load_state", return_value={"downloaded_version": "0.2.99"}):
            with patch("emrg.server.daemon.asyncio.create_task") as m_ct:
                await server._maybe_auto_download("0.2.99", True)
        return m_ct.call_count

    assert asyncio.run(run()) == 0, "same version already downloaded → skip"


def test_maybe_auto_download_spawns_task_for_newer():
    import emrg.server.daemon as daemon_mod

    async def run():
        server = daemon_mod.EmrgServer.__new__(daemon_mod.EmrgServer)
        with patch("emrg.update_check.load_state", return_value={}):
            with patch.object(server, "_auto_download_update", new=AsyncMock()) as m_dl:
                await server._maybe_auto_download("0.2.99", True)
                await asyncio.sleep(0)  # let the spawned task run
        return m_dl.await_count

    assert asyncio.run(run()) == 1, "newer version + not downloaded → spawn task"
