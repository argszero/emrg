# EMRG — Agent.md

> **Principles and conventions only** — what to obey, and how to decide. History belongs in the scripts' docstrings and `--help`, in commit messages, and in the evolution memory records, not here.

## Project Overview

EMRG is a self-evolving AI agent architecture experiment: a **micro-kernel** — LLM-based task understanding and tool scheduling plus a thin file/exec shell — growing every other capability as a patch at runtime, in Python. `README.md` is the canonical description.

## Architecture

**The server is the living core; a client is only the interface.** Task understanding, tool scheduling, capability growth and self-evolution live in the server; one server serves many clients and outlives any of them.

- `emrg/` — core: `__main__.py` (CLI), `protocol.py`, `config.py` (`~/.emrg/config.toml`), `connect.py` (WebSocket over loopback, token auth), `memory.py`, `session.py`
- `emrg/server/` — the daemon: `daemon.py` (messages, the background thread running evolution, the tool loop), `llm.py`, `tool_types.py`, task templates
- `emrg/tools/` — bash, read, write, edit, glob, grep, base + registry
- `emrg/skills/` — skills from `.emrg/skills/` (progressive disclosure) + the installable-skill catalog
- `emrg/client/` — TUI: `daemon_manager.py` (daemon lifecycle + protocol client, shared with the GUI), `app.py`
- `emrg/gui/` — Electron GUI (non-developer entry point): the main process owns the daemon connection, the renderer is sandboxed
- `scripts/` — the guards · `tests/` — pytest suite · `packaging/` — installers

## Key Conventions

- A change is not done until it is pushed and its guard runs in CI — local green is necessary, never sufficient
- Server logs are discarded (`stderr=DEVNULL`); client logs go to `./.emrg/emrg-client.log`
- The server outlives any client, and a client auto-detects and starts it
- `README.md` and `Agent.md` are English; `README.cn.md` is the Chinese README
- Commits: `emrg: <description>`, English, one intent per commit
- A derived number is never written where a guard can measure it; a rule that can be mechanised is mechanised, because prose decays
- Prefer inspectable state: everything the agent knows is a file

## Terminology

The execution model, in this repo's own terms (`emrg/server/daemon.py`):

- **Tool loop** — the whole process triggered by one user message: LLM requests and tool calls repeat until a round produces no new tool calls (`_run_tool_loop`).
- **Round** — one iteration inside a tool loop: one LLM request + zero or more tool calls + their execution (`round_num`, bounded by `max_tool_rounds`).
- **Evolution cycle** — one full self-evolution run (Prepare → Review → Discover → Improve → Submit → Record). Never call it a "round".

`user message → tool loop (round 1 … round N; ends at the first round with no tool call)`.

## Capabilities

What EMRG does today — capability statements, not a changelog.

- **TUI client** — markdown/diffs, slash commands, autocomplete, pickers, clipboard images (`vision` per model), ESC interrupt, CJK-aware input
- **Electron GUI** — streaming chat, tool-status cards, sessions, models, settings, onboarding, reconnect with daemon respawn; React + TypeScript under vitest
- **Scheduled tasks** — `TaskHandler` for any registered project: builtin and custom types, CRUD, hot reload
- **Rant-driven evolution** — host feedback through `/rant` drives the self-improvement cycles
- **Memory** — project and session memory (YAML frontmatter + a Markdown index), compact/clear, a capped prompt footprint
- **Skills** — `.emrg/skills/` with progressive disclosure + an installable-skill catalog
- **Operational** — config hot-reload, project tracking, headless GitHub auth
- **Inspectable by design** — state, memory and logs are files; git is the state store

## Test Commands

One line per command: what it answers. The *why* behind each guard is in its docstring and `--help`, not here. Run the daemon in the foreground with `python -m emrg`.

- Python: `uv run pytest tests/ -v` — the number is **measured, never stored** (a stored count is a conflict magnet that goes stale silently): `uv run --no-sync python3 scripts/check-doc-count.py --measure`. Import check: `uv run python -c "from emrg.client.app import run_client"`
GUI: `cd emrg/gui && npm test` (139: 83 daemon_client + 20 conn-manager + 7 integration + 7 nav-policy + 7 gui-state + 6 build-config + 4 boot-contract + 3 preload-api + 2 theme-guard) — syntax: `node --check main.js preload.js daemon_client.js`
Renderer: `cd emrg/gui/renderer && npm run typecheck && npm test` (537: 5 snapshot-store + 9 utils + 3 ErrorBoundary + 2 App smoke + 11 commands + 4 copywriting + 11 i18n + 13 markdown + 18 transcript + 11 TranscriptView + 18 history + 15 historyReplay + 31 composer + 42 Composer + 6 LinkDialog + 16 sidebar + 17 Sidebar + 9 fileTree + 9 FileTree + 16 resultPanel + 8 ResultPanel + 27 workspaceView + 10 WorkspaceView + 10 dialog + 6 Dialog + 9 ConfirmDialog + 9 RenameDialog + 10 dialogLists + 3 HelpDialog + 9 MemoryDialog + 6 SkillsDialog + 8 openSession + 6 WelcomeDialog + 9 OpenSessionDialog + 7 NewSessionDialog + 7 rewind + 8 RewindDialog + 7 GithubDeviceDialog + 20 daemonBridge + 7 DaemonBridgeProvider + 34 Shell + 15 DialogHost + 20 SettingsPanel + 6 TaskFormDialog + 5 RantDialog + 4 vendorMarkdown)
- CI: pytest on ubuntu **and windows-2025**, the GUI suites, and an actionlint gate over `.github/workflows/`; re-trigger a dropped push with `scripts/re-trigger-ci.sh <branch>`, never an empty commit
- Merge gates, run before merging — each answers a question no other gate asks: `uv run --no-sync python3 scripts/check-vote-count.py` (approvals still valid) · `scripts/check-pr-base.py` (base reaches master) · `scripts/check-merge-freshness.py` (that green CI is about the landing tree) · `scripts/check-merge-order.py` (what merging dirties) · `scripts/check-merge-landing-diff.py` (what the landing changes) · `scripts/check-merge-pairs.py` / `scripts/check-merge-sequence.py` (two PRs together; every step of an order) · `scripts/check-merge-tree-health.py` (the merged tree passes the guards) · `scripts/check-merge-plan-suite.py` (the plan's final tree passes the suite) — all under `uv run --no-sync python3`
- Other tools: `uv run --no-sync python3 scripts/check-node-test-count.py --write` (syncs the Node totals above), `scripts/check-patch-files.py` (a rebuilt patch carries every file the previous one did), `scripts/cast-vote.py` (a vote the counter counts), `bump-version.py`, `classify-conflict.py`, `sync-master-from-api.py`, `push-branch-from-api.py`

Every guard reads the tree you are **standing in** and says so in its first line; a question it cannot answer is reported unmeasurable, never as a pass.

## Releasing

1. **Bump** — `python3 scripts/bump-version.py <x.y.z>` rewrites every version declaration at once; `tests/test_version_sync.py` proves they agree. PR it, take three approvals from different cycles, squash merge — never self-merge a release PR.
2. **Tag** — `git tag -a v<x.y.z> -m "emrg v<x.y.z>" && git push origin v<x.y.z>`. The tag is the **only** trigger of `build-release.yml`; the Test workflow never runs signing or notarization.
3. **Verify and confirm** — the tagged Build Release run is green across the platform matrix, and the GitHub Release is published as Latest (not draft, not prerelease) with the full asset set.

`bump-version.py --check` is the host-side counterpart of that CI guard: run it before pushing, not after a wasted build.

## Packaging

Only `icon.svg` is committed; the generated icon products are gitignored and CI builds them. A local build runs `bash packaging/gen-assets.sh` first — idempotent, and `.icns` needs macOS `iconutil`.

## Configuration

`~/.emrg/config.toml` — `[llm]` sets the default model (`base_url`, `api_key`, `model`, `context_window`, `vision`); each `[[llm.models]]` entry adds a model for `/model`. The GUI writes it on save; the full template is in `DEVELOPMENT.md`.
