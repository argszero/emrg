"""The shipped evolution template must read `extra_prompt`, and must not pay for §1.5 unbounded.

Why this file exists
--------------------
Host rant 2026-10-17T11:18:41.769853+08:00 (`submit_rant`, project `emrg`), taken on as
issue #2342. Two gaps, both in the one place a cycle never looked:

* `~/.emrg/tasks.yml` appeared in the template only as the `extra_prompt` injection heading.
  That field is where an installation writes what it believes its own task prompt does not
  say — a defect report about the prompt, filed by the session least able to act on it.
  Measured on the host that filed the rant: two of four tasks carry a non-empty
  `config.extra_prompt` (4,325 and 6,990 chars), mixing instance facts with general method.
* §1.5 told the cycle to read every project's `.emrg/memory/` and `.emrg/sessions/`, with
  nothing saying what counts as a finding, so the next cycle read it all again.

Both are prompt items, so both are pinned here rather than left as prose a later edit can
drop in silence — the same shape as `tests/test_evolution_prompt_issue_rule.py`, which pins
the sibling rule the host gave on 2026-09-26.

The second pin is the one that had to be measured
-------------------------------------------------
The new §1.5 answers "is this index far larger than its content justifies?" by counting.
Written first as `awk 'length($0)>512' MEMORY.md | wc -l`, that reading is **wrong on this
project's own host**: macOS `awk` measures *bytes*, while R9's 512 bound is *characters*, so
every CJK row past ~170 characters is reported as over. Measured over the eight project
indexes here, the awk form reports 66, 33 and 3 over-long rows for `emrg`, `competition` and
`pku-paper-3d`, and the true count is **0** in all three (longest rows 512, 511 and 418
characters). A cycle obeying that sentence would open 102 bodies on three projects that have
none to open — the funnel inverted. The reading now names `scripts/check-memory-index.py`,
the producer R9's own compaction uses, and the prohibition is pinned beside it because this
trap has already been recorded four times in this project's memory (`8e4b2a17` §7 among them).

Named limits
------------
* This pins the *presence* of each rule, not obedience to it: no test shows that a cycle
  rotated rather than read everything, or that an `extra_prompt` block was classified at all.
* The awk pin reads the template's text. It cannot prove a cycle never types an `awk` of its
  own; it proves the template does not *instruct* one.
* It does not check that `scripts/check-memory-index.py` exists — a template sentence is not
  the tool.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"

#: The section that must gain the funnel, its neighbours (for the order pin), and the
#: short phrases it must carry. Phrases rather than sentences: a re-wrap of the paragraph
#: must not fail the guard, while a deletion of the rule must.
FUNNEL = "#### 1.5 Memory and conversations across projects"
FUNNEL_AFTER = "#### 1.4 master"
FUNNEL_BEFORE = "#### 1.6 Comparable tools"
FUNNEL_TERMS: tuple[str, ...] = (
    "step 0 reads no content",                        # the cheap pass that is never skipped
    "it is the step that is never skipped",           # ... and that it is not optional
    "Read indices, not bodies",                       # depth decided by evidence
    "exempt from the rotation",                       # R8 item 1 is not scheduled
    "Read conversation by location, never whole",     # a history is not read end to end
    "One memory entry at project scope",              # the bookmark, as one entry
    "never append a second entry",                    # ... or it becomes the file it avoids
    "Other projects' memory is read-only for this cycle",
    # The reading that answers "is this index too big", and the one this file exists for:
    # the guard, not a byte-measuring count.
    "scripts/check-memory-index.py",
    "measures bytes rather than characters",
)

#: The new prompt-drift section, the rule it carries, and the two prohibitions that make it
#: safe to act on: a host keeps paths, accounts and credentials in `extra_prompt`, and the
#: task table is serialized without comments.
DRIFT = "#### 1.7 Task configuration and prompt drift"
DRIFT_AFTER = "#### 1.6 Comparable tools"
DRIFT_BEFORE = "### 2. Think"
DRIFT_TERMS: tuple[str, ...] = (
    "evidence about the prompt",                      # how the field is read
    "Instance configuration",                         # class 1
    "Already in the template",                        # class 2
    "Missing from the template",                      # class 3 — the cycle's work
    "A block classified as instance configuration is never quoted",
    "Never edit `tasks.yml`",                         # the host's file
    "template first, deletion afterwards",            # order, so no rule is lost between
)

#: The decision section must name the new source, or the reading produces nothing.
DECISION = "#### 2.3 Decide this cycle's work"
DECISION_TERM = "prompt-drift reading"

#: What a prohibition reads like. Small and explicit, because the pin below asks it of every
#: line that mentions `awk` and both directions of the answer are exercised by
#: `test_the_prohibition_predicate_discriminates`.
_NEGATIONS = ("do not", "never", "not ")


def _section(text: str, heading: str) -> str:
    """The template's `heading` block, up to the next Markdown heading of its level.

    Split on `^#{3,4} ` rather than a bare `#`, because the template's shell blocks carry
    `#` comments — splitting on those would cut a section off at its own example.

    A heading that is absent answers `""`, and every caller checks that before reading a
    phrase out of it: an empty section satisfies a negative pin vacuously, which is the
    reading this family has shipped before.
    """
    start = text.find(heading)
    if start < 0:
        return ""
    rest = text[start + len(heading):]
    level = len(heading) - len(heading.lstrip("#"))
    marker = "\n" + "#" * level + " "
    cut = rest.find(marker)
    return rest[:cut] if cut >= 0 else rest


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _is_prohibition(line: str) -> bool:
    """Whether `line` reads as an instruction *not* to do the thing it mentions."""
    lowered = line.lower()
    return any(marker in lowered for marker in _NEGATIONS)


def test_the_funnel_replaces_the_unbounded_read() -> None:
    """§1.5 keeps its coverage and states the procedure that makes it affordable."""
    text = _template()
    section = _section(text, FUNNEL)
    assert section, f"{FUNNEL!r} is missing from the template"
    missing = [term for term in FUNNEL_TERMS if term not in section]
    assert not missing, f"§1.5 no longer states: {missing}"


def test_the_funnel_sits_between_1_4_and_1_6() -> None:
    """The section is a reading of §1, in the place a cycle reaches it."""
    text = _template()
    at = text.find(FUNNEL)
    assert at > 0, f"{FUNNEL!r} is missing from the template"
    assert text.find(FUNNEL_AFTER) < at < text.find(FUNNEL_BEFORE), (
        "§1.5 must sit between §1.4 and §1.6"
    )


def test_no_line_in_the_funnel_instructs_a_byte_measuring_count() -> None:
    """The one wrong reading this section shipped, and every line that could reintroduce it.

    The template may *mention* `awk` only to forbid it. Measured: the instruction form reports
    66/33/3 over-long rows for three project indexes whose true count is 0.
    """
    section = _section(_template(), FUNNEL)
    assert section, f"{FUNNEL!r} is missing from the template"
    offenders = [
        line.strip()
        for line in section.splitlines()
        if "awk" in line.lower() and not _is_prohibition(line)
    ]
    assert not offenders, (
        "§1.5 mentions awk outside a prohibition, which reads as an instruction to count rows "
        f"in bytes: {offenders}"
    )


def test_the_prohibition_predicate_discriminates() -> None:
    """Both directions of the pin above, so it cannot pass by matching everything or nothing.

    A guard that accepted every line would never fire; one that rejected every line would
    fire on the correct text. Each arm states the answer it must give.
    """
    instruction = "Found by counting: `awk 'length($0)>512' MEMORY.md | wc -l`."
    prohibition = (
        "Do not count rows with `awk 'length($0)>512'`: an `awk` that measures bytes "
        "rather than characters reports a CJK index as far over a bound it is nowhere near."
    )
    assert not _is_prohibition(instruction), "an instruction must not read as a prohibition"
    assert _is_prohibition(prohibition), "the shipped prohibition must be recognised"


def test_the_drift_section_states_the_rule_and_its_two_prohibitions() -> None:
    """§1.7 classifies a block and says what may never be done with one."""
    text = _template()
    section = _section(text, DRIFT)
    assert section, f"{DRIFT!r} is missing from the template"
    missing = [term for term in DRIFT_TERMS if term not in section]
    assert not missing, f"§1.7 no longer states: {missing}"


def test_the_drift_section_sits_between_1_6_and_think() -> None:
    """A reading of §1 belongs before §2 decides, not appended after the loop."""
    text = _template()
    at = text.find(DRIFT)
    assert at > 0, f"{DRIFT!r} is missing from the template"
    assert text.find(DRIFT_AFTER) < at < text.find(DRIFT_BEFORE), (
        "§1.7 must sit between §1.6 and §2"
    )


def test_the_decision_names_the_new_source() -> None:
    """An unread source is not a stated "none" — §2.3 has to be able to mark it empty."""
    section = _section(_template(), DECISION)
    assert section, f"{DECISION!r} is missing from the template"
    assert DECISION_TERM in section, "§2.3 must name the prompt-drift reading among its sources"
