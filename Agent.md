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
  - `connect.py` — IPC connection (Unix Socket / Named Pipe, platform-adaptive)
  - `memory.py` — Memory system (ProjectMemoryStore, SessionMemoryStore, MemoryFile, MemoryIndex)
  - `session.py` — Session management (Session CRUD, history persistence, compact/clear)
- `emrg/server/` — Server (Unix socket daemon, EMRG's living core)
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
  - Slash commands: `/help`, `/clear`, `/resume`, `/rename`, `/memory`, `/sessions`, `/rant`, `/model`, `/version`
  - `/model <name>` to switch LLM models at runtime (configured via `[[llm.models]]` in config.toml)
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
- **Config hot-reload** — Detects `~/.emrg/config.toml` changes and auto-restarts server
- **Memory system** — Project and session memory with YAML frontmatter, indexing, merge/split
- **Skills** — Progressive disclosure via `.emrg/skills/` directory

## Test Commands

```bash
pkill -f "emrg.server"; rm -f ~/.emrg/emrgd.sock; python -m emrg
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

# Additional models for /model switching (optional)
[[llm.models]]
name = "deepseek-v3"
model = "deepseek-chat"
context_window = 131072

[[llm.models]]
name = "deepseek-r1"
model = "deepseek-reasoner"
context_window = 65536
```
