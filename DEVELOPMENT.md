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
# This is the DEFAULT: a [[llm.models]] entry's own `vision` key wins over it, and an
# entry without one (or no entry at all) falls back to this value — it never inherits
# the previous model's answer.
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

**Live reload — no restart needed.** The daemon watches `~/.emrg/config.toml`: every couple of seconds it **reads the file and hashes it**, parses it only when the hash moved, and applies the edit **in place** to the running process. Every key in `[llm]` moves for subsequent requests:

- The tick reads the bytes rather than comparing `(mtime, size)`. That pair was the first design and the `windows-2025` leg falsified it: two writes milliseconds apart share a timestamp there, so an edit that also keeps the file's size leaves the comparison identical and the edit is **silently never applied** — the very complaint this feature removes. The cost is one small read plus a sha256 per tick, paid on every platform (coarse timestamps are a property of the mount, not of the OS) — worth knowing if `~/.emrg` lives on a network filesystem.

- `model` travels the same path `/model` uses — the usage anchors are invalidated and `context_window` re-resolved from the matching `[[llm.models]]` entry — and every connected client is told (`model_set`).
- **`vision` is resolved, and the resolved value is what is reported.** It comes from one place — the matching entry's own key, else the top-level `[llm] vision` — and both the `model_set` frame and the `ping`/`pong` status frame carry the value the daemon will act on, so a client shows the effective capability rather than this file's declaration (the TUI prints it beside the model name in the status bar: `[gpt-4o img]` / `[deepseek-chat no-img]`). The declaration and the effective value are different things the moment a switch happens, which is why the file alone cannot answer "can I paste an image?".
- A `[[llm.models]]` entry is matched by its `name` **or** its `model`, so `/model <name>` and `/model <api-id>` name the same entry — one matcher (`config.find_model_entry`) answers every lookup a switch makes, so the entry's `context_window`, its API id and its `vision` can never come from three different rows. Before that was one function, the `context_window` lookup matched `name` only: switching by the API id matched nothing and silently kept the previous model's window.
- **A revision that moves a value the client *displays* is broadcast** (`config_applied`, issue #1374). A connected client never re-asks — the only frames carrying the effective `vision` are the `pong` and `model_set`, and a reload is neither (the TUI's `ping`s are event-driven: startup, reconnect, rewind) — so before this, `vision = true` edited here left every client showing the previous answer until it happened to reconnect. Both directions move, and the `false` one is the dangerous half: a client that keeps showing `img` while the daemon refuses images is exactly the failure the segment exists to prevent. The frame carries the fields its reader uses (`model`, `vision`) plus the keys that moved, and no others: `context_window` was in the set while nothing displayed it (issue #1384) — the frame's reader reads `type`/`model`/`vision`/`applied` and the segment has no window field — so a window-only edit is now evidenced by the reload log line alone, the collector every daemon-local reloadable field has. A client surface that starts showing another field adds it here and to `BROADCAST_ON_RELOAD`; the tests beside the frame pin that membership in both directions. It deliberately carries **no** `vision_source`, because that key names how a `/model` **switch** resolved the value and a reload resolves nothing — the daemon already holds the resolved value. It is its own frame type rather than a reused `model_set` so that a broadcast the client did not ask for can never answer a `/model` request a GUI is waiting on.
- `base_url`, `api_key`, `max_tokens`, `temperature`, `max_tool_rounds`, `context_window`, `auto_compact_threshold`, `models`, `vision`, `stream_options`, `context_refresh_interval_ms` are assigned for the next request; a stream already in flight is never rewritten.
- A half-written or wrongly-typed file is **rejected whole**: the previous good configuration stays in force, one warning is logged, and the file is re-read on your next save.

**A `config.toml` edit never restarts the daemon.** The client used to compare this file's mtime against the running server's start time and SIGTERM→SIGKILL it when the file looked newer — which killed the running scheduler handlers (a live evolution cycle among them) and dropped every connected client, to apply an edit the daemon now applies itself. That branch is gone; a **source** change is the only thing that still restarts the daemon. Both sections are live: `[llm]` as described above, and `[update]` (`enabled`, `delay_minutes`) — the daemon's upgrade manager holds the same object the reloader writes to, so a change lands on its next 5-minute check. The upgrade *interval* stays hard-coded and is not a field of that section.

Verify from the daemon log (`~/.emrg/emrgd.log`): every accepted edit logs one line naming the keys that moved —

```
config.toml reloaded: changed=max_tokens,temperature
config.toml reloaded: [update] changed=enabled
config.toml reloaded: model→gpt-4o (via the /model path)
config.toml change rejected (previous config kept): TOMLDecodeError: ...
config.toml change rejected (previous config kept): [update] delay_minutes is str, expected int
```

**Update checking** (`[update]` section): `enabled = true|false` (default true) enables the periodic GitHub release check, and `delay_minutes` (default 1440) is how long after a release is published it becomes eligible (set `1` for immediate). Both keys are **hot-reloaded** like `[llm]`: edit the file and the reloader assigns the new values within a couple of seconds, so the next upgrade check reads them — no daemon restart. That check runs every 5 minutes, and the interval is **not** configurable. The program only *triggers*: it never downloads an installer package, it starts an agent session (`emrg-upgrade`) that installs the equivalent of the release from the local evolution repo.

**Which `bash` tool runs** (`[sandbox]` section): `bash_tool_v2 = true|false` (default **false**) chooses between the two executors that both answer to the tool name `bash`. `false` is the long-standing tool, whose confinement is a **static scan of the command text**; `true` is the new one, whose confinement is the **OS process boundary** (macOS Seatbelt, Linux bubblewrap) — a write outside the session's working directory is refused by the kernel whatever language or subprocess attempts it, instead of by a parser that cannot read a `python3 -c`. The environment variable `EMRG_BASH_TOOL_V2=1|0|true|false` overrides the file, so it can be tried for one session without editing anything.

Unlike `[llm]` and `[update]`, this key is **read once at startup** and is *not* hot-reloaded: it decides which executor is built into the tool registry, and the registry is constructed once. Change it and restart the daemon. Both tools receive the session's working directory as their authorization root, injected by the daemon rather than chosen by the model (`workdir`/`workspace` in a tool call cannot widen it).

```toml
[sandbox]
bash_tool_v2 = false        # true = OS-level boundary (Seatbelt / bubblewrap); false = the static scan
```

The new path's boundary exists on **macOS (Seatbelt)** and **Linux (bubblewrap)**; that is also its current limit. On both, confinement happens at the process boundary — what is enforced is the workspace root plus a private `/tmp` (Linux) or the host temp areas (macOS), and the enforcement is reported as `full`. **Linux needs `bwrap` installed and a kernel that allows user namespaces** (a container needs `--security-opt seccomp=unconfined --security-opt apparmor=unconfined --cap-add SYS_ADMIN`); without it the command is **refused**, never run unconfined. **Windows still fails closed** — its ACL restricted-token backend is not built yet — so leave the switch at `false` there, and on Linux until `bwrap` is installed.

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

### A test file the guards cannot see yet — green locally, red in CI

Several guards here decide what to read by asking git for the **tracked** files
(`git ls-files`): the encoding rule (`test_script_decode_is_locale_independent.py`), the
CRLF rule, the conflict-marker rule, the doc-count rule, `scripts/check-doc-count.py`, and
others of the same shape. That is deliberate — an index cannot go stale — and it has one
consequence: **a new file that is not in the index is read by none of those rules.**

Measured 2026-09-18 (cycle `cyc20260918-105223`, PR #1368): a new test file was written,
the full suite ran green in its worktree, the branch was pushed — and CI failed four
minutes later on the encoding rule, naming a `subprocess.run(..., text=True)` call *in
that file*. The local run had answered about a tree the file was not part of.

The remedy is one command: **`git add` a new file before trusting a local suite run.**
Staging is enough — `git ls-files` lists staged files. `tests/test_the_index_derived_scans_reach_new_files.py`
now fails while a first-party `.py` file under `emrg/`, `packaging/`, `scripts/` or
`tests/` is untracked, so the local run cannot report a verdict over source those rules
cannot read; a file staged and then edited (the `AM` state) is a different question and no
guard answers it.

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

- **nothing exists only there** — every change is already published at that path in a
  ref git holds (`HEAD`, the upstream tip, or any other ref tip: a branch you already
  pushed, a tag, a `refs/cdrain/prNNNN` tip) — so the daemon converges the tree itself
  and the cycle keeps its configured tier;
- **something exists only there** — an untracked file, a staged addition, a conflict,
  a modification outrunning every ref tip, or a commit no other ref reaches — so the
  cycle is forced down to `read-only` whatever the configuration says (community issue
  #979), and the paths that caused it are named in the log.

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
  leaving the worktree clean and every byte in the stash. It never moves `HEAD`, so
  it cannot orphan a commit, and the stash is what makes the action undoable —
  which is why an agent is allowed to take it. Undo it with the spelling the
  receipt names, `git stash apply --index stash@{N}` with the `N` that `git stash
  list` prints for the named message: `--index` is what restores the staged side, and
  a *bare* `git stash pop` is **not** the inverse of this action. It takes the newest
  stash (this one only until the next is made), it brings a staged change back
  unstaged, and it consumes the stash — so the exact spelling is gone with it. The
  selector is the list's ordinal because no `@{…}` form names a stash **by message**
  (`gitrevisions(7)` allows ordinals, dates, upstream and push): `stash^{/<message>}`
  searches commit ancestry and stops resolving as soon as a later stash exists
  (`apply` rc=1), while `stash@{/<message>}` resolves to the newest entry whatever
  message it is given and so applies the **wrong** stash successfully — measured, both
  (issue #1284, re-measured by
  `tests/test_recover_worktree.py::test_the_advertised_selector_survives_a_later_stash`
  after `::test_the_advertised_reversal_is_the_measured_one`).
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
cycle downgrades again) — use `git stash -u`, recoverable with `git stash apply
--index`, not `git clean -fd`, which destroys the content; and an untracked file whose path
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

### `emrgd failed to start within N s`

Whichever entry point you started spawns the daemon and waits for it to accept
connections. When that wait runs out, the error carries what is known about the
attempt: whether the child is still running, its exit code if it is not, the
daemon log lines **this** attempt appended, and the contents of
`~/.emrg/emrgd-start.err` (the child's own stderr, which is where a failure
before logging starts can be read at all).

**Both entry points read the same variable**, `EMRG_START_TIMEOUT`, in seconds:
the terminal client (`emrg`, `emrg/client/daemon_manager.py`) and the GUI
(`emrg/gui/daemon_client.js`). Their defaults differ because the GUI's was
already its own — **4.5 s** (15 polls, 0.3 s apart) for the client, **5.0 s** for
the GUI — and neither changes; what the variable adds is the ability to raise
either one. Set it on a client (for the terminal) or in the environment the GUI
is launched from:

```bash
EMRG_START_TIMEOUT=30 emrg
```

A value is **a plain decimal number of seconds** — digits, an optional sign, an
optional fraction and exponent (`30`, `4.5`, `.5`, `1e2`). Anything else is
ignored with a warning in that entry point's own log and the default is used: a
typo in a tuning variable must never be the reason a start fails. The refusal is
deliberate about the shapes that merely *parse*: `0x10`, `1_000` and full-width
or Arabic-Indic digits are all refused, so the same value cannot mean 16 seconds
to one entry point and 4.5 to the other (`1_000` would be a sixteen-minute wait
if it were accepted). Both entry points read one shared list of allowed values in
their tests — `tests/data/start_window_shapes.json` — so a divergence in either
resolver is a test failure rather than a surprise.

Two facts make the window cheap to raise: a child that has **exited** is reported
on the first poll with its exit code, so the window only ever bounds a child that
is alive but not yet listening; and the reported bound is one the loop really
applied, with a floor of one poll on both sides. The two report it differently,
and both are honest: the client quantises the window to whole polls and names
that product (`1.0` → `0.9s`), while the GUI's loop is deadline-based, names the
deadline it enforced and may therefore overshoot it by up to one poll (`1.0` →
`1.0s` reported, ~1.2 s actually waited).

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

#### A relative target is resolved, not assumed

`echo x > out.txt` is allowed while the resolved target stays under the
directory the command runs in — the workspace root, or the daemon's own cwd
when the caller injected none. A target that climbs out with `..` is refused,
including the spelling that arrives through a variable the command itself
assigns (issue #1353):

```
⛔ workspace-write sandbox: blocked write to relative target '../escaped.txt':
it resolves to '/…/escaped.txt', outside '/…/ws', the directory the command runs
in (issue #1353)
```

A climb that returns to the workspace (`echo x > ../ws/out.txt`) is still
allowed: the test is on the **resolved** target, not on the spelling, so it
cannot turn `sub/../out.txt` into a refusal.

A `cd` that stays **inside** the workspace moves the write site with it — the
target is joined onto the directory the shell is in when it runs, not the one it
started in — so `cd sub && echo x > ../back.txt` writes `<ws>/back.txt` and is
allowed, while `cd sub && echo x > ../../worse.txt` climbs out of `<ws>/sub` and
is refused (issue #1370). Proving which directory that is costs the shapes that
cannot prove it, and those keep the refusal they had: a `;` or `||` chain (the
`cd` may have failed, and then the shell never moved), a `( … )` group or a
pipeline (the `cd` may be in a shell of its own), a target written *before* the
move, and — since issue #1357 — a destination only the shell could finish:

```
⛔ workspace-write sandbox: blocked write to relative target 'f': the command runs
it after changing directory to '$D', which is not a directory this workspace can
place it in (issue #1244)
```

A destination that still needs expanding is refused rather than joined onto the
workspace, because the join would read it as **inside**: `D=../outside && cd "$D"
&& cat > f`, a loop variable over a directory, and `cd "$(mktemp -d)"` were all
allowed while the shell writes outside the workspace. The class is "anything left
to expand after the environment and the command's own assignments have been
applied" — an unknown `$NAME`, a `${NAME:?}`, a `$(…)` or its backquote spelling.
**The price is real and stated**: a legitimate computed move (`cd "$(git rev-parse
--show-toplevel)"`, a directory a `read` filled in) is refused the same way, and so
is `cd "$(mktemp -d)"` even though `mktemp -d` lands in the allowed temp root —
*where* it lands is exactly what the text does not say. If you hit this, spell the
write target absolutely, or `cd` to a path the daemon can read from the text.

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
