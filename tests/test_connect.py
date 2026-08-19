"""Tests for connect module — WebSocket IPC connection layer (Phase 1)."""

import asyncio
import json
from pathlib import Path

from emrg.connect import CONNECT_ID, AuthError, cleanup_server, get_server_path, is_server_running_sync


class TestGetServerPath:
    """Tests for get_server_path — daemon port/token file path."""

    def test_returns_port_file_path(self, monkeypatch, tmp_path):
        """Returns ~/.emrg/emrgd.port in the config dir."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)

        result = get_server_path()
        expected = str(tmp_path / f"{CONNECT_ID}.port")
        assert result == expected

    def test_connect_id_constant(self):
        """CONNECT_ID is the expected value."""
        assert CONNECT_ID == "emrgd"


class TestAuthError:
    def test_is_exception(self):
        assert issubclass(AuthError, Exception)


class TestCleanupServer:
    def test_removes_port_file(self, monkeypatch, tmp_path):
        """Removes the port file if present."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)
        port_file = tmp_path / f"{CONNECT_ID}.port"
        port_file.write_text("49152\ntoken", encoding="utf-8")

        cleanup_server()

        assert not port_file.exists()

    def test_noop_when_absent(self, monkeypatch, tmp_path):
        """Does nothing when the port file doesn't exist."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)

        cleanup_server()  # must not raise

    def test_leaves_other_files(self, monkeypatch, tmp_path):
        """Only removes the port file, not other config files."""
        monkeypatch.setattr("emrg.connect.config_dir", lambda: tmp_path)
        other = tmp_path / "config.toml"
        other.write_text("x", encoding="utf-8")
        port_file = tmp_path / f"{CONNECT_ID}.port"
        port_file.write_text("1\nt", encoding="utf-8")

        cleanup_server()

        assert other.exists()
        assert not port_file.exists()


class TestIsServerRunningSync:
    """Probes the FIXED daemon port (rant 2026-08-19T08:05:21) — no port-file
    read, so a missing/stale emrgd.port never hides a live daemon."""

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
        (tmp_path / f"{CONNECT_ID}.port").write_text("49152\ntoken", encoding="utf-8")

        asyncio.run(connect_mod.connect_to_server())

        # Fixed daemon port (rant 2026-08-19T08:05:21) — the URI no longer
        # depends on the port file's port value, only the token is read from it.
        assert captured["uri"] == f"ws://127.0.0.1:{connect_mod.EMRGD_PORT}"
        assert captured["kwargs"]["proxy"] is None
        assert captured["kwargs"]["max_size"] == 16 * 1024 * 1024
