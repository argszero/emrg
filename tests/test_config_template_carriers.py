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
#
# The placeholder line is looked for in **either** form a carrier may state it in —
# the literal value, or an f-string slot naming the constant. `emrg/config.py` is
# the second kind since issue #2262: it can import the constant, so it interpolates
# it rather than repeating the value twenty lines below it, and its *source* then
# holds `api_key = "{PLACEHOLDER_API_KEY}"` instead of the literal. Both forms are
# built from `PLACEHOLDER_API_KEY` here rather than restated, so the value keeps one
# home in this file too — which is the defect #2262 was filed about.
_FSLOT = "{PLACEHOLDER_API_KEY}"
_PLACEHOLDER_FORMS = (f'api_key = "{PLACEHOLDER_API_KEY}"', f'api_key = "{_FSLOT}"')
_DEFAULT = "auto_compact_threshold = 0.0"

# carrier path -> the pattern that lifts its template literal out of it. Each is
# anchored on `[llm]`, so a match is the template and not a stray quote. The
# optional `f` is what makes `emrg/config.py`'s interpolated template readable at
# all: its literal is `f"""…"""`, and a pattern that assumed a bare triple quote
# would extract nothing from the one carrier that states the value once.
_EXTRACTORS = {
    "emrg/config.py": r'write_text\(f?"""(\[llm\].*?)"""',
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
        if any(form in text for form in _PLACEHOLDER_FORMS) and _DEFAULT in text:
            found.add(rel)
    return found


def _template(rel: str) -> str:
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    match = re.search(_EXTRACTORS[rel], text, re.S)
    assert match is not None, (
        f"no first-run config template could be lifted out of {rel} — the pattern "
        f"{_EXTRACTORS[rel]!r} has gone stale, and a stale pattern is not a pass"
    )
    # The slot is **rendered**, not compared. What a host's file ends up holding is the
    # f-string's output, and `emrg/config.py` is the carrier that states the value as a
    # slot (issue #2262). Parsing the raw slot would compare the literal
    # `{PLACEHOLDER_API_KEY}` against the constant and fail the assertion below — and it
    # would keep passing if the constant were ever changed to that literal, which is the
    # shape that makes a render the honest reading rather than a convenience.
    return match.group(1).replace(_FSLOT, PLACEHOLDER_API_KEY)


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


# ── the GUI's reader, which cannot import the constant ─────────────────────────────
#
# `emrg/gui/main.js::isKeyConfigured` is the **other** reader of the sentinel (G117: it treats the
# placeholder as unconfigured and opens the setup wizard). The census above cannot hold it: that
# census reads *template carriers*, and this is a reader — it does not write a config, it decides
# about one. Measured 2026-10-14 (issue #2262) on master `f33bbc7e`: renaming the comparison there
# left this whole module green, so the two halves of one rule could drift apart silently and
# *consistently* — move the constant, the writers' arm forces every template to follow, the
# refusal follows with the constant, and the GUI stops opening its wizard for the key the product
# actually writes. Two halves agreeing with each other and wrong together is exactly the failure
# a census is supposed to make loud, so this reader gets an arm of its own.
_GUI_READER_FILE = "emrg/gui/main.js"
_GUI_READER = re.compile(r"function isKeyConfigured\(key\)\s*\{(.*?)\n  \}", re.S)


def _code_only(body: str) -> str:
    """`body` with its line comments removed.

    A guard a comment can satisfy is not a guard, and this function names the sentinel in its own
    comment (`// G117：占位符 … 视为未配置`) — so a reading of the raw body would keep passing
    on a function whose comparison had been deleted. Scoped deliberately to one function's body,
    where no `//` occurs inside a string literal.
    """
    return "\n".join(line.split("//")[0] for line in body.splitlines())


def test_the_gui_reader_compares_against_the_placeholder_it_must_recognise():
    """The second reader of the sentinel, held to the same value — in code, not in prose.

    This arm is built to fail in all three directions the value can go missing, and each was
    verified with a mutation arm when it landed: the comparison **renamed** to another value, the
    comparison **dropped** (leaving the comment behind), and the sentinel surviving **only in the
    comment** around where it used to be. The comment strip is what makes the third one fail, and
    the operator check is what stops a body that merely mentions the value somewhere from passing.
    """
    text = (REPO_ROOT / _GUI_READER_FILE).read_text(encoding="utf-8")
    match = _GUI_READER.search(text)
    assert match is not None, (
        f"`isKeyConfigured` could not be lifted out of {_GUI_READER_FILE} — the pattern "
        f"{_GUI_READER.pattern!r} has gone stale, and a stale pattern is not a pass"
    )
    code = _code_only(match.group(1))
    assert PLACEHOLDER_API_KEY in code, (
        f"{_GUI_READER_FILE}'s isKeyConfigured does not compare the key against "
        f"emrg.config.PLACEHOLDER_API_KEY ({PLACEHOLDER_API_KEY!r}) in its code. The comparison "
        "was renamed, dropped, or left only in the comment above it — and the GUI then stops "
        "opening its setup wizard for the key this product writes, while the census above stays "
        "green because it follows the constant rather than this reader."
    )
    assert re.search(r"[!=]=", code), (
        f"{_GUI_READER_FILE}'s isKeyConfigured names the sentinel but states no comparison in its "
        "code — the value has to be *compared*, not merely mentioned"
    )
