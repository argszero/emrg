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
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run-mutation-arm.py"

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
