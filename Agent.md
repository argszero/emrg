# EMRG — Agent.md

> **Principles and conventions only** — what to obey and how to decide. History lives in docstrings, `--help`, commits and records.

## Project Overview

EMRG is a self-evolving AI agent architecture experiment: a **micro-kernel** — LLM-based task understanding and tool scheduling plus a thin file/exec shell — growing capabilities as runtime patches, in Python. `README.md` is canonical.

## Architecture

**The server is the living core; a client is only the interface.** Task understanding, tool scheduling, capability growth and self-evolution live there; one server serves many clients and outlives any of them.

- `emrg/` — core: `__main__.py` (CLI), `protocol.py`, `config.py`, `connect.py` (WebSocket over loopback, token auth)
- `emrg/server/` — the daemon: `daemon.py` (messages, the background thread running evolution, the tool loop), `llm.py`, `tool_types.py`, task templates
- `emrg/tools/` — bash, read, write, edit, glob, grep, base + registry
- `emrg/skills/` — skills from `.emrg/skills/` (progressive disclosure) + the installable-skill catalog
- `emrg/client/` — TUI: `daemon_manager.py` (daemon lifecycle + protocol client, shared with the GUI), `app.py`
- `emrg/gui/` — Electron GUI (non-developer entry point): the main process owns the daemon connection, the renderer is sandboxed
- `scripts/` — the guards · `tests/` — pytest suite · `packaging/` — installers

## Key Conventions

- A change is not done until it is pushed and its guard runs in CI — local green is necessary, never sufficient
- Server logs are discarded (`stderr=DEVNULL`); client logs go to `./.emrg/emrg-client.log`
- A client auto-detects the server and starts it if absent
- `README.md` and `Agent.md` are English; `README.cn.md` is the Chinese README
- Commits: `emrg: <description>`, English, one intent per commit
- A derived number is never written where a guard can measure it; a mechanisable rule is mechanised, because prose decays
- Prefer inspectable state: everything the agent knows is a file

## Terminology

The execution model, in this repo's own terms (`emrg/server/daemon.py`):

- **Tool loop** — the whole process triggered by one user message: LLM requests and tool calls repeat until a round brings no new tool calls (`_run_tool_loop`).
- **Round** — one iteration inside a tool loop: one LLM request + zero or more tool calls + their execution (`round_num`, bounded by `max_tool_rounds`).
- **Evolution cycle** — one full self-evolution run (Preparation → Look → Think → Act → Record). Never call it a "round".

## Capabilities

What EMRG does today — capabilities, not a changelog.

- **TUI client** — markdown/diffs, slash commands, autocomplete, pickers, clipboard images (`vision` per model), ESC interrupt, CJK-aware input
- **Electron GUI** — streaming chat, tool-status cards, sessions, models, settings, onboarding, reconnect, daemon respawn
- **Scheduled tasks** — `TaskHandler` for any registered project: builtin and custom types, CRUD, hot reload, `when` early wake
- **Rant-driven evolution** — host feedback through `/rant` drives the cycles
- **Memory** — project and session memory (YAML frontmatter + a Markdown index), compact/clear, capped prompt footprint
- **Skills** — `.emrg/skills/` with progressive disclosure + an installable-skill catalog
- **Operational** — config hot-reload, project tracking, headless GitHub auth
- **Inspectable by design** — state, memory and logs are files; git is the store

## Test Commands

Foreground daemon: `python -m emrg`.

- Python: `uv run pytest tests/ -v` — the number is **measured, never stored**: `uv run --no-sync python3 scripts/check-doc-count.py --measure`. Import check: `uv run python -c "from emrg.client.app import run_client"`
GUI: `cd emrg/gui && npm test` (228: 107 daemon_client + 22 conn-manager + 7 integration + 7 nav-policy + 11 gui-state + 9 build-config + 13 boot-contract + 3 preload-api + 2 theme-guard + 2 image-hash + 8 switch-session + 19 atp_login + 4 gui-op-log + 9 theme-setting + 5 window-title) — syntax: `node --check main.js preload.js daemon_client.js`
Renderer: `cd emrg/gui/renderer && npm run typecheck && npm test` (1142: 5 snapshot-store + 13 utils + 3 ErrorBoundary + 2 App + 12 commands + 4 copywriting + 13 i18n + 31 markdown + 22 selection + 34 transcript + 11 conversationType + 4 MarkdownText + 56 TranscriptView + 22 history + 29 historyReplay + 40 composer + 90 Composer + 14 sidebar + 32 workspaceView + 15 WorkspaceView + 10 dialog + 6 Dialog + 9 ConfirmDialog + 9 RenameDialog + 13 dialogLists + 3 HelpDialog + 9 MemoryDialog + 8 SkillsDialog + 8 openSession + 6 WelcomeDialog + 20 OpenSessionDialog + 14 NewSessionDialog + 7 rewind + 8 RewindDialog + 7 GithubDeviceDialog + 76 daemonBridge + 13 DaemonBridgeProvider + 67 Shell + 18 page + 31 EditorPanel + 25 DialogHost + 38 SettingsPanel + 18 TaskFormDialog + 5 RantDialog + 8 vendorMarkdown + 10 highlightTheme + 6 ApprovalDialog + 20 SandboxRootsDialog + 42 cockpit + 9 fileTree + 22 FileTree + 5 contextMenu + 1 residue + 24 StatusBand + 24 UsageRanking + 9 usageRanking + 6 SearchOverlay + 5 ResizeHandle + 8 IconRail + 6 SessionColumn + 5 EmrgMark + 28 editorDoc + 24 CodeEditor)
- CI: pytest on ubuntu **and windows-2025**, the GUI suites, and an actionlint gate over `.github/workflows/`; re-trigger a dropped push with `scripts/re-trigger-ci.sh <branch>`, never an empty commit
- Merge gates, run before merging — each asks what no other gate asks: `uv run --no-sync python3 scripts/check-vote-count.py` (approvals still valid) · `scripts/check-pr-base.py` (base reaches master) · `scripts/check-merge-freshness.py` (that green CI is about the landing tree) · `scripts/check-merge-order.py` (dirt and what it lands) · `scripts/check-merge-landing-diff.py` (what the landing changes) · `scripts/check-merge-pairs.py` / `scripts/check-merge-sequence.py` (a pair, or a whole order) · `scripts/check-merge-tree-health.py` (the merged tree passes one guard) · `scripts/check-merge-plan-suite.py` (the plan's final tree passes the suite) — all under `uv run --no-sync python3`
- Post-merge audit: `uv run --no-sync python3 scripts/check-merge-landed.py <N>` (did it land the reviewed tree)
- Other tools: `uv run --no-sync python3 scripts/check-node-test-count.py --write` (the Node totals), `scripts/check-memory-index.py` (a MEMORY.md vs the index rule), `scripts/check-patch-files.py` (a rebuilt patch keeps its files), `scripts/check-staged-inverse.py` (an index holding an earlier commit's tree), `scripts/check-memory-rotation.py` (whose memory moved), `scripts/cast-vote.py` (a counted vote), `scripts/feishu-gate.py` (the third-party send gate), `bump-version.py`, `classify-conflict.py`, `sync-master-from-api.py`, `push-branch-from-api.py`

Every guard names the tree it reads; what it cannot answer is unmeasurable, never a pass.

## Releasing

1. **Bump** — `python3 scripts/bump-version.py <x.y.z>` rewrites every version declaration; `tests/test_version_sync.py` proves they agree. PR it, take three approvals from different cycles, squash merge — never self-merge a release PR.
2. **Tag** — `git tag -a v<x.y.z> -m "emrg v<x.y.z>" && git push origin v<x.y.z>`. The tag is the **only** trigger of `build-release.yml`; Test never signs or notarizes.
3. **Verify and confirm** — `scripts/check-release-published.py v<x.y.z>`: green across the platform matrix, published as Latest (not draft, not prerelease) with the full asset set.

`bump-version.py --check` and `scripts/check-release-tag.py v<x.y.z>` are the host-side counterparts: run both before pushing.

## Packaging

Only `icon.svg` is committed; generated icon products are gitignored, built by CI (`bash packaging/gen-assets.sh` first; `.icns` needs macOS `iconutil`).

## Configuration

`~/.emrg/config.toml` — `[llm]` sets the default model (`base_url`, `api_key`, `model`, `context_window`, `vision`); each `[[llm.models]]` entry adds a model for `/model`. The GUI writes it on save; the template is `DEVELOPMENT.md`.
