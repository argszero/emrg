"""One pinned date, *declared* in one place, for every synthetic merge tree.

Background
----------
Tools in the merge-precheck family build a *synthetic* commit (or a chain of them) so
that a folded tree can be named, printed and compared between runs. A commit's sha
contains its dates, so pinning the date is load-bearing: the family's whole cross-tool
vocabulary is a quoted tree sha, and a reviewer quoting one tree while a sibling
measures another is only meaningful if the fold is a function of its inputs. Measured,
with the tree, the parent and the message held identical and only the date changed:

    commit d2280e341648 tree e2da340434754430176a2fcec75ec41641ac06bc  (2000-01-01)
    commit 004377294473 tree e2da340434754430176a2fcec75ec41641ac06bc  (2001-06-06)

Two shas for one tree - and until this guard existed, nothing in the repo enforced that
the three copies agreed (a claim made in three comments with nothing behind it).

What changed (2026-09-14, `cyc20260914-104220`)
-----------------------------------------------
The copies are gone. `scripts/merge_tree.py` declares the constant once and owns
`commit_env()`, the environment every synthetic fold is made with; the gates alias it
(`PLAN_COMMIT_DATE = merge_tree.PLAN_COMMIT_DATE`) or do not name it at all. So the
guard's question changed from "do the copies agree?" to the stronger **"is there only
one?"** - agreement cannot drift when there is nothing to agree with, and a second
literal declaration is now the finding. The three tools that used to declare it are
still asserted to fold with the pin (they alias it, or their comments name the owner's
constant), so a tool that quietly went back to the wall clock is still caught.

What it is not
--------------
It is **not** a proof that a fold is a function of its inputs: this file pins *where the
date comes from*. The behavioural half lives in each tool's own suite
(`test_the_same_plan_folds_to_the_same_commits_even_when_a_second_passes` is the one that
caught the unpinned fold on Windows CI, run 34754517824).
"""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

CONSTANT = "PLAN_COMMIT_DATE"

#: The one pin. Changing it changes every synthetic tree sha this repo quotes, so it is
#: asserted here rather than left to comments that agree by accident.
PIN = "2000-01-01T00:00:00 +0000"

#: Both are in a commit object, so both must be pinned.
DATE_KEYS = ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")

#: The one script that declares it.
OWNER = "merge_tree.py"

#: The tools that fold a synthetic tree. Each must still be in play with the pin.
FAMILY = (
    "check-merge-landing-diff.py",
    "check-merge-plan-suite.py",
    "check-merge-sequence.py",
)


def module_level_strings(path: Path) -> dict[str, str | None]:
    """Module-level string constants of a script, by name.

    A name assigned a non-literal maps to `None`: the value is not visible to a reader
    of the file, which for this constant is itself the finding.
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


def pinnings(directory: Path = SCRIPTS) -> dict[str, str | None]:
    """Every script that assigns the constant at module level, with its value.

    An alias (`PLAN_COMMIT_DATE = merge_tree.PLAN_COMMIT_DATE`) counts as a pin in
    play but not as a declaration: it maps to `None` here, which is why the
    declaration count is taken separately below.
    """
    return {
        path.name: module_level_strings(path)[CONSTANT]
        for path in sorted(directory.glob("*.py"))
        if CONSTANT in module_level_strings(path)
    }


def declarations(directory: Path = SCRIPTS) -> dict[str, str]:
    """The scripts that declare the constant as a *literal*, by name."""
    return {
        name: value
        for name, value in pinnings(directory).items()
        if value is not None
    }


def disagreements(found: dict[str, str | None], expected: str = PIN) -> list[str]:
    """Every reason these declarations are not the one pin this repo claims.

    Kept as a function (and tested directly below) so the finding can be produced
    from a fixture as well as from the tree: a scan that silently stopped matching
    would otherwise be a guard passing on zero measurements.
    """
    problems: list[str] = []
    for name, value in sorted(found.items()):
        if value is None:
            continue  # an alias, not a declaration - the caller counts those itself
        if value != expected:
            problems.append(f"{name}: {CONSTANT} = {value!r}, expected {expected!r}")
    return problems


def test_the_pin_is_declared_once() -> None:
    """The rule the three old copies could not express: there is one, not three."""
    found = declarations()
    assert list(found) == [OWNER], (
        "the pin must be declared exactly once, in "
        f"{OWNER}: declared in {sorted(found)}"
    )
    assert disagreements(found) == []


def test_the_pin_is_one_instant_on_every_machine() -> None:
    """An explicit offset, so the same literal is the same commit everywhere."""
    assert PIN.endswith((" +0000", " -0000")), (
        "a date without an offset is interpreted in the local timezone, so the same "
        "source line would fold to different commits on different machines"
    )
    assert declarations() == {OWNER: PIN}


def test_a_drifted_copy_is_reported() -> None:
    """The arm that makes the guard a guard: one copy re-pinned, the drift named."""
    drifted = {
        "merge_tree.py": PIN,
        "check-merge-sequence.py": "2024-02-29T00:00:00 +0000",
    }
    problems = disagreements(drifted)
    assert len(problems) == 1, problems
    assert "check-merge-sequence.py" in problems[0]
    assert "2024-02-29" in problems[0]


def test_an_alias_is_not_a_declaration() -> None:
    """The refactor's shape: `PLAN_COMMIT_DATE = merge_tree.PLAN_COMMIT_DATE` is a pin
    in play, and must not be counted (or reported) as a second declaration.

    Without this arm `pinnings()` and `declarations()` would be the same function, and
    the "declared once" assertion above would pass by measuring nothing after the
    aliases were introduced.
    """
    found = pinnings()
    assert found[OWNER] == PIN
    aliases = {name: value for name, value in found.items() if name != OWNER}
    assert aliases, "no tool aliases the pin any more - this arm measures nothing"
    assert set(aliases.values()) == {None}, aliases
    assert declarations() == {OWNER: PIN}
    assert disagreements(found) == [], "an alias must not be reported as a drift"


def test_every_family_tool_still_folds_with_the_pin() -> None:
    """Each family script must still have the pin in play, declared *or* aliased.

    A tool that dropped it (folded with the clock instead) is the regression this
    covers; a tool that aliases the owner's constant is the refactor and passes.
    """
    missing = [
        name
        for name in FAMILY
        if CONSTANT not in (SCRIPTS / name).read_text(encoding="utf-8")
    ]
    assert missing == [], missing


def test_every_declarer_applies_the_pin_to_both_dates() -> None:
    """Declaring it is not enough: it has to reach the commit's environment.

    A constant that is declared and never used would satisfy every rule above while
    the synthetic commit went back to the wall clock. The date keys must be found in
    a dict literal whose value is *the constant* - which is exactly the shape
    `merge_tree.commit_env` has.
    """
    for name in sorted(declarations()):
        keys = pinned_keys(SCRIPTS / name)
        assert keys == set(DATE_KEYS), (name, sorted(keys))


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
