#!/usr/bin/env python3
"""Bundle a working default LLM config into a **staged** runtime, from build secrets.

Why this exists
---------------
A fresh install writes ``~/.emrg/config.toml`` from a template whose ``api_key`` is the
sentinel ``sk-...``, so the first thing a new host meets is a setup wizard (GUI) or a
refused request (CLI). The host asked for EMRG to work immediately after installation
(2026-10-09T21:32:35, ``s_260727_1103_866d``: "希望emrg安装后马上就能用。所以需要内置一个key").

Read this before changing the file, because two facts decide its shape:

* **It is inert unless the environment says otherwise.** With no ``EMRG_BUNDLED_*``
  variable set, nothing is read, nothing is written, and the exit status is 0 — so the
  repository, every test run and every local build keep today's behaviour, and this
  script can only ever add a key to a staged copy that a release build is assembling.
* **The value never reaches git history.** It arrives as a build secret and is written only
  under ``--source-root``: the packaging tree (``$DIST/source``) for the runtime, and — because
  electron-builder packs the packaged GUI's ``main.js`` straight from the working copy — the
  ``emrg/gui`` subtree of a release runner's throwaway checkout. Neither is committed, and
  neither survives the job. A key in a repository is a key in its history forever; a key in a
  build secret can be rotated. That the repository's own carriers keep the sentinel is
  asserted by ``tests/test_bundled_config_injection.py``.
* **One carrier is out of reach, and deliberately so.** ``install.sh`` — the ``curl … | bash``
  installer — is fetched from the repository at run time, so no build secret can reach it, and
  writing a key into it would put one in git history. A host who installs that way still starts
  from the sentinel. The guard names it as an exclusion rather than leaving the gap implicit.

What is patched, and what is deliberately left alone
----------------------------------------------------
The carriers are **discovered, not listed**: every file under the staged root that holds
the first-run template's own ``[llm]`` section is patched, so a carrier added later is
picked up without editing this file. Only the template's ``[llm]`` **span** is rewritten —
the text from the heading ``[llm]`` to the first ``[[llm.models]]`` after it — and that
boundary is what keeps three neighbours untouched, each of which a whole-file search would
have corrupted:

* ``PLACEHOLDER_API_KEY = "sk-..."`` in ``emrg/config.py`` — the **sentinel** the request
  path refuses and the GUI's wizard recognises. Rewriting it would make the product refuse
  the very key it ships and would break the census that holds the carriers together.
* ``key !== "sk-..."`` in ``emrg/gui/main.js`` — the GUI's **reader**, outside the span.
* the ``[[llm.models]]`` entries' own ``model = "…"`` lines — the same key name as the
  top-level one, one section down.

The span is **inside a string literal**, and a value written into it is therefore syntax
before it is data (issue #2272). Both carriers are: the ``emrg/config.py`` span is the body of
an **f-string** (``CONFIG_TEMPLATE``, a triple-quoted f-string) and the ``emrg/gui/main.js``
one is the body of a **template literal** (``CONFIG_TEMPLATE``, backtick-quoted). Each reads
characters the TOML text needs to keep: a brace opens a replacement field in one and a dollar
brace a substitution in the other, and both consume a backslash — so a value written with TOML
escaping alone loses one level of it the moment the carrier's language evaluates the literal,
and a value holding a brace does not evaluate at all. Measured on ``99d593f7``:
``sk-A{b}C9d8e7f6`` made the staged ``config.py`` raise ``NameError`` **on import** — the
released runtime could not read its own config module — while the bundler reported success,
and a value holding a backslash or a double quote produced a span that no longer parsed as
TOML, in both carriers.

So the value is escaped **twice**, and the two layers are named here because getting their
order wrong is the whole defect: :func:`toml_value` produces the *rendered* text (what
``~/.emrg/config.toml`` must hold), and :func:`encode_for_literal` encodes that text for the
literal the file actually contains, with the ``KIND_*`` names deciding which literal that is —
**read from the file**, never guessed, and a literal nobody here can name (a raw or bytes
prefixed one) stops the build rather than being escaped on a hunch. The pair is inverted by
:func:`render_span`, which is what the write is checked with: the check reads the template
back the way the product will read it, so the bytes on disk are no longer mistaken for the
string the carrier yields.

Env vars, one per ``[llm]`` key this bundles (unset ⇒ that key is left as the template has
it; a name that does not occur in the span is an **error**, never a silent no-op, because
that is what a typo in a secret name looks like):

    EMRG_BUNDLED_BASE_URL  EMRG_BUNDLED_API_KEY  EMRG_BUNDLED_MODEL
    EMRG_BUNDLED_MAX_TOKENS  EMRG_BUNDLED_TEMPERATURE  EMRG_BUNDLED_CONTEXT_WINDOW
    EMRG_BUNDLED_AUTO_COMPACT_THRESHOLD  EMRG_BUNDLED_VISION

Usage (the host-side counterpart, for checking a secret before a tag):

    EMRG_BUNDLED_API_KEY=… python3 packaging/bundle_default_config.py \
        --source-root dist/runtime/source --dry-run

Exit status: 0 patched or nothing to do; 1 a fault (the message says which). Values are
never printed — only the key names and the files.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
import tomllib
from pathlib import Path

#: ``[llm]`` key -> the environment variable that overrides it. The rule is "one variable
#: per key, named after the key", so the mapping is readable at a glance and a new key is
#: one line. Order is the order the summary prints.
BUNDLED_KEYS: tuple[str, ...] = (
    "base_url",
    "api_key",
    "model",
    "max_tokens",
    "temperature",
    "context_window",
    "auto_compact_threshold",
    "vision",
)

#: The prefix every variable shares; ``BUNDLED_KEYS`` is derived from it by convention.
ENV_PREFIX = "EMRG_BUNDLED_"

#: The line the template's section opens on. Anchored with the newline because the same
#: characters occur in prose — ``[llm] model``, ``[llm] vision`` — in both carriers'
#: docstrings, and a heading is the only occurrence followed by end-of-line.
SECTION_START = "[llm]\n"

#: The section that ends the span: the template's first ``[[llm.models]]``. Both carriers
#: have one, and the top-level ``model`` key is deliberately not touched past it.
SECTION_END = "\n[[llm.models]]"

#: One assignment inside the span. The keys are bare TOML keys at column 0 in both
#: carriers, so the anchor is exact and a commented or nested line cannot match.
_ASSIGNMENT = re.compile(r"^(?P<key>[a-z_]+) = (?P<value>.*)$", re.MULTILINE)

_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")

#: The kinds of string literal a carrier's span can live inside. Which one a carrier uses is
#: **read from the file** (the quote that opens the literal, and any prefix before it) rather
#: than assumed, because the two layers compose in a different order for each: a value escaped
#: for the wrong one is a value the carrier's language rewrites (issue #2272).
KIND_F_STRING = "python-f-string"
KIND_QUOTED = "quoted-string"
KIND_TEMPLATE = "js-template-literal"

#: The opening of the literal the span sits in, matched against the text *before* the span:
#: ``CONFIG_TEMPLATE = f"""`` → prefix ``f``, quote ``"""``; ``const CONFIG_TEMPLATE = ` `` →
#: prefix ``, quote `` ` ``. An optional prefix is captured rather than ignored, so a literal
#: nobody here can name (``r"""``, ``b"""``) is refused instead of escaped on a hunch.
_LITERAL_OPEN = re.compile(r"(?P<prefix>[A-Za-z]*)(?P<quote>\"\"\"|'''|`|\"|')$")

#: Escapes each kind's language reads, as ``{the character after the backslash: what it
#: yields}`` — read in both directions by :func:`_decode_escapes`, which undoes exactly the
#: pairs :func:`encode_for_literal` writes. The f-string kind is absent on purpose: its
#: inverse is not a table of escapes but CPython itself (:func:`_render_python_literal`),
#: so a kind listed here and nowhere else is a kind this file decodes by hand.
_LITERAL_ESCAPES: dict[str, dict[str, str]] = {
    KIND_QUOTED: {"\\": "\\", '"': '"', "'": "'"},
    KIND_TEMPLATE: {"\\": "\\", "`": "`", "$": "$"},
}

#: What :func:`toml_value` must never leave raw inside a quoted TOML string: the characters
#: TOML reads itself, plus the control characters a secret can carry (a trailing newline is
#: what ``gh secret set KEY < file`` gives) and TOML has no literal form for.
_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


class LiteralError(Exception):
    """A carrier whose span cannot be read, encoded or rendered — always a stopped build.

    Every reason lands here: a span that does not open inside a string literal this file can
    name, a literal whose kind it cannot decide, an escape in the span it does not recognise,
    and a patched template that no longer parses. They are one exception because the remedy is
    the same in each case — stop the release and let a human read the carrier — and because the
    alternative, guessing an escape, is what shipped an unimportable config module.
    """


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The ``[llm]`` keys this run overrides, read from the environment.

    Only variables named for a key in :data:`BUNDLED_KEYS` are read: an unrelated
    ``EMRG_BUNDLED_*`` variable is not a key of this section, and reading it would make a
    typo look like a deliberate configuration. An empty value counts as **unset** — that
    is what an unconfigured GitHub Actions secret evaluates to, and it must mean "leave
    the template alone" rather than "write an empty string".
    """
    env = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for key in BUNDLED_KEYS:
        raw = env.get(ENV_PREFIX + key.upper())
        if raw is not None and raw != "":
            out[key] = raw
    return out


def toml_value(raw: str) -> str:
    """``raw`` as TOML on the right-hand side of an assignment.

    The environment carries everything as text, and the template mixes types: a URL and a
    model name are strings, ``max_tokens`` and ``context_window`` are integers,
    ``temperature`` and ``auto_compact_threshold`` are floats and ``vision`` is a boolean.
    Quoting everything would produce ``max_tokens = "16384"``, which the product's own
    loader rejects; leaving everything bare would produce ``base_url = https://…``, which
    is not TOML at all. So the two literal forms TOML has without quotes are recognised and
    everything else is quoted, which is the only reading that keeps a hostile value
    (``a\\"b``) from escaping the string.

    A quoted value is escaped for **TOML**, not for the literal that will hold this text —
    :func:`encode_for_literal` is the second layer, and it is applied to this function's
    output. Control characters are escaped here because a secret set from a file can carry
    one (``gh secret set KEY < file`` leaves a trailing newline), and a raw newline would end
    the assignment line: the template would stop parsing, which the read-back now refuses.
    """
    stripped = raw.strip()
    if stripped in ("true", "false"):
        return stripped
    if _NUMBER.match(stripped):
        return stripped
    escaped = "".join(
        _TOML_ESCAPES.get(ch)
        or (f"\\u{ord(ch):04x}" if ord(ch) < 0x20 or ord(ch) == 0x7F else ch)
        for ch in raw
    )
    return f'"{escaped}"'


def literal_kind(text: str, start: int) -> str:
    """The kind of string literal the span beginning at ``start`` lives inside.

    Read from the file — the characters immediately before the span's ``[llm]`` heading are
    the literal's opening prefix and quote, and nothing else in the file says what layer the
    value has to survive. An optional prefix this file cannot name (``r``, ``b``) is an
    error rather than a guess: escaping a raw literal's backslashes would write a value the
    carrier does not yield, which is the defect this function exists to prevent.
    """
    match = _LITERAL_OPEN.search(text[:start])
    if match is None:
        raise LiteralError(
            "its [llm] span does not open inside a recognised string literal, so the value "
            "cannot be escaped for it"
        )
    prefix, quote = match.group("prefix").lower(), match.group("quote")
    if quote == "`":
        if prefix:
            raise LiteralError(f"its template literal is prefixed ({prefix!r}), which is not JS")
        return KIND_TEMPLATE
    if prefix == "f":
        return KIND_F_STRING
    if prefix == "":
        return KIND_QUOTED
    raise LiteralError(
        f"its span is inside a {prefix!r}-prefixed string literal, whose escaping this script "
        "cannot name"
    )


def encode_for_literal(rendered: str, kind: str) -> str:
    """``rendered`` as source that this kind of literal evaluates back to.

    :param rendered: the TOML text the product must end up writing — the output of
        :func:`toml_value`, so this is the *second* layer and it runs second.
    :param kind: one of the :data:`KIND_*` names, from :func:`literal_kind`.
    :returns: the characters to put in the file. Order matters inside each branch: a
        backslash is doubled first, so the backslashes the later replacements introduce are
        the language's escapes and are not doubled again.
    """
    if kind == KIND_TEMPLATE:
        # The JS template-literal layer: a backslash escapes, a backtick closes the literal,
        # and ``${`` opens a substitution — which would bake the build machine's own
        # environment into the key that ships. A double quote is not special here, and the
        # backslash TOML put before it is doubled by the first replacement.
        return rendered.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    if kind in (KIND_QUOTED, KIND_F_STRING):
        # A Python string literal, f-string or not: a backslash escapes and both quote
        # characters escape in any of the four delimiters, so escaping the one this literal
        # does not use is harmless. A raw quote left in place would work only while no run of
        # three of them can form — an argument that holds today and cannot be kept by
        # inspection, and which decides whether an ``f'''``-delimited carrier survives a value
        # holding ``'''``.
        encoded = rendered.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")
        if kind == KIND_F_STRING:
            # ... and an f-string reads braces as replacement fields, so they are doubled
            # last: this replacement introduces no backslash, so the order above stays true.
            encoded = encoded.replace("{", "{{").replace("}", "}}")
        return encoded
    raise LiteralError(f"it is inside a {kind!r} literal, which no encoder here covers")


def _decode_escapes(source: str, kind: str) -> str:
    """``source`` with this kind's escapes undone — the inverse of :func:`encode_for_literal`.

    Only the pairs in :data:`_LITERAL_ESCAPES` are undone, and anything else after a
    backslash is a :class:`LiteralError`: the span of a carrier this script has not written
    (a hand-edited template, a new carrier copied from somewhere else) may hold an escape
    whose meaning this file would be guessing at, and guessing is what shipped a broken
    artifact. A carrier that trips this is a carrier a human has to look at.
    """
    pairs = _LITERAL_ESCAPES.get(kind)
    if pairs is None:
        raise LiteralError(
            f"a {kind!r} literal is rendered by its own language, not by undoing escapes"
        )
    out: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        following = source[index + 1:index + 2]
        if following not in pairs:
            raise LiteralError(
                f"the span holds the escape {source[index:index + 2]!r}, which a "
                f"{kind} does not spell that way"
            )
        out.append(pairs[following])
        index += 2
    return "".join(out)


def _module_constants(tree: ast.Module) -> dict[str, object]:
    """Every module-level name in ``tree`` bound to a literal — enough to render its f-strings.

    The template's fields are the module's own constants (``{PLACEHOLDER_API_KEY}``), so
    evaluating the literal needs exactly the values the file itself gives them, read from the
    file and bound to no name at all. Literals that are not simple are skipped: a name this
    cannot resolve simply leaves an f-string that references it unrenderable.
    """
    constants: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            constants[target.id] = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            continue
    return constants


def _render_python_literal(text: str) -> str:
    """The template ``text`` yields when its own literal is evaluated by CPython.

    A real ``eval`` of the assignment's value, not a re-implementation of Python's escaping:
    the defect is precisely that the bytes on disk are not the string the module holds, so the
    reading has to be the language's own. ``__builtins__`` is emptied because this evaluates a
    file the release build stages, and the only names an f-string here may use are the
    module's own constants.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise LiteralError(f"it is not parseable as Python: {exc}") from exc
    constants = _module_constants(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and not isinstance(value.value, str):
            continue
        if not isinstance(value, (ast.JoinedStr, ast.Constant)):
            continue
        try:
            rendered = eval(  # noqa: S307 - the subject is the carrier's own literal
                compile(ast.Expression(value), "<carrier>", "eval"),
                {"__builtins__": {}, **constants},
            )
        except Exception:  # noqa: BLE001 - an unresolvable field is a literal that did not render
            continue
        if isinstance(rendered, str) and SECTION_START in rendered and SECTION_END in rendered:
            return rendered
    raise LiteralError(
        "no string literal in it renders the [llm] span — the template's field could not be "
        "resolved, or the span moved out of the literal"
    )


def render_span(text: str, kind: str) -> str:
    """The carrier's ``[llm]`` span as **its own language** yields it.

    This is what the write is checked against. For the f-string carrier that is CPython's
    evaluation of the literal; for the others it is the exact inverse of
    :func:`encode_for_literal`, applied to the span — the same escaping question, asked from
    the other side, with an escape it does not recognise refused rather than passed through.
    """
    if kind == KIND_F_STRING:
        rendered = _render_python_literal(text)
        span = section_span(rendered)
        if span is None:
            raise LiteralError("the rendered template no longer holds an [llm] span")
        return rendered[span[0]:span[1]]
    span = section_span(text)
    if span is None:
        raise LiteralError("it no longer holds an [llm] span to render")
    return _decode_escapes(text[span[0]:span[1]], kind)


def section_span(text: str) -> tuple[int, int] | None:
    """The ``[llm]`` template span of ``text``, or ``None`` if it is not a carrier.

    ``None`` is a reading, not a failure: most files have no first-run template. The
    failure case — a file that *is* a carrier but whose shape moved — is decided by the
    caller, which knows which files it expected to patch.
    """
    start = text.find(SECTION_START)
    if start < 0:
        return None
    end = text.find(SECTION_END, start)
    if end < 0:
        return None
    return start, end


def patch_text(text: str, overrides: dict[str, str]) -> tuple[str, set[str], str]:
    """``text`` with the overrides applied inside its ``[llm]`` span.

    Returns the new text, the keys actually written, and the kind of literal the span lives
    in (so the caller can render what it wrote without reading the file twice). A requested key
    that does not occur in the span is a :class:`KeyError` — the caller turns it into an error,
    because the only two ways that happens are a typo in a variable name and a template whose
    key was renamed, and both want a human, not a silently un-bundled release. A literal
    :func:`literal_kind` cannot name is a :class:`LiteralError`, likewise a fault of the build.

    Each replacement is :func:`encode_for_literal`'s output, so what lands in the file is the
    text the carrier will *yield* after its own language has read it — never the TOML text
    itself, which the literal would reinterpret.
    """
    span = section_span(text)
    if span is None:
        raise ValueError("no [llm] template span")
    start, end = span
    kind = literal_kind(text, start)
    body = text[start:end]
    written: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        key = match.group("key")
        if key not in overrides:
            return match.group(0)
        written.add(key)
        return encode_for_literal(f"{key} = {toml_value(overrides[key])}", kind)

    patched = _ASSIGNMENT.sub(_replace, body)
    missing = sorted(set(overrides) - written)
    if missing:
        raise KeyError(missing)
    return text[:start] + patched + text[end:], written, kind


def discover(root: Path) -> list[Path]:
    """Every file under ``root`` that carries the first-run template's ``[llm]`` span.

    Discovery rather than a list, for the reason the carrier census already states: a fourth
    writer copied from one of these must be caught without anyone remembering to add it here.
    ``node_modules`` and ``__pycache__`` are skipped because the staged tree may contain them
    and neither can hold a template the product reads — and the test is against the path
    **relative to ``root``**, so a root that happens to live under a directory with one of
    those names (``…/dist/runtime/source`` on a runner, a checkout beside a ``node_modules``)
    is still searched rather than silently answered "no carrier".
    """
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in ("node_modules", "__pycache__", ".git")
               for part in path.relative_to(root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if section_span(text) is not None:
            found.append(path)
    return found


def typed_overrides(overrides: dict[str, str]) -> dict[str, object]:
    """``overrides`` with each value in the type TOML gives it — the comparison's own basis.

    ``vision = "true"`` and ``vision = true`` are different template lines and the same
    setting, so the read-back compares parsed values rather than the text that produced them.
    """
    return {key: tomllib.loads(f"value = {toml_value(raw)}")["value"]
            for key, raw in overrides.items()}


def unlanded_keys(rendered_span: str, overrides: dict[str, str]) -> list[str]:
    """The overrides whose value is **not** in ``rendered_span``, read as TOML.

    This is the reading a write is checked with, and it is not a restatement of the patch:
    ``write_text`` returning is not evidence the value landed, and the failure this step
    exists to prevent — an artifact shipped with no key, which looks exactly like an artifact
    shipped before the feature — is silent. It is checked on the **rendered** span, because
    the span's bytes are not the string the carrier yields (issue #2272): a check over the
    file's text passes on a value the carrier's own language rewrites or refuses.

    A span that does not parse as TOML is a :class:`LiteralError`, not a list of missing keys:
    it is the same class of fault — the artifact would ship a config the product cannot read —
    and the caller reports it with the parse's own words.
    """
    try:
        table = tomllib.loads(rendered_span)["llm"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise LiteralError(f"the rendered [llm] span is not valid TOML: {exc}") from exc
    expected = typed_overrides(overrides)
    return sorted(key for key, value in expected.items() if table.get(key) != value)



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-root", required=True, type=Path,
                        help="the staged source tree to patch (never the checkout)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the carriers and keys, write nothing")
    args = parser.parse_args(argv)

    overrides = env_overrides()
    if not overrides:
        print(f"bundled default config: no {ENV_PREFIX}* variable set — nothing to do")
        return 0

    root: Path = args.source_root
    if not root.is_dir():
        print(f"bundled default config: {root} is not a directory", file=sys.stderr)
        return 1

    carriers = discover(root)
    if not carriers:
        print(
            f"bundled default config: no carrier of the first-run template under {root} — "
            "the template's shape moved, and a silent miss would ship an install with no key",
            file=sys.stderr,
        )
        return 1

    keys = ", ".join(sorted(overrides))
    for path in carriers:
        text = path.read_text(encoding="utf-8")
        try:
            patched, written, kind = patch_text(text, overrides)
        except KeyError as exc:
            print(
                f"bundled default config: {path} has no {', '.join(exc.args[0])} key in its "
                f"[llm] section — check the {ENV_PREFIX}* variable names",
                file=sys.stderr,
            )
            return 1
        except LiteralError as exc:
            print(
                f"bundled default config: {path} cannot be patched — {exc}. Escape its "
                "template by hand and teach this script that literal, or the release would "
                "ship a config the product cannot read.",
                file=sys.stderr,
            )
            return 1
        except ValueError:
            print(f"bundled default config: {path} lost its [llm] template span", file=sys.stderr)
            return 1
        if not args.dry_run:
            path.write_text(patched, encoding="utf-8")
            try:
                rendered = render_span(path.read_text(encoding="utf-8"), kind)
                missing = unlanded_keys(rendered, overrides)
            except LiteralError as exc:
                print(
                    f"bundled default config: {path} was written but {exc} — the value did "
                    "not survive the write, and a half-applied patch is worse than none, so "
                    "the build stops",
                    file=sys.stderr,
                )
                return 1
            if missing:
                print(
                    f"bundled default config: {path} was written but {', '.join(missing)} did "
                    "not land — a half-applied patch is worse than none, so the build stops",
                    file=sys.stderr,
                )
                return 1
        print(f"bundled default config: {'would patch' if args.dry_run else 'patched'} "
              f"{path.relative_to(root)} ({', '.join(sorted(written))})")

    print(f"bundled default config: {len(carriers)} carrier(s), keys: {keys}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
