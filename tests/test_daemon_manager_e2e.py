"""DaemonManager end-to-end tests (real daemon, mocked LLM).

⚠️ 不经 ensure_connected() 的完整路径（它会读真实 ~/.emrg/emrgd.port 连错
daemon）——先复用 test_ws_e2e 的 _boot_server 起隔离 daemon，再对
ensure_connected 的三个内部调用打桩（check_and_restart / is_running /
connect_to_server），验证 DaemonConnection 全链路：ping → list_models →
send_task 流式。

跨文件复用：from tests.test_ws_e2e import _make_config, _boot_server,
_make_fake_chat_stream。
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import sys

# R123 (#401) 后 Windows 已有原生 TUI，但 e2e 测试 spawn 真实 daemon + asyncio
# 事件循环行为（ProactorEventLoop 无 add_reader）与 POSIX 差异大 → Windows CI
# 冒烟阶段仍跳过（纯逻辑测试见 test_buffer/test_output/test_input_parser）。
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="e2e 测试依赖 POSIX daemon spawn/事件循环语义（Windows CI 冒烟不跑）")

from emrg.client import daemon_manager
from tests.test_ws_e2e import _boot_server, _make_config, _make_fake_chat_stream


def _run(coro):
    return asyncio.run(coro)


class TestDaemonManagerE2E:
    def test_ensure_connected_ping_list_models(self):
        async def _test():
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                server, task, cleanup = await _boot_server(tmp)
                try:
                    # Stub the lifecycle internals so ensure_connected does not
                    # touch the real ~/.emrg port file or spawn a daemon.
                    with patch.object(daemon_manager, "check_and_restart_if_stale",
                                      new_callable=AsyncMock), \
                         patch.object(daemon_manager, "is_running", return_value=True):
                        conn = await daemon_manager.ensure_connected()
                        assert isinstance(conn, daemon_manager.DaemonConnection)

                    # ping → pong structure
                    await conn.send_command("ping")
                    pong = await conn.recv(timeout=5)
                    assert pong is not None
                    assert "identity" in pong
                    assert "uptime_seconds" in pong
                    assert "evolution_count" in pong

                    # list_models → models list
                    await conn.send_command("list_models")
                    resp = await conn.recv(timeout=5)
                    assert resp is not None
                    assert resp.get("type") == "models_list"

                    await conn.close()
                finally:
                    await cleanup()

        _run(_test())

    def test_send_task_streaming(self):
        async def _test():
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                server, task, cleanup = await _boot_server(tmp)
                try:
                    with patch.object(daemon_manager, "check_and_restart_if_stale",
                                      new_callable=AsyncMock), \
                         patch.object(daemon_manager, "is_running", return_value=True):
                        conn = await daemon_manager.ensure_connected()

                    # streaming task → delta frames → done
                    await conn.send_task(session_id="e2e-session",
                                         cwd=str(tmp), prompt="你好", stream=True)
                    frames = []
                    while True:
                        frame = await conn.recv(timeout=5)
                        assert frame is not None, "timeout waiting for stream"
                        frames.append(frame)
                        if frame.get("done"):
                            break

                    types = [f.get("type") for f in frames]
                    assert any(f.get("delta") or "content" in f for f in frames)  # streamed text
                    assert any("tool_name" in f for f in frames)  # tool lifecycle
                    assert frames[-1]["done"] is True
                    await conn.close()
                finally:
                    await cleanup()

        _run(_test())
