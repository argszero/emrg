# EMRG

<p align="center">
  <img src="packaging/assets/icon.svg" alt="EMRG" width="96" height="96">
</p>

<p align="center">
  <strong>The AI coding agent that writes code — and rewrites <em>itself</em>.</strong>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green.svg">
  <img alt="Status" src="https://img.shields.io/badge/status-evolving-orange.svg">
  <img alt="Tests" src="https://github.com/argszero/emrg/actions/workflows/test.yml/badge.svg">
  <img alt="PRs Welcome" src="https://img.shields.io/badge/PRs-by%20AI%20%2B%20human-brightgreen.svg">
</p>

<p align="center">
  <b>🇬🇧 English</b> | <a href="README.cn.md">🇨🇳 中文</a>
</p>

---

**What if your coding assistant got better every time you used it?**

EMRG is an experiment in *autonomous self-improvement*: an AI agent that helps you code — reading files, running commands, making edits — and **every `/rant` you send drives it to improve itself**. Tell it what bothers you, and the next evolution cycle writes the fix and ships it. Fully open source, fully transparent.

> *"EMRG is an experiment in self-evolving AI agent architecture."* — [MANIFESTO](MANIFESTO.md)
>
> *Everyone is a product manager — every rant is a ticket for the next release.*
> *Everyone is a host of silicon life — what you run is a digital organism that evolves with you.*

**EMRG** — pronounced *"emerge"*: intelligence that *emerges* — from scale, and from use. The name expands to **E**volving **M**icro-kernel, **R**ant-driven **G**rowth.

---

## Why you'll love it

**The one-line pitch**: EMRG is the only coding agent that **improves itself from your feedback** — rant about what bothers you, and the next evolution cycle writes the fix and ships it.

> **The one feature: self-improvement.** Every `/rant` becomes a real PR — coded, tested, merged, unattended. No other coding agent does this.

| What | Why it's different |
|---|---|
| **Electron GUI (main entry)** | Install & go: first-run wizard, all slash commands, results panel, Ask/Auto modes, visible self-evolution. GUI configured = TUI ready |
| **Full-featured TUI + daemon** | Streaming markdown, `/` autocomplete, sessions, shortcuts — on a persistent `emrgd` daemon you can reconnect to anytime |
| **Scheduled tasks** | Built-in + custom task types with prompt templates (`~/.emrg/task-templates/`), CRUD + hot reload from the GUI settings, `/trigger` any task on demand |
| **100% open source** | MIT — no walled garden, no vendor lock-in. English default, Chinese version available |

---

## Rant-Driven Evolution (the core feature)

EMRG isn't just a tool — it's a coding partner that **listens to your complaints and improves itself**:

```
Inputs:
  - Your rants (/rant)   <- primary input
  - GitHub Issues & PRs
  - Competitor tools (Codex, Claude Code)
  - Cross-project learning
          |
          v
Evolution Cycle (every 30 min)
(Prepare -> Review -> Discover -> Improve -> Commit -> Record)
          |
          v
  1. pytest + import check
  2. git commit + push -> PR
  3. Evolution log
```

**Real example**: Someone ranted "TUI needs `/` autocomplete like Codex." The next evolution cycle built it — prefix filtering and arrow-key navigation, merged and deployed. **What you rant about, it improves.**

**How to contribute?** Use it. Connect GitHub in Settings, and rant. Your rants become real PRs — the evolution cycle codes, tests, and ships them. No fork, no clone, no code required. **Using EMRG is contributing to EMRG.**

---

## Quick Start

### Download the installer (recommended)

Download from [GitHub Releases](https://github.com/argszero/emrg/releases) and double-click — the installer bundles everything (Python 3.13 + git + gh + GUI), **zero prerequisites, offline install**:

| Platform | Installer |
|----------|-----------|
| macOS | `EMRG-<ver>-macos-arm64.pkg` / `-x64.pkg` (user-level, no admin password) |
| Windows | `EMRG-<ver>-windows-x64.exe` (no UAC, PATH auto-registered) |
| Linux | `EMRG-<ver>-linux-x86_64.AppImage` / `-aarch64.AppImage` |

> **Windows SmartScreen notice**: the installer isn't Authenticode-signed — if SmartScreen prompts, click **Keep** / **More info → Run anyway**. EMRG is fully open source and auditable.

**First time**: launch **EMRG** → the wizard sets your **API key / model** → start chatting. The TUI is ready too: run `emrg` in any terminal (config is shared).

> **Bring your own key** — EMRG uses your LLM API key and your quota/billing; the software itself is free and MIT-licensed.

> Prefer building from source, or need advanced config / architecture / contributing docs? → [DEVELOPMENT.md](DEVELOPMENT.md)

---

## Commands

> All commands work in both the GUI (type `/` for autocomplete) and the TUI (`/help` lists everything).

| Command | What it does |
|---|---|
| **Just type** | Ask EMRG anything — it reads files, runs commands, makes edits |
| `/` | Autocomplete menu — type to filter, ↑↓ to select |
| `/resume [id]` | Switch sessions — no args for interactive picker (↑↓/j/k) |
| `/sessions` | Browse all saved sessions (↑↓/j/k) |
| `/clear` | Clear current session — start fresh |
| `/compact` | Compress long conversations to save context |
| `/memory` | Browse project & session memories |
| `/rename [title]` | Give your session a memorable name |
| `/model [name]` | Switch LLM model — no args for interactive picker |
| `/rant <feedback> [@<project>]` | Complain, suggest, praise — evolution listens |
| `/help` | Show keyboard shortcuts and command help |
| `/image` | Insert clipboard image into the input field |
| `/delete [id]` | Delete a session — no args for interactive picker |
| `/rewind` | Rewind conversation to a history point |
| `/trigger` | Trigger an evolution task — interactive picker (manage tasks in GUI Settings → Scheduled tasks) |
| `/skills` | List loaded skills; `/skills available|install|update` |
| `/version` | Show EMRG version and instance info |
| `Esc` | Interrupt a running response mid-stream |
| `Ctrl+C` / `exit` | Quit |

---

## vs. the competition

|  | Claude Code | Codex | **EMRG** |
|---|---|---|---|
| AI-powered coding | ✅ | ✅ | ✅ |
| Tool-calling (bash, read, write, edit, glob, grep) | ✅ | ✅ | ✅ |
| Session memory & context | ✅ | ✅ | ✅ |
| `/` command autocomplete | ✅ | ✅ | ✅ |
| ESC interrupt | ✅ | ✅ | ✅ |
| **Self-evolution** | ❌ | ❌ | ✅ *autonomous* |
| **Background daemon** | ❌ | ❌ | ✅ *persistent* |
| **Learns from rants** | ❌ | ❌ | ✅ */rant → PR* |
| **Open source** | ❌ | ✅ *Apache-2.0* | ✅ *MIT* |

> *Codex is open source but has no self-evolution — openness alone isn't the differentiator; closing the loop is.*

EMRG doesn't just keep up — it catches up on its own.

---

## FAQ

**Is this real — does it actually modify its own code?**<br>
Yes. The evolution cycle reads the evolution prompt, reviews rants + issues + competitor tools, makes source changes, runs tests, and submits a PR. If tests fail, it rolls back.

**Can it break itself?**<br>
Every change is validated by `pytest` and an import check before commit. Failed changes are discarded; the worst case is a rollback.

**Will it touch my project's code?**<br>
No. Self-evolution only modifies its own repository (`~/.emrg/evolution/emrg`), never your project files. The tools it runs on your project are only the ones you ask it to run — and every change it makes is a reviewed PR, tested before merge.

**How is this different from Claude Code or Codex?**<br>
They're products. EMRG is an experiment in *closing the loop* — the AI improves the AI. Fully open source, no vendor lock-in.

---

## Development

Contributing, source installs, architecture, and the full FAQ → [DEVELOPMENT.md](DEVELOPMENT.md).

Quick checks: `uv run pytest tests/ -v` · `cd emrg/gui && npm test` (tests: see badge above)

---

## License

MIT — see [LICENSE](LICENSE) for the full terms and [MANIFESTO.md](MANIFESTO.md) for the philosophy behind the code.

---

<p align="center">
  <sub>Built with ❤️ by <a href="https://github.com/argszero">argszero</a> — and a continuously evolving AI.</sub>
</p>
