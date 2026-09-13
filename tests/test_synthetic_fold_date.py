"""One pinned date, shared by every tool that folds a synthetic merge tree.

Background
----------
Several tools in the merge-precheck family build a *synthetic* commit (or a
chain of them) so that a folded tree can be named, printed and compared between
runs: `check-merge-plan-suite.py`, `check-merge-sequence.py` and
`check-merge-landing-diff.py`. Each of them carries the comment's claim that the
date is pinned rather than read from the clock, and two of the three add that it
is "the same constant ... so they cannot drift apart".

A commit's sha contains its dates, so that claim is load-bearing: the family's
whole cross-tool vocabulary is a quoted tree sha, and a reviewer quoting one
tree and a sibling measuring another is only meaningful if the fold is a
function of its inputs. Measured here, with the tree, the parent and the message
held identical and only the date changed:

    commit d2280e341648 tree e2da340434754430176a2fcec75ec41641ac06bc  (2000-01-01)
    commit 004377294473 tree e2da340434754430176a2fcec75ec41641ac06bc  (2001-06-06)

Two shas for one tree - and nothing in the repo enforced that the three copies
agreed. Each copy is one edit away from a silent divergence, and a divergence is
invisible in every way except the one that matters (two tools reporting
different shas for the same input, with no test to notice).

What the guard is, and what it is not
-------------------------------------
It is **static and cross-script**: every module-level `PLAN_COMMIT_DATE` must be
the same literal, must be a literal at all (a value computed at import time is
the defect the constant exists to prevent), must carry an explicit UTC offset so
it does not mean a different instant on a machine in another timezone, and must
actually be applied to both date variables of the environment a synthetic commit
is made with. A fourth copy added later is covered automatically, and the
coverage floor is asserted, because a scan that silently stopped matching would
be a guard passing on zero measurements.

It is **not** a proof that a fold is a function of its inputs: this file pins
agreement between the pins. The behavioural half lives in each tool's own suite
(`test_the_same_plan_folds_to_the_same_commits_even_when_a_second_passes` is the
one that caught the unpinned fold on Windows CI, run 34754517824).

A tool that stops defining the constant because it now *imports* it from a
sibling still satisfies every rule here - `test_every_family_tool_still_folds_with_the_pin`
asks each family script to mention it, not to declare it - since that is the
consolidation the standing follow-up plans.
"""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

CONSTANT = "PLAN_COMMIT_DATE"

#: The one pin. Changing it changes every synthetic tree sha this repo quotes, so
#: it is asserted here rather than left to five comments that agree by accident.
PIN = "2000-01-01T00:00:00 +0000"

#: Both are in a commit object, so both must be pinned.
DATE_KEYS = ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")

#: The tools that fold a synthetic tree today. A new one is caught by the scan
#: (its pin is checked like any other); this list is what stops the scan from
#: quietly covering nothing.
FAMILY = (
    "check-merge-landing-diff.py",
    "check-merge-plan-suite.py",
    "check-merge-sequence.py",
)


def module_level_strings(path: Path) -> dict[str, str | None]:
    """Module-level string constants of a script, by name.

    A name assigned a non-literal maps to `None`: the value is not visible to a
    reader of the file, which for this constant is itself the finding.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, str | None] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        literal = (
            value.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
            else None
        )
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = literal
    return found


def pinned_keys(path: Path) -> set[str]:
    """Date keys of any dict literal whose value is the pinned constant."""
    keys: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value in DATE_KEYS
                and isinstance(value, ast.Name)
                and value.id == CONSTANT
            ):
                keys.add(str(key.value))
    return keys


def pinnings(directory: Path = SCRIPTS) -> dict[str, str | None]:
    """Every script that assigns the constant at module level, with its value."""
    return {
        path.name: module_level_strings(path)[CONSTANT]
        for path in sorted(directory.glob("*.py"))
        if CONSTANT in module_level_strings(path)
    }


def disagreements(found: dict[str, str | None], expected: str = PIN) -> list[str]:
    """Every reason these pins are not the one pin the family's comments claim."""
    problems: list[str] = []
    for name, value in sorted(found.items()):
        if value is None:
            problems.append(f"{name}: {CONSTANT} is not a literal string")
        elif value != expected:
            problems.append(f"{name}: {CONSTANT} = {value!r}, expected {expected!r}")
    if len(found) < 2:
        problems.append(
            f"only {len(found)} script(s) pin {CONSTANT}: the scan is not covering "
            "the family, so agreement is being reported from too few measurements"
        )
    return problems


def test_the_family_pins_one_date() -> None:
    """The claim both comments make: they cannot drift apart."""
    found = pinnings()
    assert len(found) >= 3, found
    assert disagreements(found) == []


def test_the_pin_is_one_instant_on_every_machine() -> None:
    """An explicit offset, so the same literal is the same commit everywhere."""
    assert PIN.endswith((" +0000", " -0000")), (
        "a date without an offset is interpreted in the local timezone, so the "
        "same source line would fold to different commits on different machines"
    )
    assert all(value == PIN for value in pinnings().values())


def test_a_drifted_copy_is_reported() -> None:
    """The arm that makes the guard a guard: one copy re-pinned, the drift named."""
    drifted = {
        "check-merge-landing-diff.py": PIN,
        "check-merge-plan-suite.py": PIN,
        "check-merge-sequence.py": "2024-02-29T00:00:00 +0000",
    }
    problems = disagreements(drifted)
    assert len(problems) == 1, problems
    assert "check-merge-sequence.py" in problems[0]
    assert "2024-02-29" in problems[0]


def test_a_computed_pin_is_reported() -> None:
    """A pin read from the clock is the defect the constant exists to prevent.

    It cannot be caught by comparing values (`datetime.now()` is equal to itself
    within a run), so the shape is checked: the value must be a literal.
    """
    computed = {
        "check-merge-plan-suite.py": PIN,
        "check-merge-sequence.py": None,
    }
    problems = disagreements(computed)
    assert any("not a literal" in p and "check-merge-sequence.py" in p for p in problems), (
        problems
    )


def test_every_family_tool_still_folds_with_the_pin() -> None:
    """Each family script must still have the pin in play, define or import.

    A tool that dropped it (folded with the clock instead) is the regression this
    covers; a tool that imports it from a sibling is a refactor and passes.
    """
    missing = [
        name
        for name in FAMILY
        if CONSTANT not in (SCRIPTS / name).read_text(encoding="utf-8")
    ]
    assert missing == [], missing


def test_every_definer_applies_the_pin_to_both_dates() -> None:
    """Defining it is not enough: it has to reach the commit's environment.

    A constant that is declared and never used would satisfy every rule above
    while the synthetic commit went back to the wall clock.
    """
    for name in sorted(pinnings()):
        keys = pinned_keys(SCRIPTS / name)
        assert keys == set(DATE_KEYS), (name, sorted(keys))
