"""The first-run ``config.toml`` template has three carriers and one setting set.

``~/.emrg/config.toml`` is created on first run by whichever entry point the host
took, so the same template text is carried by three separate code paths:

* ``emrg/config.py::ensure_config()`` — the CLI / TUI path;
* ``emrg/gui/main.js::CONFIG_TEMPLATE`` — the **packaged** GUI, which inlines its
  copy because it cannot spawn a python there (source mode spawns
  ``ensure_config()`` instead);
* ``install.sh::generate_config_template()`` — the ``curl … | bash`` installer,
  which runs before any of the above and writes the file itself.

Nothing compared them, and ``install.sh``'s copy had already drifted: it was
missing ``vision`` at the top level and on both ``[[llm.models]]`` entries
(measured 2026-10-12 on master ``acc98a19`` — issue #2144). A host who installed
with ``install.sh`` never saw the setting, and the next key added to one carrier
would silently skip the others.

**The carriers are discovered, not listed.** Any tracked file holding both of
the template's own lines — its ``api_key`` placeholder and its
``auto_compact_threshold`` default — is a copy of this template, whatever it is
called and wherever it lives, so a fourth writer copied from one of these is
caught. Two exclusions are rules, named here because both were measured:

* **``*.md``** — a document writes nothing, and ``DEVELOPMENT.md``'s block is
  deliberately a *different* example (its second ``[[llm.models]]`` entry is
  ``gpt-4o`` with ``vision = true``, which is what that segment is there to
  illustrate).
* **``tests/``** — a test writes a config to exercise the product, never to give
  a host one. This module is the reason the rule has to be stated: it names both
  marker lines as data, so without the exclusion the discovery finds *itself* —
  and only once the file is staged, which is the trap
  ``test_the_index_derived_scans_reach_new_files`` exists to catch.

What must agree is the **settings**, not the text: each carrier keeps its own
comments and ordering (``install.sh`` addresses the host per key, the others
document the block once). ``tomllib`` is the reading — the template is TOML, so
the same parse the product performs is what is compared.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

from emrg.config import PLACEHOLDER_API_KEY

REPO_ROOT = Path(__file__).resolve().parents[1]

# The two lines every copy of this template necessarily carries: the placeholder
# secret and the last top-level key. Together they are what makes "this file
# writes the first-run config" decidable from the file's text.
_PLACEHOLDER = 'api_key = "sk-..."'
_DEFAULT = "auto_compact_threshold = 0.0"

# carrier path -> the pattern that lifts its template literal out of it. Each is
# anchored on `[llm]`, so a match is the template and not a stray quote.
_EXTRACTORS = {
    "emrg/config.py": r'write_text\("""(\[llm\].*?)"""',
    "emrg/gui/main.js": r"const CONFIG_TEMPLATE = `(\[llm\].*?)`;",
    "install.sh": r"<<'EMRGCONF'\n(\[llm\].*?)\nEMRGCONF",
}

_ABSENT = object()


def _tracked_files() -> list[str]:
    """Repo-relative paths of every tracked file, via `git ls-files`.

    git's own index rather than a directory walk: it skips the ignored trees
    (`.venv/`, `node_modules/`, `dist/`) and the untracked ones for free, and it
    is what "is published" means.
    """
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return out.stdout.split()


def _discovered_carriers() -> set[str]:
    found = set()
    for rel in _tracked_files():
        # A document writes nothing, and a test writes a config to exercise the
        # product rather than to give a host one — see the module docstring.
        if rel.endswith(".md") or rel.startswith("tests/"):
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _PLACEHOLDER in text and _DEFAULT in text:
            found.add(rel)
    return found


def _template(rel: str) -> str:
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    match = re.search(_EXTRACTORS[rel], text, re.S)
    assert match is not None, (
        f"no first-run config template could be lifted out of {rel} — the pattern "
        f"{_EXTRACTORS[rel]!r} has gone stale, and a stale pattern is not a pass"
    )
    return match.group(1)


def _differences(left, right, prefix: str = "") -> list[str]:
    """Dotted paths where two parsed templates disagree; a missing key counts."""
    if isinstance(left, dict) and isinstance(right, dict):
        out: list[str] = []
        for key in sorted({*left, *right}):
            out += _differences(
                left.get(key, _ABSENT), right.get(key, _ABSENT),
                f"{prefix}.{key}" if prefix else key,
            )
        return out
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{prefix} (length {len(left)} vs {len(right)})"]
        out = []
        for index, (one, other) in enumerate(zip(left, right)):
            out += _differences(one, other, f"{prefix}[{index}]")
        return out
    return [] if left == right else [prefix]


def test_the_carriers_are_the_three_that_are_named_here():
    """Discovery and the extractor table must agree, in both directions.

    A fourth writer copied from one of these carries both marker lines and
    appears here; a carrier that fell out of the table (renamed, reformatted,
    stopped writing the file) appears here too. Either way the next step is a
    human reading this list, not a silent drop.
    """
    assert _discovered_carriers() == set(_EXTRACTORS)


def test_every_carrier_holds_a_template_that_parses():
    """The non-empty baseline: an extraction that finds nothing is not a pass."""
    for rel in _EXTRACTORS:
        template = _template(rel)
        assert template.strip(), f"the template lifted out of {rel} is empty"
        parsed = tomllib.loads(template)
        assert parsed.get("llm"), f"{rel}'s template declares no [llm] section"


def test_the_carriers_configure_the_same_settings():
    """One template, three carriers: the settings must be identical.

    Comments and ordering are each carrier's own; what a host ends up with is
    what this reads.
    """
    settings = {rel: tomllib.loads(_template(rel)) for rel in _EXTRACTORS}
    reference, *rest = sorted(settings)
    for rel in rest:
        assert settings[rel] == settings[reference], (
            f"{rel} and {reference} are two copies of one first-run template and "
            f"must configure the same settings; they differ in "
            f"{', '.join(_differences(settings[reference], settings[rel]))}"
        )


def test_the_carriers_write_the_placeholder_the_request_path_refuses():
    """The writers of the sentinel and the reader that now honours it, held together.

    ``emrg.config.PLACEHOLDER_API_KEY`` is the value ``emrg.server.llm.LlmClient._headers``
    refuses before building a request (issue #2256). If a carrier's template spelled the
    placeholder even slightly differently, that refusal would be dead code with every test
    green: the constant would name a value no file holds, and the CLI first run would go back
    to spending a request on its own placeholder. The comparison is made from the *parsed*
    template, so it reads what a host's config file would hold rather than the file's text.
    """
    for rel in _EXTRACTORS:
        parsed = tomllib.loads(_template(rel))
        written = parsed.get("llm", {}).get("api_key")
        assert written == PLACEHOLDER_API_KEY, (
            f"{rel}'s template stores api_key={written!r}, but "
            f"emrg.config.PLACEHOLDER_API_KEY — the value `_headers` refuses — is "
            f"{PLACEHOLDER_API_KEY!r}; one of the two has to move with the other"
        )
