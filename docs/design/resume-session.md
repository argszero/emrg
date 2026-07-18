# /resume 设计

## 参考实现

### Claude Code

Client 直接从磁盘读取 session JSONL 文件：

```
~/.claude/projects/<project-hash>/<session-id>.jsonl
```

`switchSession()` 只做一件事：修改内存中的 `STATE.sessionId`。历史渲染完全在 client 端完成，不经过 server。

### Codex

TUI 通过 `thread/resume` RPC 获取完整 rollout history。但 Codex 用的是 gRPC/REST 协议，天然支持大 payload，不存在 line-based protocol 的 64KB 限制。此外 TUI 也可以直接从 `~/.codex/threads/` 读取 transcript 文件。

### 共同点

**Client 自己负责读取和渲染历史。Server 不充当历史数据的中转站。**

## 我的错误

我之前的理解：

```
Client                   Server
  | -- /resume s_xxx -->   |
  |                         | 读 history.jsonl (200KB+)
  |                         | 打包成一行 JSON
  | <-- records: [...] --   |  ← 炸在这里：readline() 64KB 限制
```

问题根源不是 "records 太大"——根源是 **不应该让 server 把历史传给 client**。

Client 和 server 共享同一个文件系统。Client 完全可以自己读 `<cwd>/.emrg/sessions/<id>/history.jsonl`。

## 正确设计

### 流程

```
1. 用户输入 /resume s_xxx
2. Client 发送 {type: "resume_session", session_id: "s_xxx", cwd: "..."} 给 Server
3. Server 验证 session 存在，返回 metadata 确认：
   {type: "resume_result", session_id: "s_xxx", meta: {message_count, created_at, ...}}
   ← 不传 records！
4. Client 收到 ack，切换 session_id
5. Client 直接从磁盘读取 <cwd>/.emrg/sessions/<id>/history.jsonl
6. Client 解析 JSONL（逐行），渲染到 ChatHistory widget
7. 用户输入新消息 → 带上 session_id=s_xxx
8. Server Session.load() → get_messages_for_llm() → LLM 看到完整上下文（已有逻辑，无需改动）
```

### 为什么逐行读 JSONL 不会有 64KB 问题

`history.jsonl` 是 JSONL 格式——每条 record 一行。Client 读文件时可以逐行 `readline()`，每行几 KB，永远不会触发 64KB 限制。

Server 端的问题是它把所有 records 拼进一个 JSON 对象再 `_send()`——这个对象是一行。Client 端逐行读文件没有这个问题。

### 改动点

**Server（`daemon.py`）**：`_handle_resume_session` 不再读 `history.jsonl`，只验证 session 存在并返回 metadata。

```python
async def _handle_resume_session(self, session_id, cwd, writer):
    session_dir = cwd / ".emrg" / "sessions" / session_id
    if not session_dir.exists():
        await self._send(writer, {
            "type": "resume_result",
            "session_id": session_id,
            "error": f"Session {session_id} not found",
        })
        return

    session = Session.load(session_id, cwd)
    await self._send(writer, {
        "type": "resume_result",
        "session_id": session_id,
        "meta": {
            "message_count": session.message_count,
            "compact_count": session.compact_count,
            "created_at": session._created_at,
            "updated_at": session._updated_at,
        },
    })
```

**Client（`app.py`）**：收到 `resume_result` 后，自己读 `history.jsonl` 并渲染。

```python
if data.get("type") == "resume_result":
    err = data.get("error", "")
    if err:
        chat.add("system", f"Resume failed: {err}")
        term.render()
        continue

    new_sid = data.get("session_id", "")
    meta = data.get("meta", {})

    # 切换 session
    session_id = new_sid

    # 清空视口
    chat.rows.clear()
    chat.dirty = True

    # 从磁盘直接读 history.jsonl
    hist_path = Path(cwd) / ".emrg" / "sessions" / session_id / "history.jsonl"
    if hist_path.exists():
        for line in hist_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = r.get("type", "")
            # ... 同现在的渲染逻辑 ...
    else:
        chat.add("system", f"(no history file for {session_id})")

    chat.add("system",
        f"Resumed session {session_id} "
        f"({meta.get('message_count', '?')} messages, "
        f"created {str(meta.get('created_at', ''))[:16].replace('T', ' ')})")
    term.render()
    continue
```

### 不需要改动的地方

- **LLM 上下文**：server 在收到 task 时 `Session.load()` → `get_messages_for_llm()` 自动加载历史，这个逻辑不需要改
- **Session 存储**：`<cwd>/.emrg/sessions/<id>/` 目录结构不变
- **`/resume` 无参数列出 sessions**：server 返回 session 列表的逻辑不变

### 与 Claude Code 的对比

| | Claude Code | EMRG (修正后) |
|---|---|---|
| Session 文件位置 | `~/.claude/projects/<hash>/` | `<cwd>/.emrg/sessions/<id>/` |
| 历史由谁读取 | Client 从磁盘读 | Client 从磁盘读 |
| Server 的角色 | 不需要 server 传历史 | 只返回 metadata 确认 |
| session_id 切换 | `switchSession()` | 修改 local 变量 `session_id` |
| LLM 上下文 | Server 内部加载 | `Session.load()` → `get_messages_for_llm()` |
