# EMRG — Agent.md

> This is the Codex-compatible project context file. See `README.md` for the canonical project description (English).

## Project Overview

EMRG is a self-evolving AI agent architecture experiment. Python implementation, based on a micro-kernel design. The name reads as "emerge" — intelligence that emerges from use — expanding to Evolving Micro-kernel, Rant-driven Growth.

## Architecture

- `emrg/` — Core package
  - `__init__.py` — Version info
  - `__main__.py` — CLI entry (`emrg`, `emrg server`, `emrg rant`, `emrg update`)
  - `protocol.py` — Communication protocol (TaskRequest, TaskResponse, ToolStart, ToolEnd, ServerPong, EvolutionLog, InstanceIdentity)
  - `config.py` — Config loading (`~/.emrg/config.toml`, Python 3.11+ tomllib)
  - `connect.py` — IPC connection (WebSocket over TCP loopback, token auth via `emrgd.token`)
  - `memory.py` — Memory system (ProjectMemoryStore, SessionMemoryStore, MemoryFile, MemoryIndex)
  - `session.py` — Session management (Session CRUD, history persistence, compact/clear)
- `emrg/server/` — Server (WebSocket daemon, EMRG's living core)
  - `daemon.py` — EmrgServer, message processing, BackgroundThread (evolution cycle), tool loop, compact/memory integration
  - `llm.py` — LLM client (chat + chat_stream, streaming retry)
  - `tool_types.py` — Tool type definitions (ToolDefinition, ToolResult)
  - `evolution_prompt.md` — Evolution prompt template
- `emrg/tools/` — Tool implementations (bash, read, write, edit, glob, grep, base + registry)
- `emrg/skills/` — Dynamically loaded skill modules (skills, progressive disclosure) + installable-skills catalog (`skill-catalog.md`, `/skills available|install|update`)
- `emrg/client/` — Client (TUI interface based on inlined python-tui)
  - `daemon_manager.py` — Daemon lifecycle (start/restart-if-stale/ensure-connected) + protocol client (DaemonConnection: send_task/send_command/recv/read_stream) — shared with GUI (Phase 3)
  - `app.py` — Main entry, event loop, ChatHistory widget, command autocomplete, session selector
- `emrg/gui/` — Electron GUI (Phase 3, non-developer entry point): main process (window/daemon lifecycle/IPC) + renderer (zero network, contextBridge sandbox) + `daemon_client.js` (protocol client mirroring `daemon_manager.py`). Start with `npm start`; unit tests `npm test` (integration tests run in CI too).

## Key Conventions

- **The server is the living core; the client is just the interface**
- Client auto-detects/starts the server on launch; server stays running on client exit
- Server logs are discarded (`stderr=DEVNULL`)
- Client logs go to `./.emrg/emrg-client.log`
- **README language**: `README.md` = English (default), `README.cn.md` = Chinese
- **Project context files**: `README.md` = English, `Agent.md` = English

## Terminology

Unified vocabulary for the agent's execution model (code terms in `emrg/server/daemon.py`):

- **Tool loop** (工具循环) — the complete process triggered by one user message: the agent calls tools and sends LLM requests repeatedly until a round produces no new tool calls. Code: "tool loop" (`_run_tool_loop`).
- **Round** (轮) — one iteration inside a tool loop: one LLM request + zero or more tool calls + tool executions. Code: `round_num` (daemon.py:1775 `for round_num in range(1, self._max_tool_rounds + 1)`), bounded by `max_tool_rounds`.
- **Evolution cycle** (演化周期) — a distinct concept: one full run of the self-evolution task ("Prepare → Review → Discover → Improve → Submit → Record"), unrelated to tool loop rounds.

Hierarchy:
```
user message
  └── tool loop (the whole process)
        ├── round 1: LLM request → tool calls → execute
        ├── round 2: LLM request → tool calls → execute
        └── round N: LLM request → no tool calls → loop ends
```

Usage: say "tool loop" for the whole process, "round N" for a single LLM request + tools. Do not call evolution cycles "rounds".

## Current Features

- **TUI Client** — Rich terminal UI with Markdown rendering, syntax highlighting, diff display
  - Command autocomplete (type `/` to list commands with filtering)
  - Slash commands: `/help`, `/clear`, `/compact`, `/resume`, `/rename`, `/rewind`, `/trigger`, `/memory`, `/sessions`, `/rant`, `/model`, `/skills`, `/image`, `/delete`, `/version`
  - `/model <name>` to switch LLM models at runtime (configured via `[[llm.models]]` in config.toml)
  - `/image` to paste a clipboard image into the input (token-based; supports multiple images, one per Enter)
  - `vision` per-model config flag gates OpenAI vision API; non-vision models degrade images to text placeholders
  - Interactive session picker (arrow keys or j/k vim-style navigation)
  - Interactive model picker (arrow keys to select from configured models)
  - Elapsed timer during LLM responses
  - ESC to interrupt responses mid-stream
  - Auto-wrap long input lines to terminal width (CJK-aware)
  - CJK-aware cursor movement (move_up/move_down)
  - SIGWINCH handler for real-time terminal resize
  - Keyboard shortcuts: Ctrl+A (line start), Ctrl+E (line end), Ctrl+W (delete word), Ctrl+K (kill line), Ctrl+U (kill to start)
  - Bracketed paste support for multi-line input
  - Terminal window title sync on session switch
  - Dynamic viewport with native terminal scrollback
  - 60fps render throttling
- **Electron GUI** — Non-developer entry point (Phase 3), chat/sessions/tool status/settings (80% daily use)
  - `npm start` from `emrg/gui/` auto-starts the daemon; main process is the only daemon connection (renderer zero network, contextBridge sandbox)
  - v0.2.5 full redesign (rant 08-05): light/dark dual theme (prefers-color-scheme), friendly tool status rows (collapsible, 2000-char truncation), multi-model management (add/edit/delete/set-default in settings + in-chat switcher), empty-state welcome screen, back-to-bottom button
  - v0.2.6 keyboard accessibility (#432-#438): ↑↓ nav in model switcher / context menu / conv list, Enter submit in settings/welcome/model/rename forms — all interactive components keyboard-usable
  - v0.2.7 macOS code signing + notarization (#441-#477): Developer ID Application + Installer dual-cert p12 (CI double-import), runtime codesign for embedded Python .so, notarytool status parse, stapler + spctl --type install final gate — Gatekeeper zero-dialog install for non-developers
  - First-run onboarding: missing config → settings dialog (no daemon spawn) → save → daemon starts; placeholder API key treated as unconfigured
  - Streaming chat with delta rendering (16ms batching), markdown on done (marked + DOMPurify + local highlight.js subset), tool call status cards (2000-char truncation + expand)
  - Session list/switch/new/delete + right-click rename (context menu, #423) synced with daemon; own-stream busy lock (G65); broadcast streams from other clients tagged "来自其他客户端"
  - Disconnect/reconnect: red status dot, auto daemon respawn (stale-port detection), session resume, input bar restored on disconnect (no 30s fake-timeout)
  - Unit tests `npm test` (89: 45 daemon_client + 20 conn-manager + 8 integration + 6 build-config + 7 gui-state + 3 preload-api); RESPONSE_TYPES mirror daemon protocol verified against `daemon.py`
  - React migration Batch 0 + Batch 1 (design `~/.emrg/designs/gui-react-migration-design.md`): `emrg/gui/renderer/` scaffold — Vite 8 + React 19 + TypeScript strict + Vitest + self-developed SnapshotStore (useSyncExternalStore); standalone build → `renderer/dist/index.html` + `assets/*` (CSP `script-src 'self'`-safe, no inline); `window.emrg` bridge (52 invoke + onEvent, API-surface guard test) + main.js + daemon protocol untouched; vanilla renderer stays live until Batch 5 switch (D3); Batch 1 ported commands.js → `lib/commands.ts` + copywriting.js → `lib/copywriting.ts` (pure logic, injectable t); Batch 1 remainder: i18n.js full dictionary (373 keys zh/en parity, auto-generated `lib/i18n-dicts.ts` + detectLocale/getLocale/setLocale/t) + markdown.js → `lib/markdown.ts` (marked/DOMPurify/hljs injectable, fence-closed heuristic + block-projection streaming); Batch 2: chat.js → `lib/transcript.ts` (pure per-session state machine — delta/done/toolStart/toolEnd/merge-group/clearTyping, sid buckets P3, 16 tests) + `components/TranscriptView.tsx` (React chat area: streaming plain-text → markdown-on-done via injectable renderer, tool rows running→done/failed with elapsed + expandable output, merge-group bar summary + collapse/user-expanded, remote label, autoScroll + back-to-bottom, 10 tests); Batch 3: Sidebar/ResultPanel/WorkspaceView/FileTree components; Batch 4: dialog components (help/memory/skills/rename/confirm/rewind/open-session/new-session/welcome/github-device) + `lib/dialog` reducer + dialogLists/rewind/openSession pure logic; Batch 5: `lib/daemonBridge.ts` event bridge (multi-subscriber onEvent → transcript/app stores, sid-routed #977, queue-injection #655) + `DaemonBridgeProvider` (mount-once/dispose, tRef) + Shell chat-loop wiring (live daemon data: transcript/composer/sidebar/result-panel, #1016-#1018) + `DialogHost` (command routing → dialogs: /help /memory /skills /rewind /rename /delete /sessions /open /resume + direct /clear /compact /version /image; sidebar new-chat/open-chat buttons, #1019) + workspace view switching (Sidebar `#side-nav` rail: sessions/projects/tasks/rants/settings with active highlight; Shell `activeView` state per vanilla `switchView` toggle semantics — panel hides chat chrome, sessions restores it; #1020) + workspace panels wired to real daemon data (Shell loads listProjects/listTasks/listRants on panel activation, project-sessions sub-view via listProjectSessions with current-sid mark, actions: switch-session / add-project (pickProjectDir+register) / delete-project (confirm) / trigger-task; Shell owns a ConfirmDialog for delete) + SettingsDialog wiring (`components/SettingsPanel.tsx`: six tabs model/github/appearance/language/about/templates — task-template CRUD (list/create/update/delete custom types, builtin read-only view, delete-confirm, textarea prompt editor; bridge taskTemplateList/Create/Update/Delete) — model CRUD + set-default via getSettings/saveSettings, github connect/disconnect status, theme data-theme apply, i18n setLocale, about version/evolution-count from Shell appState; #1021) + final switch part 1 (#1023): main.js `loadFile` → `renderer/dist/index.html` (both initial + render-process-gone reload) — production GUI now runs the React app; vanilla renderer files (renderer/js, css, index.html) + vm.runInContext smoke tests retained for revert, removed in follow-up cleanup (vanilla renderer/js + css + index.html and the vm.runInContext legacy tests deleted — React vitest 411 + GUI 89 cover the migrated logic); gui `npm start` pre-builds dist via `prestart` (fresh-clone dev flow) + post-switch production wiring: `lib/vendorMarkdown.ts` restores real vendor marked/DOMPurify/hljs into the bundle (chat markdown rendering + ResultPanel `window.hljs` code highlight — previously degraded to escaped plain text) and re-imports the vanilla `css/` suite (tokens/base/components/layout/animations, deleted with the vanilla renderer — React components reference `var(--token)` vars and vanilla class names; one pre-existing stray brace in components.css fixed for lightningcss)
- **Scheduled tasks** — Task generalization + CRUD (rant 2026-08-12T18:23:15, #709/#710/#711)
  - Task handler generalized: `TaskHandler` (renamed from `EvolutionHandler`), repo-configured self-heal for any project, template lookup builtin → `~/.emrg/task-templates/<name>.md` → fallback
  - Daemon commands: `task_create/update/delete` + `task_template_create/list/update/delete` (tasks stored in `~/.emrg/tasks.yml`, custom type templates in `~/.emrg/task-templates/`)
  - Hot reload: editing tasks.yml at runtime adds/removes handlers without daemon restart (`TaskScheduler.apply_tasks`)
  - Validation: name `^[a-z0-9][a-z0-9-]*$` ≤32 chars, type builtin-or-custom, project must be registered, interval ≥60s; builtin types/templates read-only; deleting a referenced custom type refused (error includes task count)
  - GUI settings → 定时任务 section: task list (trigger/edit/delete) + add/edit form (type + registered-project pickers, interval validation) + custom-type management (prompt-template textarea; builtin read-only); IPC wired through main.js/preload.js + RESPONSE_TYPES
- **Auto project tracking** — Automatically detects and records working directories; project-scoped sessions
- **Rant-driven evolution** — User feedback via `/rant` drives automatic self-improvement cycles
- **Headless GitHub auth** — Non-interactive evolution auto-extracts `GH_TOKEN` from git credential store (osxkeychain / credential helper); PR comment/LGTM queries fall back to REST API (GraphQL needs `read:org` scope)
- **Config hot-reload** — Detects `~/.emrg/config.toml` changes and auto-restarts server
- **Memory system** — Project and session memory with YAML frontmatter, indexing, merge/split
- **Skills** — Progressive disclosure via `.emrg/skills/` directory

### Differentiation (community-driven)

Community needs voiced in HN agent-UI discussions map directly to EMRG's design:

| Community need | EMRG's answer |
|---|---|
| **Inspectable artifacts** | Everything is a file: state files (`open_source_*_state.md`, `promote_*_state.md`), memory index (YAML frontmatter + Markdown), evolution logs (`evolution-*.json`) — browse via `/memory` |
| **Git folder as state** | Project tracking is git-based: projects.yml records repo paths, saturation detection keys off git HEAD — state stays in sync with version control |
| **Toolbar-specific shortcuts** | The terminal is the toolbar: `Ctrl+A/E/W/K/U` editing, `j`/`k` navigation, `ESC` interrupt, `/` command completion — zero mouse |
| **Session/project management** | `/sessions` browser, `/rename`, `/resume`, `/rewind`, project-scoped sessions + auto project tracking |

**Positioning**: terminal-first, TUI-driven, session memory, git-as-state — no browser plugin or extra panel needed; everything inspectable and traceable in the terminal.

## Test Commands

```bash
pkill -f "emrg.server"; rm -f ~/.emrg/emrgd.token; python -m emrg
```

Python: `uv run pytest tests/ -v` — 计数**不写死在文档里**（写死的那份数字会陈旧）：要数字就现场测量 `uv run --no-sync python3 scripts/check-doc-count.py --measure` — import check: `uv run python -c "from emrg.client.app import run_client"
GUI: `cd emrg/gui && npm test` (100: 44 daemon_client + 20 conn-manager + 7 integration + 7 nav-policy + 7 gui-state + 6 build-config + 4 boot-contract + 3 preload-api + 2 theme-guard) — syntax: `node --check main.js preload.js daemon_client.js`
Renderer: `cd emrg/gui/renderer && npm run typecheck && npm test` (514: 5 snapshot-store + 9 utils + 3 ErrorBoundary + 2 App smoke + 11 commands + 4 copywriting + 11 i18n + 13 markdown + 21 transcript + 11 TranscriptView + 15 history + 31 composer + 41 Composer + 6 LinkDialog + 16 sidebar + 17 Sidebar + 9 fileTree + 9 FileTree + 16 resultPanel + 8 ResultPanel + 27 workspaceView + 10 WorkspaceView + 10 dialog + 6 Dialog + 9 ConfirmDialog + 9 RenameDialog + 10 dialogLists + 3 HelpDialog + 9 MemoryDialog + 6 SkillsDialog + 8 openSession + 6 WelcomeDialog + 9 OpenSessionDialog + 7 NewSessionDialog + 7 rewind + 8 RewindDialog + 7 GithubDeviceDialog + 18 daemonBridge + 7 DaemonBridgeProvider + 30 Shell + 15 DialogHost + 20 SettingsPanel + 6 TaskFormDialog + 5 RantDialog + 4 vendorMarkdown) + `npm run build` → `renderer/dist/`
CI: `uv run pytest` (ubuntu + **windows-2025 matrix** — Windows pytest 回归在 PR CI 即失败，v0.2.29 教训 #725) + GUI tests + **actionlint workflow lint** (`rhysd/actionlint@v1.7.12` gate, #444 — workflow 解析错误在 PR CI 即失败，如 `if:` secrets 上下文)
Re-trigger: `scripts/re-trigger-ci.sh [branch]` (workflow_dispatch, #527 — 替代空 commit 重触发：Actions outage 会整段丢弃 push 事件，dispatch 走 API 路径不受影响)
Merge freshness: `uv run --no-sync python3 scripts/check-merge-freshness.py <PR>...` — 合并前问一句「这条绿色 CI 说的还是**将要合并的那棵树**吗」。`pull_request` 事件下 GitHub 构建的是 `Merge <head> into <merge-base>`（合到**分叉点**，不是当前 master）：分叉点就是 master 时两者同一棵树，master 一移动就不是了，而 master 移动**不触发** `synchronize`（只有 push 分支才触发），于是绿灯永远保持绿灯却已经过期。#1137 实测：`MERGEABLE/CLEAN` + 双 job 全绿，两侧计数行都写成同一个 1397 ⇒ git **无冲突**自动合并、保留 1397，而合并树实收 1401，两个守卫在 master 上才变红——**干净合并才是危险的那一种**（冲突时人被迫看一眼，反而安全）。判定不用时间戳比较（时钟/秒级竞态会骗人）而用**图结构**：master 的 tip 是否是 head 的祖先（`compare/master...<head>` 的 `identical`/`ahead`），是则 merge base 就是 master 本身、判决可平移。**祖先性只是一半**：head 含 master 但**根本没有 CI 运行**（push 事件丢失 ⇒ `no checks reported`）同样不算新鲜，故第二个条件是「该 SHA 上存在一个**通过**的运行」。按 SHA 而非分支取运行（分支推两次会有两个运行）。exit 0 全部新鲜 / 1 至少一条过期 / 2 问不出来（坏 PR、gh 失败、状态不认识）——不认识的状态一律 fail-loud，绝不默认新鲜
Release bump: `python3 scripts/bump-version.py <x.y.z>` — 一次改齐 8 处版本声明（`emrg/__init__.py`、`pyproject.toml`、`emrg/gui/package.json`、`emrg/gui/package-lock.json` 根 + `packages[""]`、`uv.lock`、`packaging/{build-runtime,make-installer,make-run-installer}.sh`）；`--check` 只报告漂移（宿主侧自检，与 CI 的 test_version_sync 对称），`--dry-run` 预览不落盘。锚点缺失/数量不符即 fail-loud，绝不猜测；`uv.lock` 只改 `name = "emrg"` 那一行（v0.2.94 教训：直接 `uv run` 会把 lock 里所有 registry URL 重写成镜像，556 行环境噪声）。bump 后用 `uv run --no-sync pytest` 避免 uv 重生成 lock。详见 Agent.md「Releasing」
Doc count sync: `uv run --no-sync python3 scripts/check-doc-count.py [--measure|--resolve-conflict]` — **「Python 测试数不写进任何 tracked 文件」的守卫**（宿主侧自检，与 CI 的 `tests/test_doc_counts.py::test_no_tracked_file_states_the_python_test_count` 同一份规则，两处读的是本工具里的同一个正则）。默认扫描全仓 tracked 文件（`tests/` 与 `scripts/` 除外——那两个树是规则与它的探针住的地方，必须能拼出「写死的样子」；该排除有实测背书：466 个 tracked 文件里这两处之外的命中只有下面那两条真实声明）并**指名文件+行号+写法**，exit 1。`--measure` 现场测量当前树收集的测试数（`pytest --collect-only`，缺 pytest/解析不出即 fail-loud，绝不报假数）。`--resolve-conflict` 专治计数行的合并冲突：**解法不是选边而是去掉那个数字**（两侧各自去掉计数后文本必须完全相同才动手；措辞也不同即拒绝——那是内容冲突，得人读）。**存在理由（2026-09-13 实测，宿主的决定）**：写死一行派生数字既是冲突磁铁又是静默错误源——14 张在飞 PR 里 11 张冲突，**11/11 冲突的都是这一行**（每张加测试的 PR 都得改同一个数字），而两个分支写**同一个值**时 git 干净合并、悄悄留下陈旧值（实测 #1179+#1180 都写 1601 而合并树实收 1603，守卫只在合并后的树上才红）。故数字不写、现场测量：`README.md`/`README.cn.md` 早就这么做了（改挂 Tests badge）。**测的是「你站着的那个 checkout」**（2026-09-11 实测的坑：解冲突要在 git worktree 里干活，而 root 原先取自 `__file__` ⇒ 跑主 checkout 的脚本、量的却是**主树**，worktree 自己的 Agent.md 写 1401 而工具报 `OK: ... documents 1420`——**读错了树还说一致**）。现在 root 从 cwd 解析（cwd 同时有 `Agent.md` 与 `scripts/` 才算 checkout），拿不准就退回脚本自身 root，并在输出首行**报出所测的树**
Node count sync: `uv run --no-sync python3 scripts/check-node-test-count.py [--write|--dry-run]` — 直接问**真实运行器**（vitest / node --test）并校验 Agent.md 的 Renderer 与 GUI 两个总数：`tests/test_doc_counts.py` 只能**静态**数 `it(`/`test(` 定义（pytest 作业没有 node_modules），而静态计数只是运行器的**模型**——R2254（445→448）、#1120（同 stem 文件整份被吞）、#1125（`it.each`/`test.skip` 正则看不见）三次都是模型与实践脱节。GUI 侧按 CI 环境（EMRG_SKIP_INTEGRATION=1）运行后减去 1 个模块级 `skip()` 原因条目（该条目数会先断言为 1，形状变了就停手而不是报个看着像对的数）。缺 node_modules 即报该原因，绝不报假数。**同样测「你站着的那个 checkout」**（与 doc count 同一处修复：root 原先取自 `__file__`，在 worktree 里跑主 checkout 的脚本会量到主树，`--write` 会改错树；现从 cwd 解析并报出所测的树）
Vote count: `uv run --no-sync python3 scripts/check-vote-count.py <PR>...` — 合并前数**仍然算数**的 LGTM 票，而不是评论里看得见的 ✅ 行数。三条规则让手工计数每次都错：① **票早于 head push 即作废**（rebase/解冲突推新 head 后，旧 ✅ 说的是已经不存在的 commit，而评论历史照样显示五六行 ✅，PR 看着能合实则零票——2026-09-11 实测 #1133/#1134/#1136/#1137 各显示 4-6 张 LGTM，解锁后**有效票全为 0**）；② **❌ 重置计数**（三次 ✅ 后一个 needs-fix，再一个 ✅ 只算一票）；③ **同一周期只算一票**（否则一个周期独自把 PR 送过门槛）。判票读正文首行**首个内容字符**（`gh pr review --comment` 让 GitHub 把 ✅ 和 ❌ 都记成 `COMMENTED`，state 字段在这里毫无用处）；该字符**透过 markdown 装饰**读取（`**❌`/`- ❌`/`> ❌`/`## ❌`/`1. ❌` 都算，装饰字符本身不可能是判决），并**由它决定整行**——✅ 开头即使后文提到 ❌ 仍是批准（那些提及是在说「没有 ❌」，说法是开放集合），整行扫描只作为无开头标记时的兜底（散文式「Not LGTM」为否决、「Result: ❌ needs fix」为否决）。周期号从正文里取（`cyc20260911-091230`）。push 时间取**该 SHA 上最早一次 workflow run 的创建时间**（就是 GitHub 收到 push 事件的时刻）；没有 run 时退回 commit 日期并**在输出里注明**（commit 日期可能早于 push，是乐观方向，不许默默采信）。exit 0 每张 PR 都够票 / 1 有 PR 缺票 / 2 问不出来（gh 失败、响应不可解析）——问不出来的问题绝不报数。评论**逐页取全**（reviews 端点默认只给 30 条且**按时间正序**，超限时丢掉的正是**最新的**票，也就是唯一算数的那几张；#1134 解锁一次就有 7 条评论），并在本地按时间排序（「❌ 重置计数」和「同周期只算一票」都是**位置相关**的规则，依赖服务端返回顺序等于把判决权外包）。两个自暴露的坑：① gh 的 `--jq` **后者覆盖前者**，helper 若自己追加 `--jq` 会吃掉调用方投影，字段名退回原名、`at` 读成空串，而 `"" <= push_time` 恒真 ⇒ **全部票作废**、有 2 票的 PR 显示 0/3（计数方向偏「安全」，没人会去查）；故 helper 只管 `--paginate`，投影归调用方，并对载荷形状**运行时断言**（缺 `at` 即 exit 2，不报数）
Merge-order forecast: `uv run --no-sync python3 scripts/check-merge-order.py [PR...] [--json]` — 合并前预报**每张 PR 合下去会弄脏哪些别的 PR**，把「合并的代价」提前变成可见数字。存在理由（cyc20260911-225712 实测）：11 张 PR 各自 `MERGEABLE/CLEAN` 且各自 CI 绿、又都领先 master，于是**任何一张都能合**；而合掉一张会让其余十张在**同一行**（Agent.md 的 Python 计数行）转 `CONFLICTING/DIRTY` — 脏 PR 拿不到 `pull_request` run（无 CI）、不能合，而解冲突必须推 head ⇒ **那些 PR 的票全部作废**（三张差一票的 PR 被打回 0/3）。代价是真的，且此前只在事后才发现。为什么不靠推理：直觉说法「它们都动计数行」**近乎**正确却**不能当规则**——同一天 55 对里有 4 对虽然共享 Agent.md 却**不冲突**（是否冲突取决于改动落点多近，是树的性质不是文件列表的性质），故问 `git merge-tree --write-tree`：rc 0 = 干净、1 = 冲突（并从输出首块读出**冲突的是哪个文件**，只报「有冲突」不足以决策）、其它 rc 一律算**没问出来**（**绝不当作冲突**——把测量失败报成冲突会凭空造出一场级联，调用方会为不存在的冲突白花一次解冲突）。推荐顺序看「合掉它弄脏几张」：弄脏最少的先合最便宜，但**最便宜 ≠ 最有价值**（当天正确的第一步恰恰是冲突**最多**的 #1149，因为它解开了后面每一张堆叠 PR 的 CI）。**自曝的坑（写这个工具的当天就被自己的实测抓住）**：先把 master fetch 进 `FETCH_HEAD`、再把**名字** `FETCH_HEAD` 当 base 传下去，而 `git fetch refs/pull/<N>/head` **也会重写 `FETCH_HEAD`** ⇒ 每对测量时的 base 已经变成最后 fetch 的那张 PR head；输出**完全像模像样**（「只有 #1152 对 master 可合、其余全冲突」），实际 base 就是 #1152 自己的 head（所以那两侧才「相同」）；破绽在计数行——那次运行关于 master 一个数字都没说，而真 master 是 1490、运行却表现得像 base 写着 1503。规则因此是：**可变 ref 名永不进 `merge-tree`**，base 先用 `rev-parse` 解析成 SHA，并由 `test_no_mutable_ref_name_reaches_merge_tree` 钉住该不变量（正是本可最早抓住它的测试）
Git-over-https 兜底: `python scripts/sync-master-from-api.py [--repo owner/name] [--ref master]` — 受限网络下 github.com:443 不可达而 api.github.com 可达时，用 Git Data API 的 verification payload + signature 字节级重建上游 commit（含 web-flow GPG 签名 squash merge，reconstruct_commit 经 hermetic 测试验证 sha 一致）并推进本地 refs；内容对象缺失时 fail-loud 提示改用 git fetch（10+ 周期实证的恢复路径）
Git-over-https push 兜底: `python scripts/push-branch-from-api.py --branch feature/x [--ref HEAD] [--force]` — 同一宕机场景下的 push 方向（#988 配对）：从本地 ref 沿一父链找到远端基点（已有分支头或首个远端已知祖先），自底向上上传 blobs（原始字节）/trees（`git mktree` 语义复算）/commits（结构化创建，author/committer 携带原始 +0800 偏移、消息去尾随换行——GitHub 规范化行为），更新远端 ref 后把本地分支 ref 重写为远端 sha 并 `git diff` 验证内容一致；失败即止不触碰 refs（hermetic 测试经忠实假 API 验证字节级 sha 一致）
Merge sequence: `uv run --no-sync python3 scripts/check-merge-sequence.py [--base <ref>] <PR>...` — 合并前问「按**这个顺序**合，每一步落地的树还过守卫吗」。健康是**步骤**属性：这一步的输入是上一步产出的树，所以任何「单 PR」事实都推不出它。存在理由（cyc20260912-174026 实测）：#1167 与 #1166 都带 master、都写 `(1541)` ⇒ 各自合入都 CLEAN 且自洽，**依次**合入却仍是 CLEAN 而守卫在合并树上红（1541 文档 vs 1560 实收）——两个相等值写同一行，git 保留一份、冲突为零、陈旧数字进 master。危险与信号**反向**：计数**值不同**必冲突（安全，逼人看一眼），**值相同**才静默通过，而 `check-merge-order.py` 恰好按「弄脏别人最少」排序 ⇒ 「挑最省事顺序」读起来正是那句不安全的建议。逐 PR 健康检查在此队列会**退化**：所有 head 都含 master ⇒ `merge-tree master head` 的结果等于分支自己的树 ⇒ 守卫只在回答「这条分支自洽吗」，而待审分支永远自洽。退出码：0 = **每一步都测过**且落地健康，1 = 有「干净合并却落地不健康树」的步骤（本工具存在的那个发现），2 = 无法测量（响亮失败，绝不把「没测成」报成健康；**空计划也算无法测量**——`args.prs or _open_pr_numbers(...)` 决定「零步」只可能是**计划根本没取到**，而旧行为会印 `all 0 step(s) ... pass the guards` 并退出 0，即对一个树都没量过的运行**断言通过**，故改为 2；实测同一行为在父修订 `6456a98` 与本修订上逐字相同 ⇒ 既有缺口而非本次回归），3 = **计划在冲突步中断**（只测了前缀，其后各步**根本没测**——`cyc20260913-084752` 实测：默认调用（全部在飞 PR 升序）在第 1 步 `#1136` 冲突即停，**一个树都没判**却退出 0，与「全部验证通过」在退出码上不可区分；三种状态药方不同——解冲突重排 vs 重排危险步 vs 重试测量失败——故各自一码，同 `check-vote-count.py` 分 `BLOCKED`/`SHORT`。只想知道「有没有发现问题」的调用方可判 `rc in (0, 3)`）。
Conflict triage: `python3 scripts/classify-conflict.py <file>... [--all]` — 合并冲突的**分类器（只报告，绝不改文件，exit 1 = 有需要人判的块）**，把「两边都动了同一个文件」拆成五种可判定的形状并给出证据支持的解法：`count-line`（两侧只差一个**带括号的计数**数字 ⇒ 先分清它是「存储的计数」还是「旧分支带进来的写死值」：GUI/Renderer 的分解是文档里的事实，必须在合并后的树上**测量**；Python 测试数自 2026-09-13 起不写进任何文档，故那一行一律**删掉数字**，永不选边，交给 `check-doc-count.py --resolve-conflict`）、`duplicate`（两侧**声明符号**互为真超集**且共有符号的正文全同** ⇒ 取超集那侧）、`disjoint`（声明符号不相交 ⇒ **两边都保留**）、`overlapping`（同名符号，或超集但共有符号正文不同，或单行无判据 ⇒ 必须人读）、`identical`。存在理由（cyc20260911-112155 实测）：同一轮里同一形状需要**相反**的解法——#1136 的分支带着 #1134 的**未合并副本**（分支基于 #1134 而后者已被 squash，祖先关系不可见），master 是严格超集故「取 theirs」正确；#1140 两侧是**不相交新增**，取任何一侧都会静默丢掉另一侧 4 个探针，唯一正解「两边都留」。一条 `--theirs` 对前者对、对后者错。两条判别轴都是被实测打出来的：① 必须是**声明符号**而非文本行——初版按内容行比较，因两侧都含 `    """` 与 `    )` 这种样板行，把两个真实冲突全判成 `overlapping`；② 名字子集**不等于**内容包含——只比名字时「master 声明了分支所有名字」被读成「master 含分支全部内容」，而两侧同名 `test_alpha` 正文不同时取超集会**静默丢掉分支对它的修改**（正是本工具要防的数据丢失）；③ `count-line` 必须限定为**带括号的计数**，否则 `x = compute(1)` vs `compute(2)` 这种纯代码差异也会被答「测量、永不选边」并 exit 0，把唯一该人看的情况关掉。三条均已钉为变异验证的回归测试（cyc20260911-120717）。**第四类：读不了的布局即拒绝**（2026-09-11 实测）——`merge.conflictStyle = diff3` 会在 ours 与分隔符之间插入 `||||||| <base>` 段，而 `CONFLICT_BLOCK` **照样匹配**（把 base 段吞进 ours），于是判出来的两侧是（ours+base）vs theirs：一段 `disjoint` 被判成 `KEEP BOTH` 并 exit 0，而「都保留」恰好会把**谁都不想要的 base 副本**拼回去；本仓库的真实形态（Agent.md 计数行）更隐蔽——判成 `duplicate - ours is a strict superset - take OURS`，因为 base 段（带着**陈旧**计数的第三份拷贝）被算成 ours 多出来的行。故先于解析检查该标记并报 `unparsed-layout` + exit 1，与 `check-doc-count.py` 对同一布局的具名拒绝一致（那条路是 read-only 无损失，只会不必要地拦住一个本可解的 PR；这条路的错误建议会让解冲突的人**删掉自己的改动**，代价不对称）。**第五类：跨行聚合与「同一段落的两个修订」**（cyc20260911-190629 实测，两个缺陷都在**本工具自己**里，且都靠「拿全部真实冲突块做差分」而非手写 fixture 才暴露出来——手写 fixture 只会覆盖我本来就相信的形状）：① 一条 `count-line` 原先是「两侧各一行」，但 51 次改 Agent.md 的提交里有 **3 次一次动掉 2 个以上计数**，git 会把它们并成**一个**块，该块因此落到内容行兜底、被判「两侧无共同行 ⇒ KEEP BOTH（拼接）」并 exit 0——拼接恰好把每个计数行**各留两份**，正是本仓库 `_duplicated_count_line_kinds` 守卫要拒的状态；现改为**对齐的、逐对只差数字的**块一律 `count-line`（任意长度，且每对都要满足带括号计数）。② 「两侧无共同行」**不等于**「各自新增」——同一段落的新旧两个修订永远不相等。当前 #1140 的 Agent.md 真实块里，ours 的两行段落正是 master 两行的**严格前缀**（890 vs 539、601 vs 471 字符），兜底判 KEEP BOTH 会同时留下陈旧与当前两份段落；现改为见到严格前缀关系即报 `overlapping` 交人读（前缀证据弱于符号子集，误差不对称：升级只是多读一次，「取 theirs」会静默丢一行）。两个修复各配变异验证（还原旧行为即测试变红）。**第六类：计数行与正文修订混在一个块里**（cyc20260911-194733 实测，发生在刚合入的第五类修复**自身**上）：第五类要求「两侧等长且**每一对**都只差数字」，而真实块常把**计数行**和**正文行**配在一起——cb651a4 的真实块正是 ours「Python 计数(1393) + Doc count sync 正文」对 theirs「同一计数行的另一个值 + 同一正文的另一次改写」，两侧等长却**有一对不是计数对**，于是仍落到内容行兜底、判 KEEP BOTH 并 exit 0，而拼接结果里有两个 ``Python: `uv run pytest` `` 行（即文档声称两个 pytest 数），正是 `_duplicated_count_line_kinds` 要拒的状态；且计数数字**在行内**，前面的严格前缀判据（`b.startswith(a)`）对它**结构性不可见**——它只认「短的是长的前缀」，而这里数字之后的内容被改写，两侧互不为前缀。改判据为**按下标配对**：只看对应位置上的行对，任一对「两边都带括号计数且遮罩数字后相等」即报 `overlapping` 交人读（不再要求等长）；证据强度就是遮罩本身——遮罩后相等意味着**同一个事实被重新测量过**，而同一事实出现两个值正是仓库守卫要拒的，故 KEEP BOTH 对它必错。实测收益（最近 400 次改 Agent.md 的提交，逐 hunk 差分）：7 个 hunk 在兜底里带上计数行且**全部**给出**重复内容**的建议——e46c160/0c8a212（对齐，已由第五类改判 count-line）、cb651a4(2v2)/5c039b4(3v3)（等长但混了正文修订，第五类看不见）、3335877(1v2)/444e1d5(1v2)/18fd0af(1v13)（不等长）；本条修复把余下 5 个全部转为 `overlapping`，全仓 150 次提交 1039 个 hunk 差分仅 6 处类别变化（5 个 Agent.md + 1 个 `preload-api.test.js`，后者也是真阳性：两侧写着 54/53 与 55/54 两个不同成员数）。变异验证：退回「只查前缀」即新增测试变红；对面（真实互不相干的两个新增）仍判 disjoint。**第七类：同一计数行的「重新拆分」**（cyc20260912-002444 实测，仍是拿真实冲突块差分出来的）：第六类要在「遮罩数字后**完全相等**」，而真实块里同一个计数**种类**还会被**重新拆分**——47af6bc2 的真实合并块里 ours 是 `GUI: … (92: 45 daemon_client + … + 3 preload-api + 3 boot-contract)`，master 是**同一行**的 `(89: … + 3 preload-api)`（少一个组件、总数也从 92 变 89），两侧**行数 1 vs 2**且遮罩后不等，于是两条规则都看不见，仍判 `disjoint - KEEP BOTH` 并 exit 0，而拼接结果里有两个 `GUI: ` 行——正是 `_duplicated_count_line_kinds` 要拒的状态（本测试直接把拼接结果喂给该守卫，不是靠眼看）。判据改为**按「计数种类」而非「整行相等」**：两侧「首个计数之前的文本完全相同」即同一行被重新测量过（该文本正是命令与种类名），报 `overlapping` 交人读。实测范围：用**真实合并提交**（对每个 merge 的三个真实 blob 跑 legacy `git merge-tree`）重建出 **185** 个冲突块，该规则**只改 1 个类**（就是这一块，`disjoint`→`overlapping`），其余不动；反向对照（`Python:` 对 `GUI:` 这种不同种类）不升级。变异验证：删掉该子句，新增测试即变红（`disjoint`）。

## Releasing

A release is a 4-stage flow; every stage is verifiable by the evolution itself (no host action required):

1. **Bump** — `python3 scripts/bump-version.py <x.y.z>` edits all 8 version declarations in one shot, then `uv run --no-sync pytest tests/test_version_sync.py -q` proves consistency (the guard covers every source, incl. both `package-lock.json` occurrences, #1065). Commit on `feature/release-v<x.y.z>` → PR → 3 LGTMs from different cycles → squash merge. Never self-merge a release PR.
2. **Tag** — `git tag v<x.y.z> && git push origin v<x.y.z>`. This is the *only* trigger for `build-release.yml` (the Test workflow on push/PR never exercises signing/notarization, the v0.2.7 lesson).
3. **Verify** — `gh run list --workflow=build-release.yml --limit 5` must show the tagged run green across the 4-platform matrix; the GUI leg reruns `npm run build` (Vite) + `npm run dist` (electron-builder), so `app.asar` is always rebuilt from source and cannot ship stale.
4. **Confirm** — GitHub Release published as Latest (not draft/prerelease) with the full asset set.

`scripts/bump-version.py --check` is the host-side counterpart to the CI guard: run it before pushing instead of discovering drift after a wasted build round. `--dry-run` previews without writing.

## Packaging

Generated icon products (`packaging/assets/icon.png/icon-512/icon-256/icon.icns/icon.ico`) are **gitignored** — only `icon.svg` design source is committed (#688); CI generates them in Build Release. Local installer builds (`make-installer.sh` / `build-runtime.sh`) require running `bash packaging/gen-assets.sh` first (idempotent; renderer priority rsvg-convert → Chrome headless → sips; `.icns` needs macOS `iconutil`, skipped elsewhere).

## Configuration

`~/.emrg/config.toml`:
```toml
[llm]
base_url = "https://api.deepseek.com"
api_key = "sk-..."
model = "deepseek-chat"
max_tokens = 8192
temperature = 0.7
context_window = 131072
auto_compact_threshold = 0.0
# vision: whether the model supports OpenAI vision API (image_url). Keep false for non-vision models.
vision = false

# Additional models for /model switching (optional — add or remove as needed)
# model: API model name (optional — defaults to name if not set)
# vision: per-model vision support flag (optional — defaults to false)
[[llm.models]]
name = "deepseek-v3"
model = "deepseek-chat"
context_window = 131072
vision = false

[[llm.models]]
name = "gpt-4o"
model = "gpt-4o"
context_window = 128000
vision = true
```
