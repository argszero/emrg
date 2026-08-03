# EMRG 协议契约（Protocol Contract）

> **用途**：这是 EMRG daemon 与客户端之间的**唯一通信契约**。任何客户端（TUI / Electron GUI / 未来客户端）按本文实现即可与 daemon 互通——**不需阅读任何 Python 源码**。
>
> **状态**：✅ 已与 daemon 实际行为逐条核对（v2，2026-08-03 —— 广播模型已实施）
> **对应实现**：`emrg/server/daemon.py`（服务端）、`emrg/connect.py`（连接层）、`emrg/protocol.py`（类型定义）
> **传输**：WebSocket over TCP loopback（Phase 1 已完成）
>
> **⚠️ 广播模型状态（Electron 端必读）**：
> - **本文主体（§1-§6）= 当前 daemon 的现状协议**，其中 §2.6 广播模型 **已实施**（2026-08-03，daemon 改造 + e2e 验证）：
>   - task 的流式响应（delta/tool_start/tool_end/done）**广播给所有订阅该 session 的连接**
>   - **session 级锁**：同一 session 同时只允许一个 task，并发请求返回 `{"error": "session busy"}`
>   - **model_set 广播**：模型切换是全局状态，向所有连接广播
>   - 断开自动退订；单客户端时广播退化为点对点，行为与 v1 一致
> - **Electron 端按本文实现即可**（含 §2.6 广播语义）

---

## 1. 传输与连接

### 1.1 传输层

| 项 | 值 |
|----|-----|
| 协议 | WebSocket（`ws://`） |
| 地址 | `ws://127.0.0.1:<port>`（本机）；`wss://<host>:<port>`（远程，Phase 5） |
| 端口 | 动态分配（daemon 启动时 `port=0`） |
| 端口/token 文件 | `~/.emrg/emrgd.port`，内容两行：`<port>\n<token>`（mode 0o600） |

**port 文件生命周期**（Electron 端判断 daemon 状态的依据）：
- **正常 shutdown**：daemon `finally` 块 `cleanup_server()` 删除 port 文件（daemon.py:202）——port 文件消失 = daemon 正常退出
- **异常崩溃**（SIGKILL/断电）：port 文件**残留**——port 文件存在 ≠ daemon 活着，需 TCP connect 验证
- **Electron 判断流程**：port 文件不存在 → daemon 未运行（可拉起）；port 文件存在 → TCP connect 到该端口，成功 = 活着，失败 = 僵尸残留（需清理 + 拉起）
- **重启**：新 daemon 启动时重写 port 文件（新端口 + 新 token）
| 最大消息 | **16MB**（Python 客户端需 `max_size=16*1024*1024`；Python websockets 默认 1MB 会断连） |

**max_size 的跨端差异**（Electron 端注意）：
- **Python 客户端**（TUI/CLI）：必须显式设 `max_size=16MB`（websockets 库默认 1MB）
- **Node 客户端**（Electron）：**内置 WebSocket（v22+）maxPayload 不可配置、默认无限制**（实测 v25）；若用 `ws` 库，默认也无限制——**Node 端无需设置**
- 服务端（Python daemon）：必须设 `max_size=16MB`（daemon.py:176，已设）——这限制的是**服务端接收**，与客户端无关
- **⚠️ 发送超限的后果（实测）**：客户端发送 >16MB 消息 → 服务端回 **`1009 (message too big)` 并关闭连接**（实测 17.x）。Electron 端**不要发送 >16MB 的消息**（客户端应自行限制），否则连接被断
- 实际消息上限约 200KB（bash 工具输出截断），16MB 余量充足
| 消息编码 | 每条 WS 消息 = 一个 UTF-8 JSON 对象（`json.dumps(..., ensure_ascii=False)`） |

### 1.2 连接流程（auth 握手）

```
1. 读 ~/.emrg/emrgd.port → 得到 port 和 token
2. 建立 WS 连接 ws://127.0.0.1:<port>
3. 发送首帧认证:  {"type": "auth", "token": "<token>"}
4. 等待确认:      收到 {"type": "auth_ok"} → 认证通过，进入正常协议
5. 认证失败:      WS 连接被关闭（无 auth_ok）→ 客户端报错，不重连
```

**关键规则**：
- **首帧必须是 auth**——daemon 收到非 auth 首帧会直接关闭连接
  - 首帧非法 JSON（`not json`）→ `json.loads` 异常 → **关闭连接**（无 error 帧）
  - 首帧合法 JSON 但非 `{"type":"auth",...}`（如 `{"type":"ping"}`）→ **关闭连接**（无 error 帧）
  - 首帧 auth 但 token 错 → **关闭连接**（无 error 帧）
  - **三种情况都不回 error 帧**——客户端只能通过"收到 ConnectionClosed / 无 auth_ok"感知（connect.py 据此抛 AuthError）
- **10 秒超时**——连接后 10s 内不发 auth，daemon 关闭连接
- **auth_ok 是唯一确认**——没有它，客户端无法区分"认证失败"和"网络瞬断"
- **认证失败 ≠ 重连**：token 错是配置问题，重连只会无限循环。`AuthError` 报错退出
- **连接被拒**（daemon 未运行）：`ConnectionRefusedError` / 读 port 文件 `FileNotFoundError` → 可重试

### 1.3 消息形态

- **每条消息是一个 JSON 对象**（不是数组/字符串）
- 客户端发非法 JSON → daemon 返回 `{"error": "invalid json: <detail>"}`，连接保持
- 客户端发合法 JSON 但非对象（如 `[1,2]`、`"hi"`）→ daemon 返回 `{"error": "message must be a JSON object"}`，连接保持
- 客户端发未知 type → daemon 返回 `{"error": "unknown message type", "received": "<type>"}`

---

## 2. 消息总览

### 2.1 客户端 → 服务端（请求）

| type | 用途 | 必填字段 |
|------|------|---------|
| `auth` | 认证（首帧） | `token` |
| `ping` | 健康探测 | — |
| `task` | 执行任务（聊天/工具循环） | `session_id`, `cwd`, `prompt` |
| `cancel` | 中断当前任务 | `session_id`（可选——TUI 实际只发 `{"type": "cancel"}`，daemon 按连接取消） |
| `init_auto_evolve` | 开启项目自动演化 | `cwd` |
| `list_tasks` | 列出演化任务 | — |
| `trigger_task` | 手动触发演化任务 | `name` |
| `compact` | 压缩会话 | `session_id`, `cwd` |
| `list_sessions` | 列会话 | `cwd` |
| `resume_session` | 恢复会话 | `session_id`, `cwd` |
| `rename_session` | 重命名会话 | `session_id`, `cwd`, `title`（空=自动生成） |
| `list_memories` | 列记忆 | `scope`, `session_id`, `cwd`（session scope 需要后两者） |
| `read_memory` | 读记忆 | `scope`, `memory_id`, `session_id`, `cwd` |
| `rant` | 提交反馈 | `message`, `project`（可选） |
| `list_models` | 列可用模型 | — |
| `set_model` | 切换模型 | `model` |
| `list_projects` | 列项目 | — |
| `clear_session` | 清空会话 | `session_id`, `cwd` |
| `delete_session` | 删除会话 | `session_id`, `cwd` |
| `list_history` | 列会话历史 | `session_id`, `cwd` |
| `rewind_session` | 回滚会话 | `session_id`, `cwd`, `record_index` |
| `shutdown` | 关闭 daemon | — |

### 2.2 服务端 → 客户端（响应/事件）

| type | 触发 | 关键字段 |
|------|------|---------|
| `auth_ok` | 认证成功（连接握手的唯一确认，见 §1.2——非业务消息，无 §3 小节） | — |
| （pong） | `ping` | `identity`, `uptime_seconds`, `evolution_count`, `started_at`, `pid`, `model`（**无 type 字段**） |
| （delta 帧） | 流式文本 | `request_id`, `content`, **`delta=true`**, `done=false`, `session_id`（**无 type 字段**，靠 `delta` 布尔标识） |
| （done 帧） | 任务完成 | `request_id`, `content`, `done=true`, `delta=false`, `session_id`（+ `cancelled=true` 若被取消；**无 type 字段**，靠 `done` 布尔标识） |
| `tool_start` | 工具开始 | `request_id`, `tool_name`, `tool_call_id`, `arguments` |
| `tool_end` | 工具结束 | `request_id`, `tool_name`, `tool_call_id`, `content`, `error` |
| `cancelled` | cancel 确认 | `session_id` |
| `error` | 错误 | `error`（+ 可选 `received`） |
| `tasks_list` | `list_tasks` | `tasks` |
| `trigger_result` | `trigger_task` | `ok`/`error` + 任务信息 |
| `compact_result` | `compact` **或主动推送** | `session_id`, `messages_compacted`, `summary`, `auto`（主动推送时 true） |
| `sessions_list` | `list_sessions` | `sessions` |
| `resume_result` | `resume_session` | `session_id`, `meta`, `error` |
| `rename_result` | `rename_session` | `session_id`, `title` |
| `memories_list` | `list_memories` | `scope`, `directory`, `index_path`, `index`, `memories` |
| `memory_content` | `read_memory` | `scope`, `memory_id`, `file`, `path`, `content`, `frontmatter`, `body` |
| `ok` | 简单确认（rant / init_auto_evolve） | `ok`, `count`/`message`（**无 type 字段**） |
| `models_list` | `list_models` | `models`, `current` |
| `model_set` | `set_model` | `model`, `context_window`, `previous` |
| `projects_list` | `list_projects` | `projects` |
| `clear_result` | `clear_session` | `session_id`, `ok`/`error` |
| `session_deleted` | `delete_session` | `session_id`, `ok`/`error` |
| `history_list` | `list_history` | `session_id`, `messages` |
| `rewind_result` | `rewind_session` | `session_id`, `ok`, `record_index`, `removed_count`/`error` |
| `shutdown_ack` | `shutdown` | — |

> **⚠️ 识别帧类型的方式（Electron 端关键）**：并非所有响应都有 `type` 字段。完整识别顺序：
> 1. 有 `type` → 按 type 分发（`auth_ok`/`tool_start`/`tool_end`/`cancelled`/`error`/`xxx_list`/`xxx_result`/...）
> 2. 无 `type` 但 `delta===true` → **流式文本增量**（delta 帧）
> 3. 无 `type` 但 `done===true` → **任务完成**（done 帧，可能带 `cancelled`）
> 4. 无 `type` 且含 `uptime_seconds` → pong
> 5. 无 `type` 且含 `ok` → 简单确认
> **TUI 即此方式**：`data.get("done")`/`data.get("delta")` 布尔判断（protocol.py TaskResponse.from_dict），**不**用 `data.type == "delta"`（那是错的）

> **服务端主动推送**（客户端未请求也会收到）：`compact_result`（`auto: true`，task 流式中途，§3.10）。客户端事件循环必须容忍响应流中插入其他类型事件。

> **📡 广播语义（Phase 2 已实施，2026-08-03）**：
> - **广播给 session 订阅者**（所有订阅了该 session 的连接，含发起者）：delta 帧、done 帧、`tool_start`、`tool_end`、`compact_result`（手动/自动）——同一 session 的所有客户端看到相同的流式响应
> - **广播给所有连接**（全局状态）：`model_set`——模型切换后所有客户端状态栏同步
> - **保持点对点（非广播）**：`auth_ok`、pong、错误帧、`cancelled` 确认（谁 cancel 谁知道）、列表/结果类响应（`sessions_list`/`models_list`/`resume_result`/...）——请求者专用
> - **session 级锁**：同一 session 同时只允许一个 task；第二个 task 请求返回 `{"error": "session busy", "session_id": "..."}`
> - 详见 §2.6

---

## 2.5 请求-响应关联（Electron 端架构级约束，勿设计成 Promise 配对）

**核心事实**：协议**没有通用的请求 ID 机制**——除 `task` 外，所有请求的响应**都不携带请求标识**。

| 请求类型 | 响应如何识别 |
|---------|-------------|
| `task` | 流式帧带 `request_id`（= 请求的 `id`，若客户端未传则 daemon 生成）——**delta/done 帧无 type**，靠 `delta`/`done` 布尔标识（§2.2）；`tool_start`/`tool_end`/`cancelled` 有 type |
| `ping` | 无 type、含 `uptime_seconds` |
| `rant` / `init_auto_evolve` | 无 type、含 `ok` |
| 其余全部（list_sessions/compact/rename/...） | **靠 `type` 字段区分**（`sessions_list`/`compact_result`/`rename_result`/...） |

**TUI 的消费模型**（Electron 端应照此设计）：
- 读循环是**单一事件分发器**：`if data.get("type") == "xxx"` 链式分发（app.py:458-935），不是"请求-响应 Promise 配对"
- **非 task 请求是"发后即忘"**——发完不等待特定响应，响应到达时按 type 更新全局状态（会话列表、模型列表等）
- `task` 是唯一的"有 id 关联"流程：发送时带 `id`，流式响应（delta/tool_start/tool_end/done）都带 `request_id`，客户端据此把流式事件归到对应任务

**Electron 端设计建议**：
- **不要**用"每个请求一个 Promise，响应按 id resolve"模式——非 task 响应没有 id，会死等
- **要**用"事件总线 + 状态存储"模式：读循环把消息按 type 分发到 store（会话列表 store、模型 store、聊天 store），UI 订阅 store
- `task` 流式事件用 `request_id` 归到当前聊天任务（单任务即可，协议不支持并发 task，§3.1）
- 若需"发 list_sessions → 等 sessions_list"，可用简单的"等待特定 type 出现"（超时保护），但**不要假设响应会带请求 id**

> **广播后的演进（§2.6 Phase 2 实施后）**：任务并发的约束从"每连接一个 task"升级为"**每 session 一个 task**"——跨连接也不允许并发（session busy 仲裁）。且响应可能来自**其他客户端发起的 task**（`request_id` 不匹配但 `session_id` 匹配）——Electron 端的事件分发需能处理"他人消息"。实施后本节的"协议不支持并发 task"应更新为"每 session 一个 task（session busy）"。

---

## 2.6 多客户端广播语义（详细设计：同一 session 的所有客户端看到相同响应）

> **决策（2026-08-03）**：从用户视角，"在多个客户端打开同一 session = 从不同地方和同一 Agent 对话，看到同样结果"。因此协议采用**广播模型**——task 的流式响应**广播给所有订阅了该 session 的连接**，而非只发给发起者。
>
> **状态**：✅ **已实施（2026-08-03）**——daemon 广播改造完成（`_broadcast`/`_broadcast_all`/session 级锁），e2e 测试覆盖广播、session busy、退订、model_set 广播。v1（点对点）成为历史行为。

### 2.6.1 数据结构

```python
# daemon 实例属性（__init__ 中初始化）
self._session_subscribers: dict[str, set[asyncio.Task]] = {}  # session_id → 连接的 _handle_client 任务
self._session_busy: dict[str, bool] = {}                       # session_id → 是否有活跃 task

# 说明：
# - 用 _handle_client 的任务对象（而非 ws）作订阅者标识——连接断开时任务结束，便于清理
# - 或者用 id(ws) 作标识 + 显式退订（见 2.6.4 两种实现）
```

### 2.6.2 订阅/退订时序

```
订阅：
  客户端发 task/session_id 消息 → _handle_client 读循环检测到 session_id
  → self._session_subscribers.setdefault(session_id, set()).add(当前连接)
  → 后续该 session 的流式响应广播给此集合所有连接

退订（两种触发）：
  a) 连接断开：_handle_client finally → 从所有订阅集合移除本连接
  b) 客户端显式切 session：读循环检测到新的 session_id → 从旧 session 订阅集合移除，加入新的
```

**精确规则**：
- 连接**最后一条消息**的 `session_id` 决定它当前订阅的 session（`last_session_id` 已是 _handle_client 局部变量，daemon.py:218）
- 读循环每次更新 `last_session_id` 时同步更新订阅表（旧 session 移除、新 session 加入）——**精确插入点：daemon.py:259**（`last_session_id = data["session_id"]` 处，读循环唯一更新点，task/cancel/compact 等所有带 session_id 的消息都会走到）
- 断开时（finally）按 `last_session_id` 从订阅表移除

```python
# daemon.py:258-261 改造（读循环内）：
if data.get("session_id"):
    new_sid = data["session_id"]
    if new_sid != last_session_id:
        if last_session_id:  # 退旧订
            self._session_subscribers.get(last_session_id, set()).discard(ws)
        self._session_subscribers.setdefault(new_sid, set()).add(ws)  # 入新订
        last_session_id = new_sid
```

### 2.6.3 广播改造点（daemon.py）

**核心**：新增一个 `_broadcast(session_id, data)` 方法，替换流式路径的 `_send(ws, ...)`。**采用方式 A（订阅 ws 对象，见 2.6.4）**：

```python
# 数据结构（方式 A）：订阅的是 ws 对象本身
self._session_subscribers: dict[str, set] = {}   # session_id → set[ws]
self._all_connections: set = set()                # 所有已认证连接（model_set 广播用）

async def _broadcast(self, session_id: str, data: dict) -> None:
    """Send data to all subscribers of session_id (including the originator)."""
    for ws in list(self._session_subscribers.get(session_id, ())):
        try:
            await self._send(ws, data)
        except Exception:
            pass  # 单个订阅者失败不影响其他（ws 已断的 _send 返回 False 或抛异常）

async def _broadcast_all(self, data: dict) -> None:
    """Send data to ALL authenticated connections (model_set 等全局状态用)."""
    for ws in list(self._all_connections):
        try:
            await self._send(ws, data)
        except Exception:
            pass
```

> 订阅/退订在 `_handle_client` 读循环中维护（2.6.2）：`setdefault(session_id, set()).add(ws)` / `discard(ws)`；`_all_connections` 在 auth 通过时 `add(ws)`、finally 时 `discard(ws)`（2.6.4）。
> **改造方式**：`_run_tool_loop`/`_run_chat_once`/`_handle_compact` 三个函数**都有 `session` 参数**（daemon.py:1035/976/1539）——把 17 处 `_send(ws, {...})` 改为 `_broadcast(session.session_id, {...})` 即可，无额外参数传递。
> **广播 key**：`_run_tool_loop` 内的流式帧用 `session.session_id`（daemon.py:1083/1106/...），session 由 `_get_or_create_session(session_id)` 创建——**`session.session_id == req.session_id`**，与订阅表 key 一致，直接 `self._broadcast(session.session_id, data)`。

**改造点清单**（_run_tool_loop 内 12 处 `_send(ws, ...)`，daemon.py:1035-1538 + _run_chat_once 内 2 处）：

| 行号 | 消息 | 是否广播 |
|------|------|---------|
| 1078 | round 间 cancel 检查的 done | ✅ 广播（session 订阅者） |
| 1104 | auto-compact 的 compact_result | ✅ 广播（所有客户端看到压缩发生） |
| 1126 | auto-compact 完成 compact_result | ✅ 广播 |
| 1158 | **delta**（流式文本） | ✅ 广播（核心） |
| 1183 | CancelledError 的 done | ✅ 广播 |
| 1193-1198 | LLM 错误 error + done | ✅ 广播（任务流式中的错误，所有客户端看到任务失败） |
| 1223 | Case 1 最终 done | ✅ 广播 |
| 1302 | tool_start | ✅ 广播（所有客户端看到工具执行） |
| 1358 | tool_end | ✅ 广播 |
| 1383 | Case 3 done | ✅ 广播 |
| 1399 | 超限 done | ✅ 广播 |
| 1018 | **_run_chat_once done 帧**（stream=false） | ✅ 广播（虽当前无客户端用 stream=false，但广播模型应一并覆盖） |
| 1031 | **_run_chat_once LLM error**（stream=false） | ✅ 广播（同上） |
| 1545 | **_handle_compact**：Not enough messages | ✅ 广播（手动 compact 结果，所有客户端看到） |
| 1564 | **_handle_compact**：Compact failed | ✅ 广播 |
| 1577 | **_handle_compact**：compact 成功 | ✅ 广播 |

**非广播的发送**（保持 `_send(ws, ...)` 点对点）：
- 认证响应（auth_ok）
- **pong（ping 响应）**——连接级健康探测，只回给发起 ping 的连接（若广播，A 的 ping 会让所有订阅者收到 pong，语义混乱）
- 错误帧（invalid json / 非 dict / 未知 type）——连接级
- 列表/结果类响应（sessions_list/models_list/resume_result/...）——**请求者专用**，不广播
- cancel 的 `cancelled` 确认（daemon.py:275）——发给**发起 cancel 的连接**（谁取消谁知道）

**⚠️ `model_set`（模型切换）→ 广播（决策 2026-08-03）**：模型是 daemon **全局状态**，切换后所有客户端应看到一致（GUI 切模型，TUI 状态栏同步更新）。实现：
- `_handle_set_model` 成功后，除回复请求者外，向**所有连接**广播 `model_set`
- 广播目标：**全部连接**（不限于 session 订阅者）——模型状态与 session 无关，所有客户端都应感知
- 需要 daemon 维护 `self._all_connections: set[ws]`（连接建立加入、断开移除）——或复用 `session_subscribers` 的所有值并集（不精确，推荐独立集合）
- 各客户端收到广播的 `model_set` → 更新状态栏模型名（TUI 现状逻辑：收到 model_set 更新状态栏，app.py:764-772——广播后该逻辑自然工作）

### 2.6.4 两种实现方式（选一）

**方式 A：订阅 ws 对象**（简单直接）
```python
self._session_subscribers: dict[str, set[ws]] = {}
self._all_connections: set[ws] = set()   # model_set 广播用（§2.6.3）
```
- 连接建立（auth 通过后）：`self._all_connections.add(ws)`
- 退订：`_handle_client` finally 中（daemon.py:330-340 改造）：
  ```python
  # finally 内，在 await ws.close() 之前：
  if last_session_id:
      self._session_subscribers.get(last_session_id, set()).discard(ws)
  self._all_connections.discard(ws)
  ```
- 复杂度：O(订阅数×session数)，连接少时无所谓（TUI + GUI + scheduler ≈ 3）

**方式 B：订阅连接任务 + ws 映射**（扩展性好，推荐）
```python
self._session_subscribers: dict[str, set[asyncio.Task]] = {}  # task = _handle_client 协程
# 每个连接维护 self._ws（_handle_client 参数），广播时通过 task 找 ws
# 退订：task 结束（finally）自然从集合移除（需显式 remove，或广播时跳过 done 任务）
```
> 推荐 **方式 A**（v1 简单）：连接数少，O(n) 退订可接受。方式 B 留给未来连接多的场景。

### 2.6.5 session 级锁（并发 task 仲裁 + 写竞态根治）

**协议语义**：同一 session 同时只允许**一个**进行中的 task——第二个客户端发 task 时，若 session 有活跃 task，返回 `{"error": "session busy", "session_id": "..."}`。

```python
# daemon 实例属性
self._session_busy: dict[str, bool] = {}

# task 处理（daemon.py:283-324 改造）：
if data.get("type") == "task":
    session_id = data.get("session_id", "")
    if self._session_busy.get(session_id):
        await self._send(ws, {"error": "session busy", "session_id": session_id})
        continue
    self._session_busy[session_id] = True
    # ... 启动 _run_tool_loop，其 finally 中 self._session_busy[session_id] = False
```

**锁的释放——⚠️ 必须外包 try/finally（现状函数无 finally）**：

> **核查发现（2026-08-03）**：`_run_tool_loop`（daemon.py:1035-1538）**没有顶层 finally**——11 个 `return` 直接散落函数体，函数内只有局部 try/except（auto-compact、LLM 流）。直接说"在函数的 finally 释放锁"是**不可实施的**。

**正确实现（二选一）**：

```python
# 方式 1：外包 wrapper（推荐，不动 _run_tool_loop 内部结构）
async def _run_tool_loop_locked(self, req, ws, session, cancel_event):
    session_id = session.session_id
    try:
        await self._run_tool_loop(req, ws, session, cancel_event)
    finally:
        self._session_busy[session_id] = False

# 调用方改为：
_tool_task = asyncio.create_task(
    self._run_tool_loop_locked(req, ws, session, _cancel_event)
)
```

```python
# 方式 2：给 _run_tool_loop 整体包 try/finally（改动函数体缩进，风险高）
# 不推荐：11 个 return 都要包进 try，diff 大、易错
```

> **注意**：调用方是 `asyncio.create_task(...)` fire-and-forget（daemon.py:316-321），**没有 await 包裹**——锁释放必须在任务内部（wrapper 的 finally），不能在调用方。
> **cancel 路径**：`_tool_task.cancel()` 后 `await _tool_task`（daemon.py:269-271）→ 任务被取消 → wrapper finally 执行 → 锁释放。✅

**这同时根治 §5 的多连接写竞态**：session 级串行后，append 与 compact 不会并发（同一 session 的写都发生在"唯一活跃 task"的协程内）。

### 2.6.6 对客户端的影响

| 项 | v1（现状） | 广播模型 |
|----|-----------|---------|
| 收到响应的连接 | 只有发起者 | 所有订阅该 session 的连接 |
| 客户端如何区分"我的响应" | request_id 匹配 | 同左（request_id 匹配 = 自己发起的） |
| 客户端如何渲染"别人的消息" | 不出现 | 按 session_id 过滤展示（可选：显示"其他设备的消息"） |
| 多客户端同时发 task | 各自独立（可能并发写 session） | **session busy 仲裁**（2.6.5） |
| compact_result（手动/自动） | 只发给请求者 | 广播（所有客户端看到压缩发生） |
| cancelled 确认 | 发给发起者 | **只发给发起 cancel 的连接**（非广播） |
| model_set（模型切换） | 只发给请求者 | **广播给所有连接**（全局状态，所有客户端状态栏同步，§2.6.3） |

**Electron 端渲染建议**：
- 维护 `current_session_id`；收到广播消息时，`session_id == current` 才渲染，否则忽略（或提示"其他设备消息"）
- `request_id` 匹配自己发起的 task → 正常流式渲染；不匹配但同 session → 作为"他人消息"渲染（只读展示）
- 收到 `session busy` 错误 → 提示"该会话正在其他客户端使用中"

### 2.6.7 协议变更汇总（实施后契约同步更新点）

| 位置 | 变更 |
|------|------|
| §2.2 消息总览 | delta/done/tool_start/tool_end/compact_result 标注"广播给 session 订阅者"；model_set 标注"广播给所有连接" |
| §2.5 请求-响应 | 补充"响应可能来自其他客户端的 task（request_id 不匹配但 session_id 匹配）" |
| §3.1 task | 响应"广播给该 session 的所有连接"；新增 `session busy` 错误 |
| §3.2 cancel | cancelled 确认只发给发起者（非广播） |
| §3.6 模型 | model_set 广播给所有连接（§2.6.3） |
| §5 写竞态 | 由 session 级锁解决（2.6.5） |
| §6 错误表 | 新增 `{"error": "session busy", "session_id": "..."}` |

### 2.6.8 边界情况

| 场景 | 行为 |
|------|------|
| 单客户端（TUI 或 GUI 单独） | 订阅者 = 自己 = 与 v1 完全一致（广播退化为点对点） |
| 连接中途切 session | 读循环更新 last_session_id → 退旧订、入新订 |
| 连接断开 | finally 从所有订阅集合移除；不影响其他订阅者 |
| 广播时某订阅者已断 | `_send` 失败 → 捕获跳过；下轮订阅集合已不含它 |
| 两个客户端同时发 task 到不同 session | 互不干扰（不同 session 的锁独立） |
| 同一客户端发 task 到 session A，又发到 session B | 每连接一个 _tool_task——发 B 会 cancel 当前 task（现状语义）；广播仍按各自 session |
| scheduler（演化引擎） | 用独立 session_id（`emrg-evolution-<name>`），不与其他客户端冲突 |

---

## 3. 核心消息详细格式

### 3.1 task（最重要的消息）

**请求**：
```json
{
  "type": "task",
  "id": "uuid",            // 建议必传：客户端生成，流式响应 request_id 即此值；
                           // 不传则 daemon 生成随机 uuid（客户端无法预知 request_id）
  "session_id": "s_xxx",   // 必填
  "cwd": "/path/to/project", // 必填
  "prompt": "你好，帮我看看这个项目",
  "timestamp": "2026-08-02T09:00:00",  // 可选
  "stream": true,          // 可选，默认 false；true=流式，false=一次性 chat
  "images": [              // 可选，图片列表（vision 模型）
    {"path": "/abs/path/img.png", "label": "截图1", "position": 0}
  ]
}
```

**images 字段语义**（Electron 端做图片功能需要）：
- `path`：图片在**客户端机器上的绝对路径**（daemon 读该文件 base64 编码发给 LLM）——**本机连接才有效**（远程 Phase 5 需重新设计，见 roadmap）
- `label`：展示用标签（非 vision 模型降级为文本占位时用）
- `position`：图片在 prompt 文本中的**字符位置**（可选，默认 -1 = 末尾）——daemon 按位置把文本分段与图片交错（daemon.py:940-975）
- **客户端负责把图片存盘**：TUI 粘贴图片 → 存 `<cwd>/.emrg/sessions/<session_id>/images/` → 传绝对路径（app.py:1018-1038）
- **非 vision 模型**：daemon 不读文件，降级为 `[用户粘贴了 N 张图片: label1, label2。当前模型不支持图片理解...]` 文本（daemon.py:944-948）
- 图片文件读取失败 → 降级为 `[Image unavailable: label]` 文本（daemon.py:963-966）
- 缺 `session_id`/`cwd` → `{"error": "task requires session_id and cwd"}`
- `stream=true` → 走 `_run_tool_loop`（流式 delta + 工具循环）——**当前所有客户端（TUI）都用此模式**（app.py:1834）
- `stream=false` → 走 `_run_chat_once`（一次性 chat，无工具循环）——**协议支持但当前无客户端使用**（daemon 内部可达，daemon.py:317-324）。Electron 端建议也用 `stream=true`（流式体验一致）
- **⚠️ `stream` 缺省默认 `false`（Electron 端高发坑）**：daemon 用 `stream=data.get("stream", False)`（daemon.py:298）——**不传 stream = 一次性 chat = 无工具执行能力**（`_run_chat_once` 虽把 tools 传给 LLM，但不执行工具调用）。Electron 端**必须显式传 `stream: true`** 才能获得工具调用能力——忘记传会表现为"LLM 想调用工具但 daemon 不执行，只回文本"
- **⚠️ 未知 session_id 自动创建**（关键语义）：daemon 对任何 session_id 调 `_get_or_create_session`（daemon.py:309）——不存在则自动创建（`create_with_id` 写 meta.json，session.py）。**Electron 端新建会话 = 生成 session_id 直接发 task，无需先验证存在**（区别于 resume_session 的"not found"校验）
- **空 prompt 无校验**：daemon 不检查 prompt 为空——空 prompt 会直接发给 LLM（浪费一次调用）。**客户端应自行校验** prompt 非空再发

**流式响应序列**（`stream=true`）——**注意：delta/done 帧无 `type` 字段**，靠 `done`/`delta` 布尔标识：
```
→ {"request_id": "...", "content": "你好", "done": false, "delta": true, "session_id": "s_xxx"}
→ {"request_id": "...", "content": "，我",   "done": false, "delta": true, "session_id": "s_xxx"}
→ {"type": "tool_start", "request_id": "...", "tool_name": "bash", "tool_call_id": "call_1",
   "arguments": {"command": "ls"}}
→ {"type": "tool_end", "request_id": "...", "tool_name": "bash", "tool_call_id": "call_1",
   "content": "file1\nfile2\n", "error": false}
→ {"request_id": "...", "content": "结果如下", "done": false, "delta": true, "session_id": "s_xxx"}
→ {"request_id": "...", "content": "", "done": true, "delta": false, "session_id": "s_xxx"}
```

**非流式响应**（`stream=false`，一次性 chat）：**无 delta 帧**，只有单个完成帧（同样无 type）：
```
→ {"request_id": "...", "content": "<完整回答>", "done": true, "delta": false,
   "session_id": "s_xxx"}
```
> `stream=false` 走一次性 chat（`_run_chat_once`），不执行工具循环，`content` 为完整回答。

**取消时的响应**（cancel 后 tool loop 终止）：
```
→ {"type": "cancelled", "session_id": "s_xxx"}        // cancel 确认（立即，有 type）
→ {"request_id": "...", "content": "", "done": true, "cancelled": true,
   "session_id": "s_xxx"}                             // tool loop 收尾（无 type，done 帧 + cancelled 标记）
```
> **注意**：cancel 会收到**两条**消息——`cancelled` 确认（有 type）+ 被取消任务的 `done` 帧（无 type，带 `cancelled: true`）。客户端应以 `done.cancelled` 判断任务被取消，而不是等 `cancelled` 就重置状态。

**关键语义**：
- `delta` 帧：`done=false`、`delta=true`、`content` 为增量文本（**拼接**而非整段）；**实际实现带 `session_id`**（daemon.py:1155-1164），客户端可用它确认消息归属会话
- `tool_start`/`tool_end` 成对出现，用 `tool_call_id` 关联
- **⚠️ `tool_start.arguments` 可能与实际执行不完全一致**：daemon **先发 tool_start，后注入默认参数**（daemon.py:1303-1315）——`bash`/`glob` 缺 `workdir` 时注入会话 cwd、`grep` 缺 `path` 时注入会话 cwd。即：`tool_start` 通知的 arguments 是 LLM 原始参数，**实际执行的 args 可能多了 `workdir`/`path`**。客户端展示工具命令时应知此差异（实际执行以注入后为准）；`tool_end` 返回执行结果，不返回执行参数
- `tool_end.content` = 工具执行的完整输出（可能很大，~200KB 上限）
- `tool_end.error` = true 表示工具执行失败
- 最终 `done` 帧：`done=true`、`delta=false`、`content` 通常为空（内容已通过 delta 流式发出）
- **LLM 错误**：`{"error": "LLM error: <detail>. Check config at ~/.emrg/config.toml"}` 后跟一个 `done` 帧（防客户端死锁）
- **超限提示**：`done.content` 可能含 "Exceeded"/"exceeded"（max tool rounds，daemon.py:1395-1405），客户端应提示"Try '继续' to resume"——这是 done 帧 content 非空的**唯一正常场景**
- **并发 task**：同一连接同一时刻只允许**一个**进行中的 task——若 `_tool_task` 未完成时收到新 task，daemon **先 cancel 旧任务再启动新任务**（daemon.py:305-308）。客户端发送新 task 前应确保旧任务已结束（或主动 cancel）。
- **广播后的并发语义（§2.6 Phase 2，与现状并存）**：现状的"同连接 cancel 旧 task"**保留**（同一客户端快速连发 task 仍 cancel 旧的）；广播新增"**跨连接同 session 仲裁**"（session busy：另一连接对同 session 发 task 被拒）。两层机制：
  - 同连接新 task → cancel 旧的（现状，daemon.py:305-308）
  - 跨连接同 session 新 task → session busy 拒绝（广播后新增，§2.6.5）
  - 跨连接不同 session → 互不干扰
- **✅ done 帧保证（Electron 端任务状态机依据）**：`_run_tool_loop` 的**所有退出路径都发 done 帧**（人工核对 11 个 return，daemon.py:1035-1538）——正常完成（Case 1/3）、LLM 错误（error + done，daemon.py:1196-1202）、cancel（cancelled:true + done）、超限（done + "Exceeded" 提示）、round 间 cancel 检查（daemon.py:1078-1086）。**客户端可安全地"收到 done 才结束任务状态"**，不会死等。唯一不发 done 的场景是 `client disconnected`（_send 失败 = 客户端已断，无需发）

### 3.2 cancel（中断）

```json
{"type": "cancel"}               // 最小形态（TUI 即此，app.py:1059）
{"type": "cancel", "session_id": "s_xxx"}  // 可带 session_id（仅作确认回显）
```
→ 响应 `{"type": "cancelled", "session_id": "s_xxx"}`
- **cancel 是连接级**：取消**当前连接**正在运行的 `_tool_task`（daemon 每连接一个工具任务）——与 session_id 无关，session_id 仅回显
- **cancel 后 tool loop 还会发一个带 `"cancelled": true` 的 `done` 帧**（§3.1 取消时响应）——客户端共收到 `cancelled` + `done(cancelled=true)` 两条

### 3.3 ping（健康探测）

```json
{"type": "ping"}
```
→ 响应（**无 type 字段**，用字段识别——客户端以 `uptime_seconds` 存在作为 pong 判定）：
```json
{
  "identity": {
    "instance_id": "emrg-xxxxxxxx",
    "host_name": "macbook",
    "fork_source": "https://github.com/argszero/emrg.git",
    "branch_id": "master"
  },
  "uptime_seconds": 123,
  "evolution_count": 5,
  "started_at": "2026-08-02T09:00:00.123",
  "pid": 12345,
  "model": "deepseek-chat"
}
```
> **pong 识别**：响应无 `type` 字段，客户端以 `"uptime_seconds" in data` 判断（TUI 即此方式，app.py:441）。这是协议中**无 type 的响应之一**（共两类：pong 含 `uptime_seconds`；`ok` 含 `ok`）。Electron 端事件分发顺序：先判 `type` → 无 `type` 则判 `uptime_seconds`（pong）→ 判 `ok`（简单确认）→ 否则未知响应。

> **保活**：daemon 侧 websockets 库每 20s 自动发协议层 ping（库内处理，不进入业务 `recv`），Node 端自动回 pong——**连接活跃性由 daemon 保证**。Electron 端**建议**每 20-30s 额外发一个应用层 `ping`（§3.3）：① 探测 daemon 是否重启（收不到 pong = 断了）② 让 GUI 空闲时也能及时发现断连。应用层 ping 与协议层 ping 互不冲突。

### 3.4 会话相关

**cwd 语义**（Electron 端理解会话隔离的基础）：
- **session 按 cwd 隔离**：会话数据存 `<cwd>/.emrg/sessions/<session_id>/`（emrg/session.py:54）——不同项目目录的会话互不可见
- **cwd 触发项目追踪**：客户端发任何带 cwd 的消息（task/cancel/compact/...）都会让 daemon `_touch_project` 记录项目（daemon.py:262）——GUI 首启选项目目录后发消息即自动追踪
- **Electron 端应有"项目/目录选择"概念**：GUI 让用户选工作目录 → 用该 cwd 发 list_sessions（看该项目的会话）→ 发 task 聊天
- cwd 必须是**绝对路径**

**session_id 生成**（客户端职责，Electron 端照做）：
- 格式：`s_YYMMDD_HHMM_xxxx`（如 `s_260803_0930_ab12`）
- 规则：时间前缀 + 4 位随机 hex；若 `<cwd>/.emrg/sessions/<sid>` 已存在则换随机后缀重试（最多 100 次）
- 客户端在新建会话时生成（TUI app.py:300），daemon 不生成 session_id——**Electron 端需自行实现**（纯客户端逻辑，参考 emrg/session.py:33）

**list_sessions**：`{"type": "list_sessions", "cwd": "..."}`
→ `{"type": "sessions_list", "sessions": [<meta 对象>, ...]}`
> **实测（2026-08-03）**：每个 session 是 meta.json 的原始内容，keys 为 `session_id, created_at, updated_at, cwd, message_count, compact_count, last_compact_at` + **`title`（仅已重命名的 session 有）**——title 是**可选字段**，未 rename 的 session 无 title。GUI 显示会话名时用 `title or session_id` 兜底。

**resume_session**：`{"type": "resume_session", "session_id": "...", "cwd": "..."}`
→ 成功：`{"type": "resume_result", "session_id": "...", "meta": {"message_count": 10, "compact_count": 1, "created_at": "...", "updated_at": "...", "title": "..."}}`
→ 失败：`{"type": "resume_result", "session_id": "...", "error": "Session xxx not found"}`
> **注意**：resume 只确认会话存在 + 返回 meta——**历史内容客户端直接从磁盘读**（`<cwd>/.emrg/sessions/<session_id>/history.jsonl`），不走协议。

**list_history**：`{"type": "list_history", "session_id": "...", "cwd": "..."}`
→ `{"type": "history_list", "session_id": "...", "messages": [{"record_index": 0, "content": "...", "preview": "...", "timestamp": "..."}, ...]}`（只含 user 消息，content 截断到 80 字）

**rewind_session**：`{"type": "rewind_session", "session_id": "...", "cwd": "...", "record_index": 5}`
→ `{"type": "rewind_result", "session_id": "...", "ok": true, "record_index": 5, "removed_count": 3}`
→ 失败：`{"type": "rewind_result", "session_id": "...", "error": "record_index 99 out of range (0-10)"}`
> **⚠️ 边界（实测 2026-08-03）**：
> - 对**不存在**的 session_id rewind → 返回**范围错误** `"record_index 0 out of range (0--1)"`（不是 "Session not found"）——因为 daemon 先读历史（空）再查 index 范围
> - 错误信息里的 `(0--1)` 是 daemon 格式化 bug（`len(records)-1 = -1` 显示为 `0--1`）——Electron 端**不要解析错误信息格式**，只按 `error` 存在处理
> - record_index 必须 < 历史记录数，否则范围错误

**clear_session**：`{"type": "clear_session", "session_id": "...", "cwd": "..."}`
→ `{"type": "clear_result", "session_id": "...", "ok": true}`
→ 失败：`{"type": "clear_result", "error": "clear_session requires session_id and cwd"}`
> **⚠️ 副作用（实测 2026-08-03）**：对**不存在**的 session_id 调 clear_session，daemon 会**自动创建该 session 再清空**（`_get_or_create_session`，daemon.py:802）——返回 `ok: true` 且目录被创建。Electron 端**不要用 clear_session 探测 session 存在性**（会用副作用创建空会话）；要探测用 `resume_session`（返回 not found）或 `list_sessions`。

**delete_session**：`{"type": "delete_session", "session_id": "...", "cwd": "..."}`
→ `{"type": "session_deleted", "session_id": "...", "ok": true}`（目录存在且删除成功）
→ 失败：`{"type": "session_deleted", "error": "Session xxx not found"}`（**目录不存在**时）
> **注意**：delete_session 以**目录是否存在**为准（daemon.py:817-823），不是以"meta.json 是否合法"为准。若某 session 目录存在但 meta 损坏，delete 仍会删除并返回 ok:true。Electron 端删除会话后应刷新列表。

**rename_session**：`{"type": "rename_session", "session_id": "...", "cwd": "...", "title": "新标题"}`（title 空 = 自动生成）
→ `{"type": "rename_result", "session_id": "...", "title": "新标题"}`
> **注意**：title 为空时 daemon 调 LLM 自动生成标题（`_generate_session_title`，daemon.py:1983）——**响应可能耗时数秒**，客户端应有等待提示。

### 3.5 记忆

**list_memories**：`{"type": "list_memories", "scope": "project"|"session", "session_id": "...", "cwd": "..."}`（session scope 需后两者）
→ `{"type": "memories_list", "scope": "...", "directory": "/path", "index_path": "/path/MEMORY.md", "index": "<MEMORY.md 全文>", "memories": [{"id", "file", "title", "type", "status", "event_at", "created_at", "updated_at"}, ...]}`

**read_memory**：`{"type": "read_memory", "scope": "...", "memory_id": "...", "session_id": "...", "cwd": "..."}`
→ `{"type": "memory_content", "scope": "...", "memory_id": "...", "file": "...", "path": "...", "content": "<markdown 全文>", "frontmatter": {...}, "body": "<正文>"}`
→ 失败：`{"type": "memory_content", "error": "Memory not found: xxx"}`

### 3.6 模型

**list_models**：`{"type": "list_models"}`
→ `{"type": "models_list", "models": [{"name": "deepseek-chat", "context_window": 131072}, ...], "current": "deepseek-chat"}`

**set_model**：`{"type": "set_model", "model": "gpt-4o"}`
→ `{"type": "model_set", "model": "gpt-4o", "context_window": 128000, "previous": "deepseek-chat"}`
→ 缺参：`{"type": "model_set", "error": "set_model requires model name"}`
> **⚠️ 未知模型名不报错（实测 2026-08-03）**：daemon **不校验模型名存在性**（daemon.py:1835-1847）——对 `[[llm.models]]` 无匹配的名字，直接用该名字作 API model 设置，仍返回 `model_set` 成功（context_window 保持当前值）。Electron 端**应先用 `list_models` 校验**再 set_model，避免手滑设置无效模型名。
> 注意：模型切换**不持久化**——daemon 重启后回落到 config.toml 默认。

### 3.7 项目

**list_projects**：`{"type": "list_projects"}`
→ `{"type": "projects_list", "projects": [{"name": "emrg", "repo": "github.com/argszero/emrg", "path": "/path"}, ...]}`

### 3.8 演化任务

**list_tasks**：`{"type": "list_tasks"}`
→ `{"type": "tasks_list", "tasks": [{"name": "...", "running": bool, "next_run_in_seconds": int|null, "interval": int}, ...]}`（每个元素是 handler 状态，scheduler.py `status()`）

**trigger_task**：`{"type": "trigger_task", "name": "emrg-evolution-task"}`
→ 成功：`{"type": "trigger_result", "name": "...", "result": "..."|..., "detail": "..."|null}`（scheduler.py `trigger()` 返回值）
→ 失败：`{"type": "trigger_result", "error": "task 'x' not found"}`
→ 不可达：`{"type": "trigger_result", "error": "scheduler not running"}`

### 3.9 反馈（rant）

**rant**：`{"type": "rant", "message": "feedback text", "project": "emrg", "timestamp": "..."}`
→ `{"ok": true, "count": 12}`
→ 失败：`{"error": "rant requires a message"}`

### 3.10 压缩

**compact**：`{"type": "compact", "session_id": "...", "cwd": "..."}`
→ `{"type": "compact_result", "session_id": "...", "messages_compacted": 42, "summary": "<压缩摘要>"}`
→ 自动压缩：同上 + `"auto": true`；不足消息数：`"messages_compacted": 0, "summary": "Not enough messages to compact."`
→ 失败：`{"type": "compact_result", "session_id": "...", "messages_compacted": 0, "error": "..."}`

> **⚠️ auto-compact 是服务端主动推送**（客户端未请求也会收到）：
> - 当 `auto_compact_threshold > 0` 且会话 token 数超阈值时，daemon 在 **task 流式响应中途**插入 `compact_result`（`"auto": true`）（daemon.py:1095-1130）
> - **客户端事件循环必须容忍"响应流中插入其他类型事件"**——读循环收到 `compact_result` 时不能当作 task 流中断，应单独处理（TUI 的 read_server 即如此），流式 delta 继续
> - 客户端可忽略 `compact_result` 仅作展示，不影响协议状态机

### 3.11 自动演化

**init_auto_evolve**：`{"type": "init_auto_evolve", "cwd": "/path"}`
→ `{"ok": true, "message": "auto_evolve enabled for <name>"}`
→ 失败：`{"ok": false, "error": "init_auto_evolve requires cwd"}`
> **⚠️ 副作用**：成功后会写 `~/.emrg/tasks.yml` 创建演化任务（`interval=600`）+ `_touch_project` 记录项目（daemon.py:579-595）。GUI 首启**不要**自动调用此消息（会污染项目追踪），仅当用户显式"开启自动演化"时使用。

### 3.12 关闭

**shutdown**：`{"type": "shutdown"}`
→ `{"type": "shutdown_ack"}`，随后 daemon 关闭 WS 连接并停止监听
> **⚠️ 测试盲区**：e2e 测试（test_ws_e2e.py 10 用例）**未覆盖 shutdown**（无对应用例）。shutdown 行为（ack + 关连接 + 停监听）来自代码核对（daemon.py:910-917），**未经自动化验证**。Electron 端实现时若遇 shutdown 异常，需补测试。

---

## 4. 断连与重连语义

| 场景 | 客户端行为 |
|------|-----------|
| 认证失败（token 错） | **报错退出**，不重连（AuthError） |
| daemon 未运行（连接被拒/port 文件缺失） | 可重试（等 daemon 起来） |
| 运行中断连（daemon 被杀/网络） | 读循环抛 `ConnectionClosed` → **重连**（TUI 的 `_reconnect`；GUI 需实现同样语义） |
| 重连成功后 | 发 `{"type": "ping"}` 验证 → 恢复 |

**断连语义分端（勿统一成重连）**：

| 客户端 | 断连行为 | 原因 |
|--------|---------|------|
| TUI / GUI（交互式） | **重连**（`_reconnect` 循环，成功后 ping 验证） | 用户正在使用，断连应恢复 |
| scheduler（演化引擎，内部客户端） | **break 收尾，不重连**（scheduler.py:348-352） | 演化周期结束即断开；断开被当作"空周期"，下次周期重连 |
| daemon 的 `_handle_client`（服务端侧） | **break 收尾**（daemon.py:246） | 客户端走了，结束本连接处理 |

> Electron GUI 属"交互式重连"类，照 TUI 语义实现。但要注意：scheduler 也连同一 daemon（演化引擎），它的断连是正常的——GUI 不需要为此做什么。

**重连必须重读 port 文件**（关键）：
- daemon **每次重启生成新 token**（`secrets.token_urlsafe(32)`，serve() 内，daemon.py:179）——旧 token 立即失效
- 客户端重连时**必须重新读 `~/.emrg/emrgd.port`** 拿最新 token 再认证（TUI 的 `connect_to_server` 每次调用都重读，app.py:421）
- **不要缓存 token**：缓存 = daemon 重启后重连必然 AuthError（且 AuthError 语义是"配置问题报错"，会错误地阻止重连）
- 判断"daemon 是否重启"的可靠方式：重连前重读 port 文件（若 port 变了 = 重启了）

**断连重连注意**：
- 进行中的任务在断连后**丢失**（daemon 侧工具任务被取消）——重连后客户端应清 busy 状态
- 会话数据**不丢**（daemon 持久化 history.jsonl），重连后可 `resume_session` 找回

---

## 5. 会话数据落盘（客户端直接读）

部分数据客户端**直接读磁盘**，不走协议（daemon 是唯一写者）：

| 路径 | 内容 |
|------|------|
| `<cwd>/.emrg/sessions/<session_id>/history.jsonl` | 完整会话历史（message/tool_call/tool_result/summary 记录） |
| `<cwd>/.emrg/sessions/<session_id>/meta.json` | 会话元数据 |
| `<cwd>/.emrg/memory/MEMORY.md` | 项目记忆索引 |

> **history.jsonl 记录格式**：每行一个 JSON，实际类型（实测）：
> - `message`（`role`: user / assistant / system）——含 `content`、`timestamp`；assistant 消息可能含 `tool_calls`（OpenAI 格式：`[{"id", "type": "function", "function": {"name", "arguments"}}]`，arguments 是 JSON 字符串）
> - `tool_result`（`tool_name`, `tool_call_id`, `content`, `error`）——工具执行结果
> - `summary`（压缩摘要）——compact 后插入
> **无 `tool_call` 记录**——工具调用参数在 assistant 消息的 `tool_calls` 字段里（daemon 不单独记录）。GUI 渲染历史时按此读取；恢复会话时把 `message`（role: user/assistant）传给 daemon 即可。

> **历史同步语义（Electron 端重要）**：TUI **不监听文件变化**（app.py 无 watch/poll）——切换会话时从磁盘**一次性读取**历史快照（resume 后 `read_text` 全量加载），之后靠**实时流式 delta** 增量。GUI 应照此：`resume_session` 后读一次磁盘全量历史渲染，然后靠 task 流式 delta 追加——**不需要文件监听/轮询**。多客户端并发（TUI+GUI 同会话）时，各自是"快照 + 自己的流式增量"，不保证实时互见对方消息（以 daemon 落盘为准）。

> **⚠️ 多连接写竞态（Electron 端须知）**：daemon/session **无跨连接锁**（核查确认，emrg/session.py 无 Lock）：
> - `append_message` 是 O_APPEND 单行写——并发 append **不损坏**（每行原子）
> - 但 `compact`/`rewind` 用 `_write_history` **全量重写**（"w" 模式，session.py:454）——若与另一连接的流式 append **并发**，重写会覆盖并发的追加（**消息丢失**）
> - 实际场景：TUI 正流式聊天（连接 A 写）时 GUI 对同 session 发 compact（连接 B 重写）→ 可能丢消息
> - **根治方案（§2.6 广播模型配套）**：session 级锁（同一 session 同时只允许一个 task）+ 写操作串行化——实施广播模型时一并解决（session.py 加 asyncio.Lock）
> - **Electron 端 v1 建议**：GUI 避免在"另一客户端可能活跃写同一 session"时发 compact/rewind；或至少提示用户

---

## 6. 错误处理汇总

| 错误 | 响应 | 连接状态 |
|------|------|---------|
| 非法 JSON | `{"error": "invalid json: <detail>"}` | 保持 |
| 非对象 JSON | `{"error": "message must be a JSON object"}` | 保持 |
| 未知 type | `{"error": "unknown message type", "received": "<type>"}` | 保持 |
| 缺必填字段 | `{"error": "task requires session_id and cwd"}`（各消息对应文案） | 保持 |
| LLM 错误 | `{"error": "LLM error: ..."}` + `done` 帧 | 保持 |
| 认证失败 | 连接关闭（无 auth_ok） | 关闭 |

> **error 字段类型**：**永远是字符串**（全仓核查，无 dict/对象情况）——Electron 端可直接当文本渲染，无需分支处理。错误消息通常是"`<消息名> requires <字段>`"格式（缺参）或具体错误描述。

---

## 7. 契约验证

### 7.0 Electron 端实现顺序（按依赖排列，每步可独立验证）

> 拿到本文的 Electron 开发者按此顺序实现，每步都有独立验证点，不依赖后续步骤：

| 步骤 | 实现内容 | 依据 | 验证点 |
|------|---------|------|--------|
| 1 | 读 port 文件 + WS 连接 + auth 首帧 + auth_ok 等待 | §1.1/§1.2 | 连上 daemon，收到 `auth_ok`；错 token 收不到（连接关闭） |
| 2 | 消息分发器（type → uptime_seconds → ok → delta/done 布尔） | §2.2 识别顺序 | 发 ping 收到 pong（uptime_seconds 识别） |
| 3 | 会话管理（list_sessions / resume / 读磁盘历史） | §3.4/§5 | 列出会话、恢复会话、渲染 history.jsonl |
| 4 | 聊天（task + 流式 delta + done） | §3.1 | 发 task 收到流式 delta → done（**必须传 `stream: true`**） |
| 5 | 工具调用（tool_start/tool_end 渲染） | §3.1 | LLM 调工具时看到工具卡片 |
| 6 | cancel / 断线重连 / 保活 ping | §3.2/§4 | ESC 中断；杀 daemon 重连；空闲保活 |
| 7 | 辅助功能（模型/项目/记忆/rant/compact/演化） | §3.5-§3.11 | 各功能对应消息往返 |
| 8 | （Phase 2 后）广播模型兼容 | §2.6 | 处理"他人 task 的流式响应"（request_id 不匹配但 session_id 匹配） |

**每步的"完成"标准**：能用真实 daemon 跑通该步的验证点。第 4 步（聊天）是最小可用里程碑。

**验证方式**（确保契约与实现一致）：
1. 对照 `emrg/server/daemon.py` 的 `_process_message`（消息分发）逐条核对
2. 运行 `tests/test_ws_e2e.py`（10 个端到端用例：auth/协议/流式/cancel/大消息/CJK/断线）
3. Electron 端实现后，用真实 daemon 跑通"ping + 流式聊天"即证明契约可执行

**⚠️ 真实 daemon 测试的副作用（实测教训，2026-08-03）**：
- `init_auto_evolve` 测试**会污染用户配置**——实测向 `~/.emrg/tasks.yml` 添加演化任务 + `~/.emrg/projects.yml` 添加项目（§3.11 警告的实证）。**契约验证勿对真实 daemon 调 init_auto_evolve**（或测后清理）
- `rant` 测试会写入 `~/.emrg/rants.jsonl`（测后需清理）
- `set_model` 测试会切换运行中 daemon 的模型（测后需切回）
- 正确做法：e2e 测试用**隔离 config_dir**（mock `config_dir` 到临时目录），或测后清理上述文件

**契约变更流程**：daemon 侧协议改动 → 更新本文 → 更新 test_ws_e2e.py → 三端（TUI/契约文档/测试）同步。
