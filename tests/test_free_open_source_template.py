"""Guards for the `free-open-source` task type's template (issue #2929).

The type exists so that taking part in a new repository costs no agent, no task record
and no configuration change: one task faces a **workspace** of clones and picks its own
repository. Almost all of that is prose, and prose decays — so the sentences that carry
the requirements are named here rather than left to a reader's memory of the issue.

Two subjects, and each is read in both directions:

1. **The five source levels, in order.** The order is the type's whole design: the
   account's own actions outrank a search started from nothing. A slot swapped, a level
   evicted to the end, or a level deleted each leaves a template that still reads fine and
   no longer says what the type is for — so the scan is self-tested by performing all
   three on the real text.
2. **The boundaries.** Write scope (the workspace and every clone under it), the sandbox
   tier, the fixed clone location, the subject being a workspace rather than one
   repository, one repository per cycle, the three things a closing summary names, and the
   participation record living in session memory rather than a state file.

Named limit: this reads the template as text. It cannot show that a run obeys the order,
that the levels resolve against a live account, or that the contributions are any good.
The rendering and placeholder guards live in `tests/test_prompt_templates.py`, which covers
every built-in type and therefore this one too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from emrg.protocol import InstanceIdentity
from emrg.server import scheduler as mod
from tests.task_handler_factory import make_handler

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "emrg" / "server"
TASK_TYPE = "free-open-source"


def _template_text() -> str:
    """The type's template text, or a failure naming the registration that is missing."""
    filename = mod.TASK_TEMPLATES.get(TASK_TYPE)
    assert filename, (
        f"{TASK_TYPE!r} is not in TASK_TEMPLATES — the type is not registered, so the "
        f"validator rejects a record of it and no template renders"
    )
    path = PROMPTS_DIR / filename
    assert path.is_file(), f"{TASK_TYPE!r} maps to {filename!r}, which is not a file in {PROMPTS_DIR}"
    return path.read_text(encoding="utf-8")


# ── 1. the five levels, in order ────────────────────────────────────────────────────
#
# The marker is the phrase that carries each level rather than a number: numbers are what
# a reordering leaves behind, and this list is what a reordering must redden.

LEVEL_HEADING = "#### 1.1 Where the next repository comes from"
LEVEL_MARKERS: tuple[tuple[str, str], ...] = (
    ("own creations", "repositories this account created"),
    ("notifications", "the account's notifications"),
    ("forks", "repositories this account forked"),
    ("stars", "repositories this account starred"),
    ("search", "repositories found by search"),
)

_SUBSECTION = re.compile(r"\n#{3,4} ", re.MULTILINE)


def _levels_block(text: str) -> str | None:
    """The §1.1 section alone, or None when the template no longer carries it.

    Both ends are found rather than assumed: a slice that ran from the heading to the end
    of the file would still "contain" a level that had been moved into a later section,
    and the order reading would pass on a template where the levels are no longer together.
    """
    start = text.find(LEVEL_HEADING)
    if start < 0:
        return None
    rest = text[start + len(LEVEL_HEADING):]
    end = _SUBSECTION.search(rest)
    return text[start:start + len(LEVEL_HEADING) + (end.start() if end else len(rest))]


def level_order_problems(text: str) -> list[str]:
    """What is wrong with the five levels' order, or [] when it holds."""
    block = _levels_block(text)
    if block is None:
        return [f"the template no longer carries the heading {LEVEL_HEADING!r}"]
    haystack = block.lower()
    missing = [label for label, marker in LEVEL_MARKERS
               if haystack.find(marker.lower()) < 0]
    if missing:
        return [f"level(s) {missing} are not stated under {LEVEL_HEADING!r}"]
    positions = {label: haystack.find(marker.lower()) for label, marker in LEVEL_MARKERS}
    order = sorted(positions, key=lambda label: positions[label])
    expected = [label for label, _ in LEVEL_MARKERS]
    if order != expected:
        return [f"the levels read {order}, and the order the type states is {expected}"]
    return []


def test_the_five_levels_are_read_in_the_stated_order() -> None:
    assert not level_order_problems(_template_text())


def _replace_ci(text: str, marker: str, replacement: str) -> str:
    """Replace the first case-insensitive occurrence of ``marker``.

    The markers are lowercase and the template capitalises each level's first word, so a
    case-sensitive `str.replace` would leave every arm silently un-applied — the shape that
    reads exactly like a survivor (`a-mutation-that-did-not-apply-reads-as-a-survivor`).
    The assertion is what makes that impossible.
    """
    match = re.search(re.escape(marker), text, re.IGNORECASE)
    assert match, f"{marker!r} is not in the template, so the arm cannot be applied"
    return text[:match.start()] + replacement + text[match.end():]


def test_the_level_scan_catches_a_swap_an_eviction_and_a_deletion() -> None:
    """The three edits the design admits, performed on the real text.

    Each one leaves a template that renders, validates and reads plausibly — which is
    exactly why the order has to be a reading rather than a sentence someone remembers.
    """
    text = _template_text()
    own, notif, search = LEVEL_MARKERS[0][1], LEVEL_MARKERS[1][1], LEVEL_MARKERS[4][1]

    # (a) the two top levels swapped: "the most interesting level first" is the mistake.
    swapped = _replace_ci(_replace_ci(_replace_ci(text, own, "\0"), notif, own), "\0", notif)
    assert swapped != text
    problems = level_order_problems(swapped)
    assert problems and "order" in problems[0], problems

    # (b) own creations evicted to the end: the search-first design, still green on prose.
    evicted = _replace_ci(_replace_ci(_replace_ci(text, own, "\0"), search, own), "\0", search)
    assert evicted != text
    problems = level_order_problems(evicted)
    assert problems and "order" in problems[0], problems

    # (c) a level deleted outright.
    deleted = _replace_ci(text, LEVEL_MARKERS[2][1], "")
    assert deleted != text
    problems = level_order_problems(deleted)
    assert problems and "not stated" in problems[0], problems


def test_the_level_scan_needs_the_heading_it_reads() -> None:
    """No heading, no levels: an empty reading is not a pass (the region-bound trap)."""
    problems = level_order_problems(_template_text().replace(LEVEL_HEADING, "#### 1.1 Sources"))
    assert problems and "no longer carries the heading" in problems[0], problems


# ── 2. the boundaries the type promises ─────────────────────────────────────────────

REQUIREMENTS: dict[str, str] = {
    "a workspace rather than one repository, with no agent or record to add":
        "no agent, no task record and no configuration change",
    # The consequence above is not the claim: a template can still keep "no configuration
    # change" while saying it faces a single repository, and that template is the one this
    # type exists to replace. So the subject sentence is named too.
    "the type's subject is a workspace, not one repository":
        "**this one faces a workspace**",
    # The write scope is a boundary of this type, not only the sandbox tier's: covering a
    # workspace of clones is what makes the scope larger than one repository, and narrowing
    # it back to a single clone leaves every other sentence reading unchanged.
    "the write scope is the workspace and every clone under it":
        "**Never write outside the workspace and the clones under it**",
    "one repository, one contribution, per cycle":
        "**One repository, one contribution, one cycle.**",
    "clones land at a fixed place inside the workspace":
        "`<workspace>/clones/<owner>/<repo>`",
    "a repository the host holds elsewhere is read-only":
        "A repository the host already holds elsewhere on this machine is **read-only**",
    "the sandbox tier stays workspace-write":
        "This task runs at the `workspace-write` tier",
    "participation lives in session memory, not a state file":
        "This task keeps no state file",
    "the closing summary names exactly three things":
        "it names exactly three things",
    "…which repository this cycle was for": "**Which repository this cycle was for**",
    "…what was done there": "**What was done there**",
    "…which repository is the next candidate": "**Which repository is the next candidate**",
    "a level is skipped by a reading, never by a preference":
        "A level is skipped by a reading, never by a preference",
    # Each level has to name how it is READ, not only that it ranks where it does: an
    # order with no fetch under it is a preference list, which is what the type forbids.
    "level 1 is fetched from the account's own repositories": "affiliation=owner&sort=pushed",
    "level 2 is fetched from notifications": "gh api notifications",
    "level 3 is fetched by selecting forks": "select(.fork)",
    "level 4 is fetched from the starred list": "user/starred",
    "level 5 is fetched by search": "gh search issues",
}


def missing_requirements(text: str) -> list[str]:
    """The requirements whose carrying sentence ``text`` no longer holds."""
    return [name for name, phrase in REQUIREMENTS.items() if phrase not in text]


def test_every_requirement_is_carried_by_a_sentence() -> None:
    assert not missing_requirements(_template_text())


@pytest.mark.parametrize("name", sorted(REQUIREMENTS))
def test_deleting_one_requirement_sentence_is_reported(name: str) -> None:
    """An arm per requirement, so a check that returned [] unconditionally cannot pass."""
    text = _template_text()
    mutated = text.replace(REQUIREMENTS[name], "")
    assert mutated != text, f"{name}: its sentence is not in the template verbatim"
    assert name in missing_requirements(mutated), (
        f"{name}: deleting its sentence went unreported, so the reading above proves nothing"
    )


# ── 3. the template binds no single repository ──────────────────────────────────────

#: A placeholder naming the one repository a `open-source` task is bound to. This type's
#: whole difference is that it has no such subject: `{{ repo }}`-style names would bind
#: every run to EMRG's own remote, which is the binding the type exists to remove.
SINGLE_REPO_PLACEHOLDER = re.compile(r"\{\{\s*(?:owner|repo|repo_url|local_source)\s*\}\}")


def bound_repository_names(text: str) -> list[str]:
    return sorted({match.group(0) for match in SINGLE_REPO_PLACEHOLDER.finditer(text)})


def test_the_template_binds_no_single_repository() -> None:
    assert not bound_repository_names(_template_text())


def test_the_binding_scan_catches_each_placeholder() -> None:
    for placeholder in ("{{ repo }}", "{{ owner }}", "{{ repo_url }}", "{{ local_source }}"):
        text = f"Target: {placeholder}\n" + _template_text()
        assert bound_repository_names(text) == [placeholder], (
            f"{placeholder} would not be flagged, so 'none bound' proves nothing"
        )


# ── 4. the wiring: registered, validated, and rendered ─────────────────────────────

def _write_agents_yml(directory: Path, name: str, entry: dict) -> Path:
    agent_dir = directory / name
    agent_dir.mkdir(exist_ok=True)
    row = {"path": str(agent_dir)}
    row.update(entry)
    (directory / "agents.yml").write_text(yaml.safe_dump([row]), encoding="utf-8")
    return agent_dir


def test_a_record_of_this_type_validates_and_an_unknown_type_does_not() -> None:
    """Both directions: the registration is what the validator reads, and it reads both."""
    base = {"name": "free-os-task", "type": TASK_TYPE, "config": {"agent": "demoproj"},
            "interval": 3600, "enabled": True}
    assert mod.validate_task_record(base) is None
    unknown = dict(base, type="free-open-source-typo")
    problem = mod.validate_task_record(unknown)
    assert problem and "unknown type" in problem, problem


def test_the_type_renders_its_own_template_with_the_workspace_named(tmp_path, monkeypatch) -> None:
    """A real handler, a real config dir, a real render — and the workspace is in the text.

    The render guard in `tests/test_prompt_templates.py` covers every built-in type; this
    one names *this* type's own subject, because a template that rendered to a generic
    prompt would still pass there.
    """
    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    agent_dir = _write_agents_yml(tmp_path, "demoproj", {"name": "demoproj"})
    handler = make_handler(
        name="free-os-task",
        config={"agent": "demoproj"},
        interval=3600,
        identity=InstanceIdentity(),
        template_path=PROMPTS_DIR / mod.TASK_TEMPLATES[TASK_TYPE],
    )
    rendered = handler._build_evolution_prompt()
    assert str(agent_dir) in rendered, (
        "the prompt does not name the workspace it may write in, so an agent would have "
        "to guess where the clones belong"
    )
    assert not re.search(r"\{\{|\{%", rendered), "an unrendered Jinja tag survived"
    assert not level_order_problems(rendered), level_order_problems(rendered)
