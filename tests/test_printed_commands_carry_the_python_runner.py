"""A printed command's runner must match its file type: the runner goes with `.py`.

The defect this pins, measured 2026-10-04
-----------------------------------------
`review-queue.py`'s re-trigger row printed its remedy by interpolating its `RUNNER`
constant in front of a **shell** script:

    command=f"{RUNNER} scripts/re-trigger-ci.sh <branch-of-{pr}>"

which renders as `uv run --no-sync python3 scripts/re-trigger-ci.sh`. That command cannot
run at all:

    uv run --no-sync python3 scripts/re-trigger-ci.sh
      File ".../scripts/re-trigger-ci.sh", line 11
        set -euo pipefail
                 ^^^^^^^^
      SyntaxError: invalid syntax                                  -> rc 1
    bash -n scripts/re-trigger-ci.sh                               -> parses
    scripts/re-trigger-ci.sh                                       -> dispatches (mode 755)

So the one row whose whole remedy is "re-trigger CI on the same head" - the state R4
describes when the push event was dropped - handed over a command that re-triggers
nothing. Why it happened is the point of putting a mechanism here: the rule lived in
`review-queue.py`'s `RUNNER` docstring as prose ("printed commands carry the runner"), and
prose does not say which file types the runner is *for*. The constant is right for the
`.py` tools (mode 644 here, so a bare `scripts/x.py` exits 126) and wrong for `.sh`, and
nothing read the difference. Same class as `Agent.md`'s "a rule that can be mechanised is
mechanised, because prose decays".

What is scanned, and what deliberately is not
---------------------------------------------
The scan reads the **interpolation sites** in `scripts/*.py` - a literal `RUNNER}` in the
source followed by `scripts/<name>`. That is exactly the set of places a command is built
for printing, and it is a set the tool can enumerate without executing anything.

Not scanned, on purpose: a bare `scripts/<name>` in prose, which is the family's ordinary
way of *naming* a reading rather than offering a command (`scripts/read-run-failure.py` in
a docstring, `check-vote-count.py` in a sentence). Flagging those would fire on the
explanations of this very rule, and a guard that fires on its own explanation is one a
reader learns to ignore. What the rule covers is narrow and load-bearing: **where a runner
is used, it must be used for the interpreter it is**, because those strings are the ones a
reader pastes.

Both directions, because a check is only evidence if it can fail
----------------------------------------------------------------
`_bearers` is exercised on a synthetic source with a `.sh` bearer - it must find it - and
on a `.py` one it must not, so the matcher is shown to discriminate rather than to agree
with whatever it is handed. And the corpus is required to be non-empty: a scan that finds
no site has measured nothing, and "nothing found" is not the verdict "nothing wrong"
(`emrg/server/evolution_prompt.md`, the `never a pass` rule).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

#: `{RUNNER} scripts/<name>` — the runner interpolated in front of a script. The name is
#: read to the first character that cannot be part of a path, so the flags after it
#: (`--json`, `<branch>`) are not swallowed.
_BEARER = re.compile(r"RUNNER\}\s+scripts/([A-Za-z0-9_./-]+)")

#: The runner constant's definition, so a file that prints commands is known by its own
#: declaration rather than by this test's idea of where the constant lives.
_DEFINES_RUNNER = re.compile(r"^RUNNER\s*=\s*", re.MULTILINE)


def _bearers(source: str) -> list[str]:
    """Every script name this source hands the runner to, in order."""
    return _BEARER.findall(source)


def _sources() -> list[tuple[Path, str]]:
    """Every `scripts/*.py` that declares a runner, with its text.

    A file that cannot be read is a failure, not a skip: this guard's whole subject is
    what those files print, and an unread file has answered nothing.
    """
    out: list[tuple[Path, str]] = []
    for path in sorted(SCRIPTS.glob("*.py")):
        text = path.read_text(encoding="utf-8")  # OSError is the failure it is
        if _DEFINES_RUNNER.search(text):
            out.append((path, text))
    return out


def test_the_matcher_finds_a_shell_bearer_and_clears_a_python_one():
    """The matcher's own two directions, before it is trusted on the tree.

    Without this the rule below could pass by finding nothing at all: a regex that
    silently matched no bearer would report a clean family forever.
    """
    with_shell = 'command=f"{RUNNER} scripts/re-trigger-ci.sh <branch-of-{pr}>"\n'
    with_python = 'command=f"{RUNNER} scripts/check-vote-count.py {pr}"\n'

    assert _bearers(with_shell) == ["re-trigger-ci.sh"]
    assert _bearers(with_python) == ["check-vote-count.py"]


def test_every_runner_interpolation_names_a_python_tool():
    """Where a runner is printed, the script after it is one an interpreter runs."""
    sources = _sources()
    assert sources, (
        "no scripts/*.py declares a runner, so this scan measured nothing - which is "
        "not the verdict that no command is spelled wrong"
    )

    sites = 0
    offenders: list[str] = []
    for path, text in sources:
        for name in _bearers(text):
            sites += 1
            if not name.endswith(".py"):
                offenders.append(f"{path.name}: {name}")
    assert sites, (
        "the runner is declared but never interpolated in front of a script, so the "
        "rule below had no subject - an unreadable queue is not a clean one"
    )
    assert not offenders, (
        "a printed command hands the python runner a script that is not a python tool: "
        + ", ".join(offenders)
        + " - a `.sh` takes `bash` (measured: the python runner gives it a SyntaxError "
        "and exit 1)"
    )
