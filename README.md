# 🧱 EMRG

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

EMRG is an experiment in *autonomous self-improvement*. It's an AI agent that helps you code — reading files, running commands, making edits — but the key difference is: **every `/rant` you send drives it to improve itself**. Tell it what bothers you, and the next evolution cycle writes code to fix it. Combined with GitHub community activity and competitor tracking, EMRG evolves continuously in the background, getting better the more you use it. All open source, all transparent.

> *"EMRG is an experiment in self-evolving AI agent architecture."* — [MANIFESTO](MANIFESTO.md)

---

## ✨ Why you'll love it

**The one-line pitch**: EMRG is the only coding agent that **improves itself from your feedback** — rant about what bothers you, and the next evolution cycle writes the fix and ships it. Chat, tools, and memory are what you'd expect; the self-improvement loop is what you won't find anywhere else.

| What | What it means |
|---|---|
| 🔄 **Gets better on its own** | **The core feature.** Background evolution cycles turn your `/rant`s, GitHub issues, and competitor updates into real improvements — analyzed, coded, tested, PR'd, merged. Self-healing workspace: packaged installs clone the repo on demand and bootstrap projects/tasks automatically |
| 🖥️ **Electron GUI (main entry)** | Install & go: first-run wizard configures your API key; all 15 slash commands work in the GUI (`/rant` evolution dialog, `/memory` browser…); WorkBuddy-inspired **results panel**, **Ask/Auto** modes, **visible self-evolution** (growth card + toasts). GUI configured = TUI ready |
| 🧠 **Reads, writes, edits, runs** | Full tool-calling agent — bash, files, diffs, glob, grep |
| 📝 **Never forgets** | Project memory + session memory + daily logs — context that persists |
| ⚡ **Full-featured TUI + daemon** | Streaming markdown, `/` autocomplete, session picker, ESC interrupt, vim-friendly keys, parallel tool calls — on a persistent `emrgd` daemon you can reconnect to anytime |
| 🌍 **100% open source** | MIT — no walled garden, no vendor lock-in. Internationalized: English default, Chinese version available |

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

Runs fully unattended: `gh` auth auto-recovers from your git credentials, PR votes use the REST API, and packaged-install workspaces self-heal (clone on demand, auto-bootstrap projects/tasks). See [MANIFESTO.md](MANIFESTO.md) — EMRG's design charter on autonomous evolution.

---

## 🚀 Quick Start

### 📦 Download installer (recommended, Phase 4 one-click)

Download the installer for your platform from [GitHub Releases](https://github.com/argszero/emrg/releases) and double-click:

| Platform | Installer | Notes |
|----------|-----------|-------|
| macOS (Apple Silicon) | `EMRG-<ver>-macos-arm64.pkg` | Double-click (user-level install, no admin password); GUI at `~/Applications/EMRG.app` |
| macOS (Intel) | `EMRG-<ver>-macos-x64.pkg` | Same |
| Windows | `EMRG-<ver>-windows-x64.exe` | Inno Setup, no UAC, Start-menu shortcut, PATH auto-registered (native TUI: run `emrg` directly in cmd/PowerShell) |
| Linux | `EMRG-<ver>-linux-x86_64.AppImage` | Self-extracts to `~/.emrg/install/` on first run |
| Linux (ARM64) | `EMRG-<ver>-linux-aarch64.AppImage` | Same |

> **Windows SmartScreen notice**: The Windows installer is not Authenticode-signed (publisher shows "Unknown"), so SmartScreen may show "usually doesn't download" or "Windows protected your PC" on first download/run. This is a standard security prompt for unsigned software — it does **not** mean the file is bad (EMRG is fully open source and auditable). To proceed:
> (1) Browser download prompt → click **Keep** (or ⋯ → Keep)
> (2) If double-clicking shows "Windows protected your PC" → click **More info** → click **Run anyway**
> (3) Or right-click the exe → Properties → check **Unblock** (if present) → OK → double-click to run

The installer bundles a full runtime (standalone Python 3.13 + deps + git + gh + GUI) — **zero prerequisites on a clean machine (no python/uv/git/gh/node)**, 100% offline install. After install:

**Three steps to start:**
1. Launch **EMRG** (GUI) from Launchpad / Start menu
2. First-run wizard configures **API Key / model**
3. Start chatting — **TUI is ready too** (run `emrg` in a new terminal)

> **Uninstall**: macOS run "卸载 EMRG.app" (uninstall app); Windows uninstall from Control Panel; Linux run `~/.emrg/install/bin/emrg-uninstall` (or delete the AppImage + symlink). Uninstall preserves non-EMRG user files in `~/.emrg`, and writes a termination report + data snapshot.
>
> **macOS signing & notarization**: since v0.2.7, macOS packages are signed with Developer ID dual-cert and notarized by Apple (zero Gatekeeper dialogs — double-click to install directly). Only for unsigned builds (e.g. self-built old versions) is right-click → Open needed on first launch.

### 🍎 macOS (source install)

**Install:**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
```

**Uninstall:**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- purge
```

### 🐧 Linux (source install)

**Install:**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
```

**Uninstall:**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- purge
```

### 🪟 Windows (WSL2, source install)

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

> Source-install prerequisites (install.sh auto-detects and prompts): git, python 3.11+, uv. gh CLI recommended. For native Windows (non-WSL), use the installer above.

### 🖥️ First-time config (GUI first)

After installing, **open the GUI to configure**:

1. **macOS**: Launchpad → **EMRG**; **Windows**: Start menu → **EMRG**
2. The first-run wizard walks you through **API Key / base URL / model** (also editable anytime in Settings ⚙)
3. Save and start chatting — **config is written to `~/.emrg/config.toml`, shared by GUI and TUI**

> 💡 **GUI configured = TUI ready**: the installer bundles a full TUI. Config saved in the GUI (API key/model/workdir) goes to `~/.emrg/config.toml`, so running `emrg` in a new terminal enters the TUI with no re-configuration. Since v0.2.8 the GUI supports **all 15 slash commands** — type `/` in the input box for an autocomplete menu (same as the TUI).

### ⌨️ Using the TUI

```bash
emrg
```

Type `/help` for all commands, or just start talking — EMRG reads files, runs commands, and makes edits.

### 🔧 Advanced config (optional)

> The GUI rewrites config on save and drops comments — advanced users can edit `~/.emrg/config.toml` directly (no manual editing needed for first-time setup; the GUI wizard handles it).

`~/.emrg/config.toml` template example (the GUI generates equivalent content on save):

```toml
[llm]
base_url = "https://api.deepseek.com"
api_key = "sk-..."
model = "deepseek-chat"
max_tokens = 8192
temperature = 0.7
context_window = 131072
auto_compact_threshold = 0.0
# vision: whether the model supports the OpenAI vision API (image_url). Keep false for
# non-vision models (e.g. DeepSeek) — pasted images degrade to text placeholders to avoid API errors.
vision = false

# Multi-model support — use /model to switch between models
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

---

## 🎮 Commands

> Since v0.2.8, **all commands work in both the GUI and the TUI**. In the GUI, type `/` in the input box for an autocomplete menu; in the TUI, `/help` lists everything.

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
| `/model [name]` | Switch LLM model — no args for interactive picker |
| `/rant <feedback> [@<project>]` | Complain, suggest, praise — evolution listens; `@project` targets a specific project |
| `/help` | Show keyboard shortcuts and command help |
| `/image` | Insert clipboard image into the input field (multiple supported, one per Enter) |
| `/delete [id]` | Delete a session — no args for interactive picker |
| `/rewind` | Rewind conversation — pick a history point and truncate after it |
| `/trigger` | Trigger an evolution task — interactive picker (↑↓/j/k) |
| `/skills` | List loaded skills (incl. skill-catalog); `/skills available` = installable catalog, `/skills install <name>` = install, `/skills update` = refresh managed skills |
| `/version` | Show EMRG version and instance info |
| `Esc` | Interrupt a running response mid-stream |
| `Ctrl+C` / `exit` | Quit |

---

## 🏗️ Architecture

```
┌─────────────┐    WebSocket (ws://)    ┌──────────────┐
│   emrg TUI  │ ◄─────────────────────► │   emrgd      │
│  (client)   │  TCP loopback + auth    │  (daemon)    │
│             │  token (emrgd.port)     │              │
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
uv run pytest tests/ -v   # run tests (currently 652 items)
uv run python -m emrg     # launch TUI
# CI includes actionlint workflow gate (#444): workflow parse errors fail PR CI

# Optional: Electron GUI (non-developer entry point, Phase 3)
cd emrg/gui
npm ci               # install deps (production: --omit=dev)
npm start            # launch GUI (auto-starts daemon)
npm test             # run Node tests (103: 29 daemon_client + 22 app-commands + 27 renderer smoke + 15 i18n + 7 integration + 3 commands; integration runs in CI, local: npm run test:integration)
```

CI runs tests and checks for conflict markers automatically via GitHub Actions (`.github/workflows/test.yml`).

### Project structure

```
emrg/
├── emrg/                   # Core package
│   ├── server/             # Daemon — LLM loop, tool execution, evolution
│   ├── client/             # TUI — python-tui based interactive chat
│   ├── gui/                # Electron GUI (non-developer entry point, Phase 3)
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

**Why does the Windows installer show "Unknown publisher"?**<br>
The Windows installer is not Authenticode-signed (that certificate costs money to obtain and is not procured yet), so SmartScreen shows "Publisher: Unknown" and may block the run. This is a standard Microsoft security prompt for newly released/unsigned software — it does **not** mean the file is bad: EMRG is fully open source (MIT) and auditable. To proceed: click "Keep" on the browser prompt; click "More info → Run anyway" on the run prompt; or right-click the exe → Properties → check "Unblock". The macOS installer is signed + notarized (v0.2.7+) and has no such prompt.

---

## 📜 License

MIT — see [LICENSE](LICENSE) for the full terms and [MANIFESTO.md](MANIFESTO.md) for the philosophy behind the code.

---

<p align="center">
  <sub>Built with ❤️ by <a href="https://github.com/argszero">argszero</a> — and a continuously evolving AI.</sub>
</p>
