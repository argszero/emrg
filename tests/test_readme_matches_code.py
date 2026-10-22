"""Every fact a README states about this tree is read from the tree.

Why this file exists
--------------------
`README.md` and `README.cn.md` described an EMRG older than the one on disk — six facts
had drifted, and all six drifted the same way: **a value a reader can obtain from the code
was written into prose by hand**, so nothing in the repository compared the two. The
command table held 15 rows against `_COMMAND_HELP`'s 17; the comparison table put ❌ in
EMRG's own `Subagents` cell while `SubagentTool` was registered at start-up; the evolution
cadence read 30 minutes where a fresh install runs the seeded 600-second record; the tool
row named six of the eight registered names; eight built-in task types went undisclosed;
and the phase list named six steps of which three appear nowhere in
`emrg/server/evolution_prompt.md`. A reader following those documents was told, in one
case, the exact opposite of what the binary does.

**Each fact has one producer, and the guard reads that producer rather than a copy.** The
tool roster is `build_tool_registry` in `emrg/server/daemon.py` — extracted for this file,
because a copy of the registration block here would be a second list to keep in step, which
is the defect `emrg/tools/shell_dialects.py` exists to name. The commands come from
`_COMMAND_HELP`, the same dictionary the TUI's `/help` renders. The cadence comes from
`DEFAULT_TASK_RECORD`, the record the daemon seeds; `DEFAULT_INTERVAL` in the same module is
a *different* subject (the fallback for a hand-written record that omits the field) and is
deliberately not the number asserted here.

**Every check reads both directions.** A row the table carries and the code does not offer
is as much a drift as the opposite, and it is the direction a one-way check never reports.

**Every check has a control arm.** A reading that prints the healthy answer whatever it is
given has measured nothing, so each test below is paired with a mutation of the real
document — a deleted row, a renamed tool, a changed number, a flipped ✅, a renamed phase —
that must turn exactly that reading red. The arms run over both languages, because the pair
is what drifts: an edit to one README alone is the shape this whole file exists to catch.

**A subject that cannot be found is reported, never passed.** A README rewritten so that an
anchor is gone makes the reading answer "nothing measured" rather than "no drift"; the
checkers return that as a problem, which is why a green run means the anchor was found *and*
agreed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from emrg.client.widgets import _COMMAND_HELP
from emrg.server.daemon import build_tool_registry
from emrg.server.scheduler import DEFAULT_TASK_RECORD, TASK_TEMPLATES

REPO_ROOT = Path(__file__).resolve().parent.parent
READMES = (REPO_ROOT / "README.md", REPO_ROOT / "README.cn.md")
EVOLUTION_TEMPLATE = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"

#: A cell boundary — a pipe that is not escaped. GFM splits a row here, and this
#: repository's `/skills` row carries escaped pipes inside a code span.
_CELL_BOUNDARY = re.compile(r"(?<!\\)\|")

#: A table row whose first cell opens with a slash command. The bare `/` row and the
#: `**Just type**` row match nothing, which is right: neither names a command.
_COMMAND_ROW = re.compile(r"^`(/[a-z][a-z-]*)")

#: The cadence sentence, in each language. Both spell the unit differently, so they are
#: two patterns rather than one with an alternation in the middle.
_CADENCE = (
    re.compile(r"Evolution Cycle \(every (\d+) min"),
    re.compile(r"演化循环（默认每 (\d+) 分钟"),
)

#: Anchor phrases whose parenthesised list names something the code owns.
_TOOL_ANCHORS = ("Tool-calling", "工具调用")
_TASK_TYPE_ANCHORS = ("Built-in task types", "内置任务类型")

#: A template's numbered section heading, e.g. `### 1. Look`.
_PHASE_HEADING = re.compile(r"^### (\d+)\. (\w+)$", re.MULTILINE)


# ── reading a document ──────────────────────────────────────────────────────


def _cells(line: str) -> list[str] | None:
    """The cells of a markdown table row, or ``None`` for any other line."""
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.strip() for cell in _CELL_BOUNDARY.split(stripped[1:-1])]


def _plain(cell: str) -> str:
    """A cell with its emphasis markers removed."""
    return cell.replace("*", "").replace("`", "").strip()


def _parenthesised(text: str, anchors: tuple[str, ...]) -> str | None:
    """The contents of the parenthesis that follows one of ``anchors``.

    Both paren styles are accepted, because a Chinese sentence legitimately uses the
    full-width pair and the tool row of the same document may use the ASCII one. What
    the reading needs is the group's contents, not the punctuation that framed it.
    """
    for anchor in anchors:
        match = re.search(re.escape(anchor) + r"\s*[\(（]([^)）]*)[\)）]", text)
        if match:
            return match.group(1)
    return None


def _names_in(text: str, anchors: tuple[str, ...]) -> set[str] | None:
    """The names a parenthesised list after ``anchors`` holds, split on either separator.

    A token may carry two names joined by a slash — `bash`/`pwsh` is the platform
    roster's spelling — so each token is split on `/` as well.
    """
    group = _parenthesised(text, anchors)
    if group is None:
        return None
    cleaned = group.replace("`", "").replace("*", "")
    names: set[str] = set()
    for token in re.split(r"[,、]", cleaned):
        for part in token.split("/"):
            part = part.strip()
            if part:
                names.add(part)
    return names


def _documented_commands(text: str) -> set[str]:
    """Every ``/command`` a table row opens with, across the whole document."""
    found: set[str] = set()
    for line in text.splitlines():
        cells = _cells(line)
        if cells:
            match = _COMMAND_ROW.match(cells[0])
            if match:
                found.add(match.group(1))
    return found


def _stated_interval(text: str) -> int | None:
    """The evolution cadence in minutes, as the document states it."""
    for pattern in _CADENCE:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def _stated_phases(text: str) -> list[str] | None:
    """The phase list that follows the cadence sentence, in document order."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if any(pattern.search(line) for pattern in _CADENCE):
            for following in lines[index + 1:]:
                if not following.strip():
                    continue
                stripped = following.strip().strip("()（）")
                return [part.strip() for part in re.split(r"->|→", stripped) if part.strip()]
    return None


def _subagent_cell(text: str) -> str | None:
    """EMRG's own cell on the `Subagents` row of the comparison table.

    The column is located from the header row rather than assumed: a column inserted
    before EMRG's would otherwise turn this reading into a reading of its neighbour.
    """
    column: int | None = None
    for line in text.splitlines():
        if not line.strip():
            column = None
            continue
        cells = _cells(line)
        if cells is None:
            column = None
            continue
        if column is None and any("EMRG" in cell for cell in cells):
            column = next(i for i, cell in enumerate(cells) if "EMRG" in cell)
            continue
        if column is not None and _plain(cells[0]) in ("Subagents", "子代理"):
            return cells[column] if column < len(cells) else None
    return None


# ── the code's own answers ──────────────────────────────────────────────────


def tool_roster() -> set[str]:
    """Every tool name the daemon can register, on any platform.

    Two platforms are built because the shell dialect is composition, not a branch:
    POSIX mounts `bash` and Windows mounts `pwsh`, and a document that named only one
    of them would be wrong on the other host.
    """
    roster: set[str] = set()
    for platform_name in ("linux", "win32"):
        roster |= set(build_tool_registry(None, None, platform_name=platform_name).names)
    return roster


def template_phases() -> list[str]:
    """The numbered section headings of the evolution template, in order."""
    text = EVOLUTION_TEMPLATE.read_text(encoding="utf-8")
    return [match.group(2) for match in _PHASE_HEADING.finditer(text)]


# ── the six readings ────────────────────────────────────────────────────────


def command_drift(text: str, offered: set[str]) -> list[str]:
    documented = _documented_commands(text)
    if not documented:
        return ["no `/command` row was found in a table — the reading measured nothing"]
    missing = sorted(offered - documented)
    extra = sorted(documented - offered)
    return (
        [f"{name} is offered by the app and has no row" for name in missing]
        + [f"{name} has a row and the app does not offer it" for name in extra]
    )


def tool_drift(text: str, roster: set[str]) -> list[str]:
    documented = _names_in(text, _TOOL_ANCHORS)
    if documented is None:
        return ["the tool-calling row was not found — the reading measured nothing"]
    return (
        [f"{name} is registered and the row omits it" for name in sorted(roster - documented)]
        + [f"{name} is named in the row and nothing registers it"
           for name in sorted(documented - roster)]
    )


def interval_drift(text: str, seeded_seconds: int) -> list[str]:
    minutes = _stated_interval(text)
    if minutes is None:
        return ["the evolution cadence sentence was not found — the reading measured nothing"]
    if minutes * 60 != seeded_seconds:
        return [
            f"the cadence reads {minutes} min; the task record the daemon seeds runs "
            f"{seeded_seconds} s"
        ]
    return []


def task_type_drift(text: str, types: set[str]) -> list[str]:
    documented = _names_in(text, _TASK_TYPE_ANCHORS)
    if documented is None:
        return ["the built-in task-type list was not found — the reading measured nothing"]
    return (
        [f"{name} is a built-in task type and is undisclosed" for name in sorted(types - documented)]
        + [f"{name} is disclosed as a built-in and no template renders it"
           for name in sorted(documented - types)]
    )


def phase_drift(text: str, phases: list[str]) -> list[str]:
    stated = _stated_phases(text)
    if stated is None:
        return ["the phase list was not found — the reading measured nothing"]
    if stated != phases:
        return [
            f"the phase list reads {' -> '.join(stated)}; the template's headings are "
            f"{' -> '.join(phases)}"
        ]
    return []


def subagent_drift(text: str, registered: bool) -> list[str]:
    cell = _subagent_cell(text)
    if cell is None:
        return ["the Subagents row was not found — the reading measured nothing"]
    marked = cell.startswith("✅")
    if registered and not marked:
        return [f"the Subagents cell reads {cell!r} while a subagent tool is registered"]
    if marked and not registered:
        return ["the Subagents cell reads ✅ and nothing registers a subagent tool"]
    return []


# ── the real corpus ─────────────────────────────────────────────────────────


def _read(readme: Path) -> str:
    return readme.read_text(encoding="utf-8")


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_every_command_the_app_offers_has_a_row(readme: Path) -> None:
    problems = command_drift(_read(readme), set(_COMMAND_HELP))
    assert not problems, f"{readme.name}: " + "; ".join(problems)


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_the_tool_row_names_every_registered_tool(readme: Path) -> None:
    problems = tool_drift(_read(readme), tool_roster())
    assert not problems, f"{readme.name}: " + "; ".join(problems)


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_the_stated_cadence_is_the_one_the_daemon_seeds(readme: Path) -> None:
    problems = interval_drift(_read(readme), DEFAULT_TASK_RECORD["interval"])
    assert not problems, f"{readme.name}: " + "; ".join(problems)


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_the_stated_task_types_are_the_templates_that_render(readme: Path) -> None:
    problems = task_type_drift(_read(readme), set(TASK_TEMPLATES))
    assert not problems, f"{readme.name}: " + "; ".join(problems)


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_the_stated_phases_are_the_templates_headings(readme: Path) -> None:
    problems = phase_drift(_read(readme), template_phases())
    assert not problems, f"{readme.name}: " + "; ".join(problems)


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_the_subagent_cell_agrees_with_the_registry(readme: Path) -> None:
    registered = "subagent" in tool_roster()
    problems = subagent_drift(_read(readme), registered)
    assert not problems, f"{readme.name}: " + "; ".join(problems)


def test_no_derived_list_was_left_out_of_the_documents() -> None:
    """The subjects themselves are not empty, so the comparisons above mean something.

    A check whose expected set is empty passes on any document: both directions are
    vacuously true. This is the arm that notices the producers being emptied.
    """
    assert len(_COMMAND_HELP) > 5
    assert len(tool_roster()) > 5
    assert len(TASK_TEMPLATES) > 5
    assert len(template_phases()) > 3
    assert DEFAULT_TASK_RECORD["interval"] > 0


# ── the control arms ────────────────────────────────────────────────────────
#
# Each one mutates the real document and asserts that exactly its own reading turns
# red. Without them a checker that returned `[]` unconditionally would pass every test
# above — the shape this repository names as "a guard nobody runs" is a sibling of it.


def _without_row(text: str, cell_prefix: str) -> str:
    """Drop the first table row whose first cell starts with ``cell_prefix``.

    Matched on the *cell*, not on the line: the command row reads
    ```| `/sandbox [mode]` |```, so a line-prefix match written against the bare command
    name finds nothing and the arm would mutate no document at all.
    """
    kept: list[str] = []
    removed = 0
    for line in text.splitlines(keepends=True):
        cells = _cells(line)
        if removed == 0 and cells and cells[0].startswith(cell_prefix):
            removed += 1
            continue
        kept.append(line)
    assert removed == 1, f"the mutation removed {removed} rows"
    return "".join(kept)


def _renumber_cadence(text: str, minutes: int) -> str:
    for pattern in _CADENCE:
        match = pattern.search(text)
        if match:
            return text[:match.start(1)] + str(minutes) + text[match.end(1):]
    raise AssertionError("the cadence sentence was not found to mutate")


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_a_deleted_command_row_is_reported(readme: Path) -> None:
    text = _read(readme)
    assert not command_drift(text, set(_COMMAND_HELP))
    mutated = _without_row(text, "`/sandbox")
    problems = command_drift(mutated, set(_COMMAND_HELP))
    assert "/sandbox is offered by the app and has no row" in problems


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_a_row_the_app_does_not_offer_is_reported(readme: Path) -> None:
    text = _read(readme)
    mutated = text.replace(
        "| `/help` |",
        "| `/nonexistent` | Nothing offers this |\n| `/help` |",
        1,
    )
    assert mutated != text
    problems = command_drift(mutated, set(_COMMAND_HELP))
    assert "/nonexistent has a row and the app does not offer it" in problems


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_a_renamed_tool_is_reported_in_both_directions(readme: Path) -> None:
    roster = tool_roster()
    text = _read(readme)
    assert not tool_drift(text, roster)
    mutated = text.replace("submit_rant", "submit_rant_renamed", 1)
    assert mutated != text
    problems = tool_drift(mutated, roster)
    assert "submit_rant is registered and the row omits it" in problems
    assert "submit_rant_renamed is named in the row and nothing registers it" in problems


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_a_changed_cadence_is_reported(readme: Path) -> None:
    seeded = DEFAULT_TASK_RECORD["interval"]
    text = _read(readme)
    assert not interval_drift(text, seeded)
    problems = interval_drift(_renumber_cadence(text, seeded // 60 + 20), seeded)
    assert problems and "the cadence reads" in problems[0]


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_a_missing_task_type_is_reported_in_both_directions(readme: Path) -> None:
    types = set(TASK_TEMPLATES)
    text = _read(readme)
    assert not task_type_drift(text, types)
    mutated = text.replace("feishu", "feishu_renamed", 1)
    assert mutated != text
    problems = task_type_drift(mutated, types)
    assert "feishu is a built-in task type and is undisclosed" in problems
    assert "feishu_renamed is disclosed as a built-in and no template renders it" in problems


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_a_renamed_phase_is_reported(readme: Path) -> None:
    phases = template_phases()
    text = _read(readme)
    assert not phase_drift(text, phases)
    second = phases[1]
    mutated = text.replace(f"-> {second} ->", "-> Review ->", 1)
    assert mutated != text
    problems = phase_drift(mutated, phases)
    assert problems and "the phase list reads" in problems[0]


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_a_flipped_subagent_cell_is_reported(readme: Path) -> None:
    registered = "subagent" in tool_roster()
    text = _read(readme)
    assert not subagent_drift(text, registered)
    cell = _subagent_cell(text)
    assert cell is not None
    flipped = "❌ *delegated*" if cell.startswith("✅") else "✅ *delegated*"
    mutated = text.replace(cell, flipped, 1)
    assert mutated != text
    problems = subagent_drift(mutated, registered)
    assert problems


@pytest.mark.parametrize("readme", READMES, ids=lambda path: path.name)
def test_an_anchor_that_went_missing_is_reported_not_passed(readme: Path) -> None:
    """A document whose anchor is gone is *not measurable*, and that is not a pass."""
    text = _read(readme)
    stripped = re.sub(r"Tool-calling[^\n]*|工具调用[^\n]*", "", text, count=1)
    problems = tool_drift(stripped, tool_roster())
    assert problems and "measured nothing" in problems[0]
