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
uv run pytest tests/ -v   # run tests (the count is measured live, not stored — see scripts/check-doc-count.py)
uv run python -m emrg     # launch TUI
# CI includes actionlint workflow gate (#444): workflow parse errors fail PR CI
```

**Quick sanity checks:**

```bash
uv run python -c "from emrg.client.app import run_client"   # import check
uv run python -m emrg --help
uv run --no-sync python3 scripts/check-rant-citations.py    # every citation names its public record
```

`scripts/check-rant-citations.py` answers one question about the instruction prose
(the built-in task templates, `prompts/*.j2`, `docs/gui-redesign.md`): does every
citation of a host rant name the public record that landed it? A rant timestamp
indexes `~/.emrg/rants.jsonl` **on the machine that wrote it**, so it is unresolvable
for every other reader; a PR number stays resolvable. It exits `0` when the rule
holds, `1` on a violation, and `2` when a file in the class cannot be read — `2` is
not a pass. The class includes `evolution_prompt.md`; the red line around that file
covers the **running** copy — the one resolved from the loaded module, i.e.
`<dir of scheduler.__file__>/evolution_prompt.md` — so the repository copy's citations
are swept like every other file's and the frozen-debt list is empty (the mechanisms that
hold a site out of the rule are exercised by synthetic tests rather than by a real
entry). `--measure` prints the whole inventory.

### Electron GUI

```bash
cd emrg/gui
npm ci               # install deps (production: --omit=dev)
npm start            # launch GUI (auto-starts daemon)
npm test             # run Node tests (the counts are measured live, not stored — see scripts/check-node-test-count.py; integration runs in CI, local: npm run test:integration)
```

### Packaging (installer builds)

Generated icon products (`icon.png`/`icon-512`/`icon-256`/`icon.icns`/`icon.ico`) are **not committed** — the repo keeps only the SVG design source (`packaging/assets/icon.svg`); CI generates them at build time (#688). When building installers locally (`packaging/make-installer.sh` / `build-runtime.sh`), run the generator first:

```bash
bash packaging/gen-assets.sh   # icon.svg → png/icns/ico (idempotent)
```

Renderer priority: `rsvg-convert` → Chrome/Chromium headless → `sips` (last resort, glow may be lost). Requires macOS `iconutil` for `.icns` (skipped with a notice on Linux/Windows).

CI runs tests and checks for conflict markers automatically via GitHub Actions (`.github/workflows/test.yml`).

> **Self-evolution from source**: the evolution workspace expects the repo at `~/.emrg/evolution/emrg`. Packaged installs self-heal (clone on demand + auto-bootstrap projects/tasks); source installs should clone there explicitly if you want the evolution daemon to work on this repo.

### A test that derives its expectation from what it *wrote* is a POSIX assumption

`test-windows` runs the same suite on a shell where text-mode writes translate every
line ending, so an expected value taken from the Python string that was written is
wrong there while staying green locally. This has cost two CI reds in this repo, both
times on a brand-new test that passed on macOS:

- `assert log.stat().st_size == len("previous run\n")` — the file holds one more byte
  per line on Windows, so the assertion is about the platform, not about the code.
  Read the expectation back from the **artifact** instead (`mark == log.stat().st_size`),
  or assert a property rather than a literal: the mark must be a point in the file, so
  the size grows by exactly the appended line's length read through the same reader.
- A `not in` check against **multi-line source text** is satisfied by the line endings
  alone on a CRLF checkout, so the guard silently stops guarding while staying green —
  normalise (`text.replace("\r\n", "\n")`) before any substring assertion that spans
  lines. A silently passing guard is worse than a red one.

Neither needs a Windows machine to reproduce: write a file containing `"line\r\n"` and
drive the code path under test with it. Local green is a statement about the platform
you are standing on; when a test's subject is bytes, sizes or whole-file text, ask what
the other platform's write path does to it.

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

## 🐛 Troubleshooting

### Why is my evolution cycle running read-only?

Every cycle gets one bash tier: `read-only`, `workspace-write` (the default), or
`danger-full-access`. The tier is decided per cycle and is keyed on **whether
discarding the working tree would lose anything**, not on whether the tree is
dirty (issue #1237). When the task's source repository has uncommitted changes:

- **nothing exists only there** — every change is already recoverable from a commit
  git holds (`HEAD`, or the upstream tip) — so the daemon converges the tree
  itself and the cycle keeps its configured tier;
- **something exists only there** — an untracked file, a staged addition, a
  conflict, a modification outrunning both, or a commit only on this branch — so
  the cycle is forced down to `read-only` whatever the configuration says
  (community issue #979), and the paths that caused it are named in the log.

Why the distinction matters more than it looks: `read-only` refuses the very git
verbs that could clean the tree, so the state that triggered the downgrade also
disabled its only exit. Measured cost of one instance of that loop: **33
consecutive zero-commit cycles**, whose verified output had to be parked outside
the repository. And the dirt in it was not work at all — of what git reported, the
modified files were byte-identical to upstream's own blobs, i.e. the second bullet
above had been applied where the first was true.

#### Recovering a tree

**A cycle recovers itself.** At the start of every cycle the daemon asks the
question above, and when the answer is "nothing exists only here" it converges the
tree itself, before the cycle's first tool call — reversibly (stash, `HEAD`
unmoved, receipt in the git state dir). The exit from a downgrade is therefore the
daemon's own action, not a command a human has to be present to run: a guard whose
only exit is manual strands the tree, which is the shape of the measured 33-cycle
loss above. Nothing is asked of the host.

The same action is available from a shell, for a tree no cycle is about to touch:

```bash
uv run --no-sync python3 scripts/recover-worktree.py --repo <task-source-dir>
uv run --no-sync python3 scripts/recover-worktree.py --repo <task-source-dir> --apply
```

The script owns no policy — it calls the daemon's implementation of both the
criterion and the action, so a diagnosis you run by hand and the tier decision the
daemon makes cannot drift apart. Without `--apply` it only reports. It asks the
same question the guard asks, and then:

- **refuses, naming the paths, when the work exists nowhere else.** Nothing is
  written, and no stash is made. That is the guard working; commit, stash or copy
  that content out deliberately and re-run.
- **stashes it when it is reconstructible** (`git stash push --include-untracked`),
  leaving the worktree clean and every byte one `git stash pop` away. It never
  moves `HEAD`, so it cannot orphan a commit, and the stash makes the action
  undoable — which is why an agent is allowed to take it.
- **writes a receipt** into the git state dir (`emrg-recovery-receipt.json`), as
  every release of a safety rule requires. Deliberately not beside the tree: a
  receipt at `<repo>/.emrg/…` re-dirties the tree it just cleaned in any repository
  that does not ignore `.emrg/`, which would re-arm the guard on the next cycle.
  It is best-effort, so the report **says so** when one could not be written
  (the action's detail and its log line, and `scripts/recover-worktree.py`'s
  `receipt:` line) rather than naming a path for a file that does not exist
  (issue #1284). The path is **normalised** before it is joined with the file name:
  Windows git prints an absolute git dir with forward slashes (`C:/…/.git`), so a
  verbatim answer made the reported path mixed-separator and unequal to
  `str(Path(state) / name)` — green locally, red on the windows-2025 leg (#1292).

Read-only blocks the destructive shapes the guard recognises: redirects to
anything but `/dev/null`, `rm` / `rmdir`, `mv` / `cp` destinations, `truncate` /
`tee` / `shred`, `sed -i`, `find -delete`, `git ... --output=<f>`, and every git
subcommand that is not on the read allowlist (`add` / `commit` / `stash` /
`checkout` / `switch` included). The recognised shapes are blocked **wherever the
target lives**, inside the workspace or outside it — only `/dev/null` is exempt.
It is a static scan, not an OS boundary (`enforcement="partial"`), so a command
outside that shape list (`mkdir`, `touch`, an interpreter writing a file) still
runs and a downgraded cycle can leave scratch files behind. The decision is per
command string, not per statement: one blocked shape anywhere in the call refuses
the whole call. Reads are unaffected — including the read-only git verbs the
recovery tool's diagnosis uses — so the problem can still be diagnosed from inside
a downgraded cycle; only the repair needs a writable tier.

#### Diagnose before you repair

"Modified" does not say *which* of four things disagrees, and only some
disagreements are safe to discard. Compare the content ids:

```bash
cd ~/.emrg/evolution/emrg
git hash-object <path>            # working tree
git ls-files -s <path>            # index
git rev-parse HEAD:<path>         # HEAD
git fetch origin master
git rev-parse FETCH_HEAD:<path>   # master
```

Then the decisive question — is the branch simply behind?

```bash
git merge-base --is-ancestor HEAD FETCH_HEAD \
  && echo "HEAD is behind master: nothing unmerged is at risk"
```

That is the common case in a workspace whose change was merged upstream after
the local checkout: the working tree holds master's content while the index
still matches the older HEAD, and the "uncommitted work" is already published.
`scripts/recover-worktree.py --apply` clears the dirty state reversibly without
moving `HEAD`; the sequence below is for when you also want to move the branch
onto upstream, which is a decision the tool deliberately leaves to you. Recover
with:

```bash
git fetch origin master
git checkout -f -B master FETCH_HEAD
```

A plain `git checkout master` will not do it — fetching does not move your local
branch, so that checks out the commit you are already on, exits 0, and leaves
the tree dirty.

`-f` is the right tool for tracked modifications only. Measured on the other two
geometries: **untracked-only** dirt survives it (exit 0, still dirty, so the next
cycle downgrades again) — use `git stash -u`, recoverable with `git stash pop`,
not `git clean -fd`, which destroys the content; and an untracked file whose path
the target commit also adds gets its content **silently replaced**. Adding the
path to `.gitignore` does not help, because that edit is itself an uncommitted
change.

Try it without `-f` first. Git refuses exactly where `-f` would discard something,
and the refusal names the file — `Your local changes to the following files would
be overwritten by checkout: <path>` for a tracked modification, and `The following
untracked working tree files would be overwritten by checkout: <path>` for an
untracked path the target commit adds. Add `-f` only once that refusal is gone, or
once the content it names is somewhere else.

#### The override

An audited escape hatch exists: `EMRG_TASK_DIRTY_OVERRIDE` takes a
comma-separated list of task names, or `*`, and the downgrade is skipped with a
receipt in the daemon log.

The daemon inherits its environment from the process that starts it, so set the
variable on the command that starts the daemon:

```bash
EMRG_TASK_DIRTY_OVERRIDE=emrg-task emrg server restart
```

Make it an explicit decision — read `git status` and `git diff` first. The guard
exists because a cycle's bash tool can otherwise overwrite work you have not
committed, and the variable disables that protection for the named tasks.

#### When a fix seems not to have taken effect

Two gaps get mistaken for a broken fix:

- **merge ≠ running** — a guard fixed on `master` protects nobody until a
  release ships it.
- **install ≠ running** — after the installer updates `~/.emrg/install/source`,
  a daemon that is already running keeps executing the old code until it
  restarts.

Check `emrg -v` and when the daemon started before re-reading the source.

### Why is a write blocked at `workspace-write`?

`workspace-write` is the default tier, and it allows a write only when the
target resolves inside one of three roots:

- the injected **workspace root** (the project the task runs in),
- the **OS temp root** `tempfile.gettempdir()`,
- the **trusted data roots** — `~/.emrg/evolution/.emrg/`, the evolution
  module's own records and scratch, trusted because its records live outside
  the workspace it is given (issue #1093).

Every other absolute path is refused, and the daemon's own state files are
refused inside any root (`~/.emrg/config.toml` is *protected*; a write to
`~/.emrg` itself is refused outright, because it can erase the daemon's data).

Measured 2026-09-16 against `master`: inside the workspace **ALLOW**,
`$TMPDIR/…` **ALLOW**, `/tmp/…` **BLOCK**, `/private/tmp/…` **BLOCK**,
`/var/tmp/…` **BLOCK**, `/dev/shm/…` **BLOCK**.

#### `$TMPDIR`, not `/tmp`

The tier means `tempfile.gettempdir()` — `$TMPDIR`, which is
`/var/folders/<…>/T` on macOS and `/tmp` on Linux. So `/tmp` is the temp root on
Linux but **not** on macOS, and the refusal names the target without naming the
rule:

```
⛔ [sandbox:workspace-write enforcement=partial] workspace-write sandbox:
blocked write outside workspace '/tmp/emrg-e2e-probe.txt' — command not executed
```

Use `$TMPDIR` (`mktemp -d` is the portable way) rather than a hardcoded `/tmp`
in anything a sandboxed task runs. Nothing promises `/tmp` is writable, so the
block is over-cautious rather than wrong — a boundary is not widened without the
host's decision.

Two more properties are worth knowing before debugging a refusal, both measured
the same day:

- **per call, not per statement** — one blocked target refuses the whole call.
  `echo a > inside.txt; echo b > /etc/x` creates neither file: the guard decides
  before the shell starts, so the legal half never runs either.
- **the same `/tmp` target is refused by both tiers, for opposite reasons** —
  `read-only` refuses the *shape* wherever the target lives (a redirect to
  anything but `/dev/null`), `workspace-write` refuses the *target* wherever it
  is written from. A refusal reason is how you tell which one you hit.

---

## 📜 License

MIT — see [LICENSE](LICENSE) for the full terms and [MANIFESTO.md](MANIFESTO.md) for the philosophy behind the code.
