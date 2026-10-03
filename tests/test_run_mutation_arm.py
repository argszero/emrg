"""Tests for scripts/run-mutation-arm.py - the arm runner that judges its own arm.

What is being pinned, and why each test is here
-----------------------------------------------
A mutation arm is only evidence if the named test *really ran* and *really failed
because of the mutation*. Every test below pins one way that judgement can be made
wrongly, because all of them look like success to a harness that only asks "did the
run fail?":

* `test_a_node_id_that_collects_nothing_is_refused_before_the_mutation` - the trap
  that has bitten this repository repeatedly: a class method named without its class
  makes pytest exit **4** having run nothing. Five arms were once recorded KILLED
  with none of them executed. The pre-flight is what makes this detectable, so the
  test also asserts the file was never written.
* `test_a_mutation_that_cannot_parse_is_not_a_kill` - a mutation that breaks the
  module's syntax makes pytest error, and an *error* carries the same exit code as a
  failure (measured 2026-09-26: 1 when a fixture loads the module, 2 when collection
  imports it). Without the `--expect` fragment this is indistinguishable from a kill.
* `test_a_failing_run_without_the_expected_assertion_is_unjudgeable` - the same rule
  at the assertion level: an unrelated failure is not this arm's failure.
* `test_a_survivor_is_reported_as_a_survivor` - the control. If every branch returned
  KILLED the suite above would pass and the tool would be useless, so the state that
  means "no coverage" is pinned in its own right.
* `test_every_decisive_run_leaves_the_file_byte_for_byte_as_it_was` - the tool writes
  to the working tree it was pointed at. A failed restore silently deletes work.

The mini tree these run in is built in `tmp_path`: the arms must never mutate this
repository's checkout, which the tests verify by construction (the tool is always
pointed at the temporary tree with `--cwd`).
"""

from __future__ import annotations

import importlib.util
import json
import ast
import re
from pathlib import Path

import pytest

from tests import shell_lines

#: One backslash. Written as `chr(92)` so this file spells **no** backslash literal at all:
#: the defect these tests are about was a literal run of two, and a pin that spells its own
#: subject is a pin a later edit can get wrong the same way. `_BS * 2` cannot.
_BS = chr(92)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run-mutation-arm.py"
DOC = REPO_ROOT / "DEVELOPMENT.md"

SUBJECT = '''"""A tiny subject for the arm runner's own tests."""


def greeting(name: str) -> str:
    """Say hello."""
    return "hello " + name
'''

TEST_FILE = '''import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subject import greeting


class TestGreeting:
    def test_hello(self):
        assert greeting("x") == "hello x"
'''

GREETING = 'return "hello " + name'
HELLO_NODE = "tests/test_subject.py::TestGreeting::test_hello"
BARE_NODE = "tests/test_subject.py::test_hello"


def _load():
    spec = importlib.util.spec_from_file_location("run_mutation_arm", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A miniature checkout: one subject module and one test that covers it."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "subject.py").write_text(SUBJECT, encoding="utf-8")
    (tmp_path / "tests" / "test_subject.py").write_text(TEST_FILE, encoding="utf-8")
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


def _arm(mod, tree: Path, *, old: str, new: str, node: str = HELLO_NODE, expect: str = "hello x",
         json_out: bool = False) -> int:
    argv = [
        "--file", "subject.py",
        "--old", old,
        "--new", new,
        "--node", node,
        "--expect", expect,
        "--cwd", str(tree),
    ]
    if json_out:
        argv.append("--json")
    return mod.main(argv)


class TestTheJudgementOfOneArm:
    """Each branch of the verdict, measured end to end against a real pytest run."""

    def test_a_kill_needs_the_expected_assertion_to_have_run(self, mod, tree, capsys) -> None:
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name')
        out = capsys.readouterr().out
        assert rc == mod.EXIT_KILLED, out
        assert "verdict: KILLED" in out, out
        # The claim is about the assertion, so the report shows the assertion.
        assert "'hello x'" in out, out

    def test_a_survivor_is_reported_as_a_survivor(self, mod, tree, capsys) -> None:
        """The control: an uncovered line is not a kill, and must not read like one."""
        rc = _arm(mod, tree, old='"""Say hello."""', new='"""Say hi."""')
        out = capsys.readouterr().out
        assert rc == mod.EXIT_SURVIVED, out
        assert "verdict: SURVIVED" in out, out
        assert "KILLED" not in out, out

    def test_a_node_id_that_collects_nothing_is_refused_before_the_mutation(
        self, mod, tree, capsys
    ) -> None:
        """The trap: a bare class method exits 4 having run no test at all.

        The pre-flight is the only thing that separates this from a kill, so the test
        pins both halves: the refusal, and that the refusal came *before* any write.
        """
        before = (tree / "subject.py").read_text(encoding="utf-8")
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name', node=BARE_NODE)
        out = capsys.readouterr().out
        assert rc == mod.EXIT_TARGET_BROKEN, out
        assert "verdict: TARGET-BROKEN" in out, out
        # The remedy has to be in the message, or the reader is left with exit 4.
        assert "TestC::test_y" in out, out
        assert (tree / "subject.py").read_text(encoding="utf-8") == before

    def test_a_target_that_ran_nothing_is_refused_even_when_pytest_succeeds(
        self, mod, tree, capsys
    ) -> None:
        """Measured: a target whose tests are all skipped exits **0** with zero passed.

        `rc == 0` alone therefore accepts a target that executed no test, and an arm
        judged on such a run is the exact failure this tool exists to catch - reached
        through another door. The pre-flight also requires that something passed.

        (Found by an arm against this tool: dropping `or passed < 1` SURVIVED all 31
        tests, which is why the state is pinned here.)
        """
        (tree / "tests" / "test_subject.py").write_text(
            "import pytest\n\n\n@pytest.mark.skip(reason='nothing runs')\n"
            "def test_hello():\n    assert False\n",
            encoding="utf-8",
        )
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name',
                  node="tests/test_subject.py::test_hello")
        out = capsys.readouterr().out
        assert rc == mod.EXIT_TARGET_BROKEN, out
        assert "0 passed" in out, out

    def test_a_mutation_that_cannot_parse_is_not_a_kill(self, mod, tree, capsys) -> None:
        """A syntax error is an error, not a failure - pytest does not say which."""
        rc = _arm(mod, tree, old=GREETING, new='return "hello " + (')
        out = capsys.readouterr().out
        assert rc == mod.EXIT_UNJUDGEABLE, out
        assert "verdict: UNJUDGEABLE" in out, out

    def test_a_failing_run_without_the_expected_assertion_is_unjudgeable(
        self, mod, tree, capsys
    ) -> None:
        """A failure that is not this arm's is not evidence for this arm."""
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name',
                  expect="a fragment no assertion prints")
        out = capsys.readouterr().out
        assert rc == mod.EXIT_UNJUDGEABLE, out

    def test_an_unjudgeable_arm_offers_the_assertions_the_run_did_echo(
        self, mod, tree, capsys
    ) -> None:
        """The remedy for a wrong `--expect` is the fragment the run really printed.

        This is the state measured 2026-09-26 (`cyc20260926-140150`): a fragment copied
        from the test's *message* never appears, because an earlier assertion in the same
        test fires first, and the caller then re-runs pytest by hand to find the line that
        does. The report has the output in hand, so it names it - and the assertion below
        is that the named text is usable as `--expect` verbatim, not a paraphrase of it.
        """
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name',
                  expect="a fragment no assertion prints")
        out = capsys.readouterr().out
        assert rc == mod.EXIT_UNJUDGEABLE, out
        assert "any of which can be --expect" in out, out
        assert "assert 'goodbye x' == 'hello x'" in out, out

    def test_a_killed_arm_does_not_gain_that_block(self, mod, tree, capsys) -> None:
        """The control: the expectation matched, so there is nothing to search for.

        Without this half, printing the candidates unconditionally would pass the test
        above while making every kill's report noisier for no reader.
        """
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name')
        out = capsys.readouterr().out
        assert rc == mod.EXIT_KILLED, out
        assert "any of which can be --expect" not in out, out

    def test_the_json_report_carries_the_verdict_and_the_observed_run(
        self, mod, tree, capsys
    ) -> None:
        """`--json` is the other consumer, so it carries the same facts as the prose."""
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name', json_out=True)
        report = json.loads(capsys.readouterr().out)
        assert rc == mod.EXIT_KILLED, report
        assert report["verdict"] == "KILLED"
        # The evidence behind the verdict, not just the verdict.
        assert report["mutated_rc"] == 1, report
        assert report["restored"] is True, report
        assert report["preflight"] == "1 passed", report
        # The candidates are a field on every report, not one the prose happens to print,
        # so a consumer reading the JSON of an UNJUDGEABLE arm gets them too.
        assert report["assertions"], report


class TestTheFileTheArmMutates:
    """The tool writes to a working tree, so what it leaves behind is part of the contract."""

    @pytest.mark.parametrize(
        "old,new,node",
        [
            (GREETING, 'return "goodbye " + name', HELLO_NODE),   # killed
            ('"""Say hello."""', '"""Say hi."""', HELLO_NODE),    # survived
            (GREETING, 'return "hello " + (', HELLO_NODE),        # does not parse
            (GREETING, 'return "goodbye " + name', BARE_NODE),    # refused pre-flight
        ],
    )
    def test_every_decisive_run_leaves_the_file_byte_for_byte_as_it_was(
        self, mod, tree, capsys, old, new, node
    ) -> None:
        before = (tree / "subject.py").read_text(encoding="utf-8")
        _arm(mod, tree, old=old, new=new, node=node)
        capsys.readouterr()
        assert (tree / "subject.py").read_text(encoding="utf-8") == before

    def test_the_run_reports_that_the_restore_happened(self, mod, tree, capsys) -> None:
        """Saying nothing about the restore is how a silent revert goes unnoticed."""
        _arm(mod, tree, old=GREETING, new='return "goodbye " + name')
        assert "restored byte-for-byte: True" in capsys.readouterr().out


class TestTheRefusalsThatNeedNoRun:
    """Arguments that cannot produce an arm at all, answered without invoking pytest."""

    def test_an_anchor_that_is_not_unique_is_refused(self, mod, tree, capsys) -> None:
        """Two candidate sites means the arm mutates something other than it names."""
        (tree / "subject.py").write_text(
            SUBJECT + "\n\ndef other() -> str:\n    " + GREETING + "\n", encoding="utf-8"
        )
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name')
        out = capsys.readouterr().out
        assert rc == mod.EXIT_NO_MUTATION, out
        assert "occurs 2 time(s)" in out, out

    def test_a_replacement_that_changes_nothing_is_not_a_mutation(self, mod, tree, capsys) -> None:
        """An arm whose edit is a no-op would report SURVIVED for having mutated nothing."""
        rc = _arm(mod, tree, old=GREETING, new=GREETING)
        out = capsys.readouterr().out
        assert rc == mod.EXIT_NO_MUTATION, out
        assert "byte-identical" in out, out

    def test_a_missing_file_is_unjudgeable_and_still_reported(self, mod, tree, capsys) -> None:
        rc = mod.main([
            "--file", "absent.py", "--old", "a", "--new", "b",
            "--node", HELLO_NODE, "--expect", "x", "--cwd", str(tree),
        ])
        out = capsys.readouterr().out
        assert rc == mod.EXIT_UNJUDGEABLE, out
        assert "no such file" in out, out


class TestTheReportConventions:
    """The family's rules, applied to a tool that writes to the tree it names."""

    def test_the_tree_is_named_before_the_verdict(self, mod, tree, capsys) -> None:
        """A verdict about a tree that is not named is the defect the family keeps fixing."""
        mod.main([
            "--file", "absent.py", "--old", "a", "--new", "b",
            "--node", HELLO_NODE, "--expect", "x", "--cwd", str(tree),
        ])
        out = capsys.readouterr().out
        assert out.index("tree:") < out.index("verdict:"), out
        assert str(tree) in out, out

    def test_every_verdict_prints_prose_not_only_an_exit_code(self, mod, tree, capsys) -> None:
        """An exit code alone is what this tool exists to refuse; so is its own."""
        for argv in (
            ["--file", "absent.py", "--old", "a", "--new", "b",
             "--node", HELLO_NODE, "--expect", "x"],
            ["--file", "subject.py", "--old", GREETING, "--new", GREETING,
             "--node", HELLO_NODE, "--expect", "x"],
        ):
            capsys.readouterr()
            rc = mod.main(argv + ["--cwd", str(tree)])
            out = capsys.readouterr().out
            assert rc != 0
            assert out.strip(), f"exit {rc} with no report"


class TestTheReasonNamesTheFailure:
    """`_why_unjudgeable` is the whole point of the third state, so it is pinned directly."""

    @pytest.mark.parametrize(
        "rc,needle",
        [
            (2, "collection"),
            (4, "no longer resolves"),
            (5, "no test was collected"),
            (1, "not on the assertion this arm names"),
        ],
    )
    def test_each_pytest_exit_code_gets_its_own_reason(self, mod, rc, needle) -> None:
        reason = mod._why_unjudgeable(rc, "the expected fragment", "")
        assert needle in reason, reason

    def test_the_expected_fragment_is_quoted_in_the_reason_it_concerns(self, mod) -> None:
        assert "the expected fragment" in mod._why_unjudgeable(1, "the expected fragment", "")

    def test_an_unrecognised_exit_code_is_not_given_a_confident_reading(self, mod) -> None:
        assert "separates neither" in mod._why_unjudgeable(99, "x", "")


class TestTheSmallReadings:
    """The parsers the verdict rests on, tested where they are cheap to test."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1 passed in 0.02s", 1),
            ("3 passed, 1 warning in 0.10s", 3),
            ("5 failed, 2 passed in 0.30s", 2),
            ("1 error in 0.06s", 0),
            ("no tests ran in 0.01s", 0),
        ],
    )
    def test_the_passed_count_comes_from_pytests_own_summary(self, mod, text, expected) -> None:
        assert mod._passed_count(text) == expected

    def test_the_failed_node_is_read_from_the_summary_not_guessed(self, mod) -> None:
        out = f"FAILED {HELLO_NODE} - AssertionError: assert 'goodbye x' == 'hello x'\n1 failed"
        assert mod._failed_node(out) == HELLO_NODE

    def test_a_run_with_no_failed_line_names_no_node(self, mod) -> None:
        assert mod._failed_node("1 passed in 0.02s") == ""

    def test_a_non_unique_anchor_has_no_mutation(self, mod) -> None:
        assert mod._apply("x x", "x", "y") is None

    def test_a_unique_anchor_is_replaced_once(self, mod) -> None:
        assert mod._apply("x y", "x", "z") == "z y"

    def test_the_echoed_assertions_are_read_marker_stripped_and_in_order(self, mod) -> None:
        """Both of pytest's echoed forms, and what the reader must *not* be offered.

        The `where` and exception lines are the ones to refuse: they sit in the same
        block, they are not assertions, and a caller who pasted one would come back
        with a second UNJUDGEABLE instead of a verdict.
        """
        report = (
            "FAILED tests/x.py::test_a - AssertionError\n"
            '>           assert mapping["a"] == 2\n'
            "E           assert 0 == 1\n"
            'E            +  where 0 = int("0")\n'
            "E       AttributeError: boom\n"
            "1 failed in 0.05s\n"
        )
        assert mod._assertion_lines(report) == [
            'assert mapping["a"] == 2',
            "assert 0 == 1",
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "1 passed in 0.02s",
            "",
            "E       AttributeError: boom\nE        +  where boom = f()\n",
        ],
    )
    def test_a_run_that_echoed_no_assertion_offers_none(self, mod, text) -> None:
        """Empty is an answer: a collection error has no assertion to hand back."""
        assert mod._assertion_lines(text) == []

    def test_the_candidate_list_is_capped_and_deduplicated(self, mod) -> None:
        """The report is read from a terminal, and a fragment is not a transcript."""
        many = "\n".join(f"E           assert n == {i}" for i in range(20))
        lines = mod._assertion_lines(many)
        assert len(lines) == mod._ASSERTION_CANDIDATES, lines
        assert len(set(lines)) == len(lines), lines
        assert mod._assertion_lines("E    assert x == 1\nE    assert x == 1\n") == [
            "assert x == 1"
        ]

    def test_a_candidate_is_truncated_to_the_length_the_report_allows(self, mod) -> None:
        """A parametrised assertion can be very long; the candidate is bounded like the rest.

        Pinned because the bound is the only thing between a caller's terminal and a
        hypothesis-generated assertion printed at full width - and an unpinned constant
        is one a later edit can drop without any test noticing.
        """
        long_assertion = "assert " + "x" * (mod._ASSERTION_MAX_CHARS * 2) + " == 1"
        (line,) = mod._assertion_lines(f"E           {long_assertion}\n")
        assert len(line) == mod._ASSERTION_MAX_CHARS, len(line)
        assert long_assertion.startswith(line), "the truncation must be a prefix, not a re-wrap"


#: How a reader meets this tool. `DEVELOPMENT.md` is where a reader who is not reading
#: the script's own header finds it, and until this pin existed the tool was named in
#: **no** tracked document at all - cycles that needed an arm wrote one by hand instead,
#: which is the drift the tool's header opens by describing.
_VERDICT_NAMES = ("KILLED", "SURVIVED", "UNJUDGEABLE", "TARGET-BROKEN", "NO-MUTATION",
                  "RESTORE-MISMATCH")


def _documented_invocation(doc: str) -> str:
    """The shell block in `doc` that invokes the arm runner, continuation lines included.

    Located by the tool's own name rather than by line number, so an edit above it cannot
    make this measure a different block; the block ends at the first line that is not a
    continuation **as a shell reads one** — an odd run of trailing backslashes, which is
    `tests/shell_lines.continues`, the one home for that rule.

    Read here as `endswith("\\")` until 2026-10-03, which is `True` for a run of two — so a
    block whose lines ended in `\\` was read as this invocation and answered with all five
    flags, while a shell runs it as three separate commands with the tool receiving no
    arguments at all (measured: `cyc20261003-194054`). The guard was silent on exactly the
    defect its own document had been fixed for one PR earlier.
    """
    lines = doc.splitlines()
    for index, line in enumerate(lines):
        if "run-mutation-arm.py" in line and shell_lines.continues(line):
            block = [line]
            for following in lines[index + 1:]:
                block.append(following)
                if not shell_lines.continues(following):
                    break
            return "\n".join(block)
    return ""


def _lines_naming_the_tool_that_do_not_continue(doc: str) -> list[str]:
    """Lines that name the tool and end a command — the shape a reader mistakes for one.

    Read by `_documented_invocation` above, which returns `""` for it: the invocation is
    named but never continued, so the flags that make it this tool's call are on lines the
    shell runs as separate commands. Named separately so the failure says which of the two
    shapes it is (the document lost the invocation, or the invocation no longer runs),
    because a reader sent to look for a missing paragraph when the real edit was a second
    backslash is a reader sent the wrong way.
    """
    return [
        line
        for line in doc.splitlines()
        if "run-mutation-arm.py" in line and not shell_lines.continues(line)
    ]


def _assert_the_document_invokes_the_arm_runner(text: str) -> None:
    """The guard's body, on any text — so its failure on a broken document is testable.

    Takes the text rather than reading `DOC` itself: the reading below is only exercised
    through the real document, which is correct today, so a guard that had stopped failing
    on a doubled block would leave every test green.
    """
    invocation = _documented_invocation(text)
    named_but_ended = _lines_naming_the_tool_that_do_not_continue(text)
    assert named_but_ended, (
        "the document no longer shows how to run scripts/run-mutation-arm.py - the tool "
        "was documented 2026-10-03 because cycles were hand-rolling arms instead"
    )
    assert invocation.strip(), (
        "the document names scripts/run-mutation-arm.py but does not continue the line, so "
        "a shell runs this as separate commands and the tool receives none of its "
        "arguments. The line that names it ends a command: "
        f"{named_but_ended[0]!r} - a trailing run of backslashes has to be ODD for the "
        "shell to read the next line as part of the same command (two backslashes are one "
        "escaped backslash, and the command ends there)"
    )
    for flag in ("--file", "--old", "--new", "--node", "--expect"):
        assert flag in invocation, f"the documented invocation dropped {flag}"


def _flags_in(text: str) -> set[str]:
    """Every long option spelled in `text`."""
    return set(re.findall(r"--[a-z][a-z0-9-]*", text))


def _documented_tool_flags(doc: str) -> set[str]:
    """The options the documented invocation passes to the **tool**, not to its launcher.

    The block opens with the launcher's own command (`uv run --no-sync python3 ...`), and
    `--no-sync` is `uv`'s flag: reading the whole block would report it as an option the
    tool must accept. So the cut is the tool's own path, which is also the one line that
    makes it this tool's invocation and not another script's.
    """
    invocation = _documented_invocation(doc)
    if "run-mutation-arm.py" not in invocation:
        return set()
    return _flags_in(invocation.split("run-mutation-arm.py", 1)[1])


def _real_flags(mod, capsys) -> set[str]:
    """The tool's own option list, asked of argparse rather than transcribed.

    Read from `--help` because the parser is built inside `main`: a second list here would
    be a copy of the flags, and a copy is what drifts.
    """
    with pytest.raises(SystemExit) as exit_info:
        mod.main(["--help"])
    assert exit_info.value.code == 0, "asking for help must not be an error"
    return _flags_in(capsys.readouterr().out)


def _module_docstring(path: Path) -> str:
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""


def _exit_code_table(text: str) -> dict[str, str]:
    """Code -> verdict, as a header writes it: a digit, then the verdict in capitals."""
    return {code: name for code, name in re.findall(r"^\s*(\d)\s+([A-Z][A-Z0-9-]*)\s", text, re.M)}


class TestTheExitCodeTableIsTheConstants:
    """The header's table is what a caller reads to interpret an exit code, so it is read.

    The judgement is built on these six codes, and the table is prose: correcting a code
    in `EXIT_*` and leaving the header as it was would send a caller to the wrong reading
    with nothing red. That already happened one line over - the header claimed a mutation
    that does not parse exits **4** ("no test ran, the node id does not resolve") while its
    own measured table gives 1 or 2 for it (found 2026-10-03, corrected there).
    """

    def test_the_header_table_matches_the_modules_own_constants(self, mod) -> None:
        expected = {
            str(mod.EXIT_KILLED): mod.KILLED,
            str(mod.EXIT_SURVIVED): mod.SURVIVED,
            str(mod.EXIT_UNJUDGEABLE): mod.UNJUDGEABLE,
            str(mod.EXIT_TARGET_BROKEN): mod.TARGET_BROKEN,
            str(mod.EXIT_NO_MUTATION): mod.NO_MUTATION,
            str(mod.EXIT_RESTORE_MISMATCH): mod.RESTORE_MISMATCH,
        }
        assert _exit_code_table(_module_docstring(SCRIPT)) == expected

    def test_the_table_is_the_family_this_test_reads(self) -> None:
        """The extractor's own shape: a line that is not `digit verdict` is not a row.

        Without this, a regex that matched nothing would leave the comparison above
        comparing two empty dictionaries and passing for a header with no table at all.
        """
        text = (
            "Exit codes\n----------\n"
            "    0  KILLED       the mutation landed\n"
            "a node id that does not resolve         4   (USAGE_ERROR)\n"
            "1. apply the replacement\n"
        )
        assert _exit_code_table(text) == {"0": "KILLED"}


class TestTheDocumentNamesTheTool:
    """`DEVELOPMENT.md` has to name the tool *and* the flags it names have to exist.

    A presence check on the tool's name would accept a paragraph whose invocation has
    been left behind by a rename, so the flags the document spells are compared with the
    ones argparse really has. What it deliberately cannot see: a document that describes
    the tool's behaviour wrongly in prose - only the spelling of the flags is mechanical.
    """

    def test_the_document_invokes_the_arm_runner(self) -> None:
        _assert_the_document_invokes_the_arm_runner(DOC.read_text(encoding="utf-8"))

    def test_the_guard_fires_on_a_block_that_does_not_continue(self) -> None:
        """The guard's own behaviour on the shape it was blind to, not just its parts.

        Without this, the reading above is pinned only through the real document — which is
        correct today — so a guard that could no longer fail on a doubled block would leave
        every test green. Found by an arm against this file: making the assertion accept the
        not-continued shape (`invocation.strip() or named_but_ended`) SURVIVED all 57 tests,
        which is this gap. The document is edited **in memory** here, so nothing on disk
        moves — the mutation that produced the defect is one byte on two lines.
        """
        lines = DOC.read_text(encoding="utf-8").splitlines()
        # The line that **invokes** the tool: the document also names it in the prose
        # paragraph below the block, so "the line naming it" is not one line (measured while
        # writing this test — the first version picked both and failed on its own premise).
        starts = [
            i for i, ln in enumerate(lines)
            if "run-mutation-arm.py" in ln and shell_lines.continues(ln)
        ]
        assert len(starts) == 1, f"expected one invoking line, got {starts}"
        start = starts[0]
        for i in range(start, len(lines)):
            if not shell_lines.continues(lines[i]):
                end = i
                break
        else:  # pragma: no cover - a document that never ends its command cannot be read
            raise AssertionError("the documented invocation never ends")
        doubled = lines[:]
        for i in range(start, end + 1):
            doubled[i] = doubled[i] + _BS
        assert doubled != lines

        with pytest.raises(AssertionError) as failure:
            _assert_the_document_invokes_the_arm_runner("\n".join(doubled))
        assert "does not continue the line" in str(failure.value), (
            "the guard failed for the wrong reason - a doubled backslash must be reported "
            f"as an invocation that does not run, not as a missing paragraph: {failure.value}"
        )

    def test_every_flag_the_document_spells_is_a_flag_the_tool_has(self, mod, capsys) -> None:
        documented = _documented_tool_flags(DOC.read_text(encoding="utf-8"))
        assert documented, "the invocation named no flag at all, so this would measure nothing"
        unknown = documented - _real_flags(mod, capsys)
        assert not unknown, (
            f"DEVELOPMENT.md spells {sorted(unknown)}, which the tool does not accept - a "
            "renamed flag leaves the document showing an invocation that cannot run"
        )

    def test_the_check_would_fire_on_a_flag_the_tool_does_not_have(self, mod, capsys) -> None:
        """The control: without it, a `_flags_in` that returned nothing would pass above."""
        assert _flags_in("--file x --not-a-flag y") - _real_flags(mod, capsys) == {"--not-a-flag"}

    def test_the_document_names_every_verdict_the_tool_can_print(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for name in _VERDICT_NAMES:
            assert name in text, (
                f"a reader who meets the tool in DEVELOPMENT.md cannot interpret "
                f"{name!r} because the document does not name it"
            )

    def test_the_exit_codes_the_document_spells_are_the_tools_own(self, mod) -> None:
        """The `3`/`4`/`5` pairs the document writes are compared with the constants.

        The `0`/`1`/`2` sentence names its three verdicts by order rather than by pair, so
        it is not extractable and the test above covers their presence instead.
        """
        spelled = dict(re.findall(r"`(\d)`\s+([A-Z][A-Z-]+)", DOC.read_text(encoding="utf-8")))
        assert spelled, "no `code` VERDICT pair was found - this assertion would measure nothing"
        for code, name in spelled.items():
            assert int(code) in (
                mod.EXIT_KILLED, mod.EXIT_SURVIVED, mod.EXIT_UNJUDGEABLE,
                mod.EXIT_TARGET_BROKEN, mod.EXIT_NO_MUTATION, mod.EXIT_RESTORE_MISMATCH,
            ), f"{code} is not an exit code this tool can produce"
        expected = {
            str(mod.EXIT_TARGET_BROKEN): mod.TARGET_BROKEN,
            str(mod.EXIT_NO_MUTATION): mod.NO_MUTATION,
            str(mod.EXIT_RESTORE_MISMATCH): mod.RESTORE_MISMATCH,
        }
        assert spelled == expected


class TestTheContinuationTheShellPerforms:
    """The reader behind this file's doc guard, against the shell's own rule.

    Every assertion here is about one byte — how many backslashes end a line — and each is
    paired with what the **shell** does with that shape, so the pair is the evidence. The
    counts are written as `_BS * n`, never as a literal run: a pin on a literal run is a pin
    a later edit can get wrong the same way the defect did.
    """

    @pytest.mark.parametrize("count,expected", [(0, False), (1, True), (2, False), (3, True), (4, False)])
    def test_only_an_odd_run_continues(self, count, expected) -> None:
        """The rule, at the boundary: an even run is whole escaped backslashes."""
        line = "uv run --no-sync python3 scripts/run-mutation-arm.py " + _BS * count
        assert shell_lines.continues(line) is expected
        assert shell_lines.trailing_backslashes(line) == count

    def test_a_backslash_with_whitespace_after_it_continues_nothing(self) -> None:
        """The shape `rstrip()` gets wrong: `x \\ ` escapes the **space**, not the newline.

        Measured 2026-10-03: the shipped reader called this a continuation, because it
        stripped the space away before asking whether the line ended in a backslash — the
        same defect as the doubled run, reached from the other side.
        """
        assert shell_lines.continues("uv run --no-sync python3 x.py " + _BS + " ") is False
        assert shell_lines.trailing_backslashes("x " + _BS + " ") == 0

    def test_the_reader_tells_the_two_shapes_apart(self) -> None:
        """The measurement that produced this class, on the reader it was measured on.

        One byte differs between the docs below. In a shell the first is one command and
        the second is three, so a reader that answers the same for both is not reading the
        document a shell would run — it was answering "3 lines, five flags" for each.
        """
        continued = "\n".join([
            "uv run --no-sync python3 scripts/run-mutation-arm.py " + _BS,
            "    --file <file> --old <text> --new <text> " + _BS,
            "    --node <id> --expect <text>",
        ])
        ended = continued.replace(" " + _BS + "\n", " " + _BS * 2 + "\n")
        assert ended != continued

        assert _documented_invocation(continued).count("\n") == 2
        assert _documented_invocation(ended) == "", (
            "a block whose lines end in TWO backslashes was read as the documented "
            "invocation — a shell runs that as three commands, the first of which passes no "
            "argument to the tool at all"
        )
        # And the reading it must not silently become: flags found in lines the shell never
        # joins. `_documented_tool_flags` follows `_documented_invocation`, so it is empty
        # for the ended shape rather than full. (`--no-sync` is `uv`'s, so the comparison is
        # the tool's own flags — the cut this file already makes for that reason.)
        assert _documented_tool_flags(ended) == set()
        assert _documented_tool_flags(continued) == {
            "--file", "--old", "--new", "--node", "--expect"
        }

    def test_the_document_must_not_be_read_past_the_command_it_ends(self) -> None:
        """The other direction: prose after the block is not part of the invocation.

        A reader that counts "is there a backslash" instead of the run swallows the next
        line, and the flag comparison then **accuses** the document of spelling an option
        for this tool that its invocation never mentions — a false failure whose remedy
        sends the reader to delete a flag that is not in the command.
        """
        doc = "\n".join([
            "uv run --no-sync python3 scripts/run-mutation-arm.py " + _BS,
            "    --file <file> " + _BS * 2,
            "Prose naming another tool's option: --only-in-prose.",
            "uv run --no-sync python3 scripts/run-mutation-arm.py " + _BS,
            "    --file <file>",
        ])
        invocation = _documented_invocation(doc)
        assert "--only-in-prose" not in invocation, (
            "the reader read past the end of the command and counted a prose line as part "
            "of it, so a flag the invocation never spells is reported as one it does"
        )
        assert "--file" in invocation

    def test_a_block_that_ends_mid_command_keeps_its_last_line(self) -> None:
        """An input that stops mid-command is reported by what it has, not by what it lost.

        `commands` is the whole-block mirror of `continues`; a trailing continuation with no
        successor must not drop a line, which is what a reader that only ever looks *ahead*
        would do.
        """
        lines = ["a " + _BS, "b", "c " + _BS]
        assert shell_lines.commands(lines) == [["a " + _BS, "b"], ["c " + _BS]]

    def test_the_guards_message_names_the_shape_it_actually_saw(self) -> None:
        """A doubled backslash is not a missing paragraph, and the remedy differs.

        The guard's failure text used to be one sentence — "no longer shows how to run" —
        which sends the reader looking for a deleted block when the real edit was a second
        backslash on a line that is still there.
        """
        ended = (
            "uv run --no-sync python3 scripts/run-mutation-arm.py " + _BS * 2 + "\n"
            "    --file <file> --old <t> --new <t>\n"
        )
        named = _lines_naming_the_tool_that_do_not_continue(ended)
        assert named, "the reader did not even see the line that names the tool"
        assert _documented_invocation(ended) == ""
