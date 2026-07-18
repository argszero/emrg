# Compact 容错设计

## 问题分析

### 当前 compact 流程

```
用户输入 /compact
  → _handle_compact()
    → _read_history() 读取全部 history.jsonl 记录
    → 拼接为纯文本 prompt (含 tool_call/tool_result)
    → llm.chat(compact_prompt + history_text, tools=None)
    → 成功 → session.compact(summary, keep_recent=5) 替换历史
```

### 失效场景

当对话历史超过 LLM context window（DeepSeek-chat: 128K tokens）时：

1. **场景 A：正常对话已无法继续**
   - `get_messages_for_llm()` 读取全量 history，拼入 messages
   - 发送给 LLM → API 返回 context length exceeded 错误
   - 此时 `/compact` 也走相同的路径 → **同样超限，compact 失败**
   - **死锁**：无法对话，也无法 compact

2. **场景 B：finish_reason == "length"**
   - 当前工具循环中，`"length"` 只被当作普通 stop 处理（Case 3）
   - 没有触发任何 compact 或警告
   - `"length"` 意味着达到 `max_tokens` 上限（当前配置 4096），输出被截断
   - 虽然不直接等于 context 超限，但是一个值得预警的信号

3. **场景 C：compact prompt 本身超限**
   - `_handle_compact` 把全部 history 展开为纯文本（含 tool_call/tool_result）
   - 纯文本可能比原始 JSON 消息更大（tool results 截断到 500 字符但积累起来仍然很大）
   - 发送给 LLM → context length exceeded → compact 失败

## 设计方案

### 🔴 分层容错设计

#### Layer 1: Token 监测 + 自动 compact

在 `_run_tool_loop` 每轮开始前，估算 prompt token 数。超过阈值时**自动触发 compact**，不需要用户手动 `/compact`。

**Token 估算**（不使用 tokenizer，基于字符数）：
```python
def _estimate_tokens(messages: list[dict]) -> int:
    """粗略估算 messages 的 token 数。
    
    - 英文/代码: ~4 chars/token
    - 中文: ~1.5 chars/token
    - 保守取 3 chars/token
    - 每条 message 额外 +3 tokens 用于 role/content 元数据
    """
    total = 0
    for m in messages:
        total += 3  # role overhead
        content = m.get("content") or ""
        if isinstance(content, str):
            total += len(content) // 3
        for tc in (m.get("tool_calls") or []):
            tc_str = json.dumps(tc, ensure_ascii=False)
            total += tc_str // 3
    return total
```

**阈值配置**（`config.toml`）：

```toml
[llm]
# ... 其他配置 ...
context_window = 131072
auto_compact_threshold = 0.7    # 可选，不配置时默认 0.0（禁用自动 compact）
```

**默认值**（`LlmConfig` dataclass）：
```python
auto_compact_threshold: float = 0.0  # 0.0 = 禁用；0.7 = 推荐值
```

**行为**：
- 每轮 tool loop 开始前，`_estimate_tokens(messages)` 估算当前 prompt token 数
- `auto_compact_threshold == 0.0`：跳过检查，不自动 compact
- `auto_compact_threshold > 0.0`：当 `estimated_tokens > context_window * auto_compact_threshold` 时触发

**为什么同步而非异步**：如果异步 compact（不等待），当前轮仍然会用超限的 messages 发送给 LLM → API 报错。同步 compact 确保下一轮对话用精简后的 history。

**阈值选择**：

| 值 | 含义 | 适用场景 |
|---|------|---------|
| 0.7 | context_window 的 70% | 推荐，在超限前有充足缓冲 |
| 0.85 | 85% | 激进，最大化利用 context |
| 0.0 | 禁用（默认） | 不自动 compact，只手动 `/compact` |

#### Layer 2: finish_reason == "length" 兜底

当 LLM 返回 `finish_reason == "length"`（达到 `max_tokens` 上限，输出被截断）：
- 在 Case 3 分支增加检测
- 自动触发 compact（后台异步，不阻塞当前响应）
- 客户端收到 compact_result 后显示
- 注意：此时 prompt 可能还没到 auto_compact_threshold，但 `length` 是另一个信号——输出空间不够了

#### Layer 3: chunked compact（兜底）

当常规 compact 的 LLM 调用失败（context too long）时，启用**按 token 量动态分片**的 compact。

**核心思想**：不按记录数分片（记录大小差异太大），而是按 token 估算值贪心分片，确保每个 chunk 的 prompt 能放入 context window。

**常量定义**（从配置推导，不硬编码）：

```python
# config.toml 中配置:
#   context_window = 131072             # 模型的 context window (输入+输出总量)
#   max_tokens = 4096                   # 单次输出上限
#   auto_compact_threshold = 0.7        # 自动 compact 阈值
#
# 推导:
#   CTX_WINDOW = config.context_window
#   MAX_PER_CHUNK = CTX_WINDOW - max_tokens - 2000
#     = 131_072 - 4096 - 2000
#     ≈ 124_000
#   (留下 max_tokens 给 summary 输出，2000 给 prompt 模板)
#   AUTO_COMPACT_AT = CTX_WINDOW * auto_compact_threshold
#     = 131_072 * 0.7 ≈ 91_750

CTX_WINDOW = config.llm.context_window
MAX_PER_CHUNK = CTX_WINDOW - config.llm.max_tokens - 2000
AUTO_COMPACT_AT = int(CTX_WINDOW * config.llm.auto_compact_threshold)
MERGE_BATCH = MAX_PER_CHUNK
```

**`config.toml` 完整示例**：
```toml
[llm]
base_url = "https://api.deepseek.com"
api_key = "sk-..."
model = "deepseek-chat"
max_tokens = 4096
context_window = 131072            # DeepSeek-chat: 128K (131072)
auto_compact_threshold = 0.7       # 可选，91K tokens 时自动 compact（不配置默认 0.0）
temperature = 0.7
```

**分片算法**（token-aware 贪心装箱）：

```
_chunked_compact(records, keep_recent=5):
  to_compact = records[:-keep_recent]

  # Step 1: 贪心分片 — 按 token 估算填满每个 chunk
  chunks = []
  current_chunk = []
  current_tokens = 0

  for record in to_compact:
      rec_tokens = _estimate_single(record)
      if current_tokens + rec_tokens > MAX_PER_CHUNK and current_chunk:
          chunks.append(current_chunk)
          current_chunk = []
          current_tokens = 0
      current_chunk.append(record)
      current_tokens += rec_tokens
  if current_chunk:
      chunks.append(current_chunk)

  # Step 2: 逐片总结
  summaries = []
  for idx, chunk in enumerate(chunks):
      chunk_text = _records_to_text(chunk)
      summary = await llm.chat(
          "Summarize this conversation segment "
          f"({idx+1}/{len(chunks)}):\n\n{chunk_text}",
          tools=None
      )
      summaries.append(summary)

  # Step 3: 递归合并 summaries（如果 summaries 太多也会超限）
  return await _merge_summaries(summaries)
```

**合并算法**（递归，自适应 token 量）：

```
_merge_summaries(summaries, max_per_chunk, merge_batch):
  total_tokens = _estimate_text("\n---\n".join(summaries))

  if total_tokens <= merge_batch:
      # 单次合并即可
      return await llm.chat(
          "Merge these conversation segment summaries "
          "into one coherent summary:\n\n" + "\n---\n".join(summaries),
          tools=None
      )

  # summaries 太多 → 分组合并，递归
  batches = []
  current = []
  current_tokens = 0
  for s in summaries:
      st = _estimate_text(s)
      if current_tokens + st > merge_batch and current:
          batches.append(current)
          current = []
          current_tokens = 0
      current.append(s)
      current_tokens += st
  if current:
      batches.append(current)

  # 逐组合并（串行，因为每组合并可能涉及多次 LLM 调用）
  merged = []
  for batch in batches:
      m = await _merge_summaries(batch, max_per_chunk, merge_batch)  # 递归
      merged.append(m)

  # 现在 merged 少了很多，继续递归
  if len(merged) == 1:
      return merged[0]
  return await _merge_summaries(merged, max_per_chunk, merge_batch)
```

**极端场景推演**（context_window=10000, max_tokens=4096 → MAX_PER_CHUNK=5904）：

```
history: 200 条记录，每条 ~50 tokens = ~10K tokens

Step 1 — 贪心分片 (MAX_PER_CHUNK=5904):
  10K tokens → ~2 个 chunk (5904 + 4096)

Step 2 — 逐片总结:
  2 次 LLM 调用 ✓

Step 3 — 合并:
  2 个 summaries < 5904 → 单次合并 ✓
  → 总计 3 次 LLM 调用
```

**真实场景**（context_window=131072, max_tokens=4096, history=200K tokens）：

```
MAX_PER_CHUNK = 131072 - 4096 - 2000 = 124_976

Step 1 — 贪心分片:
  200K tokens → ~2 个 chunk (124K + 76K)

Step 2 — 逐片总结:
  2 次 LLM 调用 ✓

Step 3 — 合并:
  2 个 summaries × ~500 tokens = 1K << 124K → 单次合并 ✓
  → 总计 3 次 LLM 调用
```

**超长场景**（context_window=131072, max_tokens=4096, history=500K tokens）：

```
MAX_PER_CHUNK = 124_976

Step 1 — 贪心分片:
  500K tokens → ~5 个 chunk

Step 2 — 逐片总结:
  5 次 LLM 调用 ✓

Step 3 — 合并:
  5 个 summaries ≈ ~2.5K << 124K → 单次合并 ✓
  → 总计 6 次 LLM 调用
```

**容错链条（修正后）**：
```
compact 请求
  → 常规 compact (全量 LLM 调用)
    → 成功 ✓
    → 失败 (context too long / 400 error)
      → _chunked_compact (token-aware 贪心分片)
        → MAX_PER_CHUNK 保证每个 chunk 都能放入 context
        → 递归合并处理 summaries 过多的情况
        → 成功 ✓
        → 极端失败 (单条 record 就超过 MAX_PER_CHUNK)
          → 截断该 record 的 content，标记 "truncated"
          → 成功 ✓ (带截断标记)
```

## 修改文件

| 文件 | 变更 |
|------|------|
| `emrg/server/daemon.py` | 新增 `_estimate_tokens`、`_estimate_single`、`_records_to_text`、`_chunked_compact`、`_merge_summaries`、`_truncate_record`；修改 `_run_tool_loop`（length 检测 + 预警）、`_handle_compact`（容错回退） |
| `emrg/config.py` | `LlmConfig` 增加 `context_window: int = 131072`、`auto_compact_threshold: float = 0.0` 字段 |
| `emrg/client/app.py` | `compact_result` 处理增加 chunked 状态显示 |

## 实现细节

### `_estimate_tokens(messages)` — 新增

- 输入：`list[dict]`（OpenAI 格式 messages）
- 输出：`int`（估算 token 数）
- 算法：`sum(len(json.dumps(m, ensure_ascii=False)) // 3 + 3 for m in messages)`

### `_estimate_single(record)` — 新增

- 输入：单个 history record（dict）
- 输出：`int`（估算 token 数）
- 用途：贪心分片时计算每条 record 的开销
- 算法：`len(_records_to_text([record])) // 3`

### `_records_to_text(records)` — 抽取

- 输入：`list[dict]`（history records）
- 输出：`str`（紧凑文本表示，用于 compact prompt）
- 当前 `_handle_compact` 中的 history_text 拼接逻辑抽取为独立函数
- tool_result 截断到 500 字符（保持现有逻辑）

### `_run_tool_loop` 修改

1. **每轮开始前**：如果 `auto_compact_threshold > 0.0`，调用 `_estimate_tokens(messages)`，若超过 `context_window * auto_compact_threshold`：
   - 通知客户端 "auto-compacting..."
   - **同步**调用 `_handle_compact_inline(session)` 执行 compact
   - compact 完成后用精简后的 messages 继续本轮
2. **`auto_compact_threshold == 0.0`**（默认值）：跳过自动 compact，只能手动 `/compact`

### `_handle_compact` 修改

```python
async def _handle_compact(self, session, writer):
    records = session._read_history()
    if len(records) <= 5:
        # 不足，跳过
        return

    # 尝试常规 compact
    try:
        summary = await self._do_compact(records)
    except RuntimeError as e:
        if "context" in str(e).lower() or "too long" in str(e).lower():
            # 回退到分片 compact
            logger.warning("normal compact failed, trying chunked: %s", e)
            try:
                summary = await self._chunked_compact(records)
            except Exception as e2:
                # 分片也失败，通知客户端
                await self._send(writer, {
                    "type": "compact_result",
                    "session_id": session.session_id,
                    "messages_compacted": 0,
                    "error": f"Compact failed (both normal and chunked): {e2}",
                })
                return
        else:
            raise

    count = session.compact(summary, keep_recent=5)
    # 通知客户端...
```

### `_chunked_compact` — 新增（token-aware 贪心分片）

```python
async def _chunked_compact(self, records, keep_recent=5):
    """Token-aware 分片 compact。
    
    不按记录数分片（记录大小差异太大），而是按 token 估算贪心装箱。
    MAX_PER_CHUNK 由 config.context_window - config.max_tokens - 2000 动态推导。
    """
    max_per_chunk = self.llm.config.context_window - self.llm.config.max_tokens - 2000
    merge_batch = max_per_chunk

    to_compact = records[:-keep_recent]
    total_tokens = sum(_estimate_single(r) for r in to_compact)
    logger.info("chunked compact: %d records, ~%d tokens", len(to_compact), total_tokens)

    # Step 1: 贪心分片
    chunks = []
    current_chunk = []
    current_tokens = 0
    
    for record in to_compact:
        rec_tokens = _estimate_single(record)
        # 单条记录超大 → 截断 content
        if rec_tokens > MAX_PER_CHUNK:
            record = _truncate_record(record, MAX_PER_CHUNK)
            rec_tokens = _estimate_single(record)
        if current_tokens + rec_tokens > MAX_PER_CHUNK and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0
        current_chunk.append(record)
        current_tokens += rec_tokens
    if current_chunk:
        chunks.append(current_chunk)
    
    logger.info("chunked compact: %d chunks created", len(chunks))

    # Step 2: 逐片总结
    summaries = []
    for idx, chunk in enumerate(chunks):
        chunk_text = _records_to_text(chunk)
        msg = await self.llm.chat([{
            "role": "user",
            "content": (
                f"Summarize this conversation segment ({idx+1}/{len(chunks)}). "
                "Include key decisions, context, and unresolved items:\n\n"
                f"{chunk_text}"
            ),
        }], tools=None)
        summaries.append(msg.get("content", ""))
        logger.debug("chunked compact: chunk %d/%d done", idx+1, len(chunks))

    if len(summaries) == 1:
        return summaries[0]

    # Step 3: 递归合并
    return await self._merge_summaries(summaries)


async def _merge_summaries(self, summaries, max_per_chunk, merge_batch):
    """递归合并 summaries。
    
    如果所有 summaries 能放入一次 LLM 调用 → 直接合并。
    否则分组合并，然后递归。
    """
    combined = "\n---\n".join(summaries)
    if _estimate_text(combined) <= merge_batch:
        msg = await self.llm.chat([{
            "role": "user",
            "content": (
                "Merge these conversation segment summaries into one "
                "coherent summary:\n\n" + combined
            ),
        }], tools=None)
        return msg.get("content", "")

    # 分组
    batches = []
    current = []
    current_tokens = 0
    for s in summaries:
        st = _estimate_text(s)
        if current_tokens + st > merge_batch and current:
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(s)
        current_tokens += st
    if current:
        batches.append(current)

    logger.info("merge_summaries: %d summaries → %d batches", len(summaries), len(batches))

    # 递归合并每批
    merged = []
    for batch in batches:
        m = await self._merge_summaries(batch, max_per_chunk, merge_batch)
        merged.append(m)

    if len(merged) == 1:
        return merged[0]
    return await self._merge_summaries(merged, max_per_chunk, merge_batch)


def _truncate_record(record, max_tokens):
    """截断超大 record 的 content，使其不超过 max_tokens。"""
    record = dict(record)
    content = record.get("content", "")
    max_chars = max_tokens * 3
    if len(content) > max_chars:
        record["content"] = content[:max_chars] + "\n...[truncated for compact]"
    return record
```

**复杂度分析**：

| 场景 | chunks | 总结调用 | 合并调用 | 总计 |
|------|--------|----------|----------|------|
| history < 64K tokens | 1 | 1 | 0 | 1 |
| history = 200K tokens | ~4 | 4 | 1 | 5 |
| history = 1M tokens | ~16 | 16 | 3 | 19 |

每次 LLM 调用输出约 200-1000 tokens（summary 不长），token 成本可控。

## token 使用约束

- `_estimate_tokens` 不调用外部 tokenizer，纯字符估算
- 保守估算（3 chars/token）确保不会低估
- 所有 threshold 为常量，可在代码中调整

## 测试命令

```bash
# 现有测试通过
uv run pytest tests/ -v

# 手动测试 chunked compact：
# 1. 构造一个包含大量消息的 session
# 2. 发送 /compact
# 3. 检查 daemon 日志中的 "trying chunked" 消息
```

## 不做的

- ❌ 不引入 tiktoken 依赖（保持零依赖）
- ❌ 不在 compact 前强制等待（用户体验差）
- ❌ 不修改 history.jsonl 格式
- ❌ 不做增量 compact（每次 compact 都是全量替换）
