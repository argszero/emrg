"""Every word the write-target walk dispatches on is in the guard set.

The walk visits **every** token and matches its word against the verb sets wherever it
stands — that is what reaches `sudo rm` and `find . -exec rm`. `_WRITE_VERB_WORDS` is
how it asks the shell's own question first: a word that is a verb only in spelling,
standing where the shell would pass it as data, is not an invocation (`_runs_as_a_command`).
Membership in that set is therefore the price of *every* branch in the chain, and the two
edits are made by hand and separately, so a branch can be added without its word.

Measured on master `398e2319`, one row per command through `_extract_write_targets` and
then through both tiers of `_check_sandbox`:

    echo brotli -o <outside>/f x       targets ['<outside>/f']  BLOCK / BLOCK
    grep -rn brotli -o <outside>/f x   targets ['<outside>/f']  BLOCK / BLOCK
    printf %s brotli -o <outside>/f    targets ['<outside>/f']  BLOCK / BLOCK
    echo zip -o <outside>/f x          targets []               ALLOW / ALLOW
    echo pzstd -o <outside>/f x        targets []               ALLOW / ALLOW

The first three write nothing and were refused; the last two are the same shape for words
that *are* in the set. `brotli` was missed because the guard set's last edit (`fcbe224c`,
#1479) predates the branch that reads it (#1528), while `zip` reached the literal set in
the same change that added its branch. The guard is not "the verb is disabled": the same
word where a command can begin is still refused — `brotli -o <outside>/f x` names the
destination at both tiers.

The first test reads the source rather than a list, so it stays true of branches that do
not exist yet: a word compared against `word` and absent from the set is reported by
name. The rest are the rows — one per dispatched word, because the claim is about every
one of them.

No command here executes. `_extract_write_targets` only parses and `_check_sandbox` is a
pure predicate (`realpath`, opening nothing), so `OUTSIDE` is an argument to a predicate
rather than a path a test can damage.
"""

import ast
from pathlib import Path

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import _WRITE_VERB_WORDS, _check_sandbox, _extract_write_targets

# Outside every allowed root (the workspace, the OS temp root, the evolution data dir)
# and used only as an argument to the pure predicate — never executed.
OUTSIDE = "/outside/emrg"

MODULE = Path(bash_tool.__file__)

# The function whose dispatch chain the scan reads. Named here so a rename fails the
# scan loudly instead of passing it vacuously.
WALK = "_extract_write_targets"


def _word_conditions(tree: ast.AST, function_name: str) -> tuple[set[str], set[str]]:
    """The names and literal words one parsed function compares ``word`` against.

    Returns ``(set names, literal words)``. Only the three forms the chain can use are
    read — `word in <NAME>`, `word in {...}` and `word == "<literal>"`; a `not in` /
    `!=` would mean the opposite claim, so it is raised on rather than skipped (the
    scan cannot be right about a chain whose shape it has not been taught).
    """
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    ]
    assert functions, f"{function_name} is not in the scanned source"
    assert len(functions) == 1, f"{function_name} is defined {len(functions)} times"

    names: set[str] = set()
    literals: set[str] = set()
    for node in ast.walk(functions[0]):
        if not isinstance(node, ast.If):
            continue
        for comparison in ast.walk(node.test):
            if not (
                isinstance(comparison, ast.Compare)
                and isinstance(comparison.left, ast.Name)
                and comparison.left.id == "word"
            ):
                continue
            for operator, comparator in zip(comparison.ops, comparison.comparators):
                if isinstance(operator, ast.NotIn) or isinstance(operator, ast.NotEq):
                    raise AssertionError(
                        f"{function_name} now tests `word` with "
                        f"{type(operator).__name__}: a negative condition means the word "
                        "is believed *unless* it is in a set, which is a different claim "
                        "from the one this file guards — re-read the chain before "
                        "extending the scan"
                    )
                if isinstance(operator, ast.In) and isinstance(comparator, ast.Name):
                    names.add(comparator.id)
                elif isinstance(operator, ast.In) and isinstance(comparator, ast.Set):
                    literals.update(
                        element.value
                        for element in comparator.elts
                        if isinstance(element, ast.Constant)
                    )
                elif (
                    isinstance(operator, ast.Eq)
                    and isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, str)
                ):
                    literals.add(comparator.value)
    return names, literals


def _dispatch_conditions() -> tuple[set[str], set[str]]:
    """The words ``_extract_write_targets`` compares against, read from its source."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    return _word_conditions(tree, WALK)


def test_the_scan_reads_the_chain_that_runs() -> None:
    """The scan is only a guard while it is pointed at the real dispatch chain.

    A refactor that renames a set, or a chain that stopped comparing `word` at all,
    would leave the coverage test below green while covering nothing. This pins the
    parts: the branch this change is about, three literal branches, and a floor on the
    number of sets — so the scan cannot quietly shrink to nothing.
    """
    names, literals = _dispatch_conditions()
    assert "_BROTLI_VERBS" in names, sorted(names)
    assert {"zip", "git", "dd", "sed"} <= literals, sorted(literals)
    assert len(names) >= 12, sorted(names)
    assert len(_WRITE_VERB_WORDS) >= 50, len(_WRITE_VERB_WORDS)


def test_the_scan_reads_each_form_the_chain_can_use() -> None:
    """The instrument's own proof: every accepted condition form is really read.

    Measured while writing this file: the chain happens to spell all its branches as
    `word in <NAME>` or `word == "<literal>"`, so the `{...}` form of the scan is never
    reached by today's source — an arm that deleted it survived until this test existed.
    A branch written that way tomorrow would then be dispatched *outside* the guard set
    with the coverage test green, which is the same defect one level down. So the scan
    is run over a synthetic chain containing all three forms, and over one containing a
    negative form, which must be refused rather than silently ignored.
    """
    source = (
        "def _extract_write_targets(tokens, i):\n"
        "    word = tokens[i]\n"
        "    if word in _REMOVER_VERBS:\n"
        "        pass\n"
        "    elif word in {'alpha', 'beta'}:\n"
        "        pass\n"
        "    elif word == 'gamma':\n"
        "        pass\n"
        "    return []\n"
    )
    names, literals = _word_conditions(ast.parse(source), WALK)
    assert names == {"_REMOVER_VERBS"}
    assert literals == {"alpha", "beta", "gamma"}

    negative = source.replace("elif word == 'gamma':", "elif word not in _REMOVER_VERBS:")
    with pytest.raises(AssertionError, match="NotIn"):
        _word_conditions(ast.parse(negative), WALK)


def test_every_dispatched_word_is_in_the_guard_set() -> None:
    """A branch whose word is not in the set is believed in data position.

    The set is the only place the walk asks whether the word stands where a command can
    begin, so membership is what keeps a branch from claiming a *mention* as an
    invocation. Reported per source set, by word, because "some word is missing" is not
    actionable and the missing word is the whole finding.
    """
    names, literals = _dispatch_conditions()
    uncovered: dict[str, list[str]] = {}
    for name in sorted(names):
        members = getattr(bash_tool, name, None)
        # `word in <NAME>` reaches a set of words or the keys of a per-verb table
        # (`_OPTION_DESTINATION_VERBS` maps a verb to its destination options); both
        # forms dispatch the word alike, so both are read the same way here.
        if isinstance(members, dict):
            members = set(members)
        assert isinstance(members, (set, frozenset)), f"{name} is not a set of words"
        missing = sorted(set(members) - _WRITE_VERB_WORDS)
        if missing:
            uncovered[name] = missing
    missing_literals = sorted(literals - _WRITE_VERB_WORDS)
    if missing_literals:
        uncovered["<literal branch>"] = missing_literals
    assert not uncovered, (
        "words dispatched by the write-target walk but absent from _WRITE_VERB_WORDS: "
        f"{uncovered} — each is read as a verb wherever it stands, so a line that only "
        "mentions it is refused"
    )


def test_the_missing_word_this_change_is_about_is_the_brotli_branch() -> None:
    """The measured row, asserted so the fix cannot be undone silently.

    `brotli` is the word this change added, and the branch that reads it is still the
    branch — both halves are the claim, so an edit that dropped either would leave the
    defect back in place with the coverage test satisfied for a *different* reason.
    """
    names, _ = _dispatch_conditions()
    assert "brotli" in _WRITE_VERB_WORDS
    assert "_BROTLI_VERBS" in names
    assert "brotli" in getattr(bash_tool, "_BROTLI_VERBS")


@pytest.mark.parametrize("word", sorted(_WRITE_VERB_WORDS))
def test_a_dispatched_word_in_data_position_names_nothing(word: str) -> None:
    """The claim the guard set buys, one row per word in it.

    `printf %s <word> -o <outside>/f <outside>/g` mentions the verb twice — as a datum
    the run prints and as an option letter's argument. Nothing in the line writes, and
    both tiers must agree. Parametrized by the set itself, so the word added next is
    covered by the same row without an edit here.
    """
    cmd = f"printf %s {word} -o {OUTSIDE}/f {OUTSIDE}/g"
    assert _extract_write_targets(cmd) == []
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is True, f"{word}: {tier} refused a mention ({reason})"


@pytest.mark.parametrize("row,cmd", (
    ("echo", f"echo brotli -o {OUTSIDE}/f x"),
    ("grep", f"grep -rn brotli -o {OUTSIDE}/f x"),
    ("printf", f"printf %s brotli -o {OUTSIDE}/f"),
))
def test_the_measured_mention_rows_are_allowed_at_both_tiers(row: str, cmd: str) -> None:
    """The three rows of the table above, kept as the reproduction they are.

    They are the same shape at three different word positions (last argument, option
    letter's argument, data operand of a reader) because the defect was positional:
    `_runs_as_a_command` said "not a command" in all three and nothing asked it.
    """
    assert _extract_write_targets(cmd) == [], row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is True, f"{row}: {tier} refused a line that writes nothing ({reason})"


def test_the_same_word_where_a_command_can_begin_is_still_refused() -> None:
    """The control: the guard did not disable the verb it was applied to.

    Adding a word to the guard set must not be readable as "this verb names nothing".
    In command position `brotli` keeps its branch, so the destination it writes is
    still named at both tiers — the reading that would make the change above pass while
    re-opening the hole the branch was added for (#1528).
    """
    cmd = f"brotli -o {OUTSIDE}/f x"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/f"]
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is False, f"{tier} allowed a real write to {OUTSIDE}/f"
        assert reason, tier


def test_a_verb_the_walk_reads_after_a_wrapper_is_still_a_command() -> None:
    """Membership reaches the wrapper rows too, which is why the set is a union.

    `sudo brotli …` and `find . -exec brotli …` are what the walk is *for*: the word
    stands where a command can begin (a wrapper prefix, `-exec`), so the guard keeps
    the branch and the write is named. If the guard were "the word must be the first
    token" these would go back to naming nothing.
    """
    for cmd in (f"sudo brotli -o {OUTSIDE}/f x", f"find . -exec brotli -o {OUTSIDE}/f {{}} ;"):
        assert _extract_write_targets(cmd) == [f"{OUTSIDE}/f"], cmd
        allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir="/workspace")
        assert allowed is False, f"{cmd}: {reason}"
