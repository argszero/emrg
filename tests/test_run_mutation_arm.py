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
            (4, "still parses"),
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

    def test_every_failed_node_is_read_not_only_the_first(self, mod) -> None:
        """Which nodes failed is the evidence a refused pre-flight turns on.

        One of them says no more than "something failed"; the whole list is what shows
        whether the failures are the arm's own target or files it never touched.
        """
        out = (
            "FAILED tests/test_bash_v2_boundary.py::test_a - AssertionError\n"
            "FAILED tests/test_check_node_test_count.py::test_b - AssertionError\n"
            "FAILED tests/test_bash_v2_boundary.py::test_c - AssertionError\n"
            "12 failed, 4381 passed in 165.56s\n"
        )
        assert mod._failed_nodes(out) == [
            "tests/test_bash_v2_boundary.py::test_a",
            "tests/test_check_node_test_count.py::test_b",
            "tests/test_bash_v2_boundary.py::test_c",
        ]
        assert mod._failed_nodes("1 passed in 0.02s") == []

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
    backslash continuation.
    """
    lines = doc.splitlines()
    for index, line in enumerate(lines):
        if "run-mutation-arm.py" in line and line.rstrip().endswith("\\"):
            block = [line]
            for following in lines[index + 1:]:
                block.append(following)
                if not following.rstrip().endswith("\\"):
                    break
            return "\n".join(block)
    return ""


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
        invocation = _documented_invocation(DOC.read_text(encoding="utf-8"))
        assert invocation.strip(), (
            "DEVELOPMENT.md no longer shows how to run scripts/run-mutation-arm.py - the "
            "tool was documented 2026-10-03 because cycles were hand-rolling arms instead"
        )
        for flag in ("--file", "--old", "--new", "--node", "--expect"):
            assert flag in invocation, f"the documented invocation dropped {flag}"

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


class TestANonParsingMutationIsNamedNotGuessed:
    """A mutated file that does not parse must be *named*, not read as a bad node id.

    Measured 2026-10-03 (`cyc20261003-202837`), driving one non-parsing mutation through
    this tool and changing only the target form: a **node id** gives pytest exit **4**
    and a **path** gives **2**, because pytest reports an uncollectable node as a usage
    error. The exit-4 branch used to conclude "the target no longer resolves" — an
    assertion about a target the pre-flight had just proved good, sending the caller to
    re-check a node id that was never wrong. Both arms below run the real tool against
    the real mini tree; the node-id row is the one that used to misattribute.
    """

    def test_the_syntax_error_is_read_from_text_that_does_not_parse(self, mod) -> None:
        assert mod._syntax_error("x = 1\n", "subject.py") == ""
        message = mod._syntax_error('return "hello " + (\n', "subject.py")
        assert message, "a file that cannot be compiled must yield a reason"
        assert "line 1" in message, message

    def test_the_reason_puts_the_syntax_error_before_any_exit_code_reading(self, mod) -> None:
        reason = mod._why_unjudgeable(4, "assert", "", syntax_error="'(' was never closed at line 2")
        assert "no longer parses" in reason, reason
        assert "'(' was never closed at line 2" in reason, reason
        assert "usage error" not in reason, (
            "with a syntax error in hand the exit code needs no interpreting, and the "
            f"old reading is exactly what must not be printed: {reason}"
        )

    def test_a_target_that_really_does_not_resolve_keeps_its_reason(self, mod) -> None:
        """The other direction: with a file that still parses, exit 4 is still a bad target."""
        reason = mod._why_unjudgeable(4, "assert", "", syntax_error="")
        assert "still parses" in reason and "test_x.py::TestC::test_y" in reason, reason
        assert "no longer parses" not in reason, reason

    def test_a_non_parsing_mutation_under_a_node_id_names_the_syntax_error(
        self, mod, tree, capsys
    ) -> None:
        """End to end, on the target form that yields exit 4 - the misattributed one."""
        rc = _arm(mod, tree, old=GREETING, new='return "hello " + (')
        out = capsys.readouterr().out
        assert rc == mod.EXIT_UNJUDGEABLE, out
        assert "verdict: UNJUDGEABLE" in out, out
        assert "no longer parses" in out, out
        assert "was never closed" in out, (
            "the report has to name the syntax error itself, not just say the run did "
            f"not separate the outcomes:\n{out}"
        )
        assert "target no longer resolves" not in out, (
            "the node id was proved good by the pre-flight, so this reading is false "
            f"here:\n{out}"
        )

    def test_a_non_parsing_mutation_under_a_path_still_names_it(
        self, mod, tree, capsys
    ) -> None:
        """The same mutation on the other target form, which exits 2 instead of 4.

        Neither form may fall back to a guess: the discriminator is the mutated text,
        not the code the run happened to return.
        """
        rc = _arm(mod, tree, old=GREETING, new='return "hello " + (',
                  node="tests/test_subject.py")
        out = capsys.readouterr().out
        assert rc == mod.EXIT_UNJUDGEABLE, out
        assert "no longer parses" in out and "was never closed" in out, out

    def test_the_tree_is_left_intact_by_a_non_parsing_arm(self, mod, tree, capsys) -> None:
        """The restore is the one thing an unjudgeable arm must never lose."""
        before = (tree / "subject.py").read_text(encoding="utf-8")
        _arm(mod, tree, old=GREETING, new='return "hello " + (')
        capsys.readouterr()
        assert (tree / "subject.py").read_text(encoding="utf-8") == before


def _parse_bullet() -> str:
    """The docstring's bullet about a mutation that does not parse, or "" if it is gone."""
    text = _module_docstring(SCRIPT)
    if "the mutation did not parse" not in text:
        return ""
    return text.split("the mutation did not parse", 1)[1].split("\n* **", 1)[0]


def test_the_parse_bullet_keeps_every_code_that_bullet_has_now_been_wrong_about() -> None:
    """The one part of the header nothing mechanical reads — and it has been wrong twice.

    Its first version gave **4** for a non-parsing mutation (true for a node-id target,
    false for a path one); the version this branch first carried gave 1 or 2 and said it
    is *not* 4 (the reverse, measured 2026-10-03). The behaviour is pinned end to end in
    `TestANonParsingMutationIsNamedNotGuessed`, which is the stronger half; this is the
    weaker one, and it is deliberate: a *presence* check has no false-verdict class when
    the prose goes missing, and it cannot certify that a sentence still present is
    still true. What it does certify is that a future edit cannot drop a code from a
    bullet whose whole job is to state them.
    """
    bullet = _parse_bullet()
    assert bullet, (
        "the docstring no longer has a bullet about a mutation that does not parse - "
        "re-measure before trusting this check's silence"
    )
    for code in ("**1**", "**2**", "**4**"):
        assert code in bullet, f"the parse bullet stopped naming {code}:\n{bullet}"
    assert "node-id" in bullet or "node id" in bullet, (
        "the bullet has to name the target form that decides between 2 and 4, which is "
        f"the distinction it was rewritten for:\n{bullet}"
    )




class TestTheArmRunsUnderAnInterpreterThatCanImportPytest:
    """The interpreter is resolved, not assumed - and the report names the one it used.

    Measured 2026-10-05 (`cyc20261005-212445`), on this host, under a bare `python3`
    (which resolves to the *installed* interpreter, no pytest):

        check-merge-plan-suite._suite_interpreter()  ->  <checkout>/.venv/bin/python
        python3 scripts/run-mutation-arm.py …        ->  TARGET-BROKEN, "check the node id"

    One question - which interpreter runs pytest here - answered two ways, and the answer
    the arm runner gave was a refusal that named a cause which was not the cause. The rule
    now has one implementation: it is asked of the gate that owns it, by file path, the
    same way that gate asks the sequence tool.
    """

    def test_the_resolved_interpreter_can_import_pytest(self, mod) -> None:
        """The property the resolution exists for, measured rather than assumed."""
        resolved = mod._pytest_interpreter()
        assert resolved, "the resolver answered with nothing"
        assert mod._load_gate()._can_import_pytest(resolved), (
            f"the resolved interpreter cannot import pytest, so an arm judged under it "
            f"would measure nothing: {resolved}"
        )

    def test_the_rule_is_asked_of_the_gate_not_reimplemented(self, mod, monkeypatch) -> None:
        """A second implementation is the drift this delegates to avoid.

        Driven through the gate's own function with a sentinel, because the *equality*
        check (`_pytest_interpreter() == gate._suite_interpreter()`) cannot discriminate
        on a machine where both answer with the caller's interpreter - which is every
        machine whose suite can run at all. Measured, this is the arm that does the work:
        hardcoding `sys.executable` survives the equality form and dies here.
        """
        gate = mod._load_gate()
        monkeypatch.setattr(gate, "_suite_interpreter", lambda own=None, root=None: "/sentinel/py")
        assert mod._pytest_interpreter() == "/sentinel/py", (
            "the resolver did not read its answer from the gate, so the rule now has two "
            "implementations - the state this delegation was added to end"
        )

    def test_the_resolution_answers_for_a_layout_it_is_given(self, mod, monkeypatch) -> None:
        """The arguments are forwarded, so the rule is testable off this machine's layout."""
        gate = mod._load_gate()
        seen: list[tuple] = []

        def fake(own=None, root=None):
            seen.append((own, root))
            return "/from/the/gate"

        monkeypatch.setattr(gate, "_suite_interpreter", fake)
        assert mod._pytest_interpreter("/x/python", Path("/y")) == "/from/the/gate"
        assert seen == [("/x/python", Path("/y"))], (
            f"the gate has to be asked the question it can answer: {seen}"
        )

    def test_the_report_names_the_interpreter_and_the_run_uses_it(
        self, mod, tree, capsys, monkeypatch
    ) -> None:
        """Both halves: printed for the reader, and passed to the child that ran."""
        import subprocess

        sentinel = "/nonexistent/sentinel-python"
        seen: list[str] = []

        def fake_run(node, cwd, home, python):
            seen.append(python)
            return subprocess.CompletedProcess(
                args=node, returncode=0, stdout="1 passed in 0.01s", stderr=""
            )

        monkeypatch.setattr(mod, "_pytest_interpreter", lambda *a, **k: sentinel)
        monkeypatch.setattr(mod, "_run_target", fake_run)
        _arm(mod, tree, old=GREETING, new='return "goodbye " + name')
        out = capsys.readouterr().out
        assert f"interpreter: {sentinel}" in out, out
        assert seen and set(seen) == {sentinel}, (
            f"every run must use the resolved interpreter, and both did not: {seen}"
        )

    def test_the_child_command_carries_the_resolved_interpreter(
        self, mod, tmp_path, monkeypatch
    ) -> None:
        """The command line itself, because every other test here stubs the run out.

        Found by an arm, not by reading the code: with only the tests above,
        `[python, "-m", "pytest", ...]` -> `[sys.executable, "-m", "pytest", ...]` in
        `_run_target` **SURVIVED**, since the arms that judge the report monkeypatch
        `_run_target` and never reach the argv it builds. This test spawns nothing - it
        intercepts `subprocess.run` and reads the list - so the pin is on the command, not
        on a stub's return value.
        """
        import subprocess

        seen: dict[str, list[str]] = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = list(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="1 passed", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        mod._run_target(["tests/test_x.py::test_y"], tmp_path, tmp_path, "/resolved/python")
        assert seen["cmd"][0] == "/resolved/python", (
            f"the child must be started with the interpreter the arm was resolved under, "
            f"not the one this process happens to run as: {seen['cmd']}"
        )
        assert seen["cmd"][1:3] == ["-m", "pytest"], seen["cmd"]

    def test_the_json_carries_it_too(self, mod, tree, capsys, monkeypatch) -> None:
        """The machine-readable report is read by callers who see no prose."""
        import subprocess

        monkeypatch.setattr(mod, "_pytest_interpreter", lambda *a, **k: "/sentinel/py")
        monkeypatch.setattr(
            mod, "_run_target",
            lambda node, cwd, home, python: subprocess.CompletedProcess(
                args=node, returncode=0, stdout="1 passed in 0.01s", stderr=""
            ),
        )
        _arm(mod, tree, old=GREETING, new='return "goodbye " + name', json_out=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["interpreter"] == "/sentinel/py", payload


class TestARefusedPreflightNamesTheCauseItFound:
    """A refusal has two causes with different remedies, so the report names which one.

    Measured 2026-10-05 (`cyc20261005-191637`), on this host: `python3
    scripts/run-mutation-arm.py …` - a bare `python3` resolves to the *installed*
    interpreter here, which has no pytest - gave `preflight: refused (rc=1, 0 passed)`
    and a verdict whose only remedy was "check the node id". The node id was fine; the
    same node under `uv run --no-sync pytest` collected and passed. The run's output said
    so ("<python>: No module named pytest"), which is the text these tests drive, and the
    interpreter the message names is the one the run *used* - not `sys.executable`, which
    is no longer what runs when the resolver substitutes the checkout's `.venv`.
    """

    def test_the_missing_pytest_cause_is_named_with_its_interpreter(self, mod) -> None:
        line = f"{mod.sys.executable}: No module named pytest"
        reason = mod._why_target_broken(1, 0, f"collected 0 items\n\n{line}\n", mod.sys.executable)
        assert "No module named pytest" in reason, reason
        assert line in reason, (
            f"the report has to quote the run's own line, not paraphrase it: {reason}"
        )
        assert mod.sys.executable in reason, reason
        assert "uv run --no-sync python3" in reason, (
            f"a cause without its remedy is half a report: {reason}"
        )
        assert "test_x.py::TestC::test_y" not in reason, (
            "the node id was never wrong here, and sending the reader to it is the "
            f"defect this branch exists to remove: {reason}"
        )

    def test_both_spellings_of_the_error_are_read(self, mod) -> None:
        """`-m pytest` prints the unquoted form; a collection ImportError the quoted one.

        One matcher for one spelling would miss the other, and a missed spelling falls
        back to the node-id reading - which is exactly the false remedy above.
        """
        unquoted = mod._why_target_broken(
            1, 0, "/usr/bin/python3: No module named pytest", "/usr/bin/python3"
        )
        quoted = mod._why_target_broken(
            1, 0, "ModuleNotFoundError: No module named 'pytest'", "/usr/bin/python3"
        )
        for reason in (unquoted, quoted):
            assert "cannot import pytest" in reason, reason

    def test_a_target_that_really_failed_keeps_the_node_id_reading(self, mod) -> None:
        """The other direction: with pytest running, the target's own failure stands."""
        reason = mod._why_target_broken(
            1, 0, "FAILED tests/test_subject.py::test_hello - assert 1 == 2", mod.sys.executable
        )
        assert "test_x.py::TestC::test_y" in reason, reason
        assert "cannot import pytest" not in reason, reason
        assert mod.sys.executable in reason, (
            "the interpreter is printed in both branches, so the two causes stay "
            f"separable when the detector does not fire: {reason}"
        )

    def test_the_message_names_the_interpreter_that_ran_not_the_invoked_one(self, mod) -> None:
        """The resolver can substitute one, so `sys.executable` is the wrong thing to name."""
        reason = mod._why_target_broken(
            1, 0, "/somewhere/else/python: No module named pytest", "/checkout/.venv/bin/python"
        )
        assert "/checkout/.venv/bin/python" in reason, reason
        assert mod.sys.executable not in reason, (
            "the message must describe the run that happened, not the interpreter this "
            f"process happens to be: {reason}"
        )

    def test_a_missing_plugin_is_not_read_as_a_missing_pytest(self, mod) -> None:
        """The veto's shape: the same sentence names a module that is not pytest.

        Measured 2026-10-06 (`cyc20261006-131455`, the veto on PR #1861): the matcher had
        no right boundary, so `No module named 'pytest_asyncio'` - pytest imports, a plugin
        does not - was reported as "the interpreter this tool ran it with cannot import
        pytest". A cause the report did not measure is the family's own defect class, so
        every name the run can print is enumerated here rather than the two that happen to
        start with the same letters.
        """
        for module in ("pytest_asyncio", "pytest_timeout", "pytest_cov", "pytest.core"):
            reason = mod._why_target_broken(
                1, 0, f"ModuleNotFoundError: No module named '{module}'", "/usr/bin/python3"
            )
            assert "cannot import pytest" not in reason, (
                f"{module} is not pytest, and reporting it as an interpreter that cannot "
                f"import pytest is a cause nobody measured: {reason}"
            )
            assert module in reason, (
                f"the module the run named has to be in the report - it is the cause, and "
                f"the remedy is an install that only the name can point at: {reason}"
            )
            assert "test_x.py::TestC::test_y" not in reason, (
                "the target never collected, so the node id is not the cause either: "
                f"{reason}"
            )
            assert "uv run --no-sync python3" in reason, (
                f"a cause without its remedy is half a report: {reason}"
            )

    def test_the_missing_module_reader_names_the_module_and_stops_at_it(self, mod) -> None:
        """The reader itself, both spellings and the boundary, on names it must not swallow.

        A greedy pattern would take `pytest.core` for pytest and a boundary-less one takes
        `pytest_asyncio`; the reader is where that is decided, so it is asked directly
        rather than through the message it produces.
        """
        cases = {
            "/usr/bin/python3: No module named pytest": "pytest",
            "ModuleNotFoundError: No module named 'pytest'": "pytest",
            "No module named pytest.": "pytest",
            "ModuleNotFoundError: No module named 'pytest_asyncio'": "pytest_asyncio",
            "ModuleNotFoundError: No module named 'pytest.core'": "pytest.core",
            "ModuleNotFoundError: No module named 'numpy.linalg'": "numpy.linalg",
        }
        for out, expected in cases.items():
            assert mod._missing_module(out) == expected, (
                f"{out!r} names {expected!r}, and reading it as anything else sends the "
                "reader to a remedy for a different failure"
            )
        for out in (
            "ERROR: not found: tests/test_x.py::test_y",
            "collected 0 items / 1 error",
            "FAILED tests/test_x.py::test_y - assert 1 == 2",
        ):
            assert mod._missing_module(out) == "", (
                f"this output names no missing module, and a reader that returns one "
                f"invents a cause: {out!r} -> {mod._missing_module(out)!r}"
            )
        assert "pytest" in mod._missing_module("ModuleNotFoundError: No module named 'pytest'"), (
            "the reader is the one place the module name is read; a rewrite that stops "
            "reading it has to fail here rather than silently widen a message"
        )

    def test_the_report_carries_the_cause_end_to_end(self, mod, tree, capsys, monkeypatch) -> None:
        """Through `main()`, on the output a bare interpreter really produced.

        The run is stubbed rather than spawned: this test is about what the report says,
        and the honest fixture for it is the bytes that interpreter wrote. The resolver is
        stubbed to an interpreter that cannot run pytest, which is the state the message
        describes (after resolution the real one usually can).
        """
        import subprocess

        def fake_run(node, cwd, home, python):
            return subprocess.CompletedProcess(
                args=node,
                returncode=1,
                stdout="",
                stderr=f"{python}: No module named pytest\n",
            )

        monkeypatch.setattr(mod, "_pytest_interpreter", lambda *a, **k: "/bare/python3")
        monkeypatch.setattr(mod, "_run_target", fake_run)
        before = (tree / "subject.py").read_text(encoding="utf-8")
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name')
        out = capsys.readouterr().out
        assert rc == mod.EXIT_TARGET_BROKEN, out
        assert "verdict: TARGET-BROKEN" in out, out
        assert "No module named pytest" in out, out
        assert "/bare/python3" in out, out
        assert "test_x.py::TestC::test_y" not in out, (
            f"the false remedy must not survive in the printed report:\n{out}"
        )
        assert (tree / "subject.py").read_text(encoding="utf-8") == before, (
            "a refused pre-flight must not have written anything"
        )

    def test_the_reason_points_at_the_failing_nodes_when_the_run_named_some(self, mod) -> None:
        """The fourth cause, and the only one the run states in its own output.

        A refusal whose failures are in files the arm never touched is *not* a node-id
        problem and *not* an interpreter problem, and the two remedies above are both
        wrong for it. The nodes are what say so.
        """
        reason = mod._why_target_broken(
            1,
            4381,
            "FAILED tests/test_bash_v2_boundary.py::test_a - assert 1 == 2\n"
            "12 failed, 4381 passed in 165.56s\n",
            "/checkout/.venv/bin/python3",
        )
        assert "already red" in reason, reason
        assert "pre-flight line" in reason, reason

    def test_the_reason_keeps_the_shape_it_had_when_the_run_named_none(self, mod) -> None:
        """The control: a collection error and a missing module name no node, so no clause.

        Without this, the sentence could be printed unconditionally and the report would
        send the reader looking for a list that is not there.
        """
        for out in (
            "ERROR: not found: tests/test_x.py::test_hello\n",
            "/usr/bin/python3: No module named pytest\n",
        ):
            reason = mod._why_target_broken(1, 0, out, "/usr/bin/python3")
            assert "already red" not in reason, reason

    def test_a_refused_preflight_prints_the_nodes_it_failed_on(self, mod, tree, capsys) -> None:
        """End to end, on a tree whose failures are unrelated to the arm's target.

        Measured 2026-10-09 (`cyc20261009-133913`): a full-suite arm was refused with
        `failed node: -` while its output held twenty `FAILED` lines, every one of them in
        another file. The report is the only place a reader could have seen that.
        """
        (tree / "tests" / "test_other.py").write_text(
            "def test_oops():\n    assert 1 == 2\n", encoding="utf-8"
        )
        before = (tree / "subject.py").read_text(encoding="utf-8")
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name', node="tests")
        out = capsys.readouterr().out
        assert rc == mod.EXIT_TARGET_BROKEN, out
        assert "the pre-flight failed before any mutation, on:" in out, out
        assert "tests/test_other.py::test_oops" in out, (
            f"the node that failed is the evidence the verdict turns on:\n{out}"
        )
        assert (tree / "subject.py").read_text(encoding="utf-8") == before, (
            "a refused pre-flight must not have written anything"
        )

    def test_the_json_report_carries_the_failing_nodes(self, mod, tree, capsys) -> None:
        """The other consumer gets them too, as a field rather than only as prose."""
        (tree / "tests" / "test_other.py").write_text(
            "def test_oops():\n    assert 1 == 2\n", encoding="utf-8"
        )
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name', node="tests",
                  json_out=True)
        payload = json.loads(capsys.readouterr().out)
        assert rc == mod.EXIT_TARGET_BROKEN, payload
        assert payload["preflight_failures"] == ["tests/test_other.py::test_oops"], payload

    def test_a_kill_prints_no_preflight_failure_block(self, mod, tree, capsys) -> None:
        """The control for the print: the block belongs to the refusal, not to the report."""
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name')
        out = capsys.readouterr().out
        assert rc == mod.EXIT_KILLED, out
        assert "the pre-flight failed before any mutation" not in out, out


class TestAGateThatWillNotLoadIsAVerdict:
    """The resolution's own failure leaves through the report, not out of `main`.

    Measured 2026-10-06 (`cyc20261006-020931`), on this host, in two shapes and with the
    same outcome: a gate with one unparsable line appended, and a copy of this tool
    standing alone without a gate beside it, each gave **no `verdict:` line at all** and
    exit code **1** - which this file defines as `EXIT_SURVIVED`. A caller that reads the
    exit code therefore read "the target still passed with the mutation in place" out of a
    run that never started, which is the class of defect this file's own contract names:
    "Every verdict leaves through `_report` … an exit code with no prose is the failure
    this family keeps naming".
    """

    @pytest.fixture
    def unloadable_gate(self, mod, tmp_path, monkeypatch):
        """Point `_GATE` at a path that is not there, and forget the cached module.

        The cache is the reason this is a fixture rather than one line in each test: a gate
        already loaded by an earlier test would be returned without touching the disk, and
        the test would then pass without ever exercising the failure.
        """
        monkeypatch.setattr(mod, "_GATE", tmp_path / "check-merge-plan-suite.py")
        monkeypatch.setattr(mod, "_GATE_MODULE", None)
        return mod._GATE

    def test_the_failure_is_a_verdict_and_never_the_survived_code(
        self, mod, tree, capsys, unloadable_gate
    ) -> None:
        before = (tree / "subject.py").read_text(encoding="utf-8")
        rc = _arm(mod, tree, old=GREETING, new='return "goodbye " + name')
        out = capsys.readouterr().out
        assert rc == mod.EXIT_UNJUDGEABLE, out
        assert "verdict: UNJUDGEABLE" in out, out
        assert rc != mod.EXIT_SURVIVED, (
            "the crash used to exit 1, so a caller reading the code read a survivorship "
            "finding it never measured"
        )
        assert (tree / "subject.py").read_text(encoding="utf-8") == before, (
            "the arm stopped before it ran anything, so nothing may have been written"
        )

    def test_the_reason_names_the_gate_it_could_not_load(
        self, mod, tree, capsys, unloadable_gate
    ) -> None:
        """A verdict the reader cannot act on is half a refusal."""
        _arm(mod, tree, old=GREETING, new='return "goodbye " + name')
        out = capsys.readouterr().out
        assert unloadable_gate.name in out, out
        assert "No such file" in out, (
            f"the cause the load really raised has to be quoted: {out}"
        )
        assert "check-merge-plan-suite.py --help" in out, (
            f"a cause without a remedy sends the reader back to guessing: {out}"
        )
        assert "test_x.py::TestC::test_y" not in out, (
            f"the node id was never the subject here: {out}"
        )

    def test_the_json_report_carries_it_too(self, mod, tree, capsys, unloadable_gate) -> None:
        _arm(mod, tree, old=GREETING, new='return "goodbye " + name', json_out=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "UNJUDGEABLE", payload
        assert payload["exit"] == mod.EXIT_UNJUDGEABLE, payload
        assert "No such file" in payload["why"], payload
