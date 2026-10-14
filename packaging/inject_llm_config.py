#!/usr/bin/env python3
"""Write a release build's `[llm]` defaults into the first-run template carriers.

Usage
-----
    EMRG_LLM_BASE_URL=... EMRG_LLM_MODEL=... EMRG_LLM_API_KEY=... \
        python3 packaging/inject_llm_config.py <carrier> [<carrier> ...]

Why this exists
---------------
A fresh install writes ``~/.emrg/config.toml`` from a first-run template whose
``api_key`` is the placeholder ``sk-...`` (:data:`emrg.config.PLACEHOLDER_API_KEY`),
so a new host meets the setup wizard before the product does anything (issue
#2265). The host asked for EMRG to work as soon as it is installed and supplied
the values to do it with (a base URL, a model and an API key). They are a secret,
so nothing can be done in source -- a key written into the repository is a key in
its history forever. This step is the other half: it takes the values from
**build secrets** and writes them into the carriers a release build packages.

What it patches, and what it leaves alone
-----------------------------------------
Only the three **top-level** ``[llm]`` keys -- ``base_url``, ``api_key`` and
``model`` -- and only within the ``[llm]`` block. The ``[[llm.models]]`` entries
below it carry their own ``model`` (and a matching default), and those must not
move: replacing every ``model = "..."`` in the file would rewrite the switch
list the GUI and ``/model`` read. Each key moves only when **its own** variable
is set, so a build that ships a key and leaves the base URL to the template takes
the same path as one that ships all three.

Byte-identical when no secret is set
------------------------------------
With none of the three variables set the target files are not opened for writing
at all, so the artefacts are byte-identical to a build without this step --
adding the secret *names* to the workflow before configuring their *values* is
safe, and a local build (where the host has set no build secret) never carries a
key.

The carriers are passed as arguments rather than looked up: this script does not
know a checkout's layout, and the same file is a different path in the two places
a release writes it (the runtime stages ``emrg/`` under ``dist/runtime/source/``;
the GUI is packed from the project directory).

Exit codes: 0 injected, or nothing to inject / 1 a carrier did not carry a key it
was asked to patch / 2 could not measure (a carrier could not be read, no carrier
was named, or a value cannot be written into the template).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: The three top-level ``[llm]`` keys this step can set, each with the variable
#: that carries its value. Order is only the order of the report.
FIELDS: tuple[tuple[str, str], ...] = (
    ("base_url", "EMRG_LLM_BASE_URL"),
    ("api_key", "EMRG_LLM_API_KEY"),
    ("model", "EMRG_LLM_MODEL"),
)

#: The section heading every carrier of the template holds. It is matched as a
#: substring, not as a whole line: both carriers put it at the end of the line
#: that opens their template (``CONFIG_TEMPLATE = f"""[llm]`` in python,
#: ``const CONFIG_TEMPLATE = `[llm]`` in the GUI). It is also unambiguous --
#: ``[[llm.models]]`` does not contain the four characters ``[llm]``.
HEADING = "[llm]"


def secret_value(name: str) -> str | None:
    """The value of ``name`` in the environment, or None when it is unset/empty.

    An empty secret (GitHub hands a configured-but-empty one through as ``""``)
    is read as "not set" rather than as "set this key to the empty string": an
    empty ``api_key`` would be a worse first run than the placeholder.
    """
    raw = os.environ.get(name, "").strip()
    return raw or None


def _block_bounds(lines: list[str]) -> tuple[int, int] | None:
    """``[start, end)`` of the top-level ``[llm]`` block's lines, or None.

    ``start`` is the line the heading sits on (its content may begin after the
    marker on that same line); ``end`` is the next line that opens a table
    array (``[[``), which is where ``[[llm.models]]`` starts.
    """
    start = None
    for i, line in enumerate(lines):
        if HEADING in line:
            start = i
            break
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        if lines[j].lstrip().startswith("[["):
            return start, j
    return start, len(lines)


def inject(text: str, fields: list[tuple[str, str | None]]) -> tuple[str, list[str], list[str]]:
    """Return ``(new_text, changed_keys, missing_keys)`` for one carrier.

    ``fields`` is ``(key, value)`` pairs; a ``None`` value means "leave this key
    as the template has it". Line endings are preserved per line, because the
    two carriers are checked out under different rules (a ``.py`` and a ``.js``,
    which ``.gitattributes`` may or may not convert).
    """
    lines = text.split("\n")
    bounds = _block_bounds(lines)
    if bounds is None:
        return text, [], [key for key, value in fields if value is not None]
    start, end = bounds

    changed: list[str] = []
    missing: list[str] = []
    for key, value in fields:
        if value is None:
            continue
        pattern = re.compile(r'^(\s*' + re.escape(key) + r'\s*=\s*")[^"\n]*(")$')
        for i in range(start, end):
            body = lines[i].rstrip("\r")
            eol = lines[i][len(body):]
            match = pattern.match(body)
            if match:
                lines[i] = match.group(1) + value + match.group(2) + eol
                changed.append(key)
                break
        else:
            missing.append(key)
    return "\n".join(lines), changed, missing


def _refuse_unwritable(var: str, value: str) -> str | None:
    """A sentence naming why ``value`` cannot go into the template, or None."""
    bad = [c for c in ('"', "\n", "\r") if c in value]
    if bad:
        shown = ", ".join(repr(c) for c in bad)
        return f"{var} contains {shown}, which cannot be written into a TOML string"
    return None


def main(argv: list[str] | None = None) -> int:
    targets = sys.argv[1:] if argv is None else argv
    if not targets:
        print(
            "could not measure: no carrier was named, so there is nothing to inject "
            "into (pass the template's carriers as arguments)",
            file=sys.stderr,
        )
        return 2

    fields = [(key, secret_value(var)) for key, var in FIELDS]
    wanted = [(key, value) for key, value in fields if value is not None]
    if not wanted:
        print("no LLM build secret is set - nothing injected, artefacts byte-identical")
        return 0

    for key, value in wanted:
        var = dict((k, v) for k, v in FIELDS)[key]
        reason = _refuse_unwritable(var, value)  # type: ignore[arg-type]
        if reason:
            print(f"could not measure: {reason}", file=sys.stderr)
            return 2

    rc = 0
    for target in targets:
        path = Path(target)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"could not measure: {path} - {exc}", file=sys.stderr)
            rc = max(rc, 2)
            continue
        new_text, changed, missing = inject(text, wanted)
        if missing:
            print(
                f"{path} does not carry {', '.join(missing)} in its [llm] block - the "
                f"template changed shape and this step would ship the placeholder; a "
                f"carrier the build can reach must be patched, not skipped",
                file=sys.stderr,
            )
            rc = max(rc, 1)
            continue
        if new_text != text:
            try:
                path.write_text(new_text, encoding="utf-8")
            except OSError as exc:
                print(f"could not measure: {path} - {exc}", file=sys.stderr)
                rc = max(rc, 2)
                continue
        print(f"injected {', '.join(changed)} into {path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
