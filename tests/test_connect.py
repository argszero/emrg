"""Tests for connect module — WebSocket IPC connection layer (Phase 1)."""

import asyncio
import json
from pathlib import Path

import pytest
import websockets.exceptions as wexc

from emrg.connect import CONNECT_ID, AuthError, cleanup_server, get_server_path, is_server_running_sync

# The shapes a daemon's first frame can take that are NOT this client's
# `auth_ok`. Every one of them means the same thing to `emrg server stop` — a
# daemon answered and this client could not accept it (see
# TestTheHandshakeNamesItsRefusal) — so every one of them must be an AuthError.
# Measured on master when this catalogue was written (2026-10-04): four of the
# five frames and two of the four raised shapes escaped un-guarded; the legs
# that already passed are the control against widening the guard into a no-op.
_DECLINED_FRAMES = [
    "not json at all",             # not JSON → JSONDecodeError on master
    "[1,2,3]",                     # JSON, not an object → AttributeError on master
    "123",                         # a JSON scalar
    '"auth_ok"',                   # a JSON string, not the object
    json.dumps({"type": "bye"}),   # an object with the wrong `type` (guarded before)
]

_DECLINED_RAISES = [
    wexc.ConnectionClosed(None, None),   # daemon closed the socket (guarded before)
    wexc.InvalidMessage("bad frame"),    # protocol error → escaped on master
    wexc.PayloadTooBig(20 * 1024 * 1024, 16 * 1024 * 1024),  # over max_size → escaped on master
    asyncio.TimeoutError(),              # no answer inside the budget (guarded before)
]


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


def _handshake(monkeypatch, tmp_path, *, frame=None, raises=None):
    """Point `connect_to_server` at a fake daemon that answers with `frame` or
    raises `raises` on its first `recv()`. Returns the module under test."""
    from emrg import connect as connect_mod

    class FakeWS:
        async def send(self, data):
            pass

        async def recv(self):
            if raises is not None:
                raise raises
            return frame

        async def close(self):
            pass

    async def fake_connect(uri, **kwargs):
        return FakeWS()

    monkeypatch.setattr(connect_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(connect_mod, "connect", fake_connect)
    (tmp_path / f"{CONNECT_ID}.token").write_text("token", encoding="utf-8")
    return connect_mod


def _attempt(connect_mod):
    """Run the handshake, returning the ws on success or the raised exception."""
    try:
        return asyncio.run(connect_mod.connect_to_server())
    except BaseException as exc:  # noqa: BLE001 — the point is to see what escapes
        return exc


class TestTheHandshakeNamesItsRefusal:
    """Every way a daemon can decline the handshake leaves as AuthError.

    `emrg server stop` reads the exception's TYPE, not its text:
    `__main__._stop_failure_message` answers "daemon not running." for anything
    that is not an AuthError. So a daemon that answers with a frame this client
    cannot read as its `auth_ok` — a version mismatch, the case this module's
    own docstring names as AuthError — must not escape as a raw
    JSONDecodeError / AttributeError / websockets error: the host would be told
    to go hunting for a daemon process that is standing right there. Measured on
    master before the fix (2026-10-04): six of the nine shapes below escaped
    un-guarded — the four frames that are not a `{"type": ...}` object (`not
    json at all`, `[1,2,3]`, `123`, `"auth_ok"`) as a raw JSONDecodeError or
    AttributeError, and `InvalidMessage` / `PayloadTooBig` as raw websockets
    errors.
    """

    def test_the_population_is_what_the_guard_reads(self):
        """The catalogues are closed over the shapes the guard names.

        Each entry drives one branch of `connect_to_server`'s two `except`
        clauses plus the `isinstance`/`type` check. Keeping the lists (and
        asserting their size) is what stops the guard from silently widening or
        narrowing without a test.
        """
        assert len(_DECLINED_FRAMES) == 5
        assert len(_DECLINED_RAISES) == 4

    def test_a_valid_ack_returns_the_socket(self, monkeypatch, tmp_path):
        """Control leg: the one answer the client wants still returns a ws."""
        mod = _handshake(monkeypatch, tmp_path, frame=json.dumps({"type": "auth_ok"}))
        result = _attempt(mod)
        assert not isinstance(result, BaseException)

    @pytest.mark.parametrize("frame", _DECLINED_FRAMES)
    def test_a_frame_the_client_cannot_accept(self, monkeypatch, tmp_path, frame):
        mod = _handshake(monkeypatch, tmp_path, frame=frame)
        assert isinstance(_attempt(mod), AuthError)

    @pytest.mark.parametrize("exc", _DECLINED_RAISES, ids=lambda e: type(e).__name__)
    def test_a_raised_transport_error(self, monkeypatch, tmp_path, exc):
        """ConnectionClosed / protocol errors / timeout all mean "no auth_ok"."""
        mod = _handshake(monkeypatch, tmp_path, raises=exc)
        assert isinstance(_attempt(mod), AuthError)


class TestTheHandshakeKeepsRealTransportFailures:
    """Control legs in the other direction: what the guard must NOT swallow.

    A daemon that is genuinely absent (the token file gone, or nothing bound to
    the fixed port) really is "not running", and `emrg server stop` must be able
    to say so. Collapsing these into AuthError would trade one lie for another.
    """

    def test_a_missing_token_file_still_raises(self, monkeypatch, tmp_path):
        from emrg import connect as connect_mod

        monkeypatch.setattr(connect_mod, "config_dir", lambda: tmp_path)

        result = _attempt(connect_mod)

        assert isinstance(result, FileNotFoundError)
        assert not isinstance(result, AuthError)

    def test_a_refused_connection_still_raises(self, monkeypatch, tmp_path):
        from emrg import connect as connect_mod

        async def refused(uri, **kwargs):
            raise ConnectionRefusedError(61, "Connection refused")

        monkeypatch.setattr(connect_mod, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(connect_mod, "connect", refused)
        (tmp_path / f"{CONNECT_ID}.token").write_text("token", encoding="utf-8")

        result = _attempt(connect_mod)

        assert isinstance(result, ConnectionRefusedError)
        assert not isinstance(result, AuthError)
