# 🧱 EMRG

<p align="center">
  <strong>The AI coding agent that writes code — and rewrites <em>itself</em>.</strong>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10+-blue.svg">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green.svg">
  <img alt="Status" src="https://img.shields.io/badge/status-evolving-orange.svg">
  <img alt="Tests" src="https://github.com/argszero/emrg/actions/workflows/test.yml/badge.svg">
  <img alt="PRs Welcome" src="https://img.shields.io/badge/PRs-by%20AI%20%2B%20human-brightgreen.svg">
</p>

<p align="center">
  <b>🇬🇧 English</b> | <a href="README.md">🇨🇳 中文</a>
</p>

---

**What if your coding assistant got better every time you used it?**

EMRG is an experiment in *autonomous self-improvement*. It's an AI agent that helps you code — reading files, running commands, making edits — but the key difference is: **every `/rant` you send drives it to improve itself**. Tell it what bothers you, and the next evolution cycle writes code to fix it. Combined with GitHub community activity and competitor tracking, EMRG evolves continuously in the background, getting better the more you use it. All open source, all transparent.

> *"EMRG 是一个自我演进的 AI 智能体架构实验。"* — [MANIFESTO](MANIFESTO.md)

---

## ✨ Why you'll love it

| What | What it means |
|---|---|
| 🧠 **Reads, writes, edits, runs** | Full tool-calling agent — bash, files, diffs, all in your terminal |
| 🔄 **Gets better on its own** | Background evolution cycles review rants + GitHub + competitor tools, then auto-PR improvements |
| 📝 **Never forgets** | Project memory + session memory + daily logs — context that persists |
| 🖥️ **Beautiful TUI** | Slash-command autocomplete, session picker, streaming markdown, elapsed timer, ESC interrupt |
| ⚡ **Parallel tools** | Independent tool calls run concurrently for speed |
| 🔌 **Micro-kernel daemon** | `emrgd` runs persistently — reconnect anytime without losing state |
| 🎮 **Vim-friendly** | `j`/`k` navigation, `Ctrl+W`/`Ctrl+K` editing, `Tab` to expand tool cards |
| 🌍 **100% open source** | MIT license — no walled garden, no vendor lock-in |

---

## 🚀 Quick Start

### 🍎 macOS

**Install:**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
```

**Uninstall:**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- purge
```

### 🐧 Linux

**Install:**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
```

**Uninstall:**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- purge
```

### 🪟 Windows (WSL2)

**Install:**

```powershell
# Install WSL2 (skip if already installed)
wsl --install

# Enter WSL, then install
wsl
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
```

**Uninstall:**

```bash
# Run inside WSL
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- purge
```

> Prerequisites (install.sh auto-detects and prompts): git, python 3.10+, uv. gh CLI recommended.

After installing, edit the auto-generated config template:

```bash
# install.sh already creates ~/.emrg/config.toml template — just set your api_key and model:
vim ~/.emrg/config.toml

emrg
```

Type `/help` to see all commands, or just start talking — EMRG reads files, runs commands, and makes edits.

---

## 🎮 Commands

| Command | What it does |
|---|---|
| **Just type** | Ask EMRG anything — it reads files, runs commands, makes edits |
| `/` | Autocomplete menu — type to filter, ↑↓ to select |
| `/resume [id]` | Switch sessions — no args for interactive picker (↑↓/j/k to navigate) |
| `/sessions` | Browse all saved sessions (↑↓/j/k to navigate) |
| `/clear` | Clear current session — start fresh |
| `/compact` | Compress long conversations to save context |
| `/memory` | Browse project & session memories |
| `/rename [title]` | Give your session a memorable name |
| `/rant <feedback> [@<project>]` | Complain, suggest, praise — evolution listens; `@project` targets a specific project |
| `/help` | Show keyboard shortcuts and command help |
| `/version` | Show EMRG version and instance info |
| `Esc` | Interrupt a running response mid-stream |
| `Ctrl+C` / `exit` | Quit |

---

## 🔄 Rant-Driven Evolution (the core feature)

EMRG isn't just a tool — it's a coding partner that **listens to your complaints and improves itself**. **Your rants are the primary driver of evolution.** Every `/rant` is read, analyzed, and turned into code improvements:

```
 📢 Your rants (/rant) ←── primary input
 📥 GitHub Issues & PRs
 📥 Competitor tools (Codex, Claude Code)
 📥 Cross-project learning
         ↓
    🧬 Evolution Cycle (every 30 min)
    (Prepare → Review → Discover → Improve → Commit → Record)
         ↓
    ✅ pytest + import check
    ✅ git commit + push → PR
    ✅ Evolution log
```

**Real example**: Someone ranted "TUI needs `/` autocomplete like Codex." Next evolution cycle, EMRG built it — complete with prefix filtering and arrow-key navigation. Merged. Deployed. Done. **What you rant about, it improves.**

> 💡 See [MANIFESTO.md](MANIFESTO.md) — EMRG's design charter on autonomous evolution in the AI era.

---

## 🏗️ Architecture

```
┌─────────────┐     Unix Socket IPC     ┌──────────────┐
│   emrg TUI  │ ◄──────────────────────► │   emrgd      │
│  (client)   │   JSON newline-delimited │  (daemon)    │
│             │                          │              │
│  • Chat     │                          │  • LLM loop  │
│  • Markdown │                          │  • Tools     │
│  • ToolCards│                          │  • Evolution │
│  • Autocomplete                       │  • Sessions  │
└─────────────┘                          └──────────────┘
```

- **`emrgd`** — The daemon: runs the LLM tool-calling loop, manages sessions, drives evolution
- **`emrg`** — Your terminal: streaming markdown, command autocomplete, session browser
- **Skills** — Dynamically loaded modules (browser harness, installers, etc.)
- **Memory** — YAML frontmatter + Markdown files, auto-indexed, searchable

---

## 📊 vs. the competition

|  | Claude Code | Codex | **EMRG** |
|---|---|---|---|
| AI-powered coding | ✅ | ✅ | ✅ |
| Tool-calling (bash, read, write, edit, glob, grep) | ✅ | ✅ | ✅ |
| Session memory & context | ✅ | ✅ | ✅ |
| `/` command autocomplete | ✅ | ✅ | ✅ |
| Arrow-key session picker | ✅ | ✅ | ✅ |
| ESC interrupt | ✅ | ✅ | ✅ |
| **Self-evolution** | ❌ | ❌ | ✅ *autonomous* |
| **Background daemon** | ❌ | ❌ | ✅ *persistent* |
| **Learns from rants** | ❌ | ❌ | ✅ */rant → PR* |
| **Open source** | ❌ | ❌ | ✅ *MIT* |

EMRG doesn't just keep up — it catches up on its own.

---

## 🧪 Development

```bash
git clone https://github.com/argszero/emrg.git
cd emrg
uv sync              # install deps
uv run pytest tests/ -v   # run tests (currently 159 items)
uv run python -m emrg     # launch
```

CI runs tests and checks for conflict markers automatically via GitHub Actions (`.github/workflows/test.yml`).

### Project structure

```
emrg/
├── emrg/                   # Core package
│   ├── server/             # Daemon — LLM loop, tool execution, evolution
│   ├── client/             # TUI — python-tui based interactive chat
│   ├── tools/              # bash, read, write, edit, glob, grep
│   ├── skills/             # Dynamically loadable modules
│   └── __main__.py         # CLI entry point
├── tests/
├── .github/workflows/      # CI pipeline (pytest + conflict marker check)
├── MANIFESTO.md            # Design constitution
└── pyproject.toml
```

---

## ❓ FAQ

**Is this real — does it actually modify its own code?**<br>
Yes. The evolution cycle reads the evolution prompt, reviews rants + issues + competitor tools, makes source changes, runs tests, and submits a PR. If tests fail, it rolls back.

**Can it break itself?**<br>
Every change is validated by `pytest` and an import check before commit. Failed changes are discarded. The worst case is a rollback.

**What LLMs work with it?**<br>
Any OpenAI-compatible API. Tested with DeepSeek and OpenAI. Works with Anthropic (via proxy), Ollama, vLLM, and other local models.

**How is this different from Claude Code or Codex?**<br>
They're products. EMRG is an experiment in *closing the loop* — the AI improves the AI. Also: fully open source, no vendor lock-in, and you control your data.

---

## 📜 License

MIT — see [LICENSE](LICENSE) for the full terms and [MANIFESTO.md](MANIFESTO.md) for the philosophy behind the code.

---

<p align="center">
  <sub>Built with ❤️ by <a href="https://github.com/argszero">argszero</a> — and a continuously evolving AI.</sub>
</p>
