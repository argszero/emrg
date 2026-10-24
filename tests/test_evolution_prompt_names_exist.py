"""A prompt that names code must name code that exists.

Why this file exists
--------------------
`emrg/server/evolution_prompt.md` is the instruction set every cycle runs, and it carries one
worked example of its own safety rule: a host path (`~`, `$HOME`) may reach a *pure predicate*
and nothing else. Measured 2026-10-24 on master `ffdf65f1`, that example named `_check_sandbox`,
`check_workspace_write` and `_protected_paths`, and **none of the three was defined anywhere**
under `emrg/` or `tests/`:

    git grep -n -E "def _check_sandbox|def check_workspace_write|def _protected_paths"  -> exit 1

The surviving mentions are the prose recording what those gates used to be
(`emrg/tools/command_scan.py`, `emrg/tools/file_policy.py`) plus the guard that refuses their
return (`tests/test_file_policy_has_one_home.py:77`, "the legacy scanner … must not come back").
So the sentence a reader would use to answer "is my call the permitted kind" named three
functions that cannot be called: the example was unrunnable and nothing noticed, because no
guard read it.

That is the defect class pinned here: **prose naming an identifier is a claim that the
identifier exists**, and a rename is the moment the claim goes false — silently, since the
sentence goes on reading like every verified sentence beside it.

What this measures and what it does not
---------------------------------------
It reads the identifiers out of that one sentence and asserts each resolves to a `def` in the
file the sentence names, so a later rename turns red here instead of rotting in a cycle's
reading. It does not judge whether those functions are *pure*: purity is a property of their
bodies, and `tests/test_file_policy_has_one_home.py` and the sandbox family own behaviour. What
is kept true is that the example points at something a reader can open and call.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"

#: The sentence, bounded by two phrases rather than by a line number: the template is edited
#: above this sentence too, and a line number would point at a different sentence after the
#: next such edit while still resolving — a reader that still finds *something* is worse than
#: one that reports it found nothing.
SENTENCE_START = "may only be inputs to **pure predicates**"
SENTENCE_END = "and must never reach an executed command"

#: The names the example carried until 2026-10-24, none of which this tree defines. Pinned as a
#: negative: restoring one is the same defect as never having fixed it, wherever it is written.
RETIRED_NAMES = ("_check_sandbox", "check_workspace_write", "_protected_paths")

_TOKEN = re.compile(r"`([^`]+)`")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def the_sentence(text: str) -> str:
    """The purity example: from the phrase that opens it to the clause that closes it."""
    start = text.index(SENTENCE_START)
    return text[start:text.index(SENTENCE_END, start)]


def faults(text: str) -> list[str]:
    """Everything wrong with the example, each fault a sentence a reader can act on."""
    try:
        sentence = the_sentence(text)
    except ValueError:
        return [
            "the purity example is not in the template: neither anchor phrase is present, so "
            "this guard has nothing to read. An example that was moved or reworded must be "
            "re-anchored here in the same change — a guard reading nothing is not a pass"
        ]

    tokens = _TOKEN.findall(sentence)
    modules = [token for token in tokens if token.endswith(".py")]
    found: list[str] = []
    if len(modules) != 1:
        return [
            f"the example names {len(modules)} Python file(s) {modules!r}: it must name exactly "
            "one, because that file is where every identifier beside it is looked up"
        ]

    module = REPO_ROOT / modules[0]
    if not module.is_file():
        return [f"the example names {modules[0]!r}, which is not a file in this tree"]

    defined = {
        node.name
        for node in ast.parse(module.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    identifiers = [token for token in tokens if _IDENTIFIER.match(token)]
    if not identifiers:
        found.append(
            f"the example names no callable at all (backticked tokens were {tokens!r}), so it "
            "gives its reader nothing to open"
        )
    found.extend(
        f"{modules[0]} defines no {name!r}, so the example names code that cannot be called"
        for name in identifiers
        if name not in defined
    )

    retired = [name for name in RETIRED_NAMES if name in text]
    if retired:
        found.append(
            f"the template still carries the retired name(s) {retired}: those gates were "
            "deleted, so naming them sends a reader looking for code that is not in the tree"
        )
    return found


def test_the_purity_example_names_predicates_that_exist() -> None:
    """The example a cycle follows must point at something it could call."""
    text = TEMPLATE.read_text(encoding="utf-8")
    found = faults(text)
    assert not found, (
        "emrg/server/evolution_prompt.md states where a host path may travel, and its example "
        "no longer holds: " + "; ".join(found)
    )


def test_the_pin_answers_both_ways() -> None:
    """Three synthetic arms, so the reading above is not silent on a tree with no fault.

    Each arm first asserts the mutation landed: an arm that did not change the text measures
    the control, and its "pass" is the control's verdict reported under another name.
    """
    text = TEMPLATE.read_text(encoding="utf-8")

    # Arm 1 — one identifier renamed, everything else intact.
    renamed = text.replace("`same_file`", "`a_name_no_module_defines`")
    assert renamed != text, "`same_file` is not in the template, so arm 1 measured nothing"
    assert faults(renamed) == [
        "emrg/tools/file_policy.py defines no 'a_name_no_module_defines', so the example names "
        "code that cannot be called"
    ], faults(renamed)

    # Arm 2 — the file the sentence points at is gone.
    moved = text.replace("emrg/tools/file_policy.py", "emrg/tools/gone_away.py")
    assert moved != text, "the example names no file to move, so arm 2 measured nothing"
    assert faults(moved) == [
        "the example names 'emrg/tools/gone_away.py', which is not a file in this tree"
    ], faults(moved)

    # Arm 3 — the defect itself, put back: a retired gate named in the template, outside the
    # example's own span. Written here rather than inside the span on purpose: inside, the
    # identifier and the retired-name checks would both fire, and an arm that proves two
    # things at once cannot show which one discriminates.
    restored = text + "\n- `_check_sandbox` is the predicate to use.\n"
    assert restored != text, "arm 3 did not change the text, so it measures the control"
    assert faults(restored) == [
        "the template still carries the retired name(s) ['_check_sandbox']: those gates were "
        "deleted, so naming them sends a reader looking for code that is not in the tree"
    ], faults(restored)
