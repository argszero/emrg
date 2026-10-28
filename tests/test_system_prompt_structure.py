"""The shape of `system.j2`: one section per topic, and no engineering history in the prefix.

Why this file exists
--------------------
`emrg/server/prompts/system.j2` is the single carrier every session receives, so every character in
it is paid on every round. Its two failure modes are structural rather than verbal, and neither is
visible by reading one sentence at a time:

1. **One topic, one section.** The memory rules were once split across a `## Memory` block and a
   later `## Memory Management` fifty lines away, so a reader met "how to organise a memory" twice, in
   two wordings, with the file format in neither place its rules lived. The reading here is
   deliberately *structural*: one `## ` heading whose title is `Memory`, and both the rules and the
   format they explain inside that one region.
2. **A rule is a rule, not the story of a rule.** A prompt that spends its first paragraphs on when a
   defect was measured, which pull request landed the fix and what the host said about it gives its
   reader no instruction — and the same content is what makes a prompt grow. So history belongs in
   the charter, in the pull request that landed the line and in the agent's memory; the prompt
   carries none of it, and `test_the_template_carries_no_digit_at_all` is the strongest form of that
   claim the file admits.

The two permanent limits are pinned here too, one carrier over: their verbatim text is
`MANIFESTO.md`'s (附则二 and 附则三), the prompt states them as English rules under `## Hard limits`,
and both halves are asserted — the charter because deleting it would leave the prompt pointing at
nothing, the prompt because a limit stated under some other heading governs nothing.

Named limit
-----------
This pins placement and composition, not wording: a rewrite that keeps one section per topic and no
provenance can change every sentence and pass here. The wording of each rule is pinned by the file
that owns that rule (`test_no_background_process_reach.py`, `test_upgrade_chain_red_line.py`,
`test_plain_language_rule_reach.py`, `test_language_policy_reach.py`, `test_several_calls_rule_reach.py`,
`test_memory_row_bound_reach.py`), each of which reads the render as well as the template.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"
MANIFESTO = REPO_ROOT / "MANIFESTO.md"

#: The memory section's identity, as the *whole* heading line.
MEMORY_HEADING = "## Memory"

#: The format sentence and the rules that share that one region — the two things a reader arriving at
#: either must find without crossing an unrelated section.
FORMAT_MARKER = "A memory is frontmatter, then a Markdown body under a title"
HYGIENE_MARKERS = (
    "A write is two steps",
    "The index is a pointer list",
    "Organise rather than accumulate",
)

#: The session section, and the temp-file rule that is a bullet inside it rather than a `## ` heading
#: of its own — the heading it had made one rule look like a sibling of the topic it serves.
SESSION_HEADING = "## Session and history"
TEMP_RULE_MARKER = "Throwaway scripts and payloads go under"

#: The section that owns the three permanent limits, and their headings in order.
LIMITS_HEADING = "## Hard limits"
LIMIT_HEADINGS = (
    "### Never background a command",
    "### Never stop or restart the server",
    "### Never reach the real auto-upgrade chain",
)

#: Every `## ` heading the prompt carries, in order. The order is the design: who you are → where you
#: are → what you were given → how you work → what you keep → where the conversation lives → how you
#: write → two special actions → the limits → the shape a round must have. Pinned as a list rather than
#: as a count, because a duplicated heading and a missing one are different defects and a count sees
#: neither. The last entry is deliberate: the round's shape closes the file, after every rule a round
#: must not break.
EXPECTED_H2 = [
    "## Where you are",
    "## Agent context",
    "## Skills",
    "## How you work",
    "## Memory",
    "## Session and history",
    "## Writing",
    "## Rants",
    "## Your own prompt",
    "## Hard limits",
    "## Plan before every call",
]

#: The two permanent principles, quoted from the charter — `MANIFESTO.md` 第四条附则二 and
#: 第四条附则三, in the charter's own wording (the session prompt used to carry a paraphrase of
#: each; the prompt now states the rule in English and points here). These are copies, not
#: paraphrases: the property they pin is that not one character of either changes.
STOP_PRINCIPLE = (
    "**任何时候、任何实例，禁止编写、恢复或以任何形式引入「停止（stop）/ 重启（restart）"
    "emrg server / emrgd」的测试用例、脚本或代码路径**——包括直接调用 `stop_all()`、"
    "`stop_daemon()`、`emrg server stop/restart`，或任何会终止/重启本服务端进程的测试。"
    "违反将导致演化完全失效：服务端是 EMRG 生命的本体，此类测试在本机运行时直接杀死或重启"
    "服务端进程，导致宿主连接中断、全部调度任务重建、演化机制自身崩溃。"
)

UPGRADE_PRINCIPLE = (
    "**任何时候、任何实例，禁止编写、恢复或以任何形式引入「执行自动升级触发链」的测试用例、"
    "脚本或代码路径**——包括真实请求 GitHub releases API、真实读写 "
    "`~/.emrg/install/version.txt`、真实创建/写入 `emrg-upgrade` 会话、直接调用 "
    "`UpgradeManager.tick()` / daemon `_run_upgrade_session` 而未经完全隔离的测试。"
)

#: The shapes engineering history takes in prose: a date, a pull request, an issue, a rant. Read on
#: the render as well as the file, because a provenance note can arrive from a `{% for %}` as easily
#: as from a repeated paragraph.
HISTORY_SHAPES = (
    re.compile(r"(?:19|20)\d{2}-\d{2}-\d{2}(?!\d)"),       # a date (also the head of a timestamp)
    re.compile(r"\bPR #\d+"),                                # a pull request
    re.compile(r"\bissues? #\d+"),                           # an issue
    re.compile(r"\brants? \d{4}-\d{2}-\d{2}"),               # a rant's timestamp
)


def _section(text: str, heading: str) -> str:
    """The region `heading` owns: from that line to the next `## ` heading at a line start.

    A `###` subsection stays inside it — which is the point for the limits, whose three blocks are
    subsections of one section.
    """
    match = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
    if match is None:
        return ""
    rest = text[match.end():]
    end = re.search(r"^## ", rest, re.MULTILINE)
    return rest if end is None else rest[: end.start()]


def _h2_headings(text: str) -> list[str]:
    """Every `## ` heading line in the text, in order."""
    return re.findall(r"^## .*$", text, re.MULTILINE)


def _all_headings(text: str) -> list[str]:
    """Every `## ` and `### ` heading line, in order."""
    return re.findall(r"^#{2,3} .*$", text, re.MULTILINE)


def _history_hits(text: str) -> list[str]:
    """Every provenance-shaped string in the text. Empty means the prefix carries no history."""
    return [m.group(0) for pattern in HISTORY_SHAPES for m in pattern.finditer(text)]


def _render_system_prompt() -> str:
    """The prompt a session receives, rendered by the daemon's own environment.

    `_get_jinja_env` is what `_build_system_prompt` uses, so the artifact measured here is the one a
    session runs under rather than a prompt nobody receives. The sections these tests read sit outside
    every conditional, or inside one a minimal context satisfies, so a minimal context renders them.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )


# ── one section per topic ────────────────────────────────────────────────────


def test_the_sections_are_one_per_topic_and_in_order() -> None:
    """The whole `## ` list, exactly: a missing section and a duplicated one are both defects."""
    headings = _h2_headings(SYSTEM_PROMPT.read_text(encoding="utf-8"))
    assert headings == EXPECTED_H2, (
        "the prompt's sections moved, multiplied or vanished: found "
        f"{headings}\n— one section per topic, in this order, is the shape the file is organised "
        f"by; expected {EXPECTED_H2}"
    )


def test_memory_is_one_section() -> None:
    """One `## Memory`, and nothing else named like it.

    The defect this reads for is not a missing heading but a second one: `## Memory Management` is a
    heading whose title *starts* with `Memory`, which is how the rules once came to be stated twice in
    two wordings. A count of the exact string would miss it (`## Memory` is a prefix of
    `## Memory Management`), so the reading is over whole heading lines.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    memory_headings = [h for h in _h2_headings(text) if h.startswith("## Memory")]
    assert memory_headings == [MEMORY_HEADING], (
        "the memory rules must live under one `## Memory` heading; found "
        f"{memory_headings} — a second heading beginning with `Memory` is the split that had the "
        "hygiene rules in one place and the file format in another"
    )


def test_the_rules_and_the_format_share_that_section() -> None:
    """Both halves inside that one region — which is what inserting a `## ` between them breaks."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    section = _section(text, MEMORY_HEADING)
    assert section, "system.j2 has no `## Memory` heading line"
    assert FORMAT_MARKER in section, (
        f"{FORMAT_MARKER!r} is outside the `## Memory` section: the format a memory is written in "
        "and the rules for organising one are one topic, and a reader who arrives at either must "
        "find the other without crossing an unrelated section"
    )
    for marker in HYGIENE_MARKERS:
        assert marker in section, (
            f"{marker!r} is outside the `## Memory` section — the memory rules and the memory "
            "format are two topics under one heading"
        )


def test_the_temp_file_rule_is_a_bullet_of_the_session_section() -> None:
    """One rule, next to the session it is about, and no heading of its own.

    `## Temp File Rules` was a `## ` heading inside the session topic, which made a rule look like a
    sibling of the thing it serves. It is now a bullet of `## Session and history`; both halves are
    asserted, because a rewrite that quietly promoted it again would leave the rule present and the
    shape wrong.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    assert TEMP_RULE_MARKER in _section(text, SESSION_HEADING), (
        "the temp-file rule is no longer inside the session section it annotates"
    )
    promoted = [h for h in _all_headings(text) if "Temp File" in h]
    assert not promoted, (
        f"the temp-file rule is a heading again ({promoted}): it is one bullet of the session "
        "topic, not a topic of its own"
    )


def test_a_split_section_reads_as_split() -> None:
    """The instrument's control: the reading above must notice the shape it exists for."""
    sample = (
        "## Memory\n\nA memory is frontmatter, then a Markdown body under a title\n- a rule\n\n"
        "## Memory Management\n\nA write is two steps\n"
    )
    memory_headings = [h for h in _h2_headings(sample) if h.startswith("## Memory")]
    assert len(memory_headings) == 2
    section = _section(sample, MEMORY_HEADING)
    assert FORMAT_MARKER in section
    assert "A write is two steps" not in section, (
        "the region reading spans the whole sample, so it would report the healthy answer on a file "
        "that has been split again"
    )
    assert _section(sample, "## Not There") == ""


# ── the limits, and the charter they point at ────────────────────────────────


def test_the_three_permanent_limits_sit_under_one_section() -> None:
    """Placement is what makes the section's permanence claim reach them."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    limits = _section(text, LIMITS_HEADING)
    assert limits, "system.j2 has no `## Hard limits` section"
    subsections = [h for h in _all_headings(limits) if h.startswith("### ")]
    assert subsections == list(LIMIT_HEADINGS), (
        "the hard limits are the three permanent rules, in this order; found "
        f"{subsections} — a fourth `###` here is a limit nobody chartered, and a missing one is a "
        "rule that left the section which claims nothing can soften it"
    )
    for heading in LIMIT_HEADINGS:
        assert text.count(heading) == 1, (
            f"`{heading}` appears {text.count(heading)} times: two copies are free to drift apart"
        )
    assert "`MANIFESTO.md`" in limits, (
        "the limits section must name the charter it abbreviates — the identifiers, the function "
        "names and the incident record live there, and without the pointer a reader has the rule "
        "with no way to reach its full text"
    )


def test_the_charter_carries_the_two_principles_verbatim() -> None:
    """One character changed is the defect — in the carrier that actually holds the text.

    These two blocks are `MANIFESTO.md` 第四条附则二 and 第四条附则三: permanent, and not subject to
    any evolution mechanism. The prompt states them as English rules; the Chinese text is the
    charter's, so it is pinned there, and its absence from the prompt is pinned too — that absence is
    what the compacted prompt is for, and a rewrite that pasted the text back into the prefix would
    spend every session's context on it again.
    """
    charter = MANIFESTO.read_text(encoding="utf-8")
    prompt = SYSTEM_PROMPT.read_text(encoding="utf-8")
    for name, principle in (
        ("第四条附则二", STOP_PRINCIPLE),
        ("第四条附则三", UPGRADE_PRINCIPLE),
    ):
        assert principle in charter, (
            f"the {name} statement in MANIFESTO.md no longer matches the text this agent pinned: "
            "it is a permanent principle and the Chinese text carrying it is quoted verbatim"
        )
        assert charter.count(principle) == 1, (
            f"the {name} statement appears more than once in MANIFESTO.md: two copies are free to "
            "drift apart"
        )
        assert principle not in prompt, (
            f"the {name} statement is back in the session prompt: its full text belongs to the "
            "charter, and every session pays for it on every round"
        )


def test_the_verbatim_pin_notices_a_single_character() -> None:
    """The control: the comparison is exact, not "close enough"."""
    assert STOP_PRINCIPLE in STOP_PRINCIPLE
    mutated = STOP_PRINCIPLE.replace("服务端进程", "服务器进程")
    assert mutated not in STOP_PRINCIPLE and STOP_PRINCIPLE not in mutated


# ── no engineering history in the prefix ─────────────────────────────────────


def test_the_template_carries_no_digit_at_all() -> None:
    """The strongest form: a figure in the prefix is either history or a copied derived value.

    Those are the only two kinds of number a prompt can carry, and both are refused: a measurement
    belongs in the memory entry that records it, and a derived number is injected from the constant
    that owns it (`{{ index_title_max_chars }}`, `{{ memory_index_row_cap }}` —
    `tests/test_memory_row_bound_reach.py` holds that direction). Measured on the shipped template:
    zero digits.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    digits = re.findall(r".{0,40}[0-9].{0,40}", text)
    assert not digits, (
        "the template carries a literal number; a figure in the prefix is either a measurement that "
        f"belongs in the record that made it, or a derived value that belongs in a variable: {digits}"
    )


def test_neither_the_file_nor_the_render_carries_provenance() -> None:
    """Reading (2): what the reader gets carries no date, no `PR #n`, no `issue #n`, no rant.

    Read on both surfaces: a provenance note can arrive from the file or from a `{% for %}`.
    """
    for name, text in (
        ("system.j2", SYSTEM_PROMPT.read_text(encoding="utf-8")),
        ("the rendered prompt", _render_system_prompt()),
    ):
        hits = _history_hits(text)
        assert not hits, (
            f"{name} carries engineering history: {hits}. Which pull request landed a line, when a "
            "defect was measured and what the host said about it are notes about why the line "
            "exists — they belong in the charter, the pull request and the agent's memory, and "
            "every session pays for them here"
        )


def test_the_history_reading_reports_absence() -> None:
    """The instrument's control: text carrying provenance must read as carrying it."""
    sample = (
        "## Plain language (issue #2171)\n\n"
        "Measured 2026-10-09T14:34:54 (PR #1986); rant 2026-10-15T12:35:03 asked for it.\n"
    )
    hits = _history_hits(sample)
    assert "2026-10-09" in hits
    assert "PR #1986" in hits
    assert "issue #2171" in hits
    assert any(hit.startswith("rant 2026-10-15") for hit in hits)
    assert _history_hits("# A prompt with no provenance\n\n- Must push\n") == []
