"""Host scripts must not decode a child process's output with the locale codec.

The class, and why it keeps coming back
---------------------------------------
`subprocess.run(..., text=True)` with no `encoding=` decodes the child's bytes
with the *locale* codec. That is invisible on a UTF-8 host - which is every
developer machine and every CI runner in this repo - and fatal on the hosts this
repo actually ships to, where the locale is often cp936/GBK or cp1252:

* the child's output is UTF-8, because that is what `gh`, `git` and `node` emit;
* the locale codec cannot decode arbitrary UTF-8 bytes, so `UnicodeDecodeError`
  is raised *instead of* the result, on data that is perfectly correct.

Four instances of this class have been fixed one at a time, each after a human
found it: `bump-version.py` and `check_nonlocal.py` (#1119/#1121, *output*
encoding), the scripts' `print` literals, and `check-node-test-count.py`
(issue #1132, decode + a bare `npm`). Fixing them one at a time is exactly why
the fifth and sixth survived: they are only found if someone re-runs the same
scan by hand.

This module is that scan, kept. It is a *class* guard: it names every call site
at once and keeps naming new ones, so the next instance is caught by CI rather
than by a reviewer noticing.

Two halves, deliberately different in kind
------------------------------------------
* **static** — two rules over `scripts/` and the `emrg/` package:
  1. every `subprocess` call that asks for text (`text=True` /
     `universal_newlines=True`) must also pin `encoding=`, unless its *child
     program* is one that writes the Windows console code page
     (`_CONSOLE_PROGRAMS`);
  2. the locale codec (`locale.getpreferredencoding()` and friends) must not be
     named at all, except in the one file whose subject is the console code page.
  This half covers call sites no test drives, which is where the surviving
  instances lived: `sync-master-from-api.py`'s API fallback only runs when the
  primary path 403s, and `reader_fix_latency.py` is a manual reporting tool with
  no test at all.
* **behavioural** — a real child emits a byte that is invalid in both UTF-8 and
  the hostile locale codecs, and the two call shapes are compared: the pinned
  shape returns text, the unpinned shape raises. Without this half the static
  rule could be satisfied by a refactor that still mis-decodes somewhere else.

Scope boundary — corrected, because the first version of this paragraph was wrong
--------------------------------------------------------------------------------
The first version of this module read `scripts/` only, and justified it by saying
`emrg/` was already handled: `emrg/server/git_utils.py` pins `encoding="utf-8"`,
and `emrg/tools/bash_tool.py` deliberately tries the locale codec then UTF-8 for
console output. **That reasoning is true of those two files and false of the rest
of `emrg/`** — it was a claim about the package drawn from two of its files, which
is exactly the "stated scope wider than what it reads" failure this module exists
to prevent (measured the cycle after: `emrg/client/app.py`'s clipboard reader and
`emrg/_stop_all.py`'s `ps` reader both decoded *paths* with the locale codec).

The scan now covers `scripts/` **and** `emrg/`, and the exemption is drawn on the
axis that actually decides the encoding — the **child program**, because a path is
filesystem bytes (UTF-8) while a Windows console program's stdout is the console
code page, in the same file. `emrg/_stop_all.py` is the proof: its `ps` readers
decode *paths* and were pinned, while its `powershell`/`taskkill` readers decode
console text and legitimately are not.

An exemption needs a reason in the same list, and the test fails if the exemption's
subject disappears, so a future refactor cannot silently widen the blind spot. The
file-level list (`_CONSOLE_DECODE_ALLOWED`) covers only the explicit locale-codec
spelling, which is a property of the file; the text-mode rule exempts by program.

Splats are resolved, not assumed
--------------------------------
The first version of the `emrg/` scan reported every `**expr` call as unreadable,
which produced 27 false positives: `**win32_no_window_kwargs()` and `**_no_window()`
can only ever supply `creationflags`, and `**_PATH_DECODE` *is* the pin. Those
providers are now resolved (one local-assignment hop included, for
`kw = _no_window()`), so the rule reports the calls it cannot prove safe rather
than every call whose keywords are spelled indirectly.

The same mistake in the other direction, and the fix
----------------------------------------------------
The correction above then over-corrected: the first `emrg/` version reported every
splat as unsafe (27 false positives) and exempted `emrg/_stop_all.py` by *file*,
which would have hidden a genuinely unpinned `ps` read beside the console ones. Both
were the same error - drawing the line on syntax ("does it spell `encoding=`?")
instead of on subject ("does this child emit UTF-8?"). The rules now resolve what can
be resolved (splat providers, locally aliased kwarg dicts, concatenated argv) and
report only what they cannot.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
PACKAGE = REPO_ROOT / "emrg"

_SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "call", "check_call"}

# Text-mode by *construction*: these decode the child's bytes with the locale codec
# and never look at `text=`, so a call site carries no marker the scan above can
# find. Measured on a cp936 host (see test_the_invisible_entry_points_are_reported):
# a child emitting UTF-8 U+2014 raises `UnicodeDecodeError: 'gbk' codec can't
# decode byte 0xad` out of both, and `subprocess.getoutput` propagates it rather
# than returning it. They are still returners - `getoutput` returns the decoded
# text and `getstatusoutput` a `(status, text)` pair - so pinning them is possible
# (both take `encoding=`/`errors=` since 3.10; this repo requires 3.11).
_TEXT_BY_CONSTRUCTION = {"getoutput", "getstatusoutput"}

# Text-mode and *unpinnable*: `os.popen(cmd, mode='r', buffering=-1)` accepts no
# `encoding=` at all (measured: `TypeError`), so there is no repair at the call
# site. It is reported with the opposite advice - use `subprocess.run(...,
# encoding=...)` - rather than told to add a keyword its signature rejects.
_UNPINNABLE_TEXT_CALLS = {"popen"}

_TEXT_KWARGS = {"text", "universal_newlines"}

# The explicit spelling of "decode with the host's locale". Matched through the
# AST, not a line scan, so a comment or docstring that merely names the API is not
# mistaken for a call to it.
_LOCALE_CODEC_ATTRS = {"getpreferredencoding", "getdefaultencoding", "getencoding"}


def _locale_codec_lines(source: str) -> list[int]:
    """Line numbers of real `locale.<codec>()` calls in `source`."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOCALE_CODEC_ATTRS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "locale"
    ]

# Files allowed to keep the locale codec because their *subject* is the Windows
# console code page. Format: "relative/path.py" -> why. This is a file-level
# exemption for the explicit `locale.getpreferredencoding()` spelling only; the
# text-mode rule below exempts by child program, which is the narrower axis.
_CONSOLE_DECODE_ALLOWED = {
    "emrg/tools/bash_tool.py": (
        "`_decode_output` reads commands the user runs: cmd.exe/dir output is the "
        "Windows console code page, git/gh output is UTF-8, so it tries the locale "
        "codec strictly and then UTF-8 (rant 2026-08-08T09:35:30). It makes no "
        "text-mode subprocess call - the child is an asyncio subprocess - so this "
        "exemption is live through the locale-codec rule, not the text-mode rule; "
        "covered by tests/test_bash_tool.py"
    ),
}

# Child programs whose stdout really is the Windows console code page, so a
# text-mode read of them keeps the locale codec by design. Exempting the
# *program* rather than the file is deliberate: the first version of this module's
# `emrg/` scan exempted `emrg/_stop_all.py` wholesale, which would have hidden a
# genuinely unpinned `ps` read in the same file. With this axis, a new
# `subprocess.run(["git", ...], text=True)` anywhere is still reported.
_CONSOLE_PROGRAMS = {
    "powershell": "PowerShell writes the console code page unless OutputEncoding is set",
    "taskkill": "taskkill's output is a console message, not data",
    "tasklist": "tasklist's output is a console table, not data",
    "cmd": "cmd.exe writes the console code page, the case _decode_output documents",
    "where": "the Windows console `where` command emits console text",
    "chcp": "reads or sets the console code page itself",
    "wmic": "the deprecated console WMI CLI emits console text",
}

# A byte invalid in UTF-8 *and* in the codecs the affected hosts use (cp936/GBK,
# cp1252, ascii), so the probe does not depend on which hostile locale the runner
# happens to have. Pinned by test_the_probe_byte_is_invalid_in_every_relevant_codec.
_PROBE_BYTE = 0x81


# Splat expressions whose keys are knowable without running the program, and
# what they may contribute. This module had a first version that reported every
# `**expr` call as unreadable and therefore unsafe - 27 false positives, because
# three of the four providers in this repo can only ever supply `creationflags`,
# and one (`_PATH_DECODE`) *is* the pin. Resolving providers keeps the rule
# honest in both directions; a splat from an unknown provider is still reported.
#
# Format: provider expression -> (keys it may pass, why it is safe to skip).
_SPLAT_PROVIDERS = {
    "win32_no_window_kwargs()": (
        {"creationflags"},
        "emrg/_win.py returns creationflags only, on every platform",
    ),
    "_no_window()": (
        {"creationflags"},
        "emrg/_stop_all.py returns creationflags only (mirrors _win.py)",
    ),
    "_PATH_DECODE": (
        {"encoding", "errors"},
        "the pin itself: {'encoding': 'utf-8', 'errors': 'replace'}",
    ),
}


def _leading_text(value: ast.expr) -> str:
    """The leading literal text of an expression, or "" if it does not start literal.

    `subprocess.run("powershell -NoProfile -Command " + script, ...)` is as
    resolvable as a list argv: the program name is the first word of the literal
    part, and everything appended after it cannot change that. Without this the
    scan reported one real PowerShell read as "child not statically known", which
    is true of the *argument* and false of the *program*.
    """
    while isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        value = value.left
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return ""


def _child_program(node: ast.Call, assigns: dict[str, ast.expr]) -> str:
    """The command name of a subprocess call's argv, or "" when unresolvable.

    The child program is what decides the encoding of its output, so it is the
    unit this module exempts: `powershell`, `taskkill` and `tasklist` write the
    Windows console code page, while `git`, `gh`, `ps` and `node` write UTF-8.
    An argv the static analysis cannot follow returns "" and is treated as
    "not known to be a console program", i.e. it must be pinned.
    """
    if not node.args:
        return ""
    value = node.args[0]
    if isinstance(value, ast.Name) and value.id in assigns:
        value = assigns[value.id]
    if isinstance(value, (ast.List, ast.Tuple)) and value.elts:
        value = value.elts[0]
    text = _leading_text(value)
    if not text and isinstance(value, ast.JoinedStr) and value.values:
        first = value.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            text = first.value
    parts = text.split()
    return parts[0] if parts else ""


def _text_mode_calls(
    source: str, label: str = "<string>"
) -> list[tuple[int, str, set[str], str]]:
    """(lineno, func_name, kwargs, child_program) for every text-mode call in `source`.

    A call is text-mode if it asks for text (`text=True` / `universal_newlines=True`)
    directly, or if it splats a provider that could supply either key. Locally
    assigned names (``kw = _no_window(); ... **kw``) are resolved one step, which
    is what `emrg/_stop_all.py` does 14 times.

    Keywords forwarded through an *unknown* splat cannot be read, so such a call
    reports the empty name `"**"` in place of the func name - the caller can then
    refuse to treat it as clean instead of assuming the options are visible.
    """
    forwarders = {
        t.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
        for t in node.targets
        if isinstance(t, ast.Name)
        and (
            node.value.func.attr
            if isinstance(node.value.func, ast.Attribute)
            else getattr(node.value.func, "id", "")
        )
        in {"_no_window", "win32_no_window_kwargs"}
    }
    tree = ast.parse(source)
    assigns: dict[str, ast.expr] = {
        t.id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    found: list[tuple[int, str, set[str], str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", "")
        )
        if name in _TEXT_BY_CONSTRUCTION:
            # No `text=`/`universal_newlines=` to look for: the decode is the
            # function's whole job. Same repair as an explicit text-mode call.
            found.append(
                (node.lineno, name, {k.arg for k in node.keywords if k.arg},
                 _child_program(node, assigns))
            )
            continue
        if name in _UNPINNABLE_TEXT_CALLS:
            # Attribute access is enough (no module resolution needed), and the
            # empty kwarg set is what distinguishes it in `_violations`.
            found.append(
                (node.lineno, name, set(), _child_program(node, assigns))
            )
            continue
        if name not in _SUBPROCESS_FUNCS:
            continue
        kwargs = {k.arg for k in node.keywords if k.arg}
        unknown = False
        for k in node.keywords:
            if k.arg is not None:
                continue
            expr = ast.unparse(k.value)
            if expr in _SPLAT_PROVIDERS:
                kwargs |= _SPLAT_PROVIDERS[expr][0]
            elif isinstance(k.value, ast.Name) and k.value.id in forwarders:
                kwargs |= {"creationflags"}
            else:
                unknown = True
        if unknown:
            # options are forwarded; text= and encoding= may both be in there
            found.append((node.lineno, "**", kwargs, _child_program(node, assigns)))
        elif kwargs & _TEXT_KWARGS:
            found.append((node.lineno, name, kwargs, _child_program(node, assigns)))
    return found


# Directories that hold third-party code. They are skipped because a guard over
# *our* code must not fail on a file we are not allowed to edit - and because
# `emrg/gui/node_modules` only exists after `npm install`, so including it made the
# scan's file count (and therefore the rule's reach) depend on whether a developer
# had installed the GUI, the reverse of the green-locally/red-in-CI asymmetry this
# module exists to prevent. Measured: 12 of 81 scanned files were vendored.
_VENDORED_DIRS = {"node_modules", "site-packages", ".venv", "venv", "dist-info"}


def _tracked_first_party_py() -> list[Path]:
    """Every tracked `.py` file git knows about, minus vendored trees.

    This is the *definition* of the rule's scope, and it is derived from the
    index rather than from a directory list on purpose: an index cannot go stale,
    and a hand-maintained root list is exactly what let this module's scope be
    wrong twice (first `scripts/` alone, then `scripts/` + `emrg/` while 70
    tracked first-party Python files - `tests/` 69 and `packaging/` 1 - were
    never read at all).

    `git ls-files` output is decoded as UTF-8 explicitly. Measured: under GBK with
    `core.quotePath=false`, `git ls-files` emits the raw UTF-8 path bytes
    (`\\xe5\\x9b\\xbe\\xe7\\x89\\x87.py`); unpinned, that raises UnicodeDecodeError - the
    very class this module guards. The default `quotePath=true` escapes them to
    ASCII, which is why the bug is invisible on a default-configured host.
    """
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    assert proc.returncode == 0, proc.stderr
    return [
        REPO_ROOT / rel
        for rel in proc.stdout.split("\0")
        if rel.endswith(".py") and not _VENDORED_DIRS & set(Path(rel).parts)
    ]


def _scan_roots() -> list[Path]:
    """Every tracked first-party Python file this rule covers.

    Vendored trees are excluded by `_VENDORED_DIRS`, and the exclusion is asserted
    to be doing something (rather than silently matching nothing).
    """
    files = _tracked_first_party_py()
    assert files, "git reported no first-party Python files - the fixture is broken"
    vendored_paths = [
        p for p in PACKAGE.rglob("*.py") if _VENDORED_DIRS & set(p.parts)
    ]
    for v in vendored_paths:
        assert v not in files, f"vendored file leaked into the scan set: {v}"
    return sorted(files)


def _violations(source: str, rel: str) -> list[str]:
    """The calls in `source` that must be pinned but are not. Pure, so it is testable.

    Split out of the scan below deliberately: this decision - "is this call exempt?"
    - is the rule, and while it was inline the only way to exercise it was to edit a
    real file. A mutation that widened the exemption to `if True:` therefore left the
    suite green, because every site the scan *reached* was legitimately exempt; the
    rule's reach and its logic had never been tested separately.
    """
    out: list[str] = []
    for lineno, func, kwargs, child in _text_mode_calls(source, rel):
        if child in _CONSOLE_PROGRAMS:
            continue
        if "encoding" in kwargs:
            continue
        where = f"child {child!r}" if child else "child not statically known"
        if func in _UNPINNABLE_TEXT_CALLS:
            out.append(
                f"{rel}:{lineno} {func}() decodes with the locale codec and takes "
                f"no encoding= (its signature is (cmd, mode='r', buffering=-1)), so "
                f"there is nothing to pin at this call site ({where}) - read the "
                "child with subprocess.run(..., encoding='utf-8', errors='replace')"
            )
            continue
        if func == "**":
            out.append(
                f"{rel}:{lineno} forwards **kwargs into a subprocess call "
                f"({where}) - the encoding is not visible here, check the caller"
            )
        else:
            out.append(
                f"{rel}:{lineno} subprocess.{func}("
                f"{', '.join(sorted(kwargs))}) has no encoding= ({where})"
            )
    return out


def test_every_text_mode_subprocess_pins_its_encoding() -> None:
    """Text-mode subprocess calls pin `encoding=`, unless the *child* is a console.

    Covers `scripts/` and the whole `emrg/` package. The rule had read `scripts/`
    alone until a later cycle measured that its stated reason for skipping `emrg/`
    held for two files, not the package - see the module docstring.

    The exemption is the child program (`_CONSOLE_PROGRAMS`), so it cannot hide an
    unpinned read of a UTF-8-emitting child that happens to live in the same file.
    """
    violations: list[str] = []
    for path in _scan_roots():
        rel = path.relative_to(REPO_ROOT).as_posix()
        violations += _violations(path.read_text(encoding="utf-8"), rel)

    assert not violations, (
        "a text-mode subprocess without encoding= decodes with the locale codec, "
        "so it raises UnicodeDecodeError (or silently mojibakes) on a non-UTF-8 "
        "host (cp936/GBK, cp1252) for data that is correctly UTF-8 - the class "
        "behind #1119/#1121, issue #1132, and the clipboard/`ps` path readers. "
        'Add `encoding="utf-8", errors="replace"`, or - if the child really writes '
        "the Windows console code page - add it to _CONSOLE_PROGRAMS with a "
        "reason:\n  " + "\n  ".join(violations)
    )


def test_no_locale_codec_is_used_to_decode_subprocess_output() -> None:
    """Rule two: the locale codec is not a decoder for a child process.

    Rule one catches the *implicit* form (`subprocess.run(..., text=True)` with no
    `encoding=`). This catches the explicit one: a name like
    `locale.getpreferredencoding()` in a file that runs subprocesses is the same
    policy written out loud. Only `emrg/tools/bash_tool.py` is allowed it, because
    its subject really is the Windows console code page; the exemption is tied to
    the file by `_CONSOLE_DECODE_ALLOWED`, and the dead-entry test fails if the
    usage disappears.
    """
    violations: list[str] = []
    for path in _scan_roots():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _CONSOLE_DECODE_ALLOWED:
            continue
        source = path.read_text(encoding="utf-8")
        for i in _locale_codec_lines(source):
            violations.append(f"{rel}:{i} {source.splitlines()[i - 1].strip()}")

    assert not violations, (
        "the locale codec re-encodes the *host's* assumption onto a child process's "
        "output, which is UTF-8; a child's bytes are not console bytes unless the "
        "child is a Windows console program. Decode with an explicit encoding=, or "
        "add the file to _CONSOLE_DECODE_ALLOWED with a reason:\n  "
        + "\n  ".join(violations)
    )


def test_the_allowlist_has_no_dead_entries() -> None:
    """Each exemption names a file that exists and still makes the call it claims.

    An allowlist with a stale entry silently widens the rule's blind spot, which
    is the same failure mode as the scope paragraph this module had to correct.
    The entry must be *live* for one of the two rules: this module's first
    `emrg/` version allowlisted `emrg/tools/bash_tool.py` for console output while
    the scan looked for `subprocess.run(text=True)` - which that file never makes,
    because it uses `asyncio.create_subprocess_shell`. The entry was true and
    irrelevant at once, and only this check could say so.
    """
    for rel, reason in _CONSOLE_DECODE_ALLOWED.items():
        path = REPO_ROOT / rel
        assert path.exists(), f"allowlisted file is gone: {rel} (reason: {reason})"
        source = path.read_text(encoding="utf-8")
        live = bool(_text_mode_calls(source, rel)) or bool(_locale_codec_lines(source))
        assert live, (
            f"{rel} is allowlisted for console-output decoding but neither makes a "
            "text-mode subprocess call nor names the locale codec any more - "
            f"remove the exemption (reason given: {reason})"
        )


def test_the_scan_catches_an_unpinned_site() -> None:
    """Positive control: the scan must actually flag the shape it forbids.

    Otherwise a refactor that stops matching (say the rule starts requiring a
    keyword nothing uses) turns the guard above into a test that always passes.
    """
    bad = "import subprocess\nr = subprocess.run(['gh', 'api', 'x'], capture_output=True, text=True)\n"
    pinned = (
        "import subprocess\n"
        "r = subprocess.run(['gh', 'api', 'x'], capture_output=True, text=True,\n"
        "                   encoding='utf-8', errors='replace')\n"
    )
    forwarded = "import subprocess\nr = subprocess.run(['gh'], **opts)\n"
    no_window = (
        "import subprocess\n"
        "from emrg._win import win32_no_window_kwargs\n"
        "r = subprocess.run(['taskkill', '/F'], capture_output=True,\n"
        "                   **win32_no_window_kwargs())\n"
    )
    local_alias = (
        "import subprocess\n"
        "kw = _no_window()\n"
        "r = subprocess.run(['taskkill', '/F'], capture_output=True, **kw)\n"
    )

    assert [f for _, f, _, _ in _text_mode_calls(bad)] == ["run"], (
        "the scan no longer sees a plain unpinned call"
    )
    flagged = [(f, k) for _, f, k, _ in _text_mode_calls(bad) if "encoding" not in k]
    assert flagged, "an unpinned call must be reported, not just seen"

    seen_pinned = [k for _, _, k, _ in _text_mode_calls(pinned)]
    assert seen_pinned and all("encoding" in k for k in seen_pinned), (
        "a pinned call must be seen and must not be reported"
    )

    assert [f for _, f, _, _ in _text_mode_calls(forwarded)] == ["**"], (
        "a call forwarding an unknown **kwargs must be reported as unreadable, "
        "never ignored"
    )

    # The opposite direction: a splat that *cannot* carry text= or encoding= is
    # resolved, not flagged. Without this the rule's first `emrg/` version
    # reported 27 sites that were already pinned or never text-mode.
    for label, src in (("direct", no_window), ("assigned to a local", local_alias)):
        assert _text_mode_calls(src) == [], (
            f"a {label} `_no_window()` splat supplies creationflags only, so it is "
            "not a text-mode call and must not be reported"
        )

    # ...but a text=True written next to a harmless splat is still a text-mode read.
    beside = (
        "import subprocess\n"
        "r = subprocess.run(['gh'], text=True, **win32_no_window_kwargs())\n"
    )
    assert [f for _, f, _, _ in _text_mode_calls(beside)] == ["run"], (
        "a splat must not hide a text=True written beside it"
    )


def test_the_invisible_entry_points_are_reported() -> None:
    """The three text-mode entry points that carry no `text=` marker are seen.

    The rule's stated job is that "the next instance is caught by CI rather than by
    a reviewer", so this drives the shapes an instance could actually take. All
    three decode through the locale codec and none of them can be found by looking
    for `text=True` - measured on a cp936 host, a child emitting UTF-8 U+2014
    (`e2 80 94`, the em dash, present in **95 of the first 100** closed issues of
    this very repo, so it is ordinary prose and not an exotic input):

    ```
    subprocess.getoutput       -> UnicodeDecodeError: 'gbk' ... byte 0xad
    subprocess.getstatusoutput -> UnicodeDecodeError: 'gbk' ... byte 0xad
    os.popen(...).read()       -> U+FFFD per byte - mojibake, no exception at all
    ```

    Before this test the scan reported *none* of them (measured: the whole probe
    file yielded only the one `subprocess.run` violation), so a future instance
    written with any of them would have shipped green.
    """
    src = (
        "import os, subprocess\n"
        "a = subprocess.getoutput('gh pr list')\n"
        "b = subprocess.getstatusoutput('gh pr list')\n"
        "c = os.popen('gh pr list').read()\n"
    )
    seen = {f for _, f, _, _ in _text_mode_calls(src)}
    assert seen == {"getoutput", "getstatusoutput", "popen"}, (
        f"every entry point that decodes with the locale codec must be seen; got {seen}"
    )

    pinned = (
        "import subprocess\n"
        "a = subprocess.getoutput('gh', encoding='utf-8', errors='replace')\n"
        "b = subprocess.getstatusoutput('gh', encoding='utf-8', errors='replace')\n"
    )
    assert _violations(src + pinned, "probe.py") == [
        v for v in _violations(src, "probe.py")
    ] + [], (
        "pinning must not be reported - `getoutput`/`getstatusoutput` accept "
        "encoding= (3.10+), so adding it is the whole repair"
    )
    assert not [v for v in _violations(pinned, "probe.py")], (
        "a pinned getoutput/getstatusoutput pair must be clean"
    )

    # The unpinnable one is reported with the *other* advice: its signature is
    # `(cmd, mode='r', buffering=-1)` and it rejects `encoding=` with a TypeError,
    # so "add encoding=" would send the reader to an error rather than a fix.
    popen_only = _violations("import os\nc = os.popen('gh pr list').read()\n", "probe.py")
    assert len(popen_only) == 1 and "subprocess.run" in popen_only[0], (
        f"os.popen must be reported as unpinnable, with the replacement named; "
        f"got {popen_only}"
    )
    assert "no encoding=" in popen_only[0], (
        "the message must not tell the reader to add a keyword the signature rejects"
    )


def test_the_entry_points_really_decode_with_the_locale_codec() -> None:
    """Behavioural half: the three shapes yield no usable text on a hostile locale.

    Without this the rule above could be satisfied by a *list* rather than by a
    measurement. The child runs the same byte through each entry point under an
    explicitly non-UTF-8 locale, and prints only the *length* of what came back -
    the decoded text holds U+FFFD, and printing it would make the child's own
    stdout raise under the very locale under test.

    The failure has more than one shape here, as it does for `subprocess.run`
    (`test_pinned_decoding_survives_a_hostile_locale`): on POSIX the decode runs in
    the parent and raises; on Windows it runs in a reader thread, `threading`
    swallows the error, the stream returns `None`, and `getstatusoutput` then dies
    slicing it (`TypeError: 'NoneType' object is not subscriptable`) - the same
    uncaught-`TypeError` shape issue #1132 is about. `os.popen` raises neither on a
    codec that accepts the bytes: it silently substitutes, which is why the probe
    reports text and the assertion below is on one pointer, not on the exception.
    """
    child = (
        "import os, subprocess, sys\n"
        "cmd = sys.executable + \" -c \\\"import sys; "
        "sys.stdout.buffer.write(bytes([%d]))\\\"\"\n" % _PROBE_BYTE
    ) + (
        "def show(label, fn):\n"
        "    try:\n"
        "        out = fn()\n"
        "    except Exception as exc:\n"
        "        print(label, 'NO-TEXT', type(exc).__name__)\n"
        "        return\n"
        "    print(label, 'TEXT', len(out))\n"
        "show('GETOUTPUT-UNPINNED', lambda: subprocess.getoutput(cmd))\n"
        "show('GETSTATUS-UNPINNED', lambda: subprocess.getstatusoutput(cmd)[1])\n"
        "show('POPEN-UNPINNED', lambda: os.popen(cmd).read())\n"
        "show('GETOUTPUT-PINNED', lambda: subprocess.getoutput(cmd, encoding='utf-8', errors='replace'))\n"
        "show('RUN-PINNED', lambda: subprocess.run(cmd, shell=True, capture_output=True,"
        " text=True, encoding='utf-8', errors='replace').stdout)\n"
    )
    env = dict(os.environ, PYTHONUTF8="0", LC_ALL="C", LANG="C")
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = dict(
        line.split()[:1] + [" ".join(line.split()[1:])]
        for line in proc.stdout.splitlines()
        if line.strip()
    )
    # `os.popen` under the C locale substitutes rather than raising in some builds,
    # so only its *pinned replacement* is asserted - the point is that a repair
    # exists and is named by the rule, not that every codec picks the same failure.
    for label in ("GETOUTPUT-UNPINNED", "GETSTATUS-UNPINNED"):
        assert "NO-TEXT" in lines.get(label, ""), (
            f"{label} must be shown to yield no usable text under a hostile locale, "
            "or this test would pass on a UTF-8 host and prove nothing:\n"
            + proc.stdout + proc.stderr
        )
    for label in ("GETOUTPUT-PINNED", "RUN-PINNED"):
        assert "TEXT" in lines.get(label, ""), (
            f"{label} must return text - it is the repair the rule names:\n"
            + proc.stdout + proc.stderr
        )


def test_the_scan_equals_the_index_and_every_directory_is_reached(tmp_path: Path) -> None:
    """The scan is the index, and no *directory* is silently unreachable.

    The version of this test that preceded it was **circular**, and the cycle that
    wrote it proved that to itself: it built the expected set with
    `git ls-files emrg scripts` and compared it to `_scan_roots()`, which globs
    exactly those two directories. Both sides were derived from the same root list,
    so 70 tracked first-party Python files were invisible to it. Mutating the scan
    to read `emrg/` alone **and relaxing the assertion to match** left the whole
    module green (12 passed) - a self-consistent narrowing, which is precisely the
    failure this module exists to catch. Measured: 9 unpinned text-mode calls lived
    in those unseen files, one of them in this module's own probe.

    Assertions that cannot be satisfied by narrow-*and*-relax are used instead:

    * the scan equals `git ls-files '*.py'` minus vendored trees - an *index*
      derivation, independent of any directory list in this file;
    * every top-level directory holding tracked `.py` files must be represented,
      so dropping a root fails even if the count is adjusted to hide it.
    """
    tracked_all = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.split("\0")
    expected = sorted(
        (REPO_ROOT / rel).resolve()
        for rel in tracked_all
        if rel.endswith(".py") and not _VENDORED_DIRS & set(Path(rel).parts)
    )
    assert expected, "git reported no first-party Python files - the fixture is broken"
    scanned = sorted(p.resolve() for p in _scan_roots())
    assert scanned == expected, (
        "the scanned set must be exactly the tracked first-party Python files; a "
        "tracked file that is not scanned is a silent hole in the rule"
    )

    # Independent of the equality above: every directory that holds tracked Python
    # must contribute at least one scanned file. A refactor that drops `tests/` (or
    # `packaging/`) from the scan *and* re-anchors the equality would still fail here.
    tops = {Path(rel).parts[0] for rel in tracked_all if rel.endswith(".py")
            and not _VENDORED_DIRS & set(Path(rel).parts)}
    scanned_tops = {p.relative_to(REPO_ROOT).parts[0] for p in scanned}
    missing = tops - scanned_tops
    assert not missing, (
        f"these top-level directories hold tracked Python but are never scanned: "
        f"{sorted(missing)} - a directory absent from the scan is a directory whose "
        "violations this guard cannot see"
    )
    # `tests/` is not decoration: it is where the largest unreached set lives, and
    # it is where this module itself lives - a guard that exempts its own file's
    # directory from its own rule is the circularity this test exists to prevent.
    assert "tests" in scanned_tops, (
        "tests/ must be scanned: the module's own probe reads a child process too"
    )

    # The vendored exclusion is exercised on a synthetic tree rather than on
    # `emrg/gui/node_modules`: that tree exists only where someone ran `npm install`,
    # and asserting it exists made an earlier version of this test fail on both CI
    # jobs while passing locally.
    tree = tmp_path / "pkg" / "node_modules" / "vendored"
    tree.mkdir(parents=True)
    (tree / "third_party.py").write_text("x = 1\n", encoding="utf-8")
    (tree.parent.parent / "ours.py").write_text("x = 1\n", encoding="utf-8")
    found = [p for p in (tmp_path / "pkg").rglob("*.py")]
    assert len(found) == 2, "the fixture must contain one vendored and one first-party file"
    assert [p for p in found if not _VENDORED_DIRS & set(p.parts)] == [
        tmp_path / "pkg" / "ours.py"
    ], "the vendored exclusion must drop the third-party file and keep ours"


def test_the_exemption_decides_on_the_child_not_the_file() -> None:
    """The rule's decision, driven directly, in the case the scan itself cannot reach.

    Every text-mode site left in the repo is legitimately a console program, so a
    mutation that widened the exemption to *everything* still left the scan green:
    the rule's reach was tested, its logic was not. These fixtures supply the pair
    the repo does not contain - a UTF-8-emitting child next to a console child in
    one file - and assert that only the first is reported.
    """
    mixed = (
        "import subprocess\n"
        "a = subprocess.run(['powershell', '-Command', 'x'], text=True)\n"
        "b = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True)\n"
    )
    reported = _violations(mixed, "x.py")
    assert len(reported) == 1, reported
    assert "x.py:3" in reported[0], "only the `git` read is a violation"
    assert "child 'git'" in reported[0]

    # The console exemption is a program, not a prefix: a program whose name merely
    # contains one must still be pinned (the #461 singular/plural lesson, in the
    # other direction - match the thing, not a substring of it).
    assert _violations(
        "import subprocess\nsubprocess.run(['powershellish'], text=True)\n", "x.py"
    ), "a lookalike child name must not inherit the exemption"

    # An unresolvable argv must be reported, not credited to a console program.
    assert _violations(
        "import subprocess\nsubprocess.run([exe, '-x'], text=True)\n", "x.py"
    ), "an argv the scan cannot follow must be pinned, not exempted"

    # Pinned stays clean even when the child is unknown.
    assert not _violations(
        "import subprocess\n"
        "subprocess.run([exe, '-x'], text=True, encoding='utf-8', errors='replace')\n",
        "x.py",
    )


def test_the_child_program_is_read_from_the_argv() -> None:
    """The exemption axis is the child program, so it must actually be resolved.

    A resolver that quietly returned "" for everything would make
    `_CONSOLE_PROGRAMS` dead while the rule still looked live - the same
    "exemption that cannot fire" failure the dead-entry test exists for.
    """
    assert _child_program(
        ast.parse("import subprocess; subprocess.run(['powershell', '-Command', 'x'], text=True)").body[1].value,
        {},
    ) == "powershell"
    assert _child_program(
        ast.parse("import subprocess; subprocess.run(cmd, text=True)").body[1].value,
        {"cmd": ast.parse("'tasklist'").body[0].value},
    ) == "tasklist"
    assert _child_program(
        ast.parse("import subprocess; subprocess.run([exe, '-m', 'x'], text=True)").body[1].value,
        {},
    ) == "", "an unresolved argv must not be credited to a console program"
    assert _child_program(
        ast.parse("import subprocess; subprocess.run(f'powershell {x}', text=True, shell=True)").body[1].value,
        {},
    ) == "powershell"
    for program in _CONSOLE_PROGRAMS:
        assert _CONSOLE_PROGRAMS[program], f"{program} needs a reason"


def test_the_locale_codec_rule_catches_the_explicit_spelling() -> None:
    """Positive control for rule two, so it cannot pass by matching nothing."""
    assert _locale_codec_lines("enc = locale.getpreferredencoding(False) or 'utf-8'"), (
        "the explicit locale-codec spelling must be reported"
    )
    assert not _locale_codec_lines("enc = data.decode('utf-8', errors='replace')")
    # AST, not a line scan: a comment or a docstring naming the API is not a use of
    # it, and a line scan cannot tell the difference.
    assert not _locale_codec_lines("# locale.getpreferredencoding(False)")
    assert not _locale_codec_lines('"""locale.getdefaultencoding() is not used here."""')


def test_the_probe_byte_is_invalid_in_every_relevant_codec() -> None:
    """The discriminator holds on any host, so the behavioural test cannot self-pass.

    If `_PROBE_BYTE` were decodable in one of these codecs, the test below would
    pass for a reason unrelated to the fix on that host - the "green where I ran
    it" failure this whole class of bug is made of.
    """
    for codec in ("utf-8", "ascii", "cp936", "gbk", "cp1252"):
        with pytest.raises(UnicodeDecodeError):
            bytes([_PROBE_BYTE]).decode(codec)


_CHILD = (
    "import subprocess, sys\n"
    "child = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(bytes([%d]))']\n"
    % _PROBE_BYTE
) + (
    "for pinned in (True, False):\n"
    "    kw = {'text': True}\n"
    "    if pinned:\n"
    "        kw.update(encoding='utf-8', errors='replace')\n"
    "    label = 'PINNED' if pinned else 'UNPINNED'\n"
    "    try:\n"
    "        out = subprocess.run(child, capture_output=True, **kw).stdout\n"
    "    except UnicodeDecodeError:\n"
    # POSIX: the decode happens in the parent, so the error propagates here.
    "        print(label, 'NO-TEXT', 'raised')\n"
    "        continue\n"
    # Windows: the decode happens in subprocess's reader thread, `threading`
    # swallows the error, and the stream comes back as None. Both shapes are the
    # same failure - "no usable text" - so the probe must accept either, or it
    # would report the Windows behaviour as a pass for the wrong reason.
    "    if not isinstance(out, str):\n"
    "        print(label, 'NO-TEXT', 'none')\n"
    "        continue\n"
    # Only the *length* is printed: the decoded text holds U+FFFD, and printing
    # it here would make the child's own stdout raise under the hostile locale.
    "    print(label, 'TEXT', len(out))\n"
)


@pytest.mark.parametrize("locale_codec", ["C", "en_US.ISO-8859-1"])
def test_pinned_decoding_survives_a_hostile_locale(locale_codec: str) -> None:
    """Behavioural half: pinning returns text exactly where the unpinned shape does not.

    Both shapes run in the same child under an explicitly non-UTF-8 locale, so the
    difference is real on any host - including a UTF-8 developer machine, where an
    unpinned decode would succeed and this test would prove nothing.

    The unpinned failure has **two measured shapes**, and the probe accepts both
    because the first version of this test accepted only the first one and went
    red on the Windows CI job:

    * POSIX - the decode runs in the parent, so `UnicodeDecodeError` propagates
      out of `subprocess.run`;
    * Windows - the decode runs in subprocess's reader thread, `threading`
      swallows the error, and the stream comes back as `None`.

    The second shape is the one issue #1132 is about, and it is only visible on
    the platform where this class of bug actually bites: on a POSIX host the
    `None` branch is unreachable, so nothing here would have caught it.
    """
    env = dict(os.environ, PYTHONUTF8="0", LC_ALL=locale_codec, LANG=locale_codec)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PINNED TEXT" in proc.stdout, (
        "pinned decoding must return text; got:\n" + proc.stdout + proc.stderr
    )
    assert "UNPINNED NO-TEXT" in proc.stdout, (
        "the unpinned shape must be shown to yield no usable text here, else this "
        "test would pass on a UTF-8 host and prove nothing:\n"
        + proc.stdout
        + proc.stderr
    )


def _load_script(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader, f"cannot load scripts/{name}.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gh_api_helper_pins_encoding(monkeypatch) -> None:
    """`reader_fix_latency._gh` really passes encoding= to its subprocess.

    The static half names the site; this one drives the function, so a future
    edit that moves the decoding somewhere the AST rule cannot see must still
    keep the behaviour the rule exists to protect.
    """
    module = _load_script("reader_fix_latency")
    seen: dict[str, object] = {}

    class _Proc:
        returncode = 0
        stdout = '{"number": 7}'
        stderr = ""

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen.update(kwargs)
        return _Proc()

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    assert module._gh("repos/o/r/issues?state=closed") == {"number": 7}
    assert seen.get("encoding") == "utf-8", (
        f"`gh api` JSON must not be decoded with the locale codec; got {seen!r}"
    )
    assert seen.get("text") is True
