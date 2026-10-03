"""The arm runner's refusals name what they measured, and show the run they are about.

Two states of `scripts/run-mutation-arm.py` were measured on 2026-10-03 (`cyc20261003-183203`)
to report something other than what they had:

* **the pre-flight refusal discarded its evidence.** The refusal exists for the case where the
  target does not run at all, and its one sentence offered a single cause - "check the node id
  (a class method needs its class)". Run under an interpreter with no `pytest` in it, that
  sentence is wrong, and the child's own words (`No module named pytest`) had been reduced to a
  pass count and thrown away, so nothing in the report could contradict it. The run's output is
  now kept and printed for this verdict, and the report names the interpreter the target ran
  under - which is the fact that separates a mistyped node id from a target that could not run.
* **an anchor that occurs 0 times was reported as a mistyped anchor.** This repository's
  `scripts/*.py` are CRLF in a Windows checkout and `read_text()` reads them with universal
  newlines, so an anchor copied from an editor can never match - and `the anchor occurs 0
  time(s)` reads as "you copied the wrong text". The line ending is now named, when it is the
  difference that holds.

Both are the family's own convention applied to this tool's refusals: a verdict that does not
name what it measured is a verdict the reader has to re-derive by hand.

The mini tree these run in is built in `tmp_path`, and the tool is always pointed at it with
`--cwd`, so no test here can mutate this repository's checkout.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run-mutation-arm.py"

SUBJECT_LF = (
    '"""A tiny subject for the arm runner\'s evidence tests."""\n'
    "\n"
    "\n"
    "def greeting(name: str) -> str:\n"
    '    """Say hello."""\n'
    '    return "hello " + name\n'
)

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
#: A node id pytest cannot resolve: the method exists, but only under its class, so the run
#: collects nothing. This is the refusal the tool's own sentence used to explain alone.
BARE_NODE = "tests/test_subject.py::test_hello"

#: A two-line anchor, in both spellings. `read_text()` reads the file with universal
#: newlines, so the CRLF spelling is the one that can never match.
ANCHOR_LF = 'def greeting(name: str) -> str:\n    """Say hello."""'
ANCHOR_CRLF = ANCHOR_LF.replace("\n", "\r\n")


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
    """A miniature checkout whose subject file is written with CRLF bytes."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "subject.py").write_bytes(
        SUBJECT_LF.replace("\n", "\r\n").encode("utf-8")
    )
    (tmp_path / "tests" / "test_subject.py").write_text(TEST_FILE, encoding="utf-8")
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


def _arm(mod, tree: Path, *, old: str = GREETING, new: str = 'return "goodbye " + name',
         node: str = HELLO_NODE, expect: str = "hello x", json_out: bool = False) -> int:
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


class TestTheRefusalShowsTheRunItRefuses:
    """TARGET-BROKEN is diagnosed from outside this tool, so its evidence has to survive."""

    def test_a_refused_preflight_shows_the_run_it_refused(self, mod, tree, capsys) -> None:
        """The child's own words reach the reader, rather than being counted and dropped."""
        rc = _arm(mod, tree, node=BARE_NODE)
        out = capsys.readouterr().out
        assert rc == mod.EXIT_TARGET_BROKEN, out
        assert "the pre-flight run this refuses printed:" in out, out
        # pytest's own text about the node id it could not find - the sentence that used to
        # be thrown away, and the only one that says *which* node id was wrong.
        assert "ERROR: not found" in out, out
        # ...and the refusal still hands over the remedy it always named.
        assert "TestC::test_y" in out, out

    def test_the_report_names_the_interpreter_that_ran_the_target(
        self, mod, tree, capsys
    ) -> None:
        """A refusal from an interpreter without pytest is not a mistyped node id.

        The tool runs its child under `sys.executable`, so that is the interpreter every
        reading here belongs to - and the one a reader has to check when the pre-flight
        refuses a target that stands up fine under another one.
        """
        _arm(mod, tree, node=BARE_NODE)
        out = capsys.readouterr().out
        assert f"interpreter: {sys.executable}" in out, out
        assert out.index("tree:") < out.index("interpreter:") < out.index("verdict:"), out

    def test_a_killed_arm_gains_none_of_that_output(self, mod, tree, capsys) -> None:
        """The control: a verdict this tool *can* judge is not buried in someone else's words."""
        rc = _arm(mod, tree)
        out = capsys.readouterr().out
        assert rc == mod.EXIT_KILLED, out
        assert "the pre-flight run this refuses printed:" not in out, out

    def test_the_json_report_carries_the_interpreter_and_the_run(
        self, mod, tree, capsys
    ) -> None:
        """`--json` is the other consumer, so the same two facts are fields on it."""
        rc = _arm(mod, tree, node=BARE_NODE, json_out=True)
        report = json.loads(capsys.readouterr().out)
        assert rc == mod.EXIT_TARGET_BROKEN, report
        assert report["interpreter"] == sys.executable, report
        assert "ERROR: not found" in report["preflight_output"], report

    def test_a_judgeable_arm_carries_no_run_output_in_its_json(self, mod, tree, capsys) -> None:
        """The control for the field: empty is what a run that was not refused leaves."""
        _arm(mod, tree, json_out=True)
        report = json.loads(capsys.readouterr().out)
        assert report["verdict"] == mod.KILLED, report
        assert report["preflight_output"] == "", report


class TestAnAnchorThatOccursNowhere:
    """`0 time(s)` has two causes, and only one of them is a mistyped anchor."""

    def test_a_crlf_anchor_names_the_line_ending_it_differs_by(self, mod, tree, capsys) -> None:
        """The measured trap: an anchor copied out of an editor can never match this reader."""
        assert ANCHOR_LF in SUBJECT_LF  # the premise: the anchor is really in the file
        rc = _arm(mod, tree, old=ANCHOR_CRLF)
        out = capsys.readouterr().out
        assert rc == mod.EXIT_NO_MUTATION, out
        assert "occurs 0 time(s)" in out, out
        assert "universal newlines" in out, out
        assert "CRLF on disk" in out, out

    def test_the_same_anchor_spelled_the_way_the_reader_reads_it_works(
        self, mod, tree, capsys
    ) -> None:
        """The other direction, so the note above is a reading and not a constant.

        SURVIVED is the signal that the anchor *matched*: nothing covers the docstring it
        changes, so the arm is a survivor rather than a kill - and either verdict is
        evidence that the anchor was found, which is the only thing this control claims.
        """
        rc = _arm(mod, tree, old=ANCHOR_LF,
                  new='def greeting(name: str) -> str:\n    """Say hi."""')
        out = capsys.readouterr().out
        assert rc == mod.EXIT_SURVIVED, out
        assert "occurs 0 time(s)" not in out, out
        assert "universal newlines" not in out, out

    def test_an_anchor_the_file_lacks_is_not_blamed_on_line_endings(
        self, mod, tree, capsys
    ) -> None:
        """The control: a genuinely absent anchor is reported as absent, and nothing else.

        Written because the tempting reader is "the anchor has CRLF in it, so say CRLF" -
        which would be right about the spelling and wrong about the file, on every anchor
        the file really does not carry.
        """
        rc = _arm(mod, tree, old="a line\r\nthat is not in this file")
        out = capsys.readouterr().out
        assert rc == mod.EXIT_NO_MUTATION, out
        assert "occurs 0 time(s)" in out, out
        assert "universal newlines" not in out, out

    def test_an_anchor_that_occurs_twice_is_not_given_a_line_ending_story(
        self, mod, tree, capsys
    ) -> None:
        """The other branch of `occurrences == 0`: two sites is a different remedy entirely."""
        (tree / "subject.py").write_bytes(
            (SUBJECT_LF + "\n\ndef other() -> str:\n    " + GREETING + "\n")
            .replace("\n", "\r\n")
            .encode("utf-8")
        )
        rc = _arm(mod, tree)
        out = capsys.readouterr().out
        assert rc == mod.EXIT_NO_MUTATION, out
        assert "occurs 2 time(s)" in out, out
        assert "universal newlines" not in out, out

    def test_the_helper_answers_empty_when_there_is_nothing_to_say(self, mod, tree) -> None:
        """The note's own contract, read directly: no CRLF, or no LF-form match, is silence."""
        lf_only = (tree / "subject.py").read_text(encoding="utf-8")
        assert mod._anchor_line_ending_note("no CRLF here", lf_only, tree / "subject.py") == ""
        assert mod._anchor_line_ending_note("gone\r\nmissing", lf_only, tree / "subject.py") == ""
        note = mod._anchor_line_ending_note(ANCHOR_CRLF, lf_only, tree / "subject.py")
        assert "matches exactly once" in note, note
        assert "CRLF on disk" in note, note


class TestTheTailIsBounded:
    """The report is read from a terminal, so the run it shows is capped."""

    def test_only_the_last_lines_are_kept(self, mod) -> None:
        text = "\n".join(f"line {n}" for n in range(100))
        tail = mod._tail(text, lines=3).splitlines()
        assert tail == ["line 97", "line 98", "line 99"], tail

    def test_blank_lines_are_dropped_but_the_order_is_kept(self, mod) -> None:
        tail = mod._tail("first\n\n\nsecond\n\n", lines=20).splitlines()
        assert tail == ["first", "second"], tail
