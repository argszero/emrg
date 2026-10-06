"""A crash leaves with the family's "could not measure" code, never a verdict's.

Measured 2026-10-06 (`cyc20261006-042523`), on this host, before the fix: ten
`sibling-load` sites across eight tools guarded the load with

    if spec is None or spec.loader is None:  # pragma: no cover - the file is in this repo
        raise RuntimeError(f"could not load {path}")

and that guard **could not fire**. `importlib.util.spec_from_file_location` returns a real
spec *and* a `SourceFileLoader` for a path that does not exist:

    ModuleSpec(name='x', loader=<SourceFileLoader ...>, origin='/definitely/not/here.py')

so the branch was dead code and the live path was the exception `exec_module` raises. Left
uncaught, Python exits **1** — and `1` is a *verdict* in every one of these tools' exit
tables, while `2` is the code for "the question could not be answered". Measured, with one
sibling left unparsable:

    check-merge-freshness.py  -> exit 1   # the STALE verdict, whose remedy is a re-merge
                                          # and a push that voids every standing vote
    review-queue.py           -> exit 1   # a code this tool's table does not define at all
    run-mutation-arm.py       -> exit 1   # == EXIT_SURVIVED, "the target still passed with
                                          # the mutation in place"

So this module pins the rule the family now shares: `_entry()` catches what leaves `main`,
prints the cause, and answers `2`. The clause reads the tools the way the other family-wide
clauses do — by file, from `scripts/` — because the rule is about all of them at once.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

#: Every tool that is *run* rather than only imported: each one prints a report and answers
#: with a code, so each one needs an entry point that cannot answer with a crash's code.
#: `check-notary-credentials.py` joined 2026-10-07 (`cyc20261007-072231`): it loads no
#: sibling, so the derivation below could not reach it, and it answers **1 = "Apple refused
#: the credentials"** — a verdict code — so an uncaught exception was read as Apple's
#: refusal. Measured on the invocation `DEVELOPMENT.md` recommends for that tool, with the
#: named file absent: traceback, exit 1.
TOOLS = [
    "check-merge-freshness.py",
    "check-vote-count.py",
    "review-queue.py",
    "check-merge-plan-suite.py",
    "cast-vote.py",
    "check-merge-pairs.py",
    "check-merge-order.py",
    "check-merge-landed.py",
    "run-mutation-arm.py",
    "check-notary-credentials.py",
]


def _load(name: str):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(f"entry_{name[:-3].replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=TOOLS)
def tool(request):
    """Every tool of the family, one case each.

    Parametrised rather than looped inside one test: a failure names the tool it is about,
    and a tool that stops loading is a failure rather than a shorter loop.
    """
    return request.param, _load(request.param)


def test_every_tool_of_the_family_has_the_entry_point():
    """The set is measured, not assumed: a new tool has to be added here and to the list."""
    missing = [name for name in TOOLS if not (SCRIPTS / name).is_file()]
    assert not missing, f"this list names tools that are not there any more: {missing}"
    for name in TOOLS:
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        assert 'raise SystemExit(_entry())' in text or "sys.exit(_entry())" in text, (
            f"{name} has a `main` but its `__main__` block does not go through `_entry`, so "
            "a crash inside it exits 1 - a verdict code in this family"
        )


def test_the_rule_has_one_home_not_one_per_tool():
    """A copy of a rule per tool is a chance to drift per tool, so they are pinned equal.

    Count-free on purpose: the family has grown since this clause was written (nine members
    when it was named `…_not_nine`, ten after `check-notary-credentials.py` joined on
    2026-10-07), and a number in a name is a claim the next member falsifies without
    touching this file.

    Compared through `inspect.getsource` rather than by re-reading the file, because the
    subject is the function each module really defines.
    """
    sources = {name: inspect.getsource(_load(name)._entry) for name in TOOLS}
    distinct = sorted({text for text in sources.values()})
    assert len(distinct) == 1, (
        "the entry points of this family are no longer one rule: "
        f"{[(name, len(text)) for name, text in sources.items()]}"
    )


def test_a_crash_answers_two_and_names_its_cause(tool, monkeypatch, capsys):
    """Both directions at once: the code, and the prose a caller reads instead of one.

    Asserted on the **whole summary line**, not on the tool's name appearing somewhere in
    stderr: the traceback the wrapper also prints carries a frame of the tool itself, so a
    check for the bare name is satisfied by the frames alone. Measured 2026-10-06 - an arm
    that dropped `{Path(__file__).name}` from the message (the only place the summary names
    its tool) **SURVIVED** the name-only form, because `File ".../review-queue.py", line N,
    in _entry` was already there.
    """
    name, mod = tool

    def boom(*_args, **_kwargs):
        raise RuntimeError("the sibling exploded")

    monkeypatch.setattr(mod, "main", boom)
    assert mod._entry() == 2, (
        f"{name} answered something other than the unmeasurable code for a crash; 1 is a "
        "verdict in this family"
    )
    captured = capsys.readouterr()
    expected = f"{name}: could not measure - RuntimeError: the sibling exploded"
    assert expected in captured.err, (
        f"{name} did not print the summary line a caller reads: {captured.err!r}"
    )


def test_a_verdict_is_passed_through_untouched(tool, monkeypatch):
    """The control: an entry point that answered 2 for everything would pass the clause above.

    Every code a tool can really reach is checked, because a wrapper that swallowed or
    re-mapped a deliberate one would change what the caller reads while looking like a
    successful fix.
    """
    name, mod = tool
    for code in (0, 1, 2, 3):
        monkeypatch.setattr(mod, "main", lambda *a, _c=code, **k: _c)
        assert mod._entry() == code, f"{name} did not pass {code} through"


def test_a_deliberate_exit_is_not_swallowed(tool, monkeypatch):
    """`argparse` exits for a bad flag, and that has to stay an exit, not a `2` from here.

    `SystemExit` is a `BaseException`, so the wrapper does not see it — which is the reason
    it catches `Exception` rather than everything, and the reason this is pinned: catching
    `BaseException` would turn a usage error into a report of an unmeasurable question.
    """
    name, mod = tool

    def exits(*_args, **_kwargs):
        raise SystemExit(7)

    monkeypatch.setattr(mod, "main", exits)
    with pytest.raises(SystemExit) as raised:
        mod._entry()
    assert raised.value.code == 7, f"{name} rewrote a deliberate exit code"


def test_the_dead_guard_is_gone_from_the_family():
    """The branch that could never fire is not kept as documentation of a handling.

    Asserted on the shape the ten sites shared, so this clause fails if one is re-added —
    the state that made the crash reach the caller in the first place. A guard that cannot
    fire is worse than no guard: a reader takes it for a path that is handled.
    """
    offenders = []
    for path in sorted(SCRIPTS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "spec is None or spec.loader is None" in text:
            offenders.append(path.name)
    assert not offenders, (
        "these files still guard a load with `spec is None or spec.loader is None`, which "
        f"`spec_from_file_location` cannot produce: {offenders}"
    )


def test_the_family_is_derived_from_the_source_not_listed():
    """A tool that loads a sibling by file is in the family whether or not anyone listed it.

    Read out of the source with `ast`, because the shape that matters is the *call*: two
    scripts name `spec_from_file_location` only in prose (`check-citation-resolves.py`,
    `check-memory-index.py`) and are not members, while a gate added later that really loads
    a sibling would be one. Before this clause the family was a hand-written list, so the
    `DEVELOPMENT.md` sentence claiming the rule for "the gate family" was pinned by a module
    that could not measure the claim (measured 2026-10-06, `cyc20261006-091811`).

    The set is required to be a **subset** of `TOOLS`, not equal to it: `run-mutation-arm.py`
    is pinned here and loads nothing — it asks a gate as a subprocess — so equality would be
    false. What is asserted is the direction that can rot: no sibling-loading gate escapes
    the list.
    """
    loaders = set()
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "spec_from_file_location"
            ):
                loaders.add(path.name)
                break

    assert loaders, (
        "no tool under scripts/ loads a sibling by file any more, so this clause measured "
        "nothing - a rule that silently stopped applying is not a rule that passed"
    )
    missing = sorted(loaders - set(TOOLS))
    assert not missing, (
        "these tools load a sibling by file and are not in TOOLS, so nothing pins what their "
        f"entry point answers to a crash: {missing} - add them to TOOLS and give them "
        "`_entry()`"
    )


def test_the_documented_rule_names_the_class_it_was_fixed_for():
    """The sentence a reader checks has to be one the guard can falsify.

    It read "Every tool in the gate family ends its `__main__` block in `_entry()`", and "the
    gate family" has no definition in this repo — the only other naming of the gates is
    `Agent.md`'s "Merge gates, run before merging" list, four of whose entries end in
    `sys.exit(main())` and have no `_entry()` at all. So the sentence was false for a reader
    who took the documented gate list for the family, and true only under an unstated narrower
    one (measured 2026-10-06, `cyc20261006-091811`). Both directions are pinned: the unscoped
    wording must not come back, and the class the guard measures must be named.
    """
    text = (REPO_ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")
    assert "Every tool in the gate family ends its" not in text, (
        "this sentence claims a family the repo does not define - name the class the guard "
        "measures instead of a name a reader has to guess at"
    )
    assert "the gates that load a sibling by file path" in text, (
        "the rule has to name the class `TOOLS` is derived from, or a reader cannot check "
        "which tools it is about"
    )


def test_the_crash_gone_through_main_is_a_report_not_a_traceback_on_its_own(
    tool, monkeypatch, capsys
):
    """End to end through the wrapper the `__main__` block calls, with a real failure.

    The failure is the one measured on the family: a sibling loaded by file that does not
    parse. Raised from `main` here rather than staged on disk, because the subject of this
    test is the *report*, and the exception is the same object either way.
    """
    name, mod = tool

    def crash(*_args, **_kwargs):
        raise SyntaxError("invalid syntax (check-merge-plan-suite.py, line 1526)")

    monkeypatch.setattr(mod, "main", crash)
    code = mod._entry()
    captured = capsys.readouterr()
    assert code == 2, captured.err
    assert "SyntaxError" in captured.err, captured.err
    assert "line 1526" in captured.err, (
        "the cause's own words have to survive into the report, or the reader cannot act "
        f"on it: {captured.err!r}"
    )
    assert "Traceback" in captured.err, (
        "the traceback is kept beside the summary line: a reader debugging a tool that "
        f"failed unexpectedly needs the frame that raised: {captured.err!r}"
    )
