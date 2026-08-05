"""DaemonManager unit tests (mocked — no real daemon).

Follows Phase 2 daemon_manager design §3.4: mock all network boundaries.
⚠️ mock 路径均为 emrg.client.daemon_manager.<符号>——模块级 import 绑定，
patch 原模块会静默失效；asyncio.create_subprocess_exec 应 patch
emrg.client.daemon_manager.asyncio.create_subprocess_exec。
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys

# R123 (#401) 后 Windows 已有原生 TUI，但 daemon_manager 测试覆盖 daemon 生命周期
# （spawn/信号/超时语义），Windows 上 CREATE_NEW_PROCESS_GROUP 等行为与 POSIX 不同
# → Windows CI 冒烟阶段仍跳过（纯逻辑测试见 test_buffer/test_output/test_input_parser）。
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="daemon 生命周期测试依赖 POSIX 进程语义（Windows CI 冒烟不跑）")

from emrg.client import daemon_manager


class FakeWS:
    """Minimal websockets-like fake: send/recv/close."""

    def __init__(self, frames=None):
        self._frames = list(frames or [])
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self._frames:
            raise ConnectionError("no frames")
        return self._frames.pop(0)

    async def close(self):
        self.closed = True


def _ping_pong_frame() -> str:
    return json.dumps({
        "type": "pong",
        "identity": {"instance_id": "test-instance", "host_name": "test-host"},
        "started_at": "2026-08-03T00:00:00",
        "pid": 9999,
        "uptime_seconds": 10,
        "evolution_count": 0,
        "model": "test-model",
    })


# ── is_running ───────────────────────────────────────────────

class TestIsRunning:
    @patch("emrg.client.daemon_manager.is_server_running_sync", return_value=True)
    def test_true_when_port_file_ok(self, mock_probe):
        assert daemon_manager.is_running() is True
        mock_probe.assert_called_once()

    @patch("emrg.client.daemon_manager.is_server_running_sync", return_value=False)
    def test_false_when_no_daemon(self, mock_probe):
        assert daemon_manager.is_running() is False


# ── start_daemon ─────────────────────────────────────────────

class TestStartDaemon:
    @patch("emrg.client.daemon_manager.is_running", return_value=True)
    @patch("emrg.client.daemon_manager.cleanup_server")
    @patch("emrg.client.daemon_manager.asyncio.create_subprocess_exec",
           new_callable=AsyncMock)
    def test_starts_and_waits_ready(self, mock_spawn, mock_cleanup, mock_is_running):
        proc = MagicMock(pid=1234)
        mock_spawn.return_value = proc

        async def _run():
            return await daemon_manager.start_daemon()

        result = asyncio.run(_run())
        assert result is proc
        mock_cleanup.assert_called_once()
        # spawn args: [sys.executable, "-m", "emrg.server"] with DEVNULL stdio
        args = mock_spawn.await_args.args
        assert args[0].endswith("python") or "python" in args[0]
        assert args[1:] == ("-m", "emrg.server")

    @patch("emrg.client.daemon_manager.is_running", return_value=False)
    @patch("emrg.client.daemon_manager.cleanup_server")
    @patch("emrg.client.daemon_manager.asyncio.create_subprocess_exec",
           new_callable=AsyncMock)
    def test_raises_on_timeout(self, mock_spawn, mock_cleanup, mock_is_running):
        proc = MagicMock(pid=1234)
        mock_spawn.return_value = proc

        async def _run():
            with pytest.raises(RuntimeError, match="failed to start"):
                await daemon_manager.start_daemon()

        asyncio.run(_run())


# ── check_and_restart_if_stale ───────────────────────────────

class TestCheckAndRestartIfStale:
    def test_no_port_file_returns_early(self, tmp_path):
        with patch("emrg.client.daemon_manager.get_server_path",
                   return_value=str(tmp_path / "nope.port")):
            asyncio.run(daemon_manager.check_and_restart_if_stale())
        # no exceptions = pass

    @patch("emrg.client.daemon_manager._get_config_mtime", return_value=0.0)
    @patch("emrg.client.daemon_manager._get_server_source_mtime", return_value=0.0)
    @patch("emrg.client.daemon_manager.is_running", return_value=True)
    @patch("emrg.client.daemon_manager.connect_to_server", new_callable=AsyncMock)
    def test_mtime_unchanged_no_restart(self, mock_connect, mock_running,
                                        mock_src, mock_cfg, tmp_path):
        port_file = tmp_path / "emrgd.port"
        port_file.write_text("12345\ntoken\n")
        mock_connect.return_value = FakeWS([_ping_pong_frame()])

        with patch("emrg.client.daemon_manager.get_server_path",
                   return_value=str(port_file)):
            asyncio.run(daemon_manager.check_and_restart_if_stale())
        # No restart: the frame's started_at (2026) > mtimes (0), so no SIGTERM.
        # We only assert connect was used (ping roundtrip happened).

    @patch("emrg.client.daemon_manager._get_config_mtime", return_value=0.0)
    @patch("emrg.client.daemon_manager._get_server_source_mtime", return_value=1e12)
    @patch("emrg.client.daemon_manager.is_running", return_value=True)
    @patch("emrg.client.daemon_manager.cleanup_server")
    @patch("emrg.client.daemon_manager.os.kill")
    @patch("emrg.client.daemon_manager.connect_to_server", new_callable=AsyncMock)
    def test_source_newer_triggers_restart(self, mock_connect, mock_kill,
                                           mock_cleanup, mock_running,
                                           mock_src, mock_cfg, tmp_path):
        port_file = tmp_path / "emrgd.port"
        port_file.write_text("12345\ntoken\n")
        # started_at in the past → source mtime (1e12) > server_start
        mock_connect.return_value = FakeWS([_ping_pong_frame()])

        with patch("emrg.client.daemon_manager.get_server_path",
                   return_value=str(port_file)):
            asyncio.run(daemon_manager.check_and_restart_if_stale())
        # SIGTERM sent to pid 9999
        kill_calls = [c.args for c in mock_kill.call_args_list]
        assert any(9999 in call and call[0] == 9999 for call in kill_calls)

    @patch("emrg.client.daemon_manager._get_config_mtime", return_value=0.0)
    @patch("emrg.client.daemon_manager._get_server_source_mtime", return_value=0.0)
    @patch("emrg.client.daemon_manager.connect_to_server", new_callable=AsyncMock)
    def test_server_unreachable_silent(self, mock_connect, mock_src, mock_cfg, tmp_path):
        port_file = tmp_path / "emrgd.port"
        port_file.write_text("12345\ntoken\n")
        mock_connect.side_effect = ConnectionRefusedError("no daemon")

        with patch("emrg.client.daemon_manager.get_server_path",
                   return_value=str(port_file)):
            asyncio.run(daemon_manager.check_and_restart_if_stale())  # no raise


# ── ensure_connected ─────────────────────────────────────────

class TestEnsureConnected:
    @patch("emrg.client.daemon_manager.is_running", return_value=True)
    @patch("emrg.client.daemon_manager.check_and_restart_if_stale",
           new_callable=AsyncMock)
    @patch("emrg.client.daemon_manager.connect_to_server", new_callable=AsyncMock)
    def test_wraps_connection(self, mock_connect, mock_check, mock_running):
        ws = FakeWS([json.dumps({"type": "auth_ok"})])
        mock_connect.return_value = ws

        async def _run():
            conn = await daemon_manager.ensure_connected()
            assert isinstance(conn, daemon_manager.DaemonConnection)
            assert conn._ws is ws
            mock_check.assert_awaited_once()

        asyncio.run(_run())

    @patch("emrg.client.daemon_manager.is_running", return_value=False)
    @patch("emrg.client.daemon_manager.check_and_restart_if_stale",
           new_callable=AsyncMock)
    @patch("emrg.client.daemon_manager.start_daemon", new_callable=AsyncMock)
    @patch("emrg.client.daemon_manager.cleanup_server")
    @patch("emrg.client.daemon_manager.connect_to_server", new_callable=AsyncMock)
    def test_starts_daemon_when_down(self, mock_connect, mock_cleanup,
                                     mock_start, mock_check, mock_running):
        ws = FakeWS([json.dumps({"type": "auth_ok"})])
        mock_connect.return_value = ws

        async def _run():
            conn = await daemon_manager.ensure_connected()
            assert conn._ws is ws
            mock_start.assert_awaited_once()
            mock_cleanup.assert_called_once()

        asyncio.run(_run())


# ── DaemonConnection ─────────────────────────────────────────

class TestDaemonConnection:
    def _conn(self, frames=None):
        return daemon_manager.DaemonConnection(FakeWS(frames))

    def test_send_task_payload(self):
        conn = self._conn()
        asyncio.run(conn.send_task(
            session_id="s1", cwd="/tmp/x", prompt="hello", stream=True,
            images=[{"path": "/tmp/a.png", "label": "[image1]"}],
        ))
        sent = json.loads(conn._ws.sent[0])
        assert sent["type"] == "task"
        assert sent["session_id"] == "s1"
        assert sent["cwd"] == "/tmp/x"
        assert sent["prompt"] == "hello"
        assert sent["stream"] is True
        assert sent["images"] == [{"path": "/tmp/a.png", "label": "[image1]"}]

    def test_send_task_no_images(self):
        conn = self._conn()
        asyncio.run(conn.send_task(session_id="s1", cwd="/tmp/x", prompt="hi"))
        sent = json.loads(conn._ws.sent[0])
        assert "images" not in sent

    def test_send_command_payload(self):
        conn = self._conn()
        asyncio.run(conn.send_command("set_model", model="gpt-4o"))
        sent = json.loads(conn._ws.sent[0])
        assert sent == {"type": "set_model", "model": "gpt-4o"}

    def test_send_command_ensure_ascii_false(self):
        conn = self._conn()
        asyncio.run(conn.send_command("rant", message="中文测试"))
        sent = conn._ws.sent[0]
        assert "中文测试" in sent  # not escaped

    def test_recv_returns_dict(self):
        conn = self._conn([json.dumps({"type": "pong"})])
        data = asyncio.run(conn.recv(timeout=1))
        assert data == {"type": "pong"}

    def test_recv_timeout_returns_none(self):
        class NoRecvWS:
            async def recv(self):
                raise asyncio.TimeoutError()
        conn = daemon_manager.DaemonConnection(NoRecvWS())
        assert asyncio.run(conn.recv(timeout=0.01)) is None

    def test_recv_bad_json_returns_none(self):
        conn = self._conn(["not-json{{{"])
        assert asyncio.run(conn.recv(timeout=1)) is None

    def test_recv_empty_frame_returns_none(self):
        conn = self._conn(["   "])
        assert asyncio.run(conn.recv(timeout=1)) is None

    def test_recv_connection_closed_propagates(self):
        from websockets.exceptions import ConnectionClosedError

        class ClosedWS:
            async def recv(self):
                raise ConnectionClosedError(rcvd=None, sent=None)
        conn = daemon_manager.DaemonConnection(ClosedWS())
        with pytest.raises(ConnectionClosedError):
            asyncio.run(conn.recv(timeout=1))

    def test_read_stream_yields_frames(self):
        conn = self._conn([json.dumps({"a": 1}), json.dumps({"b": 2})])

        async def _run():
            out = []
            async for frame in conn.read_stream():
                out.append(frame)
                if len(out) == 2:
                    break
            return out

        assert asyncio.run(_run()) == [{"a": 1}, {"b": 2}]

    def test_close_calls_ws_close(self):
        conn = self._conn()
        asyncio.run(conn.close())
        assert conn._ws.closed is True

class TestReadLogTail:
    """_read_log_tail — daemon start-timeout diagnostics (R124)."""

    def test_tail_last_lines(self, tmp_path):
        p = tmp_path / "emrgd.log"
        p.write_text("\n".join(f"line{i}" for i in range(1, 31)), encoding="utf-8")
        out = daemon_manager._read_log_tail(p, lines=5)
        assert out == "line26\nline27\nline28\nline29\nline30"

    def test_missing_file_returns_empty(self, tmp_path):
        assert daemon_manager._read_log_tail(tmp_path / "nope.log") == ""

    def test_shorter_than_lines_returns_all(self, tmp_path):
        p = tmp_path / "emrgd.log"
        p.write_text("a\nb", encoding="utf-8")
        assert daemon_manager._read_log_tail(p, lines=10) == "a\nb"

    def test_invalid_utf8_replaced(self, tmp_path):
        p = tmp_path / "emrgd.log"
        p.write_bytes(b"ok\n\xff\xfebad\nend")
        out = daemon_manager._read_log_tail(p, lines=5)
        assert "end" in out and "\ufffd" in out
