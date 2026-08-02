# EMRG — Agent.md

> This is the Codex-compatible project context file. See `README.md` for the canonical project description (中文).

## Project Overview

EMRG is a self-evolving AI agent architecture experiment. Python implementation, based on a micro-kernel design.

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
- `emrg/skills/` — Dynamically loaded skill modules (skills, progressive disclosure)
- `emrg/client/` — Client (TUI interface based on inlined python-tui)
  - `app.py` — Main entry, event loop, ChatHistory widget, command autocomplete, session selector

## Key Conventions

- **The server is the living core; the client is just the interface**
- Client auto-detects/starts the server on launch; server stays running on client exit
- Server logs are discarded (`stderr=DEVNULL`)
- Client logs go to `./.emrg/emrg-client.log`
- **README language**: `README.md` = Chinese (default), `README.en.md` = English
- **Project context files**: `README.md` = Chinese, `Agent.md` = English

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
