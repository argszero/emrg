"""A guard pointed at a tree it can read nothing from must not answer a clean verdict.

The class this file exists for
------------------------------
A guard that reads a tree can be pointed at one it reads nothing from, and the cheapest
possible implementation of that case is to fall through to its success line: the walk
found no subject, so there is nothing to report, so it prints its green verdict. The
reading is then a statement over an empty set dressed as a measurement, which is the one
answer a guard must never give - `Agent.md` states it as *a question it cannot answer is
reported unmeasurable, never as a pass*.

It is not hypothetical, and it has now been fixed twice by hand. Measured 2026-10-05 on
this repository's `master`:

    scripts/check-citation-resolves.py <an empty directory>
      -> rc 0, "every citation names a node id pytest collects"   (fixed in cyc20261005-064033)
    scripts/check_unbound_reads.py --root <an empty directory>
      -> rc 0, "OK: no name is read before its first binding ..."  (fixed in cyc20261005-145352)

Both are the same defect written twice, in two files that do not know about each other,
which is why the rule is stated once here instead of being re-derived by each cycle that
happens to think of it. Each fix was correct on its own; neither could have prevented the
other, and neither prevents the next one - so what is mechanised here is not a guard's
behaviour but the *class*: every guard that takes a tree is pointed at a tree with nothing
in it, and the answer must be `2`, the family's "could not measure".

What is deliberately not asserted here
--------------------------------------
The tree is not required to be *named* in the refusal, and the guards' own suites cover
that (`tests/test_a_tree_reading_guard_names_its_tree.py`) - one rule, one home. Neither
is the exact wording of the message: what discriminates is the exit code, because that is
what a caller branches on, and `0` is the only answer this file is about.

Finding the guards rather than listing them
-------------------------------------------
The set is *derived* from each script's own `--help`, because a hand-written list of
guards is a list that goes stale the moment someone adds one. The derivation is narrow on
purpose: it reads the `usage:` line only, and only the arguments part of it, because the
**program name** carries the same word - `check-merge-tree-health.py` and
`recover-worktree.py` both match a naive search for "tree" in their help while taking no
tree at all. The explicit tuple below is not a substitute for the derivation but a
*second* copy of it: the two are asserted equal, so a guard that grows a `--root` turns
this file red and someone has to point it at an empty tree and see what it says.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

#: The argument that makes a guard a tree reader. Matched against the usage line's
#: *arguments*, never the program name - see the module docstring for the two scripts
#: that word alone would drag in.
_TREE_ARGUMENT = re.compile(r"--root\b|\[root\]")

#: `usage: <program> <arguments>` - the arguments are group 2, and only they are read.
_USAGE_LINE = re.compile(r"^usage:\s+(\S+)\s*(.*)$", re.MULTILINE)

#: The guards this rule ranges over. Kept explicit so that a guard growing a tree
#: argument has to be classified here rather than silently escaping the sweep; the first
#: test asserts it is exactly what the derivation finds.
_TREE_GUARDS = (
    "bump-version.py",
    "check-citation-resolves.py",
    "check-release-tag.py",
    "check-workflows.py",
    "check_unbound_reads.py",
)

#: Guards whose subject cannot be present on every host, so pointing one at a *measurable*
#: tree would measure the host rather than this rule. Named with the reason, because an
#: exclusion without one is how a rule quietly loses its members.
_CANNOT_MEASURE_HERE = {
    "check-workflows.py": (
        "the gate it reads is actionlint, which is not installed on every host (the "
        "repository's CI installs it). Pointed at this checkout without it the guard "
        "answers 2 for a missing tool - true about the host, and not evidence that it "
        "can measure a tree that has a subject"
    ),
}


def _candidate_scripts() -> list[Path]:
    """Every script this rule could range over: the `check*` guards and the version gate.

    `bump-version.py` is not named `check*` and is in scope anyway: it is the guard the
    family's own docstrings credit for the original defect (`check-doc-count.py`'s
    docstring records the 2026-09-11 confident `OK` about a checkout the caller was not
    in), and `--root ... --check` is the mode that reads a tree.
    """
    return sorted(SCRIPTS.glob("check*.py")) + [SCRIPTS / "bump-version.py"]


def _help_arguments(script: Path) -> str:
    """The arguments part of `script`'s usage line, or `""` when it does not parse.

    A script that cannot render `--help` is not thereby a tree reader, so the empty
    string is the honest answer: the derivation is about what the CLI *advertises*.
    """
    proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    match = _USAGE_LINE.search(proc.stdout or "")
    return match.group(2) if match else ""


def _takes_a_tree(script: Path) -> bool:
    """Whether `script`'s own CLI advertises an argument naming a tree."""
    return bool(_TREE_ARGUMENT.search(_help_arguments(script)))


def _tag_for(tree: Path) -> str:
    """The tag to name for `tree`: the version it declares, else a placeholder.

    The value cannot matter for a tree that declares no version - the guard refuses while
    reading the declaration, before any comparison - and hard-coding the tag for a
    measurable tree would rot at the next bump, which is the same defect as a derived
    number written where a guard can measure.
    """
    declaration = tree / "emrg" / "__init__.py"
    if declaration.is_file():
        found = re.search(
            r'__version__\s*=\s*"([^"]+)"', declaration.read_text(encoding="utf-8")
        )
        if found:
            return found.group(1)
    return "0.0.0"


def _invocation(name: str, tree: Path) -> list[str]:
    """How to point guard `name` at `tree`.

    One function for both directions, because the two calls differ only in *which* tree
    they name - a refusal measured with one invocation and a clean reading measured with
    another would be two questions, and the comparison between them would mean nothing.
    """
    if name == "check-citation-resolves.py":
        return [str(tree)]
    if name == "check-release-tag.py":
        return ["--root", str(tree), f"v{_tag_for(tree)}"]
    if name == "bump-version.py":
        return ["--root", str(tree), "--check"]
    return ["--root", str(tree)]


def _run(script: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=str(REPO_ROOT),
    )


def _said(proc: subprocess.CompletedProcess[str]) -> str:
    return f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_the_rule_ranges_over_every_guard_that_takes_a_tree() -> None:
    """The explicit tuple and the derivation must agree, or one of them is stale.

    This is the half that keeps the sweep from going stale: a guard that grows a `--root`
    changes the derived set, so it fails here - and the remedy is to add it to the tuple
    and find out what it answers over a tree with nothing in it, not to widen this test.
    """
    derived = {path.name for path in _candidate_scripts() if _takes_a_tree(path)}
    assert derived == set(_TREE_GUARDS), (
        "the guards whose CLI advertises a tree argument are not the ones this rule "
        f"ranges over. Derived from `--help`: {sorted(derived)}. Listed here: "
        f"{sorted(_TREE_GUARDS)}. A guard that grew a `--root` belongs in the tuple - "
        "point it at an empty tree first, then add it; a guard in the tuple whose "
        "argument went away belongs out of it"
    )


def test_the_program_name_is_not_part_of_what_is_searched() -> None:
    """The split between program and arguments, pinned because a claim needs a reader.

    The module docstring says the derivation reads the arguments and not the program
    name; that is only worth writing down if something measures it. Pinned after a
    mutation arm that removed the split **SURVIVED** - not because the split does nothing
    but because the argument pattern is narrow (`--root`, `[root]`), so today a search of
    the whole usage line happens to find the same set. The split is what keeps that a
    coincidence, and this is the reading that says so.

    Derived from the names rather than listed, so a script named after a tree is covered
    as soon as it exists - and required to be non-empty, because a sweep over no script
    would pass by finding nothing.
    """
    named_after_a_tree = [p for p in sorted(SCRIPTS.glob("*.py")) if "tree" in p.name]
    assert named_after_a_tree, (
        "no script in scripts/ carries `tree` in its name, so this test measured "
        "nothing - `_help_arguments` excluding the program name is a claim about scripts "
        "whose name would otherwise be searched"
    )
    for script in named_after_a_tree:
        searched = _help_arguments(script)
        assert script.name not in searched, (
            f"{script.name}: the program name is part of what `_help_arguments` returns "
            f"({searched!r}), so a name that reads like an argument is now searched as "
            "one - the derivation would classify a guard by what it is called"
        )


def test_every_tree_guard_refuses_an_empty_tree(tmp_path: Path) -> None:
    """Pointed at a tree with nothing to read, each must answer `could not measure`.

    `2` is this family's exit code for that answer, distinct from `0` (clean) and `1` (a
    subject was found and is wrong) - the three states the repository's guards share.
    """
    offenders = []
    for name in sorted(_TREE_GUARDS):
        tree = tmp_path / name
        tree.mkdir()
        proc = _run(SCRIPTS / name, _invocation(name, tree))
        if proc.returncode != 2:
            offenders.append(f"{name} -> rc {proc.returncode}; {_said(proc)}")

    assert not offenders, (
        "a guard answered something other than `could not measure` (2) over an empty "
        "tree. `0` here is a green verdict about a tree nothing was read from, which is "
        "the defect this file exists for; `1` is a fault reported over no evidence. "
        "Measured:\n  " + "\n  ".join(offenders)
    )


def test_a_guard_pointed_at_a_tree_it_can_read_does_not_refuse() -> None:
    """The control: the cheapest way to satisfy the test above is a guard that never passes.

    So each guard is pointed at *this* checkout, where its subject exists, and must not
    answer `could not measure`. `!= 2` rather than `== 0` on purpose: whether the tree is
    clean is the guards' own question and their own suites answer it - what is asserted
    here is only that the refusal is about the empty tree and not about the guard.
    """
    offenders = []
    for name in sorted(set(_TREE_GUARDS) - set(_CANNOT_MEASURE_HERE)):
        proc = _run(SCRIPTS / name, _invocation(name, REPO_ROOT))
        if proc.returncode == 2:
            offenders.append(f"{name} -> rc 2; {_said(proc)}")

    assert not offenders, (
        "a guard could not measure a checkout that has its subject, so the refusal the "
        "test above reads is not evidence about empty trees - it is a guard that refuses "
        "everything. Measured:\n  " + "\n  ".join(offenders)
    )


def test_the_excluded_guards_are_named_with_a_reason() -> None:
    """An exclusion is a claim about a host; a blank one is how a member is lost in silence."""
    for name, reason in _CANNOT_MEASURE_HERE.items():
        assert name in _TREE_GUARDS, (
            f"{name} is excluded from the control but is not in the rule's set, so the "
            "exclusion excludes nothing and will outlive the reason for it"
        )
        assert len(reason.strip()) > 40, (
            f"{name} is excluded from the control with a reason too short to be one: "
            f"{reason!r} - say what the host would have to provide"
        )
