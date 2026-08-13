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
  - `connect.py` — IPC connection (WebSocket over TCP loopback, token auth via `emrgd.port`)
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
  - Unit tests `npm test` (235: 44 daemon_client + 19 conn-manager + 22 app-commands + 113 renderer smoke + 15 i18n + 7 integration + 3 commands + 5 build-config + 7 gui-state); RESPONSE_TYPES mirror daemon protocol verified against `daemon.py`
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
pkill -f "emrg.server"; rm -f ~/.emrg/emrgd.port; python -m emrg
```

Python: `uv run pytest tests/ -v` (777) — import check: `uv run python -c "from emrg.client.app import run_client"`
GUI: `cd emrg/gui && npm test` (235: 44 daemon_client + 19 conn-manager + 22 app-commands + 113 renderer smoke + 15 i18n + 7 integration + 3 commands + 5 build-config + 7 gui-state) — syntax: `node --check main.js preload.js daemon_client.js renderer/js/*.js`
CI: `uv run pytest` (ubuntu + **windows-2025 matrix** — Windows pytest 回归在 PR CI 即失败，v0.2.29 教训 #725) + GUI tests + **actionlint workflow lint** (`rhysd/actionlint@v1.7.12` gate, #444 — workflow 解析错误在 PR CI 即失败，如 `if:` secrets 上下文)
Re-trigger: `scripts/re-trigger-ci.sh [branch]` (workflow_dispatch, #527 — 替代空 commit 重触发：Actions outage 会整段丢弃 push 事件，dispatch 走 API 路径不受影响)

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
