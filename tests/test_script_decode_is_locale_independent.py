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


def _scan_roots() -> list[Path]:
    """Every first-party Python file this rule covers: `scripts/` + the `emrg/` package.

    Vendored trees are excluded by `_VENDORED_DIRS`; the exclusion is asserted
    below, because an exclusion that silently stops matching re-widens the scope.
    """
    files = sorted(SCRIPTS.glob("*.py")) + sorted(PACKAGE.rglob("*.py"))
    files = [p for p in files if not _VENDORED_DIRS & set(p.parts)]
    assert files, f"no files found under {SCRIPTS} or {PACKAGE}"
    first_party = [
        p for p in sorted(PACKAGE.rglob("*.py")) if not _VENDORED_DIRS & set(p.parts)
    ]
    assert len(files) == len(sorted(SCRIPTS.glob("*.py"))) + len(first_party), (
        "the vendored-code exclusion no longer removes anything - check that the "
        "scan is still reading real files and not an empty tree"
    )
    return files


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


def test_the_scan_covers_every_first_party_file_and_no_vendored_one() -> None:
    """The scan's file set equals the first-party set, so its reach is not a guess.

    Two failures this pins, both measured:

    * `emrg/gui/node_modules` contains 12 vendored `.py` files, so `rglob` over
      `emrg/` silently included third-party code whose warnings surfaced in this
      module's output; a guard must not depend on files it may not edit.
    * that tree only exists after `npm install`, so the scan's reach differed
      between a developer machine and CI - the asymmetry this whole module is
      about, one level up.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "emrg", "scripts"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    expected = {
        (REPO_ROOT / t).resolve()
        for t in tracked
        if t.endswith(".py") and not _VENDORED_DIRS & set(Path(t).parts)
    }
    assert expected, "git reported no first-party files - the fixture is broken"
    assert {p.resolve() for p in _scan_roots()} == expected, (
        "the scanned set must be exactly the tracked first-party files; a tracked "
        "file that is not scanned is a silent hole in the rule, and a scanned file "
        "that is not tracked is vendored code"
    )
    vendored = [p for p in PACKAGE.rglob("*.py") if _VENDORED_DIRS & set(p.parts)]
    assert vendored, (
        "no vendored tree was found - if node_modules was removed this assertion "
        "can go, but until then its absence means the exclusion is untested"
    )


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
