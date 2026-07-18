# 演化机制重构：BackgroundThread 作为内部客户端

## 问题分析

### 当前状态

`BackgroundThread` 的演化循环只是一个骨架：

```python
def _summarize_state(self, seq: int) -> str:
    return f"instance={...} host={...} evolutions={...} runtime_healthy=true"

def _absorb_learnings(self, summary: str) -> str:
    return "no-new-learnings-to-absorb"
```

- 没有调用 LLM
- 没有读取或修改任何文件
- 唯一的产出是 `~/.emrg/logs/evolution-*.json`（内容几乎完全相同）
- `BackgroundThread` 和 `EmrgServer` 在同一进程，但没有共享任何能力

### 目标

让演化周期真正执行"自我改进"：LLM 反思近期状态 → 调用工具读写文件 → 产出可观测的变化。

### 当前实现状态

**已完成 (commit f5d1702)**：
- `BackgroundThread._run_evolution_cycle()` — 通过 `connect_to_server()` 发送 `stream: true` 的 task 到 server，LLM 可调用完整工具链
- `BackgroundThread._build_evolution_prompt()` — 从 `emrg/server/evolution_prompt.md` 读取模板，Python 字符串模板替换变量
- `EmrgServer._touch_project(cwd)` — 每次用户交互后记录 `~/.emrg/logs/projects.jsonl`
- `emrg/server/evolution_prompt.md` — 独立的 prompt 模板文件，源码的一部分

**待实现**：

| 功能 | 说明 |
|------|------|
| 演化 session 的 history 上下文 | 演化周期走到 `_run_tool_loop` 时，`_get_or_create_session` 会加载已有 session → `get_messages_for_llm()` 会把之前的演化对话历史拼入 messages。但需要实际运行验证 |
| 演化后的 git 操作 | prompt 说"如果测试失败则回滚"，但没有强制机制 |

## 设计方案

### 核心思路：BackgroundThread 作为内部客户端

`BackgroundThread` 不注入 server 引用，而是通过 `connect.py` 提供的 `connect_to_server()` 函数连接自己的 server，就像一个普通客户端一样发送 `task` 消息。

```
┌──────────────────────────────────────────────┐
│                 emrgd 进程                    │
│                                              │
│  ┌─────────────┐  connect_to_server()  ┌────┐│
│  │ Background  │ ── Unix socket / ───→│    ││
│  │ Thread      │    Named Pipe        │Srv ││
│  │ (内部客户端) │ ←── stream ──────────│    ││
│  └─────────────┘                      └────┘│
│                                              │
│  演化 session: emrg-evolution                 │
│  工作目录:     ~/.emrg/evolution              │
└──────────────────────────────────────────────┘
```

**优势**：
- `BackgroundThread` 零侵入，不需要访问 server 内部
- 复用全部 server 能力：LLM 调用、工具注册、session 管理、compact、memory
- 演化对话有完整历史记录（history.jsonl、llm.jsonl）
- 如果演化过程出错，日志和普通 session 一样可追溯

**无死锁**：`BackgroundThread` 和 `_handle_client` 都在同一个事件循环中，asyncio 的 server 实现为每个连接创建独立协程，同进程连接不会阻塞。

### 演化工作目录

```
~/.emrg/evolution/
  .emrg/
    sessions/
      emrg-evolution/       # 固定 session
        meta.json
        history.jsonl       # 演化对话历史
        history_YYMMDD.jsonl
        llm.jsonl           # LLM 原始调用记录
        memory/             # 演化自身产生的记忆
          MEMORY.md
    memory/                 # 演化产出的项目级记忆
      MEMORY.md
  source/                   # 演化自行管理的工作区
    emrg/                   # emrg 源码（LLM 自行 git clone）
  README.md                 # 说明此目录用途
```

`cwd = ~/.emrg/evolution`，演化过程中的文件操作默认都落在自己的目录里。emrg 源码由 LLM 在演化时自行 `git clone` 到 `source/emrg/`，后续演化只需 `git pull` 更新。

### 演化 session

- **固定 session_id**: `"emrg-evolution"` — 每次演化都在同一个 session 中，历史累积
- **生命周期**: 不过期，由正常的 compact 机制管理历史长度
- **首次运行**: `Session.create_with_id("emrg-evolution", evolution_cwd)` 自动创建

### 演化 prompt 模板

Prompt 模板存储在源码目录 `emrg/server/evolution_prompt.md`，是源码的一部分，随 git 分发。`_build_evolution_prompt()` 读取后用 Python 字符串模板替换变量。

**好处**：
- 模板跟着源码走，修改后在 git 里可追溯
- 演化周期修改模板时可以走常规的 git diff → review → commit 流程
- 不需要担心 `~/.emrg/evolution/` 目录不存在的情况

每次演化周期，从模板文件读取并替换变量后发送给 LLM：

```markdown
## 演化周期 #{seq}

你是 EMRG 的自我演化模块。你的任务是检查当前状态，发现可改进之处，并执行改进。

### 当前实例信息
- instance_id: {instance_id}
- host: {host_name}
- 运行时间: {uptime}
- 已完成演化次数: {evolution_count}

### 可用资源
- **emrg 源码仓库**: `{emrg_repo_url}` — 如果 `source/emrg/` 不存在，用 `git clone` 下载；已存在则 `git pull` 更新
- **本地源码路径**: `~/.emrg/evolution/source/emrg/` — clone 后在此处读取和修改代码
- **用户活跃项目**: `~/.emrg/logs/projects.jsonl` — 每个 cwd 及其最后活跃时间
- **config**: `~/.emrg/config.toml` — 当前的配置文件
- **本 session 历史**: 上面的消息中包含了之前的演化对话

### 如何发现改进点

Server 会在每次用户交互后自动记录活跃项目：

```
~/.emrg/logs/projects.jsonl
{"cwd": "/Users/argszero/scm/github.com/argszero/emrg", "last_active": "2026-07-15T10:35:00Z"}
{"cwd": "/Users/argszero/scm/work/other-project", "last_active": "2026-07-14T08:00:00Z"}
```

演化时按以下步骤分析：

1. 读 `projects.jsonl`，按 `last_active` 排序，重点关注最近活跃的项目
2. 对每个 cwd，深入分析：
   - `.emrg/sessions/*/history.jsonl` — 最近的对话内容
   - `.emrg/sessions/*/llm.jsonl` — LLM 调用记录（是否有频繁错误）
   - `.emrg/memory/MEMORY.md` — 项目累积的记忆和反馈
3. 从分析中提取改进点：
   - 用户是否有不满意的反馈？（"不对"、"换个方案"、"还是用 B"）
   - 是否有反复出现的错误模式？
   - 用户是否重复问相同的类型的问题，暗示缺了某个工具？
   - 系统提示词是否限制了 LLM 的发挥？

Server 端改动很小：`_handle_client` 收到 task 时写 `projects.jsonl`。（见下方实现方案。）

### 首次运行的准备工作
如果 `source/emrg/` 目录还不存在：
```bash
mkdir -p source
git clone {emrg_repo_url} source/emrg
```
如果已存在，先更新：
```bash
cd source/emrg && git pull
```

### 你可以做的事情
1. **分析用户交互**: 读 `~/.emrg/logs/projects.jsonl`，找出最近活跃的项目 cwd，去对应的 `.emrg/sessions/` 下读对话历史，发现改进机会
2. **反思**: 回顾之前的演化记录，看上次的改进是否有持续性效果
3. **改进**: 修改 emrg 源代码（在 `source/emrg/` 下）
4. **记录**: 在 `~/.emrg/evolution/.emrg/memory/` 下创建/更新 memory 文件，记录这次演化发现了什么、改了什么、效果预期
5. **清理**: 整理过时的日志文件、合并重复的 memory

### 约束
- 每次演化只做 1-3 件小事，不要大规模重构
- 修改代码前先理解上下文
- 改动后运行测试验证: `cd source/emrg && uv run pytest tests/ -v`
- 如果测试失败，回滚改动
- 把你的决策记录到 `~/.emrg/evolution/.emrg/memory/` 中
- 如果没有明确需要改进的地方，诚实地说 "nothing to evolve"，不要强行找事做

### 指南（不是硬性要求）
- 优先修复之前演化引入的问题
- 其次优化自身的系统提示词或演化逻辑
- 再次改进工具实现或添加新工具
```

### BackgroundThread 改造

```python
class BackgroundThread:
    # 固定常量
    EVOLUTION_CWD = Path.home() / ".emrg" / "evolution"
    EMRG_REPO_URL = "https://github.com/argszero/emrg.git"
    SESSION_ID = "emrg-evolution"

    def __init__(
        self,
        identity: InstanceIdentity,
        interval: int = 1800,
    ) -> None:
        self.identity = identity
        self.interval = interval
        self.evolutions: list[EvolutionLog] = []
        self._running = False
        self._logs_dir = config_dir() / "logs"
        self.EVOLUTION_CWD.mkdir(parents=True, exist_ok=True)

    async def _run_evolution_cycle(self, seq: int) -> None:
        """Send evolution task to the server, read streaming response."""
        prompt = self._build_evolution_prompt(seq)

        try:
            reader, writer = await connect_to_server()
        except (ConnectionRefusedError, FileNotFoundError) as e:
            logger.warning("evolution: cannot connect to server: %s", e)
            return

        task_msg = json.dumps({
            "type": "task",
            "id": f"evolution-{seq}",
            "session_id": "emrg-evolution",
            "cwd": str(self.EVOLUTION_CWD),
            "prompt": prompt,
            "stream": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }) + "\n"

        try:
            writer.write(task_msg.encode())
            await writer.drain()

            # Read streaming responses until done
            while True:
                line = await reader.readline()
                if not line:
                    break
                resp = json.loads(line.strip())

                if resp.get("done"):
                    logger.info("evolution cycle #%d complete", seq)
                    break

                # Log tool calls for observability
                if "tool_name" in resp:
                    logger.debug(
                        "evolution #%d tool: %s (err=%s)",
                        seq, resp.get("tool_name"), resp.get("error"),
                    )
        except Exception as e:
            logger.warning("evolution cycle #%d error: %s", seq, e)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        # Write evolution log entry
        log = EvolutionLog(
            timestamp=datetime.now(timezone.utc).isoformat(),
            trigger=f"background-cycle-#{seq}",
            impact=[f"evolution-cycle-#{seq}-complete"],
            operations=["llm-reflection", "tool-execution", "self-improvement"],
        )
        await self._write_evolution_log(seq, log)
        self.evolutions.append(log)

    def _build_evolution_prompt(self, seq: int) -> str:
        """Read evolution prompt template from source dir (emrg/server/evolution_prompt.md).

        The template uses Python string formatting with these variables:
          {seq}, {instance_id}, {host_name}, {uptime}, {evolution_count},
          {emrg_repo_url}, {evolution_cwd}
        """
        template_path = self._prompt_template_path
        template = template_path.read_text()
        uptime_seconds = 0  # We don't track start time for now
        uptime = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m"

        return template.format(
            seq=seq,
            instance_id=self.identity.instance_id,
            host_name=self.identity.host_name,
            uptime=uptime,
            evolution_count=len(self.evolutions),
            emrg_repo_url=self.EMRG_REPO_URL,
            evolution_cwd=str(self.EVOLUTION_CWD),
        )
```

### EmrgServer.serve() 修改

只改一行：`BackgroundThread` 构造时不再需要 `socket_path`。

```python
# Before
self._bg = BackgroundThread(self.identity, self.llm.config.evolution_interval)

# After
self._bg = BackgroundThread(self.identity, self.llm.config.evolution_interval)
```

实际上参数签名变了但调用没变——`socket_path` 从参数中移除，因为 `connect_to_server()` 内部已经封装了平台自适应逻辑。BackgroundThread 不再需要知道底层传输细节。
### 安全护栏

演化过程中 LLM 有完整的工具访问权限（bash、read、write、edit），以下几层保护：

| 层级 | 措施 |
|------|------|
| **工作目录隔离** | cwd = `~/.emrg/evolution`，工具操作默认在此目录 |
| **源码隔离** | 演化工作区 `source/emrg/` 与运行中的 emrg 实例是两份独立的代码，演化改动不影响当前运行 |
| **Prompt 约束** | 明确告知可做的事和禁区 |
| **Git 安全网** | `source/emrg/` 在 git 管理下，改坏了可以 `git checkout` 恢复 |
| **测试自动验证** | prompt 要求改动后跑测试，失败则回滚 |
| **演化历史可追溯** | 所有演化对话记录在 `emrg-evolution` session 中 |
| **不自动提交** | 演化只做本地改动，不自动 commit/push（除非 config 中 `auto_commit = true`）

### config.toml 扩展（可选）

`emrg_repo_url` 写死在 `_build_evolution_prompt` 中，不需要从 config 读取。后续如果项目分叉或迁移，需要改代码中的 URL。

第一版不增加 `[evolution]` 配置段。后续可以加 `enabled`、`auto_commit` 等开关。

### 修改文件清单

| 文件 | 变更 |
|------|------|
| `emrg/server/daemon.py` | `BackgroundThread.__init__` 移除 `socket_path` 参数；`_run_evolution_cycle` 重写为 `connect_to_server()` 客户端；删除 `_summarize_state`、`_absorb_learnings`；新增 `_build_evolution_prompt`（读源码中的 `evolution_prompt.md` 模板文件） |
| `emrg/server/evolution_prompt.md` | **新增**。演化 prompt 模板文件，源码的一部分，使用 Python 字符串模板变量 |
| `emrg/connect.py` | （已完成）BackgroundThread 复用此模块的 `connect_to_server()`，无需改动 |
| `~/.emrg/evolution/` | 首次运行时自动创建完整目录结构 |

### 复杂度评估

| 方面 | 评估 |
|------|------|
| 代码改动量 | ~80 行新增 + ~50 行删除 + ~10 行修改 |
| 新依赖 | 无 |
| 破坏性变更 | 无（BackgroundThread 是内部实现细节） |
| 测试影响 | 现有 30 个测试不受影响 |
| 风险 | 低 — 演化失败不影响 server 正常运行，有 try/except 包裹 |

### 不做的事

- ❌ 不在 BackgroundThread 中注入 server 内部引用
- ❌ 不让演化自动 commit/push（手动 review 后再决定）
- ❌ 不修改现有的 session 管理、工具注册逻辑
- ❌ 不给 BackgroundThread 单独配 LLM client（复用 server 的）
