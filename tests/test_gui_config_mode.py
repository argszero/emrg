"""The GUI's config writer applies the config file's permission rule on every write.

`~/.emrg/config.toml` holds `api_key` in plaintext, so it is private — mode `0o600` on every path
that writes it. Two of the three writers state that as code (`emrg/config.py::CONFIG_FILE_MODE`,
`install.sh`'s `chmod 600`); the GUI is the third, and it could not: `fs.writeFileSync`'s `mode`
option applies **only when the file is created**, so a file an earlier install left at `0644`
keeps `0644` through every save. Measured 2026-10-12 (issue #2148) and it is the one path the
host's packaged install has — the daemon calls `load_config()`, never `ensure_config()`.

Why the rule lives in `config_mode.js` and is driven from here
--------------------------------------------------------------
`main.js` is an Electron main script: requiring it starts the app, so no test can call
`writeConfig()` even with electron installed. The main-process modules that *are* testable
(`gui-state.js`, `conn-manager.js`, `daemon_client.js`) each hold one subject and have a
behaviour test, so the rule goes in one of those — `config_mode.js` — and `main.js` calls it.
The behavioural arms run the real module through `node` in a subprocess, on files under
`tmp_path`: the mode is the subject, and nothing here reads or writes the host's own config.

The guard asks two questions, because one of them cannot see the other's answer
----------------------------------------------------------------------------------
Running the module proves the rule; it cannot prove that the writer *calls* it — a missing wire
is exactly the defect this file exists for, and `main.js` has no runtime coverage. So the last
two tests read `main.js` statically: `writeConfig`'s body must carry the call, and the negative
control applies the same extraction to `readConfig`, which must not (a reader that wrote would
be a surprise of its own).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUI_DIR = REPO_ROOT / "emrg" / "gui"
MODULE = GUI_DIR / "config_mode.js"
MAIN_JS = GUI_DIR / "main.js"
PACKAGE_JSON = GUI_DIR / "package.json"

#: Windows has no POSIX permission bits for `chmod` to set: `stat` reports the read-only flag
#: there (`0o666` for a writable file), so every arm below would read the same value and prove
#: nothing about the rule. The Python writer's arms carry the same gate for the same reason.
posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="Windows carries no POSIX permission bits to assert",
)

_HARNESS = """
const fs = require("node:fs");
const { makeConfigPrivate, CONFIG_FILE_MODE } = require(%(module)s);

const target = %(target)s;
const existsBefore = fs.existsSync(target);

let calls = 0;
const realChmod = fs.chmodSync;
fs.chmodSync = function (...args) { calls += 1; return realChmod.apply(fs, args); };

let threw = null;
try {
  makeConfigPrivate(target);
} catch (e) {
  threw = String(e && e.message ? e.message : e);
}
fs.chmodSync = realChmod;

const modeOf = (p) => (fs.existsSync(p) ? fs.statSync(p).mode & 0o777 : null);
console.log(JSON.stringify({
  exported: typeof makeConfigPrivate,
  constant: CONFIG_FILE_MODE,
  existsBefore,
  threw,
  calls,
  before: %(before)s,
  after: modeOf(target),
  content: existsBefore ? fs.readFileSync(target, "utf8") : null,
}));
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "no `node` on PATH: this guard drives the real GUI module to read the mode it leaves "
            "behind, and cannot do that without the runtime the module ships in"
        )
    return node


def _drive(tmp_path: Path, mode: int | None) -> dict:
    """Run the real module against a file created at `mode` (None = the file is not created)."""
    target = tmp_path / "config.toml"
    if mode is not None:
        target.write_text('api_key = "a-real-key"\n', encoding="utf-8")
        os.chmod(target, mode)
    before = None if mode is None else os.stat(target).st_mode & 0o777

    harness = tmp_path / "harness.js"
    harness.write_text(
        _HARNESS
        % {
            "module": json.dumps(str(MODULE)),
            "target": json.dumps(str(target)),
            "before": "null" if before is None else oct(before),
        },
        encoding="utf-8",
    )
    result = subprocess.run(
        [_node(), str(harness)],
        cwd=GUI_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0 and result.stdout.strip(), (
        "the harness produced no reading, so nothing below would be measured:\n"
        f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
    )
    reading = json.loads(result.stdout.strip().splitlines()[-1])
    # The non-empty baseline: a module that stopped exporting the rule would make every arm
    # below a comparison against `undefined`.
    assert reading["exported"] == "function", (
        f"{MODULE.name} does not export makeConfigPrivate as a function: {reading['exported']}"
    )
    assert reading["constant"] == 0o600, (
        f"{MODULE.name} declares CONFIG_FILE_MODE = {oct(reading['constant'])}, which is not the "
        "rule the other two writers state (0o600)"
    )
    return reading


@posix_only
def test_a_world_readable_config_file_is_tightened(tmp_path):
    """The half the GUI could not do before: an existing `0644` file, on the write path."""
    reading = _drive(tmp_path, 0o644)

    assert reading["existsBefore"] is True, "the fixture did not create the subject"
    assert reading["after"] == 0o600, (
        f"the file was left at {oct(reading['after'])} — the config file holds `api_key` in "
        "plaintext, and `writeConfig` is the save path the host's packaged GUI uses"
    )
    assert reading["calls"] == 1, f"expected one chmod, saw {reading['calls']}"
    assert reading["content"] == 'api_key = "a-real-key"\n', (
        "the mode was the subject; the host's file content is not this function's to touch"
    )


@posix_only
def test_a_file_the_owner_closed_further_is_left_alone(tmp_path):
    """The direction that must not move: a mode narrower than the rule is already closed.

    `0400` carries none of `0o077`, so the rule's predicate is false and nothing is written —
    the function closes a file to other users, it never opens one its owner shut.
    """
    reading = _drive(tmp_path, 0o400)

    assert reading["after"] == 0o400, f"the rule reopened a 0400 file as {oct(reading['after'])}"
    assert reading["calls"] == 0, "a chmod was issued for a file that was already closed"


@posix_only
def test_no_chmod_when_the_mode_is_already_the_rule(tmp_path):
    """`writeConfig` runs on every settings save, so the rule must be a no-op at the rule.

    Observing it as "no syscall" rather than as "the same mode after" is the difference between
    reading the predicate and reading its effect: `chmod 0600` on a `0600` file is invisible in
    the mode it leaves behind.
    """
    reading = _drive(tmp_path, 0o600)

    assert reading["after"] == 0o600
    assert reading["calls"] == 0, (
        f"chmod reached a file already at the rule ({reading['calls']} call(s))"
    )


@posix_only
def test_a_missing_file_is_not_an_error(tmp_path):
    """The swallow, pinned: a filesystem or a race that has no file to stat must not fail a save.

    The caller's content is what a save is for; the mode is best effort, exactly as
    `emrg/config.py::_make_config_private` treats it.
    """
    reading = _drive(tmp_path, None)

    assert reading["existsBefore"] is False, "the fixture created a file it did not mean to"
    assert reading["threw"] is None, f"the module raised on a missing file: {reading['threw']}"
    assert reading["calls"] == 0


def _function_body(source: str, name: str) -> str:
    """The source of `function <name>(...) { ... }` in a 2-space-indented nested scope."""
    match = re.search(rf"^  function {name}\(.*?^  \}}$", source, re.S | re.MULTILINE)
    assert match is not None, (
        f"no `function {name}(...)` could be lifted out of main.js — the pattern has gone stale, "
        "and a stale pattern is not a pass"
    )
    return match.group(0)


def _strip_js_comments(source: str) -> str:
    """`source` with `//` and `/* */` comments blanked, line structure kept.

    A plain substring search cannot tell a call from a commented-out one — measured on this
    guard's first mutation arm: commenting `makeConfigPrivate(configPath());` out in
    `writeConfig` left it green, because the text it searched for was still there. The scanner
    tracks string literals, so a `//` inside a URL or a path is not read as a comment.
    """
    out: list[str] = []
    i, n, quote = 0, len(source), None
    while i < n:
        char = source[i]
        if quote is not None:
            out.append(char)
            if char == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in "\"'`":
            quote = char
            out.append(char)
            i += 1
            continue
        if char == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                i += 1
            continue
        if char == "/" and i + 1 < n and source[i + 1] == "*":
            i += 2
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                if source[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def test_the_comment_stripper_reads_both_shapes():
    """Both directions on the instrument itself, or every assertion below it is unfounded.

    A comment inside a string literal must survive (a URL in `main.js` carries `//`), and both
    comment syntaxes must be removed.
    """
    kept = 'const u = "https://example.com/x"; const t = `a//b`;'
    assert _strip_js_comments(kept) == kept
    stripped = _strip_js_comments('f(); // call()\ng(); /* h() */ h2();')
    assert "call()" not in stripped and "h()" not in stripped
    assert "f();" in stripped and "g();" in stripped and "h2();" in stripped


def test_the_config_writer_applies_the_rule_and_the_neighbour_does_not(tmp_path):
    """The wire, read in both directions on the same extractor.

    A module test cannot see a missing call, and `main.js::writeConfig` is the seam this whole
    issue is about. `readConfig` is the control: the extractor has to be able to answer "no" for
    it, or "yes" for `writeConfig` would only be evidence that the pattern matches anything.
    Comments are stripped first, because a commented-out call is not a call.
    """
    source = _strip_js_comments(MAIN_JS.read_text(encoding="utf-8"))

    assert re.search(r'require\(["\']\./config_mode["\']\)', source), (
        "main.js does not require ./config_mode.js, so the writer below has nothing to call"
    )
    writer = _function_body(source, "writeConfig")
    assert re.search(r"^[ \t]+makeConfigPrivate\(", writer, re.MULTILINE), (
        "writeConfig does not call makeConfigPrivate as a statement — the module's rule is never "
        "applied on the path that rewrites an existing config file (issue #2148)"
    )
    assert "fs.writeFileSync(" in writer, (
        "writeConfig no longer writes the file, so this guard is watching the wrong function"
    )
    reader = _function_body(source, "readConfig")
    assert "makeConfigPrivate(" not in reader, (
        "readConfig applies the permission rule; a read that writes is not what it says it does"
    )


def _packaging_files() -> list[str]:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["build"]["files"]


def _is_covered(rel: str, entry: str) -> bool:
    """Whether a `build.files` entry covers the repo-relative path `rel`.

    One rule, stated here rather than inferred: an entry naming a directory (`<dir>/**`) covers
    everything under it, and any other entry is exact. exclusions (`!…`) never cover anything —
    they take coverage away, which is why they are skipped rather than matched.
    """
    if entry.startswith("!"):
        return False
    if entry.endswith("/**"):
        prefix = entry[: -len("/**")]
        return rel == prefix or rel.startswith(prefix + "/")
    return rel == entry


def test_every_main_process_module_a_module_requires_is_packaged():
    """A new main-process module must be declared, or the product ships without it.

    `scripts/check-gui-package-refs.py` reads the `from` / `buildResources` / `icon` declarations
    and not this one, so an omission here is caught by nothing: `require("./config_mode")` would
    resolve in this checkout and fail inside the packaged app's asar. The class of files is
    **discovered** (`emrg/gui/*.js` at the project root), not listed, so the next module is
    covered without this test being edited.
    """
    files = _packaging_files()
    assert "main.js" in files, "the packaging list was not read as expected"

    modules = sorted(p.name for p in GUI_DIR.glob("*.js"))
    assert len(modules) >= 5, f"discovery found only {modules} — that is not this project's tree"

    missing: list[str] = []
    for module in modules:
        source = (GUI_DIR / module).read_text(encoding="utf-8")
        for spec in re.findall(r'require\(["\']\./([^"\']+)["\']\)', source):
            target = spec if (GUI_DIR / spec).is_file() else f"{spec}.js"
            if not (GUI_DIR / target).is_file():
                continue  # not a file in this project dir (a package, or a build-time path)
            if not any(_is_covered(target, entry) for entry in files):
                missing.append(f"{module} requires ./{spec}, but no `build.files` entry covers {target}")
    assert missing == [], (
        "the packaged app would be missing a module that main-process code requires:\n  "
        + "\n  ".join(missing)
    )
