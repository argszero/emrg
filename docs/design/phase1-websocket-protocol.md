# Phase 1 设计：协议 WebSocket 化

> 主路线图：[`roadmap.md`](roadmap.md) Phase 1
> 本文是该阶段的完整设计文档：现状分析、目标形态、改动清单、测试策略、验收标准。
> 关联文档：`packaged-installer.md` §7（GUI 复用本协议）、§8（远程 wss 建立在本协议之上）。

---

## 1. 现状：字节流协议全貌

### 1.1 三层结构

```
┌─ 消息层  protocol.py      JSON 对象（TaskRequest/ToolStart/ToolEnd/…）
├─ 帧层    framing.py       4 字节大端长度前缀 + body（16MB 上限）
└─ 传输层  connect.py       UDS (Unix) / Named Pipe (Windows)
```

### 1.2 调用面盘点（WebSocket 化必须覆盖的全部连接点）

| 位置 | 用途 | 帧调用 |
|------|------|--------|
| `server/daemon.py` | 服务端：`_handle_client(reader, writer)` 读循环 + **63 处** `self._send(writer, data)` 流式响应 + **1 处裸 `write_frame(writer, err)`**（daemon.py:226 JSON 解析错误分支——`_send` 之外，改造为 `self._send(ws, {...})`） | `read_frame(reader)` / `write_frame(writer, …)` / `_send(writer, …)` |
| `client/app.py` | TUI：`read_server()` 读循环 + **30+ 处** `write_frame(writer, …)` 直接调用 + 多处 `read_frame` | 发送任务/cancel/rant + 接收流式帧 |
| `server/scheduler.py` | 演化引擎：以内部客户端身份连 daemon | `write_frame` + `read_frame` |
| `__main__.py` | CLI：`server stop` / `ping` / `rant` | `write_frame` + `read_frame` |
| `connect.py` | 传输层：`start_server` / `connect_to_server` / `is_server_running_sync` / `cleanup_server` / `get_server_path` | — |

### 1.3 关键约束

- `daemon.py` 有 **13 个函数**接收 `writer` 参数并传递（含发送入口 `_send`）：`_handle_client`(196) / `_send`(323) / `_process_message`(523) / `_run_chat_once`(951) / `_run_tool_loop`(1010) / `_handle_compact`(1514) / `_handle_list_sessions`(1722) / `_handle_list_projects`(1732) / `_handle_list_models`(1763) / `_handle_set_model`(1794) / `_handle_resume_session`(1837) / `_handle_list_memories`(1868) / `_handle_read_memory`(1911)。（注：`_generate_session_title`(1958) **不带 writer**，无需改。）`_send(writer, data)` 返回 `bool`（False = 客户端断开，调用方停止）——63 处调用中 **7 处检查返回值**（流式/工具循环，断开即停），56 处不检查（`_process_message` 一次性响应）。
- `client/app.py` 的 `read_server()` 是核心读循环，含**超时轮询（`wait_for(…, 0.1)`）+ 断线重连**逻辑。
- 消息边界语义：一条 JSON 消息 = 一个帧（长度前缀保证消息不被截断，解决过 64KB NDJSON 限制）。

---

## 2. 目标

1. **协议统一为 WebSocket**：JSON 消息直接映射为 WS 消息（每个 WS 消息 = 一个 JSON 对象），不再自造长度前缀帧。
2. **本机/远程同一套传输逻辑**：本机 `ws://127.0.0.1:<port>`、远程 `wss://<host>:<port>`——差异只在"要不要 TLS + token"，URL 规则一条，无平台分叉。
3. **远程就绪**：同一协议未来加 TLS + token 即 `wss://`（Phase 5），本机路径零改动。
4. **换路彻底，不留旧路痕迹**：删除 `framing.py`（长度前缀层）与 `WsStream`（StreamReader 伪装）——字节流时代的产物在 WS 时代没有存在意义。业务代码直接用 WS 原生 `ws.recv()` / `ws.send()`。

---

## 3. 核心设计：WS 原生语义（彻底换路）

### 3.1 设计原则

长度前缀帧（`framing.py`）、`(reader, writer)` 参数形态、`read_frame`/`write_frame` 函数名——**全部是字节流时代的产物**。WS 时代它们没有存在意义：

- **`framing.py` 删除**——"帧"是字节流的词，WS 的消息边界由协议保证，长度前缀是死代码；
- **`read_frame`/`write_frame` 不存在**——业务直接 `ws.recv()` / `ws.send()`（WS 原生消息语义，一条消息 = 一个 JSON 帧）；
- **`WsStream` 适配层不需要**——不再伪装 StreamReader，handler 直接收 ws 连接对象；
- **`(reader, writer)` 元组不存在**——`connect_to_server()` 返回单个 ws 对象。

> **原则**：换路要彻底，一次性付清迁移成本（改调用点），而不是用适配层把旧 API 续命——续命就是给未来留垃圾补丁。

**改动面其实是收敛的**（并非 40+ 处）：daemon 的 63 处 `_send(writer, …)` 全部收敛在 `_send` 一个入口，机械替换参数名 `writer→ws` 即可，pytest 兜底。

### 3.2 WS 原生形态（目标代码长什么样）

**服务端（daemon.py）**——`_handle_client` 直接收 ws，首帧认证：

```python
# websockets 12.x asyncio 接口：serve(handler, host, port)，handler 收 1 参 (ws)
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
import secrets, json, asyncio

async def _handle_client(self, ws):           # 签名从 (reader, writer) 改为 (ws)
    # ⚠️ _handle_client 直接作 websockets handler（收 1 参 ws）——无需 _ws_handler 包装层
    last_session_id = last_cwd = None
    _tool_task = _cancel_event = None
    try:
        # ── 首帧认证（本机/远程统一）──
        # 必须有超时：否则恶意/异常客户端连接后不发 auth，此协程永久悬挂（socket+协程泄漏）
        try:
            auth_msg = await asyncio.wait_for(ws.recv(), timeout=10)
            auth = json.loads(auth_msg)
        except (ConnectionClosed, json.JSONDecodeError, asyncio.TimeoutError):
            await ws.close(); return
        # ⚠️ auth 必须是 dict：json.loads 可能返回 list/str，.get() 会 AttributeError
        if not isinstance(auth, dict) or auth.get("type") != "auth" or not secrets.compare_digest(
            str(auth.get("token", "")), self._auth_token
        ):
            logger.warning("auth failed — rejecting connection")
            await ws.close(); return

        # ⚠️ 认证成功必须发确认帧：否则客户端无法感知认证结果（见 §3.3 客户端 auth 验证）。
        # 没有 auth_ok，客户端 connect_to_server 只能"发完 auth 就返回"——认证失败时服务端
        # close，客户端要等到首次业务交互才抛 ConnectionClosed，会被 _reconnect 当普通断线无限重连。
        await self._send(ws, {"type": "auth_ok"})

        while True:
            try:
                msg = await ws.recv()         # 一条 WS 消息 = 一个 JSON 帧
            except ConnectionClosed:          # 断开语义：异常，而非 None
                break
            try:
                data = json.loads(msg)        # WS 消息必非空，无空消息分支
                # ⚠️ 显式检查 dict：json.loads 合法 JSON 可能是 list/str（如 [1,2]、"hi"），
                # 现状隐式依赖 msg.get AttributeError → 外层 except 关连接；WS 版显式处理更清晰
                if not isinstance(data, dict):
                    await self._send(ws, {"error": "message must be a JSON object"})
                    continue
            except json.JSONDecodeError as e:
                await self._send(ws, {"error": f"invalid json: {e}"})
                continue
            ...
            await self._send(ws, {...})       # 63 处调用点仅参数名 writer→ws
    finally:
        if _tool_task and not _tool_task.done():   # 断连时取消运行中的工具任务（现状语义）
            if _cancel_event:
                _cancel_event.set()
            _tool_task.cancel()
            try:
                await _tool_task
            except asyncio.CancelledError:
                pass
        try:
            await ws.close()          # 现状 writer.close()+wait_closed() 有 try/except 保护，保持
        except Exception:
            pass
        # ⚠️ 保留断连记忆整合（现状 daemon.py:316-321 关键逻辑，勿漏）：
        if last_session_id and last_cwd:
            try:
                await self._consolidate_session_memories(last_session_id, Path(last_cwd))
            except Exception:
                logger.debug("session memory consolidation failed", exc_info=True)
```

> **为什么首帧必须认证**：loopback 端口对本机其他用户可见，token（存 600 权限文件）是对齐原 UDS 文件权限隔离的唯一机制。**不校验 = 认证形同虚设**。本机与远程（Phase 5）走同一认证逻辑——本机 token 客户端自动读取，远程 token 显式配置。

> **服务端健壮性已验证**：`_handle_client` 内未捕获异常 → websockets 关闭该连接（客户端收 `ConnectionClosedError`），**服务端继续运行**（`server.is_serving()` 仍 True，实测 17.x）——与现状"外层 `except Exception` 兜底关连接"行为一致，无需额外 try/except 包裹整个读循环。但非 dict JSON（list/str）应显式检查（见上），不依赖隐式 AttributeError。

```python
async def _send(self, ws, data: dict) -> bool:   # 内部从 write_frame 改为 ws.send
    """发送一条 JSON 消息。返回 False 表示客户端已断开。"""
    try:
        await ws.send(json.dumps(data, ensure_ascii=False))
        return True
    except (ConnectionClosed, OSError):
        # ConnectionClosed 覆盖：客户端主动关 / 服务端关（ConnectionClosedOK/Error 均子类，实测）；
        # OSError 兜底：半开连接等边缘场景。两类都不是 OSError 的 ConnectionClosed 主类在 12.x 需确认，
        # 稳妥起见两者都捕（现状 _send 捕 ConnectionResetError/BrokenPipeError/OSError）
        logger.debug("client disconnected during send")
        return False
```

**客户端（app.py / scheduler.py / __main__.py）**——直接用 ws：

```python
# 连接：返回单个 ws 对象（不再是 (reader, writer) 元组）
ws = await client_connect_to_server()

# 发送（connect_to_server 内部已发 auth 首帧，这里是认证后的第一条业务消息）
await ws.send(json.dumps({"type": "ping"}))

# 读循环（read_server）——WS 消息必非空，无空消息分支
while True:
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=0.1)
    except asyncio.TimeoutError:
        continue
    except ConnectionClosed:          # 断开 → 重连
        await _reconnect()
        continue
    data = json.loads(msg)
    ...

# 关闭
await ws.close()
```

**调用点变化总览**：

| 现状 | 改造后 |
|------|--------|
| `from emrg.framing import read_frame, write_frame` | 删除 import（`emrg/framing.py` 删除） |
| `reader, writer = await connect_to_server()` | `ws = await connect_to_server()`（**全部解包点**：__main__.py:136/166/241、app.py:82/293/421、scheduler.py:321——后两者经 `client_connect_to_server()`/`connect_to_server()` 返回） |
| `frame = await read_frame(reader)` + `json.loads(frame)` | `data = json.loads(await ws.recv())` |
| `await write_frame(writer, json.dumps(x).encode())` | `await ws.send(json.dumps(x))`（发 str 或 bytes 均可——服务端 `json.loads` 两者兼容；scheduler 现状发 `task_msg.encode()` bytes，可保留） |
| `_send(writer, data)`（63 处） | `_send(ws, data)`（仅参数名） |
| `frame is None`（断开） | `except ConnectionClosed`（断开） |
| `writer.close()` + `await writer.wait_closed()` | `await ws.close()`（__main__ 3 处：143/169/257；app.py 3 处：85/416/1924） |
| daemon `shutdown` 分支（daemon.py:884-892）：`writer.close()` + `wait_closed()` | `await ws.close()`；**`self._server.close()` 不变**（websockets `Server.close()` 是同步的，已验证 17.x）——注意 `ws.close()` 是 async coroutine，与现状 `writer.close()` 同步不同 |

> **断连语义的分端差异（勿照抄）**：三端读循环对 `ConnectionClosed` 的处理不同——
> - **client（TUI）**：`except ConnectionClosed: await _reconnect()`（重连）
> - **scheduler（演化）**：`except ConnectionClosed: break`（正常收尾，不重连——演化周期完成即断开；现状 `frame is None: break` 语义保留）
> - **daemon（服务端）**：`except ConnectionClosed: break`（客户端走了，结束本连接处理）
> 实现时按各端现状语义对应转换，不要统一成重连。

> **`_reconnect()` 的重连语义（现状 app.py:405-430）**：关闭旧连接 → 循环 `client_connect_to_server()`（内部已发 auth 首帧）→ **成功后立即发 `{"type":"ping"}` 验证**（认证后第一条业务消息）→ 更新状态。改造后仅 `write_frame(writer, …)` → `ws.send(...)`，流程不变。注意：`read_server` 里的 `reader`/`writer` 是 **nonlocal 变量**（`_reconnect` 闭包引用），改造后为单个 `ws` 变量，`_reconnect` 闭包同样引用它——nonlocal 语义保留。

**scheduler（演化引擎）改造示例**（读循环无超时、断连 break——现状语义保留）：

```python
# 现状 scheduler.py:345-366
task_bytes = task_msg.encode()
await write_frame(writer, task_bytes)
while True:
    frame = await read_frame(reader)
    if frame is None:
        break
    resp = json.loads(frame.decode())
    if resp.get("done"): ... break
    ...

# 改造后
ws = await connect_to_server()               # 内部已发 auth 首帧
await ws.send(task_msg)                       # 发 str 或 bytes 均可（task_msg 现状是 dict，直接 json.dumps）
while True:
    try:
        resp = json.loads(await ws.recv())
    except ConnectionClosed:                  # 断开 = 正常收尾（现状 frame is None: break 语义）
        break
    if resp.get("done"): ... break
    ...
# finally: await ws.close()
```

> scheduler 断开后走"git HEAD 未变 → empty_cycles+1"（现状语义）——改造后 ConnectionClosed → break → 同样路径，**语义一致**。断开被当作"空周期"是现状行为，非本次改动引入。

### 3.3 连接面变化

**服务端（daemon.py `serve()`）**：

```python
# 现状
self._server = await start_server(self._handle_client)   # connect.py 封装 asyncio.start_unix_server
await self._server.serve_forever()

# 改造后（统一 TCP loopback，Unix/Windows 同一路径）——serve() 完整形态，其余逻辑保持现状
# ⚠️ 本代码块是完整 serve()：除标注的改动外，PID 文件、scheduler、finally 清理全部保留——
#    照抄实现时勿只抄监听 6 行（会丢掉演化引擎与清理逻辑）。

# ── PID 文件创建/僵尸检测（现状 daemon.py:117-168 整体保留）──
# ⚠️ 唯一改动：僵尸检测里的 sock_path = runtime_dir / "emrgd.sock" → "emrgd.port"
#   现状: sock_path = runtime_dir / "emrgd.sock"
#         if not sock_path.exists(): → force-kill 旧 daemon（socket 没了=僵尸）
#   改造: sock_path = runtime_dir / "emrgd.port"   ← 关键！
#         if not sock_path.exists(): → force-kill 旧 daemon（port 文件没了=僵尸）
#   不改这里 = 严重 bug：emrgd.sock 永远不存在 → 每次启动都 force-kill 活着的旧 daemon
#   （force-kill 分支、stale PID 清理、拒绝二次启动等逻辑原样保留，不赘述）

# ── 监听层（唯一实质改动：start_server → websockets serve）──
from websockets.asyncio.server import serve   # ← asyncio 新接口，handler 收 1 参 (ws)
self._server = await serve(self._handle_client, host="127.0.0.1", port=0,
                           max_size=16 * 1024 * 1024)  # ⚠️ 服务端也要设（默认 1MB，接收大消息会断连）
port = self._server.sockets[0].getsockname()[1]
self._auth_token = secrets.token_urlsafe(32)          # 实例属性，_handle_client 校验用
_atomic_write_bytes(f"{port}\n{self._auth_token}", config_dir() / "emrgd.port", mode=0o600)  # 见下

# ── scheduler 启动（现状 daemon.py:176-177，位置与顺序不变）──
self._scheduler = TaskScheduler(self.identity)
self._scheduler.load_and_start()

try:
    await self._server.serve_forever()
except asyncio.CancelledError:
    pass
finally:
    # ── 清理块（现状 daemon.py:183-198 整体保留，一行不改）──
    self._scheduler.stop_all()
    await self._scheduler.wait_all()
    await self.llm.close()
    cleanup_server()          # 改造后删 emrgd.port（含 token）——语义与现状删 socket 文件一致
    # PID 文件删除（现状逻辑保留，不赘述）
```

> **⚠️ websockets `serve()` 返回时已 `is_serving()`**（实测 17.x）——**不需要**像 asyncio 那样先 `start_serving()`/`serve_forever()` 才开始监听，`serve()` 一返回即可接受连接。文档保留 `await server.serve_forever()` 是**阻塞保持 daemon 运行**（与现状 asyncio 语义一致），不是启动监听。`server.close()`（同步）后 `serve_forever()` 返回——shutdown 流程（daemon.py:892）兼容，已验证与 asyncio 行为一致。

> **`_atomic_write_bytes` 需新增**：现有 `emrg/server/atomic.py` 的 `atomic_write_yaml` 只支持 `list[dict]` + YAML（不适用文本 port 文件）。需在 atomic.py 新增通用函数：
> ```python
> def atomic_write_bytes(data: str, target: Path, *, mode: int = 0o600) -> None:
>     """原子写文本：tmp 文件 + os.replace + chmod（复用 atomic_write_yaml 的 mkstemp 模式）。"""
>     target.parent.mkdir(parents=True, exist_ok=True)
>     fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".atomic_", suffix=".tmp")
>     try:
>         with os.fdopen(fd, "w", encoding="utf-8") as f:
>             f.write(data)
>         os.chmod(tmp_path, mode)
>         os.replace(tmp_path, target)
>     except OSError:
>         try: os.unlink(tmp_path)
>         except OSError: pass
> ```
> 同时 `cleanup_server()` 删 port 文件、`get_server_path()` 返回 port 路径都依赖此文件（§4 归宿表）。
```

**客户端（connect.py `connect_to_server()`）**：

```python
# 现状
return await asyncio.open_unix_connection(str(sock_path))   # Unix
return await asyncio.open_connection(host, port, path=...)  # Windows

# 改造后（统一，返回单个 ws）——统一用 asyncio 接口（见 §6，避免顶层接口版本漂移）
from websockets.asyncio.client import connect
port, token = (config_dir() / "emrgd.port").read_text().split()
ws = await connect(
    f"ws://127.0.0.1:{port}",
    max_size=16 * 1024 * 1024,   # ⚠️ 客户端也要设！默认 1MB，不设则接收大消息会断连
)
await ws.send(json.dumps({"type": "auth", "token": token}))  # 首帧认证
# ⚠️ 验证认证结果（闭环）：服务端认证成功发 auth_ok 确认帧（§3.2），失败直接 close。
# 等 auth_ok：收到 → 连接就绪；ConnectionClosed（被拒）/ 超时（服务端无响应）→ 抛 AuthError。
try:
    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
except (ConnectionClosed, asyncio.TimeoutError):
    await ws.close()
    raise AuthError("authentication failed — check token / daemon version")
if ack.get("type") != "auth_ok":          # 防御：意外帧也视为失败
    await ws.close()
    raise AuthError(f"unexpected auth response: {ack!r}")
return ws
```

> **客户端 auth 失败处理（auth_ok 闭环，勿省略）**：`connect_to_server()` 发完 auth 后**必须等待 auth_ok 确认帧**（§3.2 服务端认证成功即发；认证失败服务端 close → 客户端 recv 抛 ConnectionClosed → 抛 `AuthError`）。没有这一步，认证失败会被 `_reconnect()` 的 `except Exception: continue` 当成普通断线**无限重连**——token 不匹配是配置/安装问题，重连只会循环刷日志。`AuthError` 需在客户端侧定义（`connect.py` 内，如 `class AuthError(Exception)`），并在各调用点与普通断线区分：`_reconnect()`（app.py）与 `__main__.py` 的 `except Exception: continue` 循环中，`except AuthError: raise`（报错退出）；`ConnectionRefusedError`/`OSError`/`FileNotFoundError`（daemon 未启动）则继续重连。TUI 的 `_reconnect()` 只应在"daemon 重启/网络瞬断"时触发，不该在认证失败时触发。

> **连接失败异常已验证兼容**：`websockets.connect` 连接被拒抛 `ConnectionRefusedError`（`OSError` 子类）——scheduler.py:323、__main__.py:137/242、app.py:131 的 `except (ConnectionRefusedError, FileNotFoundError, OSError)` 捕获**无需改动**。`FileNotFoundError` 在新模式下对应"port 文件缺失"（daemon 未运行），语义一致。实测 websockets 17.x 确认。

> **⚠️ `ConnectionClosed` 不是 `OSError` 子类（严重，实测确认）**：MRO 是 `ConnectionClosed → WebSocketException → Exception`——**现有 `except (OSError, …)` 捕不到它**。受影响（**__main__.py 共 4 处**）：
> - `__main__.py:151`（`_send_shutdown` 的 except）——`emrg server stop` 时服务端关闭连接，客户端 `ws.recv()` 抛 ConnectionClosed，**若不加捕获会未捕获异常崩溃**；
> - `__main__.py:187`（**`_stop_daemon` 的外层 except**——`_get_pid`(165-173) 内部无 except，`asyncio.run(_get_pid())` 抛 ConnectionClosed 冒泡到此，但 187 的 `except (OSError, …)` 捕不到）——fallback SIGTERM 路径（daemon 不响应 shutdown 时）会崩；
> - `__main__.py:260`（`_send_rant` 读响应后的 except）；
> - （还有 app.py 读循环的 `except ConnectionClosed` 已按 §3.2 处理，不属此类）
> 这些 except 需**追加 `ConnectionClosed`**（`from websockets.exceptions import ConnectionClosed`）到元组；
> - 对比：`ConnectionRefusedError`（连接被拒，发生在握手前）是 OSError 子类，现有捕获兼容——**两类异常要区分对待**。

> **`wait_for(ws.recv(), timeout)` 超时语义已验证**：`asyncio.wait_for(ws.recv(), timeout)` 超时后**连接仍可用，可继续 recv**（实测 17.x）——这是 TUI `read_server` 的 0.1s 轮询、`__main__` rant 的 5s 超时读能工作的前提。`_send_rant` 改造后：`asyncio.wait_for(ws.recv(), timeout=5)` + `await ws.close()`，语义不变（__main__.py:256-260）。

**健康探测 `is_server_running_sync()`**：改 TCP connect 到 port 文件中的端口（Unix/Windows 统一）。**语义：只探测"daemon 是否活着"，不携带 token、不做认证**——探测是低成本存在性检查；认证发生在真实连接的首帧。该函数语义（"daemon 是否活着"）不变。

> **`is_server_running_sync` 是同步函数（现状 connect.py:105）**——在事件循环外调用（`app.py:54` 的 `_try_connect`），不能 `await`。改造后保持同步：用 `socket.create_connection(("127.0.0.1", port), timeout)`，**只读 port 文件第一行（端口），不读 token**（`read_text().splitlines()[0]`）。Windows 现状恒返回 False，改造后统一走 TCP 探测，可去除 `os.name == "nt"` 分支。

### 3.4 本机连接：TCP loopback + 端口文件 + token（全平台统一）

| | 本机（Unix 与 Windows 完全一致） |
|--|------|
| 传输 | `ws://127.0.0.1:<port>`（daemon 启动时动态分配） |
| 端口/token 位置 | `~/.emrg/emrgd.port`（`port\n token`，600 权限） |
| 安全边界 | loopback（仅本机）+ **token 首帧认证**（对齐原 UDS 文件权限边界） |
| 认证流程 | 客户端读 port 文件 → 连接 → 首帧发 `{"type":"auth","token":…}` → daemon 校验后进入正常协议 |

> **为什么本机也要 token**：loopback TCP 端口对本机其他用户可见，token（存 600 权限文件）对齐了原 UDS 的文件权限隔离。同时它与远程（Phase 5）共用同一认证机制——本机 token 自动读取用户无感，远程 token 显式配置。**一套认证逻辑，两种来源**。

> **实现注意（port 文件）**：`emrgd.port` 写入用**原子写**（tmp 文件 + rename），避免客户端在写入中途读到半个文件（`split()` 失败）。权限 600（`os.chmod`，`write_text` 默认 644 受 umask 影响）。若读取时文件损坏/缺失，视为"daemon 未运行"处理。

---

## 4. 改动清单

| 文件 | 改动 | 规模 |
|------|------|------|
| `emrg/framing.py` | **删除**（长度前缀层废弃，WS 消息边界取代） | 删除 |
| `emrg/connect.py` | **重写**：TCP loopback + port 文件 + token 首帧（无平台分叉）+ **auth_ok 验证（新增 `AuthError` 异常）** + 健康探测，返回单个 ws；`cleanup_server`/`get_server_path` 改造（见下） | ~130 行 |
| `emrg/server/daemon.py` | `serve()` 监听换 websockets；**12 个函数签名 `writer` 参数 → ws 对象**（除 `_send` 外的 12 个：`_handle_client` / `_process_message` / `_run_chat_once` / `_run_tool_loop` / `_handle_compact` / `_handle_list_sessions` / `_handle_list_projects` / `_handle_list_models` / `_handle_set_model` / `_handle_resume_session` / `_handle_list_memories` / `_handle_read_memory`，见 §1.3 行号）+ `_send` 内部 `write_frame→ws.send` + 首帧认证校验；63 处 `writer→ws` 机械替换 | ~100 行改动 |
| `emrg/client/app.py` | `read_server` 读循环改 `ws.recv()`；断线 `None→ConnectionClosed`；`(reader,writer)→ws`；`client_connect_to_server()`（app.py:135）返回单个 ws，调用点 293/421 解包改 `ws =`；**`_check_and_restart_if_stale`（app.py:69-133）适配**：`Path(server_path).exists()` 的 socket 检查 → port 文件存在检查（注意 port 文件存在≠daemon 活着，是僵尸残留可能，靠后续 ping 判定）；**`if os.name != "nt"` 分支可去除**（统一 TCP loopback 后 Windows 也有 port 文件，无需平台分支）；`reader,writer=connect_to_server()` → `ws=`；`write_frame(ping)` → `ws.send(ping)`；`writer.close()+wait_closed()` → `await ws.close()` | ~45 行改动 |
| `emrg/server/scheduler.py` | 演化客户端改 `ws.recv()` / `ws.send()` | ~10 行 |
| `emrg/server/atomic.py` | **新增** `atomic_write_bytes(data, target, mode)`——现有 `atomic_write_yaml` 只支持 list[dict]+YAML，port 文件是文本需通用版本（§3.3） | +10 行 |
| `emrg/__main__.py` | CLI（shutdown/ping/rant）改 `ws.recv()` / `ws.send()`；**3 处 except 追加 `ConnectionClosed`**（`_send_shutdown`:151 / `_stop_daemon` 外层:187——`_get_pid` 内部无 except，ConnectionClosed 冒泡到此 / `_send_rant`:260——`ws.recv()` 在服务端关连接时抛 ConnectionClosed，非 OSError 子类，现有 except 捕不到） | ~20 行 |
| `pyproject.toml` | 增加依赖 `websockets>=12`（**实施第一步：`uv add websockets`**——当前项目 venv 未装，所有验证需在装好后重跑） | 1 行 |
| `tests/test_framing.py` | **删除**（framing.py 删除） | 删除 |
| `tests/test_connect.py` | **重写**：port 文件读写、token 首帧、健康探测 | 重写 |
| `tests/test_ws_e2e.py` | **新增**：端到端 WS 连接全流程（真实 daemon + websockets） | 新增 |
| `tests/test_daemon.py` | **零改动**（37 个单测：prompt 构建、项目发现——不涉及传输层） | 0 行 |

> **为什么 client/app.py 不是零改动**：换路彻底意味着 `read_frame`/`write_frame`/`(reader,writer)` 全部消失，业务代码改为 WS 原生 API。daemon 的 63 处 `_send` 收敛于一个入口（机械替换），client 的读循环与发送点是有限几处，全部在可控范围内。pytest 兜底回归。

**connect.py 公共函数归宿**（5 个函数逐一交代，不留隐式行为）：

> **daemon 启动函数（两套，均零改动）**：`client/app.py:58` 的 `start_server_daemon()`（async，客户端启动用）与 `__main__.py:119` 的 `_start_daemon_background()`（同步 Popen，`emrg server restart` 用）——两者都调 `cleanup_server()` + 启动 `python -m emrg.server` + 轮询 `is_server_running()`。改造后**子进程启动命令不变**，仅它们依赖的 `is_server_running()`/`cleanup_server()` 内部实现变（§3.3）。无需改动这两处本身。

| 函数 | 改造后 | 调用点 |
|------|--------|--------|
| `start_server(handler)` | **删除**——daemon `serve()` 直接调 `websockets.asyncio.server.serve`，不再经 connect.py 包装 | daemon.py:170 |
| `connect_to_server()` | **重写**：读 port 文件 → 连接 → auth 首帧 → 返回单个 ws | app / scheduler / __main__ |
| `is_server_running_sync()` | **重写**：TCP connect 到 port 文件端口，不带 token 不做认证 | app.py:54 |
| `cleanup_server()` | **改语义**：原删除 socket 文件 → 现删除 `~/.emrg/emrgd.port` 文件（daemon 停止/重启时清理） | daemon.py:187、app.py:60/117/138、__main__.py:121/183 |

> **`cleanup_server` 的调用时序**：`_check_and_restart_if_stale`（app.py:117）在 SIGTERM 旧 daemon 后调用它——删 port 文件后，`is_server_running()`（app.py:120，TCP connect 探测）会因 port 文件缺失而 FileNotFoundError → False（误判"已死"）。但此时旧 daemon 正在退出、新 daemon 将写新 port 文件，时序上可接受（现状删 socket 文件同理）。实现时确认此路径不引入竞态。
| `get_server_path()` | **改语义**：原返回 socket 路径 → 现返回 `~/.emrg/emrgd.port` 路径（`_check_and_restart_if_stale` 用它判断 daemon 是否已启动） | app.py:71 |

> **为什么 `start_server` 删除而非保留**：websockets 的 serve 返回对象类型与 asyncio 不同，包装层只会变成"假抽象"（同 §3.1 原则）。daemon 直接用 `websockets.asyncio.server.serve`，connect.py 只保留客户端侧函数。

---

## 5. 测试策略

### 5.1 分层

| 层 | 测试 | 覆盖 |
|----|------|------|
| 单元 | `test_connect.py`（重写） | port 文件读写、token 首帧、健康探测（无平台分叉） |
| 集成 | `test_ws_e2e.py`（新增） | 起真实 daemon（WS）→ ping / task 流式 / tool_start/tool_end / cancel / rant / compact |
| 回归 | 既有 429 项（删 test_framing，其余保留） | 全绿（`test_daemon.py` 等单测不受传输层影响） |

> **⚠️ e2e 测试必须 mock LLM**（关键，勿漏）：起真实 daemon 后发 task 会触发 `_run_tool_loop` → `self.llm.chat_stream()` → **真实 LLM API 调用**（花钱、慢、CI 无网失败）。现状测试的处理方式（test_memory_reflection.py:19-22）：
> ```python
> server.llm = AsyncMock()   # 替换整个 LlmClient
> ```
> e2e 测试同样构造 `EmrgServer(LlmConfig(...))` 后 `server.llm = AsyncMock()`，再 `await server.serve()`。**mock `chat_stream` 需 yield 与 llm.py 一致的 delta 格式**（`_run_tool_loop` 消费这些字段，daemon.py:1125-1155）：
> ```python
> async def fake_chat_stream(messages, tools=None):
>     yield {"content": "你好", "tool_calls": None, "finish_reason": None, "usage": None}
>     yield {"content": None, "tool_calls": [
>         {"index": 0, "id": "call_1", "function": {"name": "bash", "arguments": '{"command":"echo hi"}'}}
>     ], "finish_reason": "tool_calls", "usage": None}
>     yield {"content": "完成", "tool_calls": None, "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
> server.llm.chat_stream = fake_chat_stream
> ```
> **验收用例里所有"流式 task / tool_start / tool_end"都是 mock LLM 的产物，不是真实 API 响应**。

> **⚠️ e2e 测试的环境隔离**（关键，勿漏）：`serve()` 会调用 `self._scheduler.load_and_start()`（daemon.py:176-177），读 `~/.emrg/tasks.yml` 启动演化调度器——**测试会污染真实用户配置**。处理：
> - 测试前 mock `TaskScheduler`：`server._scheduler = AsyncMock()` 或 `patch` 掉 `load_and_start`；
> - 或设置隔离环境变量让 `config_dir()` 指向临时目录（如 `EMRG_HOME`/monkeypatch `config_dir`），port 文件、tasks.yml 全进 tmp；
> - 测试结束 `await server.close()` 确保 socket/port 清理。
> 参考现状 test_memory_reflection.py 的 `EmrgServer.__new__` 绕过 `__init__`（但它不测 serve()，e2e 需要真实 serve，所以用上述 mock 调度器的方式）。

### 5.2 端到端验收用例

- [ ] `ping` → pong（identity/uptime/model）
- [ ] **认证**：无 token / 错 token 连接被拒绝；正确 token 通过后进入正常协议
- [ ] **认证健壮性**：auth 帧非 JSON / 非 dict（如 `[1,2]`、`"hi"`）被拒且服务端不崩；连接后不发 auth 的连接在 10s 内被关闭（无悬挂协程）
- [ ] 非法 JSON 消息 → 返回 error 帧（服务端不崩）
- [ ] **非 dict JSON 消息**（`[1,2]`/`"hi"`）→ 返回"message must be a JSON object" error 帧（服务端不崩、连接不关）
- [ ] 流式 task：多段 delta → done，工具调用 tool_start → tool_end → 最终回答
- [ ] **大消息**：构造 >1MB 的 JSON 消息（客户端 → 服务端，如超大 `content` 字段，绕过工具层直接发 `write_frame` 等价物 `ws.send`）往返无截断。**注意**：工具输出被 `MAX_OUTPUT_CHARS=200_000` 截断（bash_tool.py:16，UTF-8 最坏 ~800KB < 1MB），**无法通过真实工具产生 >1MB 消息**——大消息测试必须用构造 payload（这正是 max_size 保护的对象，对齐现状 `_MAX_FRAME_BYTES` 16MB）
- [ ] CJK 消息往返
- [ ] ESC 中断（cancel 消息）→ cancelled 响应，工具循环终止
- [ ] 断线重连：杀 daemon → client `read_server` 走 `_reconnect()` → 拉起 → 恢复
- [ ] 演化引擎（scheduler）内部客户端正常连、跑周期、断开
- [ ] CLI：`emrg server stop`（shutdown 消息）、`emrg rant` 正常
- [ ] Windows 冒烟（CI matrix 若可用）：port 文件、token 首帧认证

### 5.3 回退保障

- 全部改动先合入 feature 分支，跑全量测试 + 手动冒烟通过后才合 master；
- 如 websockets 库出现不可解问题，回退 = git revert 全部改动（framing.py 恢复、connect/daemon/app/scheduler/__main__ 还原），恢复原长度前缀实现；
- **不留过渡空壳**：`framing.py` 直接删除而非保留 re-export——换路彻底，不留旧路痕迹。

---

## 6. websockets 库选型

| 项 | 选择 | 理由 |
|----|------|------|
| 库 | `websockets` (asyncio 实现，≥12.x) | asyncio 原生、成熟（10+ 年）、纯 Python 可打包（PyInstaller 友好）、走标准 `ws://` 路径 |
| 替代 | `aiohttp` | 太重（含 HTTP 服务端），仅需 WS 客户端/服务端 |
| 替代 | `websockets` 同步版 | 项目全 asyncio，不需要 |
| 版本锁定 | `websockets>=12` | 12.x 起 `websockets.asyncio.server.serve(handler, host, port)` 稳定 |

**版本风险**：websockets 库 API 在 10→12 间有较大变化（legacy 接口 vs asyncio 接口）。设计锁定 12.x 的 `websockets.asyncio.*` 新接口（已用 17.x 验证 API 形态）：
- **统一用 `websockets.asyncio.server.serve` / `websockets.asyncio.client.connect`**——**任何版本**（≥12）都明确是 asyncio 接口（handler 收 1 参）；
- **⚠️ `websockets.asyncio` 是 lazy 子模块，必须显式 `import websockets.asyncio` 或 `from websockets.asyncio.server import serve`**——只 `import websockets` 后 `websockets.asyncio` 是 AttributeError（实测 17.x）；
- ⚠️ **顶层 `websockets.serve` / `websockets.connect` 的接口随版本漂移**：12-13.x 是 legacy（handler 收 2 参 `(ws, path)`）、**14+ 已是 asyncio（1 参，实测 17.x `websockets.serve is websockets.asyncio.server.serve`）**。为避免版本漂移，一律显式用 `websockets.asyncio.*`；
- `server.sockets[0].getsockname()[1]` 取动态端口——`Server.sockets` 属性存在，但实现时首步验证（若 API 差异，可改为绑定固定端口或从 `server.sockets` 调试确认）；
- **客户端 `connect` 与服务端 `serve` 的 `max_size` 默认都是 1MB**——两端都要显式设 16MB（已验证 17.x 默认值）；
- **16MB 上限绰绰有余（已审计）**：所有消息来源上限——bash 工具输出 `MAX_OUTPUT_CHARS=200_000`（bash_tool.py:16）、glob 500 条、grep 200 条、项目上下文 8000 字符截断（daemon.py:482）、记忆无单独上限但单文件远小于 16MB——实际最大消息约 200KB，16MB 余量充足（对齐现状 `_MAX_FRAME_BYTES`）；
- **自动 ping/pong 保活（净收益，无需手动处理）**：websockets 默认 `ping_interval=20` / `ping_timeout=20`（实测 17.x）——两端每 20s 自动 ping，库内自动回 pong，**TUI 空闲（用户思考中）连接不会断**。现状 TCP 流式无保活（半开连接只能靠 send 失败才发现），这是 WS 化的行为增强。ping/pong 由库内部处理，**不会出现在业务 `ws.recv()` 中**，与 TUI 的 `wait_for(recv, 0.1)` 轮询、服务端首帧 `wait_for(recv, 10)` 认证超时均无冲突（认证超时 10s < ping 间隔 20s，先触发）；
- **多客户端并发（与现状一致，非新问题）**：websockets `serve` 每连接一个 handler 协程，与现状 `start_unix_server` 相同——TUI + CLI（`emrg server stop`/`rant`）+ scheduler（演化引擎）可同时连接 daemon，互不干扰；`_handle_client` 的会话状态（`last_session_id`/`last_cwd`）是协程局部变量（§3.2），无实例级串扰；
- PyInstaller 打包时验证 hiddenimport（Phase 4 处理）。

---

## 7. 与后续 Phase 的关系

| Phase | 依赖本阶段的点 |
|-------|---------------|
| Phase 2（daemon_manager 提取） | 在 WS 协议之上提取共享客户端层——协议已稳，提取无后顾之忧 |
| Phase 3（GUI） | GUI 用同一 `ws://127.0.0.1:<port>` 连接，复用 daemon_manager |
| Phase 4（打包） | websockets 进入 hiddenimports；PyInstaller 冒烟覆盖 WS 连接 |
| Phase 5（远程） | **直接加 `wss://` 监听 + TLS + token**：`serve(handler, host, port, ssl=ctx)`；客户端 `connect("wss://…", ssl=…)`。本机 `ws://127.0.0.1` 路径不受影响——这就是"协议统一"的红利 |

> **Phase 5 前瞻（images 的远程语义）**：`TaskRequest.to_dict()` 的 `images` 含本地 `path`（`protocol.py:45`），远程连接时该路径在 daemon 所在机器无效。Phase 5 需处理（图片经 WS 传输或改为引用/占位）——Phase 1 本机不受影响，标记待办。

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| websockets Server 生命周期与 asyncio 差异 | daemon 启动/关闭异常 | **已验证兼容**（实测 17.x）：`serve()` 返回即 serving（无需 start_serving）、`serve_forever()` 阻塞保持运行、`close()` 后 `serve_forever()` 返回——与现状 asyncio 行为一致（§3.3） |
| PID 文件僵尸检测漏迁移（daemon.py:137 仍查 `emrgd.sock`） | **每次启动新 daemon 都 force-kill 活着的旧 daemon**（sock 永远不存在 → `if not exists` 恒 True） | 同步改为查 `emrgd.port`（§3.3 已标注 ⚠️）；验收含"daemon 已在运行时二次启动不被杀" |
| serve() 改造只抄监听 6 行（漏 scheduler 启动 / finally 清理 / PID 文件逻辑） | 演化引擎不启动、清理逻辑丢失（llm 不关、scheduler 不停、port/PID 文件残留） | §3.3 给出**完整 serve() 形态**：监听层为唯一实质改动，scheduler 启动与 finally 清理保留现状位置（照抄完整代码块） |
| websockets API 用错接口（顶层 `websockets.serve` 随版本漂移：12-13.x legacy 收 2 参、14+ asyncio 收 1 参） | handler 签名不匹配 TypeError | 一律显式用 `websockets.asyncio.server.serve` / `websockets.asyncio.client.connect`（§6）；e2e 测试起真实 daemon 兜底 |
| daemon `shutdown` 分支漏改（`writer.close()`/`wait_closed()` 在 ws 上不存在） | shutdown 时 AttributeError | 改 `await ws.close()`；`self._server.close()` 同步不变（§3.2 调用点总览） |
| websockets 库行为差异（recv 超时、连接关闭语义、ping/pong 保活） | 流式/断线行为异常 | 端到端测试重点覆盖读循环 + `_reconnect()`；`wait_for(recv, 0.1)` 超时语义已验证；自动 ping/pong 保活（默认 20s）为净收益，库内处理不干扰业务 recv（§6） |
| 断线语义变化（`frame is None` → `ConnectionClosed` 异常） | 重连逻辑漏改 | 逐一审计读循环：daemon / client / scheduler / __main__ 四处；**scheduler/daemon 是 `break`（收尾）、client 是 `reconnect`（重连）**——按各端现状语义对应转换，勿统一成重连（§3.2） |
| 断连记忆整合漏保留（daemon `_consolidate_session_memories`） | 会话记忆不整合 | `_handle_client` finally 保留断连整合逻辑（§3.2 已标 ⚠️）；验收含"断开后会话记忆已整合" |
| 首帧认证漏实现或漏校验 | 认证形同虚设（任何本机进程可连） | `_handle_client` 首帧强制 auth 校验，失败即拒连（§3.2）；集成测试覆盖"无 token 连接被拒" |
| 认证首帧无超时（恶意客户端连而不发） | 协程+socket 永久悬挂（资源泄漏） | 首帧 `wait_for(recv, timeout=10)`，超时即拒连（§3.2） |
| 客户端 auth 失败后无限重连 | 循环刷日志、无进展（`_reconnect` 的 `except Exception: continue` 把 ConnectionClosed 当普通断线） | **auth_ok 确认帧闭环**：`connect_to_server` 等待 auth_ok，失败抛 `AuthError`；调用点 `except AuthError: raise`（报错不重连），仅 `ConnectionRefusedError`/`OSError`/`FileNotFoundError` 继续重连（§3.3） |
| port 文件写入非原子 / 权限不对 | 客户端读到半个文件 / token 泄露 | 原子写（tmp+rename）+ chmod 600（§3.4 实现注意） |
| 空消息防御逻辑残留（字节流遗留 `if not msg`） | 死代码混淆语义 | 审查所有读循环：WS 消息必非空，删除空消息分支 |
| 非 dict JSON 消息（`[1,2]`/`"hi"`）未显式检查 | 隐式 AttributeError 依赖外层兜底（行为不清晰） | `_handle_client` 显式 `isinstance(data, dict)` 检查，非 dict 返回 error 帧（§3.2）；e2e 覆盖 |
| loopback 端口被其他进程占用 | daemon 起不来 | 动态端口（port=0）+ 端口文件，天然无冲突 |
| loopback 无文件权限隔离 | 本机其他用户可连 | 本机 token 首帧认证（见 §3.4），Unix/Windows 统一，对齐原 UDS 安全边界 |
| 大消息（WS 默认 max_size=1MB）被拒 | 大消息（>1MB，仅构造 payload 可达——工具输出被 200K 字符截断）发送/接收失败 | **服务端和客户端都要** `max_size=16MB`（§3.3 两处代码均已含；客户端 `connect` 默认也是 1MB，漏设则接收大输出断连）——对齐现状 `_MAX_FRAME_BYTES` |
| token 比较用 `!=`（时序攻击） | 理论上有被猜 token 的风险 | 用 `secrets.compare_digest`（§3.2），本机低危但属安全最佳实践 |
| 演化引擎连接被 WS 握手影响 | 演化周期失败 | scheduler 走同一 connect_to_server，端到端用例覆盖 |
| 删除 framing.py 导致漏改 import | import 断裂 | 全仓 grep `framing` 确认无残留；CI 的 import 检查（`test_imports.py`）兜底 |
| 漏改 13 个函数签名（`writer` → ws，含 `_send` + 12 个传递函数） | 类型标注错误/传递链断裂 | §1.3 已列出全部 13 个函数 + 行号（`_handle_set_model`(1794) 易漏） |
| `_check_and_restart_if_stale` 漏适配（socket 存在检查、解包、close） | TUI 启动时 mtime 检查/重启逻辑失效 | §4 已列适配点；port 文件存在≠daemon 活着，靠后续 ping 判定 |
| `is_server_running_sync` 误做成 async（现状是同步函数，事件循环外调用） | 事件循环外 await 崩溃 | 保持同步：`socket.create_connection`；只读 port 第一行（§3.3） |
| `ConnectionClosed` 逃逸现有 `except (OSError, …)`（非 OSError 子类，实测 MRO: Exception） | `emrg server stop`/`rant` 未捕获异常崩溃 | __main__.py 3 处 except 追加 `ConnectionClosed`（:151 `_send_shutdown` / :187 `_stop_daemon` 外层——`_get_pid` 无内部 except / :260 `_send_rant`）；e2e 覆盖 shutdown 流程 |
| e2e 测试忘 mock LLM（发 task 触发真实 API） | 花钱、慢、CI 无网失败 | e2e 强制 `server.llm = AsyncMock()`（§5.1 ⚠️） |
| e2e 测试污染用户配置（serve() 启动演化调度器读 `~/.emrg/tasks.yml`） | 测试改动真实用户数据 | mock `TaskScheduler` 或隔离 `config_dir` 到临时目录（§5.1 ⚠️） |

---

## 9. 验收标准（对齐 roadmap Phase 1）

- [ ] 既有测试全绿（删除 test_framing 后，协议相关改写为 WS 断言）
- [ ] `framing.py` 已删除，全仓无 `from emrg.framing import …` 残留
- [ ] `(reader, writer)` 元组已消失，`connect_to_server()` 返回单个 ws 对象
- [ ] PID 文件僵尸检测已迁移到 `emrgd.port`；daemon 已在运行时二次启动不被 force-kill
- [ ] 首帧认证强制：无 token / 错 token 连接被拒绝（集成测试覆盖）；认证成功收到 `auth_ok` 确认帧
- [ ] **认证失败不重连**：错 token 时客户端抛 `AuthError` 报错退出（不进入 `_reconnect` 无限循环）
- [ ] 认证超时：连接后不发 auth 的连接在 10s 内被关闭（无悬挂协程）
- [ ] 读循环无空消息防御残留（`if not msg` 已清理）
- [ ] 本机全功能无回退：聊天流式 / 工具调用 / 会话持久化 / `/rant` / 演化周期
- [ ] 大消息（构造 >1MB JSON payload 往返无截断；客户端/服务端 `max_size` 均 16MB）
- [ ] ESC 中断、cancel 语义与现状一致
- [ ] `emrg server stop`（shutdown 消息）正常——daemon shutdown 分支 `await ws.close()` 无 AttributeError；**客户端 `_send_shutdown` 捕获 ConnectionClosed 不崩**
- [ ] 断线 → 自动重连恢复（TUI 读循环）
- [ ] Windows（若可测）：port 文件 + token 首帧认证
- [ ] `ws://127.0.0.1` 连接成功、token 首帧认证通过、`websockets` 依赖正常打包（为 Phase 4 预验证）

