"""Tests for connect module — WebSocket IPC connection layer (Phase 1)."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from emrg.connect import CONNECT_ID, AuthError, cleanup_server, get_server_path, is_server_running_sync
from tests.bounded_read import DEADLINE_SECONDS, within


class TestGetServerPath:
    """Tests for get_server_path — daemon port/token file path."""

    def test_returns_token_file_path(self, monkeypatch, tmp_path):
        """Returns ~/.emrg/emrgd.token in the config dir."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)

        result = get_server_path()
        expected = str(tmp_path / f"{CONNECT_ID}.token")
        assert result == expected

    def test_connect_id_constant(self):
        """CONNECT_ID is the expected value."""
        assert CONNECT_ID == "emrgd"


class TestAuthError:
    def test_is_exception(self):
        assert issubclass(AuthError, Exception)


class TestCleanupServer:
    def test_removes_token_file(self, monkeypatch, tmp_path):
        """Removes the token file if present (no daemon listening → deletion ok)."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)
        monkeypatch.setattr("emrg.connect.is_server_running_sync", lambda: False)
        port_file = tmp_path / f"{CONNECT_ID}.token"
        port_file.write_text("token", encoding="utf-8")

        cleanup_server()

        assert not port_file.exists()

    def test_noop_when_absent(self, monkeypatch, tmp_path):
        """Does nothing when the token file doesn't exist."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)
        monkeypatch.setattr("emrg.connect.is_server_running_sync", lambda: False)

        cleanup_server()  # must not raise

    def test_leaves_other_files(self, monkeypatch, tmp_path):
        """Only removes the token file, not other config files."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)
        monkeypatch.setattr("emrg.connect.is_server_running_sync", lambda: False)
        other = tmp_path / "config.toml"
        other.write_text("x", encoding="utf-8")
        port_file = tmp_path / f"{CONNECT_ID}.token"
        port_file.write_text("t", encoding="utf-8")

        cleanup_server()

        assert other.exists()
        assert not port_file.exists()

    def test_keeps_token_when_daemon_listening(self, monkeypatch, tmp_path):
        """rant 2026-08-27T14:48:50 — never delete a HEALTHY daemon's token.

        A mis-triggered spawn cleanup (the background start path
        `daemon_manager.start_daemon`, which `emrg server restart` now shares, runs
        cleanup_server() unconditionally) must not remove the token of a daemon
        that is actually listening on the fixed port. The doomed spawn (EADDRINUSE
        suicide) would never rewrite it → token-missing window for new connections.
        """
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)
        monkeypatch.setattr("emrg.connect.is_server_running_sync", lambda: True)
        port_file = tmp_path / f"{CONNECT_ID}.token"
        port_file.write_text("token", encoding="utf-8")

        cleanup_server()

        assert port_file.exists(), "token must survive when a daemon is listening"


class TestIsServerRunningSync:
    """Probes the FIXED daemon port (rant 2026-08-19T08:05:21) — no token-file
    read, so a missing/stale emrgd.token never hides a live daemon."""

    def _free_port(self) -> int:
        """Bind a probe socket to port 0 to get a free port (avoids colliding
        with a real daemon on the well-known EMRGD_PORT)."""
        import socket as _socket

        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]
        finally:
            probe.close()

    def test_false_when_fixed_port_closed(self, monkeypatch):
        """Returns False when nothing listens on the fixed port (no daemon)."""
        monkeypatch.setattr("emrg.connect.EMRGD_PORT", self._free_port())

        assert is_server_running_sync(timeout=0.1) is False

    def test_true_when_fixed_port_listening(self, monkeypatch):
        """Returns True when a daemon owns the fixed port — even with NO port
        file present (the dual-instance root cause the probe must catch)."""
        import socket as _socket

        port = self._free_port()
        monkeypatch.setattr("emrg.connect.EMRGD_PORT", port)
        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        try:
            assert is_server_running_sync(timeout=1.0) is True
        finally:
            srv.close()

    def test_false_when_connection_refused(self, monkeypatch):
        """Returns False when the fixed port is closed."""
        monkeypatch.setattr("emrg.connect.EMRGD_PORT", self._free_port())

        assert is_server_running_sync(timeout=0.1) is False


class TestConnectToServer:
    def test_connect_uses_proxy_none(self, monkeypatch, tmp_path):
        """Loopback WS must never route through a system proxy.

        websockets 17 defaults proxy=True and reads the OS proxy settings; when a
        Windows system proxy is configured, the ws://127.0.0.1 handshake is sent
        to the proxy → InvalidMessage → Python clients cannot reach the local
        daemon (2026-08-14 incident). proxy=None pins direct loopback.
        """
        from emrg import connect as connect_mod

        captured = {}

        class FakeWS:
            async def send(self, data):
                self.sent = data

            async def recv(self):
                return json.dumps({"type": "auth_ok"})

            async def close(self):
                pass

        async def fake_connect(uri, **kwargs):
            captured["uri"] = uri
            captured["kwargs"] = kwargs
            return FakeWS()

        monkeypatch.setattr(connect_mod, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(connect_mod, "connect", fake_connect)
        (tmp_path / f"{CONNECT_ID}.token").write_text("token", encoding="utf-8")

        asyncio.run(connect_mod.connect_to_server())

        # Fixed daemon port (rant 2026-08-19T08:05:21 + 2026-08-20T14:32:52) —
        # the URI no longer depends on the file (single-line token only).
        assert captured["uri"] == f"ws://127.0.0.1:{connect_mod.EMRGD_PORT}"
        assert captured["kwargs"]["proxy"] is None
        assert captured["kwargs"]["max_size"] == 16 * 1024 * 1024


class TestConnectToServerAsksItsKind:
    """The token file's kind is asked before it is opened (issue #2115).

    `connect_to_server` was the last reader of the runtime's own files under `~/.emrg/` that
    opened its subject on a bare contract. The path is fixed (`<config dir>/emrgd.token`) and
    the corpus is the **client entry path**: every Python client, and the scheduler's own
    `connect_to_server` (`emrg/server/scheduler.py`, how a scheduled task reaches the daemon).
    `read_text` on a FIFO waits at the open for a writer and raises nothing, so the
    `FileNotFoundError` the docstring promises could not fire — the *client* would stop, and
    the only recovery is a restart, which belongs to the host.

    The FIFO arms carry `os.mkfifo`'s own gate so the Windows leg still runs every control
    (the shape `#2096` had to be split over); the kind is what discriminates, so the arms read
    the noun phrase out of the refusal rather than only "it raised" — an unguarded directory
    raises `IsADirectoryError`, an `OSError` too.
    """

    def _wedge(self, monkeypatch, tmp_path, make):
        """Point the module at `tmp_path`, plant the subject with `make`, stub the socket.

        `connect` is stubbed to fail loudly: the guard must answer before any handshake, and a
        live daemon on `EMRGD_PORT` would answer one.
        """
        from emrg import connect as connect_mod

        def _must_not_connect(uri, **kwargs):  # pragma: no cover - reached only on a lapse
            raise AssertionError(f"connect_to_server reached the socket: {uri}")

        monkeypatch.setattr(connect_mod, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(connect_mod, "connect", _must_not_connect)
        make(tmp_path / f"{CONNECT_ID}.token")
        return connect_mod

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
    def test_a_named_pipe_token_is_refused_without_being_opened(self, monkeypatch, tmp_path):
        """Bounded: an unguarded `read_text` here never returns, it does not raise."""
        mod = self._wedge(monkeypatch, tmp_path, os.mkfifo)

        with pytest.raises(OSError) as caught:
            within(DEADLINE_SECONDS, lambda: asyncio.run(mod.connect_to_server()))

        assert "FIFO" in str(caught.value), caught.value

    def test_a_directory_token_is_refused_by_kind(self, monkeypatch, tmp_path):
        """The pipe-free half, and it runs on Windows: the refusal names the kind."""
        mod = self._wedge(monkeypatch, tmp_path, lambda p: p.mkdir())

        with pytest.raises(OSError) as caught:
            asyncio.run(mod.connect_to_server())

        assert "not a regular file" in str(caught.value), caught.value
        assert "a directory" in str(caught.value), caught.value

    def test_a_missing_token_still_raises_file_not_found(self, monkeypatch, tmp_path):
        """The contract the kind check must not narrow: absence is still `FileNotFoundError`.

        `non_regular_kind` *stats*, so an absent path raises out of the `stat` — the answer
        every caller in this repository already has a handler for (`os.path.exists`-style
        "daemon not running"), and the one a refusal-shaped guard would have replaced.
        """
        mod = self._wedge(monkeypatch, tmp_path, lambda p: None)

        with pytest.raises(FileNotFoundError):
            asyncio.run(mod.connect_to_server())
