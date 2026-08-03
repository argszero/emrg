# Phase 2 设计：共享客户端层提取（daemon_manager）

> 主路线图：[`roadmap.md`](roadmap.md) Phase 2
> 关联文档：[`packaged-installer.md`](packaged-installer.md) §7.1（GUI 复用层）、[`phase1-websocket-protocol.md`](phase1-websocket-protocol.md)（协议基础，已实施）
> 本文是 Phase 2 的完整设计：现状分析、提取边界、目标形态、改动清单、测试策略、验收标准。
> 修订：v18（review R1-R62 全部采纳，见 §6 决策记录）

---

## 1. 现状分析

### 1.1 代码规模与耦合

`emrg/client/app.py` 当前 **1994 行**，把三类职责揉在一起：

| 职责 | 占比（估算） | 典型代码 |
|------|-------------|---------|
| **daemon 生命周期管理** | ~6% | `start_server_daemon`（58-67）、`_check_and_restart_if_stale`（69-133）、`client_connect_to_server`（135-141），合计 **116 行** |
| **协议读写 + 消息封装** | ~15% | 30 处 `writer.send`/`json.dumps`、read_server 函数（401-960）的 recv/解析 |
| **TUI 渲染与交互** | ~79% | `interactive()` 主体、事件循环、ChatHistory/InputWidget/选择器 |

**行数测算（对应验收 1，R62 精确核算）**：提取 6 个生命周期函数（116 行）+ 30 处 send 块压缩为单行（**实测省 81 行**——30 处块共 111 行，其中 14 处已是单行无压缩空间）+ recv 下沉（5 行）≈ 总省 **202 行** → 1994 − 202 ≈ **1792 行**。**验收目标 ≤1800**（1792 + 8 余量）；1600/1200 留待 Phase 3（GUI 复用后 app.py 还会瘦身）。

### 1.2 协议消息全清单（提取封装对象）

**客户端 → 服务器**（30 处 `writer.send`，分布在 app.py 各处）：

| type | 次数 | 参数 | 封装去向 |
|------|------|------|---------|
| `task` | 1（1841 行） | session_id, cwd, prompt, stream, images?（TaskRequest.to_dict） | `send_task()` |
| `ping` | 4 | — | `send_command("ping")` |
| `trigger_task` | 2 | name, session_id?, cwd? | `send_command` |
| `list_sessions` | 3 | cwd, session_id? | `send_command` |
| `set_model` | 2 | model, session_id | `send_command` |
| `resume_session` | 2 | session_id, cwd | `send_command` |
| `rant` | 2 | message, project, timestamp | `send_command` |
| `delete_session` | 2 | session_id, cwd | `send_command` |
| `rewind_session` | 1 | session_id, cwd | `send_command` |
| `rename_session` | 1 | session_id, title | `send_command` |
| `read_memory` | 1 | scope, memory_id, session_id, cwd | `send_command` |
| `list_tasks` | 1 | cwd | `send_command` |
| `list_projects` | 1 | cwd | `send_command` |
| `list_models` | 1 | —（daemon 端 `_handle_list_models` 只读 config，无需 session_id） | `send_command` |
| `list_memories` | 1 | scope, session_id, cwd | `send_command` |
| `list_history` | 1 | session_id | `send_command` |
| `init_auto_evolve` | 1（303-314 特例：发+读响应） | cwd | `send_command` + 手动 `recv` |
| `compact` | 1 | session_id, cwd | `send_command` |
| `clear_session` | 1 | session_id, cwd | `send_command` |
| `cancel` | 1 | — | `send_command` |

**⚠️ `task` vs `trigger_task` 区分**（R3）：聊天发送走 `TaskRequest`（type=`"task"`，参数 `prompt` 单条 + `images` 数组，支持 /image 粘贴图）；`trigger_task` 是 `/trigger` 命令（daemon.py:666），参数 `name`/`session_id`/`cwd`，**不是**聊天发送。二者都封装，但 `send_task` 只对应 `task`。

**服务器 → 客户端**（read_server 主循环解析）：

| type | 处理 |
|------|------|
| `uptime_seconds`（握手/identity） | 显示 server_id + 欢迎语 |
| `tool_start` | ToolCard 创建 |
| `tool_end` | ToolCard 更新 + diff/write 摘要 |
| `TaskResponse`（delta/done） | StreamingMarkdown 流式渲染 |
| `error` / `clear_result` | 系统消息 |
| 列表类响应（`memory_*`/`history`/`projects`/`models`/`sessions`/`tasks`） | 选择器数据填充 |

### 1.3 现有测试覆盖

- `tests/test_connect.py`：覆盖 `get_server_path`/`AuthError`/`cleanup_server`/`is_server_running_sync`——**只测了 connect.py，没测 app.py 的 daemon 管理逻辑**
- `tests/test_app_widgets.py`：覆盖 ChatHistory/选择器等纯渲染组件
- `tests/test_ws_e2e.py`：端到端（起真实 daemon）——**回归保障靠它**

**缺口**：`start_server_daemon`/`_check_and_restart_if_stale`/`client_connect_to_server` 这些**有副作用的生命周期逻辑零单测**，只有 e2e 间接覆盖。提取时补上。

---

## 2. 提取边界（设计核心）

### 2.1 目标形态

```
emrg/client/
├── daemon_manager.py   # ★ 新文件：daemon 生命周期 + 协议客户端封装（可被 GUI 复用）
├── app.py              # 瘦身：只留 TUI 渲染与交互（目标 ≤1800 行）
├── widgets.py          # 不变：ChatHistory/InputWidget/选择器
└── python_tui/         # 不变：终端渲染内核
```

`daemon_manager.py` 模块内容：

```
emrg/client/daemon_manager.py
  ├── is_running()                    # 薄封装 is_server_running_sync（原 app.py:53-56）
  ├── start_daemon()                  # 拉起 emrgd（原 start_server_daemon，58-67）
  ├── check_and_restart_if_stale()    # source/config mtime 变更 → 重启（原样搬迁，69-133）
  ├── ensure_connected()              # 全流程：check_stale + 拉起 + 建连（原 client_connect_to_server，135-141）。**不含 ping**（R28：与原行为一致，ping 由调用方负责）
  ├── DaemonConnection 类
  │     ├── send_task(session_id, cwd, prompt, stream, images?)   # 仅对应 type="task"
  │     ├── send_command(type_, **params)                         # 通用：ping/list_*/set_*/rant/...
  │     ├── recv(timeout)             # 单帧读取（返回 dict；超时返回 None）
  │     ├── read_stream()             # 事件流 yield（GUI 桥接用，R35）
  │     └── close()                   # 关闭连接（原 writer.close）
  └── 独立函数（保留 connect.py 兼容层，见 §2.3）
```

> **不提取** `shutdown()`（R4）：`_send_shutdown` 在 `__main__.py:133`，不在 app.py；`__main__.py` 继续走 connect.py，本模块不重复实现。
> **不提取** `ensure_running()`（R5）：`ensure_connected()` 已含拉起逻辑（原 `client_connect_to_server` 就是全流程），无需第二个入口。

### 2.2 提取的六个函数 + 30 处消息封装（从 app.py 原样搬迁 + 封装）

| # | 现有函数（app.py） | 去向 | 说明 |
|---|-------------------|------|------|
| 1 | `_get_server_source_mtime`（26-38） | daemon_manager 内部 | 原样 |
| 2 | `_get_config_mtime`（41-51） | daemon_manager 内部 | 原样 |
| 3 | `_try_connect`/`is_server_running`（53-56） | daemon_manager `is_running()` | 薄封装 `is_server_running_sync` |
| 4 | `start_server_daemon`（58-67） | daemon_manager `start_daemon()` | 原样 |
| 5 | `_check_and_restart_if_stale`（69-133） | daemon_manager `check_and_restart_if_stale()` | 原样 |
| 6 | `client_connect_to_server`（135-141） | daemon_manager `ensure_connected()` | 原样 |
| 7 | 30 处 `writer.send(json.dumps({...}))` | DaemonConnection 方法 | 逐条封装（§3.2） |

> **内部交叉调用同步改名（R38）**：3 个函数内部互相引用旧名，搬迁时必须同步：
> - `start_daemon()` 内 `is_server_running()`（63 行）→ `is_running()`
> - `check_and_restart_if_stale()` 内 `is_server_running()`（121 行）→ `is_running()`
> - `ensure_connected()` 内 `_check_and_restart_if_stale()` → `check_and_restart_if_stale()`、`is_server_running()` → `is_running()`、`start_server_daemon()` → `start_daemon()`
> 否则照抄旧名 → daemon_manager 内 NameError。

### 2.3 与 `emrg/connect.py` 的关系（重要）

- `connect.py`（109 行）是**传输层**：`connect_to_server`（建连+token 握手）、`cleanup_server`、`is_server_running_sync`。**不动**。
- `daemon_manager.py` 是**生命周期+协议层**：在其上封装 daemon 拉起/重启/消息读写。
- 分层：`app.py`/GUI → `daemon_manager.py` → `connect.py` → websockets。
- `connect.py` 保持无 app 依赖（它已被 `__main__.py` 直接使用，如 `_send_rant`/`_send_shutdown`——那些调用点**不改**，继续走 connect.py 原始函数）。
- `__main__.py` 的 `_send_shutdown`（133 行）保持现状，**Phase 2 不涉及**（R4 已定不提取 shutdown）。
- **依赖方向单向（R48）**：`daemon_manager → connect/protocol`；`app.py`/GUI → `daemon_manager`。**daemon_manager 不得 import app.py**（防循环依赖）——分层图中 daemon_manager 在最底层。

### 2.4 关键决策：类 vs 模块级函数

**倾向：模块级函数 + 轻量 `DaemonConnection` 类**（不是大状态类）。

理由：
1. **TUI 现状是无状态函数集**（`writer` 在 `interactive()` 闭包里传来传去）。提取为类会改变调用风格，引入 `self` 状态，回归风险大。
2. **GUI 需要的是"连接对象"语义**（一个连接 = 一个信号桥），`DaemonConnection` 恰好承载。
3. **daemon 生命周期函数是无状态的**（谁调用都行），保持模块级函数，GUI/TUI 直接 `daemon_manager.ensure_connected()`。

**`DaemonConnection.recv` 超时语义**（R7）：
- 签名 `async def recv(timeout: float | None = None) -> dict | None`
- `timeout=None`：阻塞直到有帧
- `timeout=N`：`asyncio.wait_for(recv(), N)`，**超时返回 `None`**（静默，不抛）
- 当前 read_server 循环（401-960 行，recv 在 432 行）是 0.1s 轮询（为响应键盘/渲染），封装后调用方写 `data = await conn.recv(0.1); if data is None: continue`，语义等价。
- `json.loads` 在 recv 内部完成；**解析失败（含空帧/空白帧）返回 `None` 并记录 warning**（R53/R54）——**不返回 `{"error": "invalid_json"}`**，否则 error dict 会进入 read_server 循环的 `if "error" in data` 分支（566 行），在 TUI 显示 "Error: invalid_json" 让用户看到莫名的错误。返回 None 静默丢弃，与现状崩循环相比是改进（不崩、无用户可见错误）。
- **⚠️ `recv()` 不捕获 `ConnectionClosed`（R11）**：断连异常必须向上传播，由调用方（read_server 循环）处理。若 recv 内部吞掉 ConnectionClosed 返回 None，断连检测将永久丢失——TUI 会卡在"已断连但无反馈"状态，**断线重连功能被破坏**。

**read_server 循环的断连检测（R11，必须保留）**：
```python
while True:
    try:
        data = await conn.recv(0.1)
    except ConnectionClosed:
        await _reconnect()
        continue
    if data is None: continue
    # ...分发逻辑
```

### 2.5 明确不提取的内容（防止过度设计）

- ❌ **不提取** `interactive()` 主体、键盘事件处理、渲染逻辑（留在 app.py）
- ❌ **不提取** ToolCard 的 diff 渲染、write 摘要逻辑（这是 TUI 视图层，GUI 有自己的展示方式）
- ❌ **不提取** `_detect_clipboard_image`/`_extract_clipboard_image`（纯 TUI 功能）
- ❌ **不新建** `gui/daemon_client.py`（Phase 3 再做信号桥，本阶段只提供纯 asyncio 接口）
- ❌ **不动** server 端（daemon.py 零改动）
- ❌ **不动** connect.py 传输层
- ℹ️ **`read_stream` 是 Phase 3 预留接口（R52）**：Phase 2 TUI 不调用它（保持 recv 单帧循环），但 roadmap/packaged-installer 明确承诺此接口，且单测覆盖（R39）——**接受此轻微超前，换取 GUI 零复制网络代码**。实施者勿删。

---

## 3. 改动清单

### 3.1 `emrg/client/daemon_manager.py`（新文件，约 200 行）

```python
"""Daemon 生命周期管理 + 协议客户端封装。

供 TUI（app.py）与 GUI（Phase 3）共用。分层：
  app.py/GUI → daemon_manager → connect.py → websockets
"""
from __future__ import annotations
import asyncio, json, logging, os, signal, subprocess, sys
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator
from websockets.exceptions import ConnectionClosed
from emrg.connect import (connect_to_server, cleanup_server,
                          is_server_running_sync, get_server_path)
from emrg.protocol import TaskRequest

logger = logging.getLogger(__name__)

# ── daemon 生命周期 ──────────────────────────
def is_running() -> bool: ...                      # 原 app.py:53-56
async def start_daemon() -> subprocess.Popen: ...  # 原 app.py:58-67
async def check_and_restart_if_stale() -> None: ... # 原 app.py:69-133
async def ensure_connected() -> "DaemonConnection": ...  # 原 app.py:135-141（含拉起）

# ── 协议客户端封装 ────────────────────────────
class DaemonConnection:
    """一条已认证的 daemon 连接。"""
    def __init__(self, writer): ...                # writer = websockets 连接
    async def send_task(self, session_id, cwd, prompt, stream=True, images=None):
        """聊天发送：TaskRequest(type="task")。images 支持 /image 粘贴图。

        内部 `json.dumps(req.to_dict(), ensure_ascii=False)` 以 **str 发送**
        （websockets 原生支持，不再 `.encode()`——与 daemon 端 `ws.recv()`
        返回 str 一致；现状 1841 行的 bytes 发送行为等价）。
        """
        ...
    async def send_command(self, type_, **params):
        """通用命令：ping/list_*/set_*/rant/compact/... 只发不读。

        内部 `json.dumps({"type": type_, **params}, ensure_ascii=False)`
        ——统一关转义（兼容中文，现状 5 处 ensure_ascii=False 的行为）。

        ⚠️ **params 不得含 `type` 键（R61）**：`{"type": type_, **params}`
        dict 展开后者覆盖前者，`send_command("task", type="x")` 会把 type
        覆盖成 "x"。当前 30 处调用点均正确，封装层仍需防呆注释。
        """
        ...
    async def recv(self, timeout: float | None = None) -> dict | None:
        """单帧读取。超时返回 None（不抛）；json.loads 内部完成；不捕获 ConnectionClosed。"""
        ...
    async def read_stream(self) -> AsyncIterator[dict]:
        """事件流：yield 服务器下发的每一帧（uptime/tool_start/tool_end/TaskResponse/...）。

        供 GUI（Phase 3）桥接 Qt Signal（R35，对齐 roadmap.md:115 /
        packaged-installer.md:351 的 read_stream 承诺）。内部用 recv(None)
        阻塞读（**R42：`recv(None)` = `asyncio.wait_for(coro, None)` 无超时
        阻塞，非"超时返回 None"语义**），不吞 ConnectionClosed（R11）。
        **TUI 保持 recv 单帧循环不动**（现状 0.1s 轮询），read_stream 是给
        GUI 的公共接口。
        """
        while True:
            data = await self.recv(None)
            if data is None: continue  # 防御性（recv(None) 实际不超时）
            yield data
    async def close(self): ...
```

**注意**：`ensure_connected()` 返回 `DaemonConnection`（封装 writer），而不是裸 writer——这样 app.py 里的 `writer.send(json.dumps(...))` 全部改为 `conn.send_command("list_sessions", cwd=cwd)`，类型清晰、GUI 可直接复用。

### 3.2 app.py 逐处替换（30 处 send + read_server 循环 + 3 处 close）

**替换规则**：

| 原代码 | 新代码 |
|--------|--------|
| `writer = await client_connect_to_server()` | `conn = await daemon_manager.ensure_connected()` |
| `await writer.send(json.dumps({"type": "ping"}))` | `await conn.send_command("ping")` |
| `await writer.send(json.dumps(req.to_dict(), ...))`（1841 行，聊天发送） | `await conn.send_task(session_id, cwd, text, stream=True, images=...)` |
| `await writer.send(json.dumps({"type": "trigger_task", ...}))` | `await conn.send_command("trigger_task", name=..., session_id=..., cwd=...)` |
| `await writer.send(json.dumps({"type": "list_sessions", "cwd": cwd}))` | `await conn.send_command("list_sessions", cwd=cwd)` |
| ...（其余 send 逐一对应） | |
| `frame = await asyncio.wait_for(writer.recv(), timeout=3)` | ⚠️ **不在 app.py 替换范围（R57）**——这是 app.py:83，在已提取的 `check_and_restart_if_stale` 内（daemon_manager 保持裸 ws 操作，无 conn，见 R44） |
| init_auto_evolve 特例内 recv（312 行，timeout=5） | `await conn.recv(timeout=5)`（R58，见 R6/R32） |
| `read_server` 循环内 `writer.recv()`（timeout=0.1 轮询） | `data = await conn.recv(0.1); if data is None: continue` |
| `_reconnect` 内 `writer = await client_connect_to_server()` | `conn = await daemon_manager.ensure_connected()` |
| `_reconnect` 内 `await writer.send(json.dumps({"type": "ping"}))`（422 行） | `await conn.send_command("ping")`（重连后刷新 server_id/uptime。**只发不读**（R33）——ping 响应由 read_server 循环异步消费，勿在 _reconnect 内 recv，否则抢走循环的帧） |
| `read_server` 循环内 rewind 分支的 ping（622 行） | `await conn.send_command("ping")`（rewind 成功后刷新状态——**易漏**，注意它在循环内非 _reconnect） |
| `await writer.close()`（85/416/1921 三处） | `await conn.close()`（416 行 _reconnect 内保持 try/except 包裹——关闭的是已断连的旧连接） |

**rant 两处 payload 变量展开**（R22 + R40，1429/1735 行）：
```python
# 原：payload = {...}; await writer.send(json.dumps(payload, ensure_ascii=False))
# 注意 1735 行结构：project 是条件字段（`if project: payload["project"] = project`）
payload = {"message": text, "timestamp": datetime.now().isoformat()}
if project: payload["project"] = project
await conn.send_command("rant", **payload)
# 不能直接 send_command("rant", project=None)——daemon 端 project.strip() 对 None 会 AttributeError
```

**nonlocal 声明同步替换**（R17）：3 处 `nonlocal ... writer` 改为 `nonlocal ... conn`：
- 403：`nonlocal _last_center, _elapsed_task, writer` → `... conn`
- 407：`nonlocal writer, busy, _elapsed_task` → `nonlocal conn, busy, _elapsed_task`
- 1006：`nonlocal inp, status, history, paste_mode, stream_buffer, writer, chat, ...` → `... conn, chat, ...`

**images 过滤逻辑留在 app.py**（R18）：app.py:1836-1839 的 `_pending_images` 过滤（`img.get("label") in inp.text`）依赖 TUI 输入框状态，是视图层逻辑——**留在 app.py**，`send_task` 只透传已过滤的 images 数组。

**TaskRequest 构造下沉到 send_task**（R27）：原 app.py:1835 的 `req = TaskRequest(session_id=..., cwd=..., prompt=text, stream=True)` 删除——app.py 只组装参数（text/images 数组），`conn.send_task(session_id, cwd, text, stream=True, images=过滤后列表)` 内部构造 TaskRequest。

**app.py import 清理（R47）**——提取后同步调整头部 import：
- **删**：`from emrg.connect import connect_to_server, cleanup_server, is_server_running_sync, get_server_path`（整行——这些函数全部走 daemon_manager，app.py 不再直接用）
- **删**：`TaskRequest`（从 `from emrg.protocol import TaskRequest, TaskResponse, ToolEnd, ToolStart` 中移除——构造已下沉 send_task，R27）
- **留**：`TaskResponse, ToolEnd, ToolStart`（read_server 分发仍用，459/475/520 行）
- **留**：`from websockets.exceptions import ConnectionClosed`（read_server 循环 `except ConnectionClosed` 仍需，R11）
- **加**：`from emrg.client import daemon_manager`

**init_auto_evolve 特例**（R6 + R32，app.py:303-314）：`send_command("init_auto_evolve", cwd=...)` 后**必须读走响应帧**——daemon 端（daemon.py:634-654）会**同步回一帧** `{"ok": True/False, ...}`，若不读会残留连接缓冲，被 read_server 循环误当成下一条消息处理（`{"ok": ...}` 会被 `TaskResponse.from_dict` 解析失败或走 error 分支）。写法：`await conn.recv(timeout=5)`（响应立即到达，超时只是兜底）。send_command 本身**只发不读**。

**两种 ping 语义（R44，必须区分）**：
- **`check_and_restart_if_stale` 内部**（已提取到 daemon_manager，app.py:80-88）：ping 是**发-读配对**——`send("ping")` → `recv(timeout=3)` 拿 `started_at`/`pid` 判断是否重启。**必须读**。
- **`_reconnect`/read_server 循环内**（app.py:422/622）：ping **只发不读**（R33）——响应由 read_server 循环异步消费。

> ⚠️ **清零范围仅限 app.py**（R44）：§4.3 的 `writer.send`/`recv`/`close` = 0 仅针对 `emrg/client/app.py`；daemon_manager 内部（check_and_restart_if_stale 原样搬迁的裸连接探测）**允许裸 websockets 操作**——它发生在建立 DaemonConnection 之前，无法走封装。

**cancel 只发不读**（R37）：`send_command("cancel")` 后**不读**——daemon 端（daemon.py:293）会同步回 `{"type": "cancelled", "session_id": ...}` 帧，由 read_server 循环静默消化（循环无专门 cancelled 分支，落 `TaskResponse.from_dict` 容错构造空响应，无害）。与 ping（R33）同原则。

**read_server 主循环改造**（函数 401-960 行，recv 在 432 行）：
- 原：`frame = await writer.recv()` → `text.strip()`/`if not text`/`json.loads`（438-441 行）→ 分发
- 新：`data = await conn.recv()`（内部已 json.loads，返回 dict）→ 分发。**删除原 438-441 行**（`text.strip()`/`if not text`/`json.loads`——recv 内部已处理空帧/坏 JSON），循环体直接从 `data.get("type")` 分发开始（否则 data 是 dict 无 `.strip()` 会 AttributeError）
- **断连检测保留**（R11）：循环必须包 `try/except ConnectionClosed → _reconnect()`（recv 不吞异常）
- **分发逻辑本身（tool_start/tool_end/TaskResponse/rewind/compact 等全部 type 分支，401-960）留在 app.py 不动**——只把"取帧 + 解析"下沉到封装

### 3.3 `_reconnect` 保留在 app.py

断线重连的 UI 反馈（"⏸ server connection lost — reconnecting..."）是 TUI 视图行为，留在 app.py。但重连的**底层操作**（`ensure_connected()`）走 daemon_manager。GUI 的断线处理（Phase 3）会是信号桥版本，不共享此函数。

### 3.4 测试（tests/ 新增 2 文件）

**`tests/test_daemon_manager.py`**（mock websockets，不依赖真实 daemon。通用 mock 设施：假 writer 类（`send`/`recv`/`close` 三件套），`recv` 返回预置帧。**mock 路径（R36）**：均为 `emrg.client.daemon_manager.<符号>`——模块级 `from emrg.connect import connect_to_server` 绑定在 daemon_manager 命名空间，patch 原模块 `emrg.connect.connect_to_server` 会静默失效；同理 `asyncio.create_subprocess_exec` 应 patch `emrg.client.daemon_manager.asyncio.create_subprocess_exec`）：
- `is_running()`：port 文件缺失/损坏/连接拒绝 → False
- `start_daemon()`：mock `create_subprocess_exec`，验证启动参数 + 等待就绪
- `check_and_restart_if_stale()`（用假 writer 的 `recv` 返回带 `started_at`/`pid` 的 ping 响应帧）：
  - source mtime > server started_at → 发送 SIGTERM + 等待退出
  - config mtime > server started_at → 同上
  - mtime 未变 → 不重启
  - 服务器不可达 → 静默 pass（不抛异常）
- `ensure_connected()`：**mock 三件套（R50）**——`connect_to_server`（返回假 writer）+ `is_running`（返回 True 跳过拉起）+ `start_daemon`（防止真起进程）。验证 DaemonConnection 包装（**不含 ping**——与原行为一致，ping 由调用方负责）。⚠️ 只 mock connect_to_server 不够：`is_running()` 走真实 TCP 探测，port 文件缺失时会真执行 `start_daemon()` 拉起真实 emrgd
- `DaemonConnection.send_task`：mock writer.send，验证 `type="task"` + prompt/images 字段
- `DaemonConnection.send_command`：mock writer.send，验证 JSON payload 正确（含 `type` + kwargs）
- `DaemonConnection.recv`：mock writer.recv，验证返回 dict、超时返回 None、坏 JSON/空帧返回 None（静默丢弃，不产生 error dict）、**ConnectionClosed 向上传播（不吞）**
- `DaemonConnection.read_stream`：mock writer.recv 依次返回多帧，验证逐帧 yield；ConnectionClosed 向上传播（不吞）
- `DaemonConnection.close`：验证 writer.close 被调用

**`tests/test_daemon_manager_e2e.py`**（起真实 daemon，复用 test_ws_e2e 的模式。**⚠️ 不经 `ensure_connected()`（R51）**——它内部 `is_running()` 读真实 `~/.emrg/emrgd.port`，本机有真实 emrgd 时会连错 daemon。改为：`daemon_mod.config_dir = lambda: tmp` 隔离 + `EmrgServer(...).serve()` 手动起 + `DaemonConnection(await connect_to_server())` 直连。**跨文件复用（R63）**：`from tests.test_ws_e2e import _make_config, _boot_server, _make_fake_chat_stream`（不复制三件套））：
- ensure_connected → send_command("ping") → 收到响应（**ServerPong 结构**：`identity`（instance_id/host_name/fork_source/branch_id）+ `uptime_seconds` + `evolution_count`）
- send_command("list_models") → 收到模型列表
- send_task(stream=True) → 收到 delta 流 → done（**复用 test_ws_e2e 的 `_make_fake_chat_stream` 模式**：`server.llm = AsyncMock()` + `server.llm.chat_stream = _make_fake_chat_stream()`，不调真实 LLM）

### 3.5 文档同步

- `roadmap.md` Phase 2 验收项打勾（在全部满足后）
- `Agent.md` 架构树补 `daemon_manager.py`
- `packaged-installer.md` §7.1 复用层图更新为实际模块结构（若与设计有出入）

---

## 4. 测试策略与风险

### 4.1 回归保障

| 层 | 手段 | 覆盖 |
|----|------|------|
| 单测 | test_daemon_manager.py（mock） | 生命周期逻辑全分支 |
| 单测 | test_app_widgets.py（既有，不改） | 渲染组件不受影响 |
| e2e | test_ws_e2e.py（既有） + test_daemon_manager_e2e.py（新） | 真实 daemon 协议往返 |
| 全量 | pytest 全量（当前 441） | 无回退 |

### 4.2 风险与对策

| 风险 | 对策 |
|------|------|
| app.py 30 处 send + 3 处 close 改签名，漏改/错改 | 逐处替换 + e2e 全量回归；替换后 grep 确认 `writer.send` 在 app.py 中仅剩 0 处、`writer.close` 仅剩 0 处 |
| `_reconnect` 闭包变量（`writer` → `conn`）牵连 | 只改赋值与调用点，闭包结构不动 |
| ensure_connected 返回类型变化（裸 writer → DaemonConnection）破坏现有调用 | 编译期无法检出（Python），靠 e2e + grep 全量扫描 `client_connect_to_server(` 调用点确认清零 |
| DaemonConnection.recv 的 json.loads 异常处理（行为改进：坏 JSON 不再崩循环） | 封装内 try/except 返回 None + 记录 warning；e2e 覆盖坏 JSON/空帧路径 |
| 聊天发送丢 images（/image 功能） | send_task 显式带 images 参数，单测覆盖 images 非空时的 payload |
| 提取函数内部交叉调用旧名 → NameError（R38/R43） | 搬迁后 grep `is_server_running\|_check_and_restart_if_stale\|start_server_daemon` 确认 daemon_manager 内清零 |

### 4.3 验收标准

- [ ] **app.py 行数 ≤1800**（当前 1994；提取 116 行生命周期 + send 压缩 81 行 + recv 下沉 5 行 = 1792，R62 精确核算。1600/1200 留待 Phase 3 GUI 复用后再瘦身）
- [ ] `daemon_manager.py` 独立单测覆盖全部生命周期函数 + DaemonConnection 方法（mock，不起 daemon）
- [ ] 全量 pytest 全绿（441 + 新增 ≥15，**覆盖 §3.4 全部条目**——质量优先于数量）
- [ ] e2e：真实 daemon 下 `ensure_connected → send_command("ping") → send_command("list_models") → send_task(stream)` 全链路通
- [ ] TUI 手动冒烟：聊天流式 / 工具调用 / 会话切换 / `/rant` / ESC 中断 / 断线重连 / **/image 粘贴图** 全功能无回退
- [ ] grep 确认：`emrg/client/app.py` 中 `writer.send` + `writer.recv` + `writer.close` 出现次数 = 0（全部走封装）；`json.dumps` 仅剩 `_format_args`（UI 层，须保留）
- [ ] grep 确认（R49）：`client_connect_to_server(` / `is_server_running(` / `cleanup_server(` / `connect_to_server(` 在 `emrg/client/app.py` 中 = 0（均走 daemon_manager）
- [ ] `emrg/connect.py` 零改动、`emrg/server/*` 零改动

---

## 5. 与 Phase 3 的衔接

- GUI（Phase 3）直接 `from emrg.client.daemon_manager import DaemonConnection, ensure_connected`
- GUI 侧新增 `gui/daemon_client.py`：`async for event in conn.read_stream():` 把服务器事件桥接到 Qt Signal（message_delta / tool_started / tool_finished / done）——**read_stream 是为此准备的公共接口**（R35）
- 线程模型：单线程 asyncio（qasync），`DaemonConnection` 天然适配（纯协程接口）

---

## 6. 决策记录

1. **提取为 `daemon_manager.py` 新模块**，app.py 瘦身至 ≤1800 行（R62 精确核算 1792；1600/1200 留 Phase 3）；connect.py 传输层不动。
2. **模块级函数 + DaemonConnection 类**（非大状态类）：生命周期函数无状态保持函数式，连接对象语义清晰供 GUI 复用。
3. **裸 writer 全面替换为 DaemonConnection**：类型清晰、json 解析下沉、GUI 白拿。
4. **read_server 的分发逻辑（UI 处理）留在 app.py**：只下沉"取帧 + 解析"。
5. **`_reconnect` UI 反馈留在 app.py**，底层重连走 daemon_manager；GUI 断线处理 Phase 3 信号桥版本。
6. **不新建 gui/daemon_client.py**（Phase 3 再做）；**不改 server**；**不改 connect.py**。

### v2 修订记录（review R1-R10 全部采纳）

| # | 修订 |
|---|------|
| R1 | 验收 1：≤1200 → **≤1600**（附行数测算：提取 116 行 + send 封装省 124 行 + recv 下沉 ≈ 1750 实际，1600 为安全目标；1200 留 Phase 3） |
| R2 | 验收 6：`json.dumps` 清零 → **`writer.send`/`writer.recv`/`writer.close` 清零**；`json.dumps` 仅剩 UI 层 `_format_args`（1988 行，须保留） |
| R3 | §1.2 区分 `task`（聊天，TaskRequest.to_dict，参数 prompt/images）与 `trigger_task`（/trigger 命令，daemon.py:666）；§3.1 send_task 签名改为 `(session_id, cwd, prompt, stream=True, images=None)` |
| R4 | §2.1 删除 shutdown()：`_send_shutdown` 在 `__main__.py:133`，不在 app.py，不重复实现 |
| R5 | §2.1 删除 ensure_running()：ensure_connected() 已含拉起全流程 |
| R6 | §3.2 补 init_auto_evolve 特例（send_command + 手动 recv(timeout=5)）；写明 send_command 只发不读 |
| R7 | §2.4 定义 recv(timeout) 超时返回 None；坏 JSON 返回 error dict（行为改进，非现状崩循环） |
| R8 | §3.2 补 writer.close() 3 处（85/416/1921）→ conn.close() |
| R9 | §3.4 补 check_and_restart_if_stale 的假 writer mock 细节 |
| R10 | §1.2 服务器→客户端清单补全：error/clear_result + 列表类响应（memory_*/history/projects/models/sessions/tasks） |

### v3 修订记录（review v2 R11-R16 全部采纳）

| # | 修订 |
|---|------|
| R11 | §2.4 + §3.1 明确：`recv()` **不捕获 ConnectionClosed**（向上传播）；read_server 循环保留 `try/except ConnectionClosed → _reconnect`（附代码示例）——否则断线重连功能被破坏 |
| R12 | §3.2 补 `_reconnect` 内的 `await conn.send_command("ping")`（422 行，重连后刷新 server_id/uptime） |
| R13 | §2.4 补一句：坏 JSON 返回 error dict 是行为改进，验收需确认不掩盖真实错误（如 server 端 bug 返回非 JSON 帧） |
| R14 | §2.2 标题改"六个函数 + 31 处消息封装"（删除 ensure_running 后从七个变六个） |
| R15 | §3.1 代码骨架补 import 行（connect.py/protocol.py/websockets） |
| R16 | §3.4 合并重复 mock 描述（假 writer 三件套提为通用设施；补 recv 不吞 ConnectionClosed 的测试断言） |

### v4 修订记录（review v3 R17-R21 全部采纳）

| # | 修订 |
|---|------|
| R17 | §3.2 补 3 处 nonlocal 声明替换（403/407/1006）：`writer` → `conn` |
| R18 | §3.2 补 images 过滤逻辑（app.py:1836-1839）留在 app.py，send_task 只透传 |
| R19 | §3.1 import 行补 `logging`/`datetime`/`logger = logging.getLogger(__name__)` |
| R20 | §3.2 close 行补说明：416 行 _reconnect 内的 close 保持 try/except 包裹 |
| R21 | §3.4 e2e 测试引用 test_ws_e2e 的 `_make_fake_chat_stream` mock 模式 |

### v5 修订记录（review v4 R22-R24 全部采纳）

| # | 修订 |
|---|------|
| R22 | §3.1 send_command 统一 `json.dumps({"type": type_, **params}, ensure_ascii=False)`（兼容中文）；§3.2 补 rant 两处 payload 变量展开为 kwargs |
| R23 | §3.2 替换表补 622 行循环内 rewind 分支的 ping（易漏） |
| R24 | §3.1 send_task 注明内部 str 发送替代 bytes（与 daemon 端 ws.recv() 返回 str 一致，行为等价） |

### v6 修订记录（review v5 R25-R27 全部采纳）

| # | 修订 |
|---|------|
| R25 | 修正 read_server 范围引用：432-620 → **401-960**（函数全范围；432 仅为 recv 行）。§1.1/§2.4/§3.2 三处同步更新 |
| R26 | §2.3 补一句 `_send_shutdown`（`__main__.py:133`）保持现状，Phase 2 不涉及 |
| R27 | §3.2 补 TaskRequest 构造下沉到 send_task（app.py:1835 删除，app.py 只组装参数） |

### v7 修订记录（review v6 R28-R31 全部采纳）

| # | 修订 |
|---|------|
| R28 | 明确 `ensure_connected()` **不含 ping**（与原行为一致，ping 由调用方负责）；§3.4 测试去掉"ping 握手"断言 |
| R29 | 全部"31 处"→"30 处"（实测 `writer.send` 仅 30 处；31 是 json.dumps 数，含 UI 层 `_format_args`）。§1.1/§1.2/§2.2/§3.2/§4.2/§4.3 共 9 处 |
| R30 | 第 6 行修订标注 v2 → v6 |
| R31 | §1.2 表格 `list_models` 参数 "session_id" → "—"（daemon 端只读 config） |

### v8 修订记录（review v7 R32-R34 全部采纳）

| # | 修订 |
|---|------|
| R32 | R6 强化：init_auto_evolve 响应**必须读走**（daemon 同步回 ok 帧，残留会污染 read_server 循环）；超时只是兜底 |
| R33 | R12 补：_reconnect 内 ping **只发不读**（响应由 read_server 循环消费，勿在 _reconnect 内 recv 抢帧） |
| R34 | §3.4 e2e ping 断言补全 ServerPong 结构（identity + uptime_seconds + evolution_count） |

### v9 修订记录（review v8 R35-R37 全部采纳）

| # | 修订 |
|---|------|
| R35 | §3.1 补 `read_stream()` 事件流方法（yield 每帧，供 GUI 桥接 Qt Signal，对齐 roadmap.md:115 / packaged-installer.md:351 承诺）；§5 衔接更新为 `async for event in conn.read_stream()` |
| R36 | §3.4 补 mock patch 路径：`emrg.client.daemon_manager.<符号>`（模块级 import 绑定，patch 原模块会静默失效） |
| R37 | §3.2 补 cancel 只发不读 + cancelled 帧由 read_server 循环静默消化（TaskResponse.from_dict 容错） |

### v10 修订记录（review v9 R38-R40 全部采纳）

| # | 修订 |
|---|------|
| R38 | §2.2 补内部交叉调用改名说明：is_server_running→is_running、_check_and_restart_if_stale→check_and_restart_if_stale、start_server_daemon→start_daemon（否则照抄旧名 NameError） |
| R39 | §3.4 单测补 read_stream 条目（逐帧 yield + ConnectionClosed 传播） |
| R40 | §3.2 rant 展开示例改为条件 project（1735 行结构），防 daemon 端 `project.strip()` 对 None AttributeError |

### v11 修订记录（review v10 R41-R43 全部采纳）

| # | 修订 |
|---|------|
| R41 | §4.3 验收"新增 ≥20"→"**≥15**（覆盖 §3.4 全部条目）"——按 §3.4 计划实际可凑 18-21 个，≥20 偏紧会诱导凑数 |
| R42 | §3.1 read_stream 注释补 `recv(None)` = `asyncio.wait_for(coro, None)` 无超时阻塞语义 |
| R43 | §4.2 风险表补"内部交叉调用旧名 → NameError"行（对策：grep 确认旧名清零） |

### v12 修订记录（review v11 R44-R46 全部采纳）

| # | 修订 |
|---|------|
| R44 | §3.2 补两种 ping 语义区分：check_and_restart 内部**发-读配对**（recv 拿 started_at）vs _reconnect/循环**只发不读**；§4.3 明确清零范围仅限 app.py（daemon_manager 内部裸 ws 探测允许） |
| R45 | 第 6 行修订标注 v6 → v11 |
| R46 | §4.1 "当前 441" 核对确认（pytest --collect-only = 441，无需改） |

### v13 修订记录（review v12 R47-R49 全部采纳）

| # | 修订 |
|---|------|
| R47 | §3.2 补 app.py import 清理清单：删 connect 整行 + TaskRequest；留 TaskResponse/ToolEnd/ToolStart + ConnectionClosed；加 `from emrg.client import daemon_manager` |
| R48 | §2.3 补依赖方向单向声明：daemon_manager 不得 import app.py（防循环） |
| R49 | §4.3 补函数名 grep 清零验收（client_connect_to_server/is_server_running/cleanup_server/connect_to_server = 0） |

### v14 修订记录（review v13 R50-R52 全部采纳）

| # | 修订 |
|---|------|
| R50 | §3.4 ensure_connected 单测改 **mock 三件套**（connect_to_server + is_running + start_daemon）——只 mock connect 会触发真实 start_daemon |
| R51 | §3.4 e2e 补"不经 ensure_connected"（避免读真实 port 文件连错 daemon），改直接 EmrgServer().serve() + DaemonConnection(connect_to_server()) |
| R52 | §2.5 补 read_stream 为 Phase 3 预留接口的说明（接受轻微超前，换取 GUI 零复制；单测已覆盖，实施者勿删） |

### v15 修订记录（review v14 R53-R55 全部采纳）

| # | 修订 |
|---|------|
| R53 | §3.1 recv 坏 JSON 改返回 **None**（静默丢弃 + log warning），不返回 `{"error": "invalid_json"}`——否则 error dict 进 `if "error" in data` 分支在 TUI 显示莫名错误。R7/R13 同步 |
| R54 | §3.1 一并明确空帧/空白帧 → None（json.loads 失败统一处理） |
| R55 | §3.2 补删除原 438-441 行 text 检查（dict 无 .strip() 会 AttributeError），循环体从 data.get("type") 分发开始 |

### v16 修订记录（review v15 R57-R59 全部采纳）

| # | 修订 |
|---|------|
| R57 | 替换表删除 254 行（app.py:83 在已提取的 check_and_restart 内，保持裸 ws，不在 app.py 范围）+ 注明 |
| R58 | 替换表补 312 行 init_auto_evolve 特例 recv（timeout=5）→ conn.recv(timeout=5) |
| R59 | 第 6 行修订标注 v11 → v15 |

### v17 修订记录（review v16 R60-R61 全部采纳）

| # | 修订 |
|---|------|
| R60 | §3.1 import 行补 `from typing import AsyncIterator`（read_stream 返回注解引用） |
| R61 | §3.1 send_command docstring 补 type 参数防呆（dict 展开后者覆盖前者） |

### v18 修订记录（review v17 R62 全部采纳）

| # | 修订 |
|---|------|
| R62 | 行数验收 ≤1600 → **≤1800**（Python AST 精确核算：提取 116 + send 压缩 81 + recv 5 = 202 行，1994−202=1792。R1 误估 send 省 124 行，实测 30 处块共 111 行、压缩省 81）。§1.1/§2.1/§4.3/§6 决策 1 同步更新 |

### v19 修订记录（review v18 R63-R64 全部采纳）

| # | 修订 |
|---|------|
| R63 | §3.4 补 e2e 跨文件复用：`from tests.test_ws_e2e import _make_config, _boot_server, _make_fake_chat_stream`（不复制三件套） |
| R64 | 第 6 行修订标注 v15 → v18 |
