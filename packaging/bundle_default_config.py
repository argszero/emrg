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
import os
import re
import sys
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
    (``a\"b``) from escaping the string.
    """
    stripped = raw.strip()
    if stripped in ("true", "false"):
        return stripped
    if _NUMBER.match(stripped):
        return stripped
    escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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


def patch_text(text: str, overrides: dict[str, str]) -> tuple[str, set[str]]:
    """``text`` with the overrides applied inside its ``[llm]`` span.

    Returns the new text and the keys actually written. A requested key that does not occur
    in the span is a :class:`KeyError` — the caller turns it into an error, because the only
    two ways that happens are a typo in a variable name and a template whose key was
    renamed, and both want a human, not a silently un-bundled release.
    """
    span = section_span(text)
    if span is None:
        raise ValueError("no [llm] template span")
    start, end = span
    body = text[start:end]
    written: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        key = match.group("key")
        if key not in overrides:
            return match.group(0)
        written.add(key)
        return f"{key} = {toml_value(overrides[key])}"

    patched = _ASSIGNMENT.sub(_replace, body)
    missing = sorted(set(overrides) - written)
    if missing:
        raise KeyError(missing)
    return text[:start] + patched + text[end:], written


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


def lines_missing(text: str, overrides: dict[str, str]) -> list[str]:
    """The overrides whose assignment is **not** present in ``text``'s ``[llm]`` span.

    This is the reading a write is checked with, and it is not a restatement of the patch:
    ``write_text`` returning is not evidence the value landed, and the failure this step
    exists to prevent — an artifact shipped with no key, which looks exactly like an artifact
    shipped before the feature — is silent. Empty means every override is on disk as the line
    the template now holds.
    """
    span = section_span(text)
    body = text if span is None else text[span[0]:span[1]]
    return sorted(k for k, v in overrides.items() if f"{k} = {toml_value(v)}" not in body)



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
            patched, written = patch_text(text, overrides)
        except KeyError as exc:
            print(
                f"bundled default config: {path} has no {', '.join(exc.args[0])} key in its "
                f"[llm] section — check the {ENV_PREFIX}* variable names",
                file=sys.stderr,
            )
            return 1
        except ValueError:
            print(f"bundled default config: {path} lost its [llm] template span", file=sys.stderr)
            return 1
        if not args.dry_run:
            path.write_text(patched, encoding="utf-8")
            missing = lines_missing(path.read_text(encoding="utf-8"), overrides)
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
