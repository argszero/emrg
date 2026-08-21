# 🛠️ EMRG — Development & Advanced Topics

This is the developer/advanced companion to [README.md](README.md). It covers source installs, advanced configuration, architecture, and the full test/CI workflow — details intentionally kept out of the concise top-level README.

---

## 📦 Source Install (without the packaged installer)

The [packaged installers](README.md) (pkg / exe / AppImage) are recommended for end users — zero prerequisites, 100% offline. Prefer building from source? Use `install.sh` below.

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

> Source-install prerequisites (install.sh auto-detects and prompts): git, python 3.11+, uv. gh CLI recommended. For native Windows (non-WSL), use the packaged installer.

---

## 🔧 Advanced Configuration

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

**Update checking** (`[update]` section): `check = true|false` (default true) enables periodic GitHub release checks; `ttl_hours = 24` controls how often. EMRG only **checks and prompts** — it never auto-downloads or auto-installs.

---

## 🏗️ Architecture

```
┌─────────────┐    WebSocket (ws://)    ┌──────────────┐
│   emrg TUI  │ ◄─────────────────────► │   emrgd      │
│  (client)   │  TCP loopback + auth    │  (daemon)    │
│             │  token (emrgd.token)    │              │
│  • Chat     │                         │  • LLM loop  │
│  • Markdown │                         │  • Tools     │
│  • ToolCards│                         │  • Evolution │
│  • Autocomplete                       │  • Sessions  │
└─────────────┘                         └──────────────┘
```

- **`emrgd`** — The daemon: runs the LLM tool-calling loop, manages sessions, drives evolution (a persistent background thread keeps thinking/evolving even while idle)
- **`emrg`** — Your terminal: streaming markdown, command autocomplete, session browser
- **Skills** — Dynamically loaded modules (browser harness, installers, etc.)
- **Memory** — YAML frontmatter + Markdown files, auto-indexed, searchable

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

## 🧪 Development Workflow

```bash
git clone https://github.com/argszero/emrg.git
cd emrg
uv sync              # install deps
uv run pytest tests/ -v   # run tests (currently 681 items)
uv run python -m emrg     # launch TUI
# CI includes actionlint workflow gate (#444): workflow parse errors fail PR CI
```

**Quick sanity checks:**

```bash
uv run python -c "from emrg.client.app import run_client"   # import check
uv run python -m emrg --help
```

### Electron GUI

```bash
cd emrg/gui
npm ci               # install deps (production: --omit=dev)
npm start            # launch GUI (auto-starts daemon)
npm test             # run Node tests (178: 43 daemon_client + 19 conn-manager + 22 app-commands + 59 renderer smoke + 15 i18n + 7 integration + 3 commands + 3 build-config + 7 gui-state; integration runs in CI, local: npm run test:integration)
```

### Packaging (installer builds)

Generated icon products (`icon.png`/`icon-512`/`icon-256`/`icon.icns`/`icon.ico`) are **not committed** — the repo keeps only the SVG design source (`packaging/assets/icon.svg`); CI generates them at build time (#688). When building installers locally (`packaging/make-installer.sh` / `build-runtime.sh`), run the generator first:

```bash
bash packaging/gen-assets.sh   # icon.svg → png/icns/ico (idempotent)
```

Renderer priority: `rsvg-convert` → Chrome/Chromium headless → `sips` (last resort, glow may be lost). Requires macOS `iconutil` for `.icns` (skipped with a notice on Linux/Windows).

CI runs tests and checks for conflict markers automatically via GitHub Actions (`.github/workflows/test.yml`).

> **Self-evolution from source**: the evolution workspace expects the repo at `~/.emrg/evolution/emrg`. Packaged installs self-heal (clone on demand + auto-bootstrap projects/tasks); source installs should clone there explicitly if you want the evolution daemon to work on this repo.

---

## ❓ Extended FAQ

**What LLMs work with it?**<br>
Any OpenAI-compatible API. Tested with DeepSeek and OpenAI. Works with Anthropic (via proxy), Ollama, vLLM, and other local models.

**Why does the Windows installer show "Unknown publisher"?**<br>
The Windows installer is not Authenticode-signed (that certificate costs money to obtain and is not procured yet), so SmartScreen shows "Publisher: Unknown" and may block the run. This is a standard Microsoft security prompt for newly released/unsigned software — it does **not** mean the file is bad: EMRG is fully open source (MIT) and auditable. To proceed: click "Keep" on the browser prompt; click "More info → Run anyway" on the run prompt; or right-click the exe → Properties → check "Unblock". The macOS installer is signed + notarized (v0.2.7+) and has no such prompt.

**Can it break itself?**<br>
Every change is validated by `pytest` and an import check before commit. Failed changes are discarded. The worst case is a rollback.

**How is this different from Claude Code or Codex?**<br>
They're products. EMRG is an experiment in *closing the loop* — the AI improves the AI. Also: fully open source, no vendor lock-in, and you control your data.

---

## 📜 License

MIT — see [LICENSE](LICENSE) for the full terms and [MANIFESTO.md](MANIFESTO.md) for the philosophy behind the code.
