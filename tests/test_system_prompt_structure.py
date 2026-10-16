"""The shape of `system.j2`: one section per topic, and the rule text that does not move.

Why this file exists
--------------------
`emrg/server/prompts/system.j2` is the single carrier every session receives, and on
2026-10-16 a report asked for it to be reorganised without changing any rule's substance.
Three properties of the reorganisation are checkable, and each is the property that the
report's own acceptance list asks to be pinned rather than asserted:

1. **One section per topic.** The memory rules were split across a `## Memory` block and a
   later `## Memory Management` separated by fifty lines of unrelated sections, so a reader
   met "how to organise a memory" twice, in two different wordings, with the file format in
   neither place its rules lived. The reading here is deliberately *structural*: one `## `
   heading whose title is `Memory`, and both the hygiene rules and the file format inside
   that one region — so re-splitting them, or inserting any other `## ` heading between them,
   fails here.
2. **The two supreme principles are pinned character for character.** They are
   `MANIFESTO.md` 第四条附则二 and 第四条附则三 — permanent, and not subject to any evolution
   mechanism — so the reorganisation may add an English translation beside them and may not
   touch one character of the Chinese text that carries them. The literals below are a copy of
   the shipped text on purpose: a copy is what makes "one character changed" visible, and a
   hash would say only that *something* moved.
3. **Engineering history is in the file and out of the render.** Which PR first landed a line,
   which report asked for it, and the rationale that used to be restated in three sections are
   notes about *why* the line exists: they belong in Jinja comments, which cost the prompt
   nothing, and they must not be written back into the body. The report's acceptance list asks
   for zero provenance in the render and one of these written back into the body as the
   mutation arm.

Named limit — the half of that acceptance item which cannot hold
----------------------------------------------------------------
The acceptance list says the rendered prompt should carry **no** `PR #n` and no `issue #n` at
all. It cannot, and the reason is not a wording choice: a **host ruling** keeps its date and
the public record spelled beside it in the body, because the timestamp indexes one machine's
store while the number beside it is the half a reader elsewhere can resolve
(`scripts/check-rant-citations.py` requires that anchor). Two other guards already assert
those two anchors *on the render* — `tests/test_daemon.py` requires `PR #1986` and
`2026-10-09T14:34:54`, and `tests/test_plain_language_rule_reach.py` requires `issue #2171`
in the block it also compares verbatim against the render. So this file pins what can hold:
the provenance that is *history* leaves the render (`test_the_history_leaves_the_render`),
and the provenance that is a *host ruling's anchor* stays
(`test_a_host_ruling_keeps_its_anchor_in_the_body`). The two together are the honest reading,
and the second is why the first names strings one by one rather than counting `PR #`.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The memory section's identity, as the *whole* heading line. Matched anchored (`^…$`) rather
#: than by `str.find`, because the file's own comments quote the heading names they describe and
#: an unanchored search would read the comment's backticked mention as the section.
MEMORY_HEADING = "## Memory"

#: The two things the report asks to find in one region — the rules, and the format they explain.
HYGIENE_MARKER = "**Memory Hygiene**"
FORMAT_MARKER = "**Memory file format**"

#: The subsection the temp-file rules were folded into, with the heading they came from.
SESSION_HEADING = "## Session & History"
TEMP_FILE_SUBHEADING = "### Temp File Rules"

#: The two permanent principles, quoted from the shipped template. These are copies, not
#: paraphrases: the property they pin is that not one character of either changes.
STOP_PRINCIPLE = (
    "**任何时候、任何实例，禁止编写、恢复或以任何形式引入「停止（stop）/ 重启（restart）"
    "emrg server / emrgd」的测试用例、脚本或代码路径**——包括直接调用 `stop_all()`、"
    "`stop_daemon()`、`emrg server stop/restart`，或任何会终止/重启本服务端进程的测试。"
    "违反将导致演化完全失效（服务端是 EMRG 生命的本体；此类测试在本机运行会直接杀死/重启"
    "服务端，宿主断连、调度任务全部重建）。此原则永久有效，不适用任何演化机制。执行周期时"
    "发现已有违反此条的测试，须立即移除并记录。MANIFESTO.md 第四条附则二 载有同款条文。"
)

UPGRADE_PRINCIPLE = (
    "**任何时候、任何实例，禁止编写、恢复或以任何形式引入「执行自动升级触发链」的测试用例、"
    "脚本或代码路径**——包括真实请求 GitHub releases API、真实读写 "
    "`~/.emrg/install/version.txt`、真实创建/写入 `emrg-upgrade` 会话、直接调用 "
    "`UpgradeManager.tick()` / daemon `_run_upgrade_session` 而未经完全隔离的测试。实证："
    "长时间运行的 pytest 曾在后台真实执行自动升级逻辑，每 5 分钟一次真实请求 releases API、"
    "真实读取 `install/version.txt`、真实向 `emrg-upgrade` 会话写入降级 prompt，跨 daemon "
    "重启、甚至在全部进程停止后依然持续写入。违反将污染升级会话、发出真实网络请求并干扰宿主"
    "环境。测试必须完全隔离这条链：releases API（httpx AsyncClient）、VERSION_FILE、"
    "SESSION_ID（`emrg-upgrade`）、run_session_cb 全部打桩；`tests/conftest.py` 的 "
    "`_guard_upgrade_hermeticity` 是兜底守卫，触发真实升级链的测试直接断言失败。此原则永久"
    "有效，不适用任何演化机制。MANIFESTO.md 第四条附则三 载有同款条文。"
)

#: The historic provenance the reorganisation moved into comments: each is a pair the citation
#: guard already accepts (`PR #n` beside the timestamp it landed), so moving it must keep both
#: halves — what changes is only which surface a reader finds them on.
HISTORY_IN_COMMENTS = (
    "PR #941",
    "rant 2026-08-23T08:04:26",
    "PR #1067",
    "rant 2026-08-28T22:12:16",
    "PR #987",
    "rant 2026-08-25T18:10:57",
    "issue #2292",
    "issue #2298",
)

#: The anchors a host ruling keeps in the body, and therefore in the render: the timestamp,
#: and the public record spelled beside it. Named here so the exemption above is a reading
#: rather than a permission.
HOST_RULING_ANCHORS = (
    "2026-10-09T14:34:54",
    "PR #1986",
    "issue #2171",
)


def _section(text: str, heading: str) -> str:
    """The region `heading` owns: from that line to the next `## ` heading at a line start.

    Anchored at both ends, because the file's comments name the headings they describe; an
    unanchored `find` would start the region inside a comment about the heading. A `###`
    subsection stays inside it — which is the point for the temp-file rules.
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


def _render_system_prompt() -> str:
    """The prompt a session receives, rendered by the daemon's own environment.

    `_get_jinja_env` is what `_build_system_prompt` uses, so the artifact measured here is the
    one a session runs under rather than a prompt nobody receives. The sections these tests read
    sit outside every conditional, so a minimal context renders them all.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )


# ── one section per topic ────────────────────────────────────────────────────


def test_memory_is_one_section() -> None:
    """One `## Memory`, and nothing else named like it.

    The defect this reads for is not a missing heading but a second one: `## Memory Management`
    is a heading whose title *starts* with `Memory`, which is how the rules came to be stated
    twice in two wordings. A count of the exact string would miss it (`## Memory` is a prefix of
    `## Memory Management`), so the reading is over whole heading lines.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    memory_headings = [h for h in _h2_headings(text) if h.startswith("## Memory")]
    assert memory_headings == [MEMORY_HEADING], (
        "the memory rules must live under one `## Memory` heading; found "
        f"{memory_headings} — a second heading beginning with `Memory` is the split that had "
        "the hygiene rules in one place and the file format in another, each stating its own "
        "way of organising a memory"
    )


def test_the_rules_and_the_format_share_that_section() -> None:
    """Both halves inside that one region — which is what inserting a `## ` between them breaks.

    The report names this exact mutation: splitting the format back out, or interleaving any
    other `## ` section, must fail. Reading the region rather than the file is what makes that
    true, since every string would still be present in a file that had been split again.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    section = _section(text, MEMORY_HEADING)
    assert section, "system.j2 has no `## Memory` heading line"
    for marker in (HYGIENE_MARKER, FORMAT_MARKER, "When organizing memories:"):
        assert marker in section, (
            f"{marker} is outside the `## Memory` section: the memory rules and the memory "
            "format are two topics under one heading, and a reader who arrives at either must "
            "find the other without crossing an unrelated section"
        )


def test_temp_file_rules_is_a_subsection_of_session_and_history() -> None:
    """The rule kept its place next to the session it is about, one level down.

    `## Temp File Rules` was a `## ` heading *inside* the `## Session & History` region, which
    made a rule of its own look like a sibling of the session topic it belongs to. It became a
    subsection; the path it anchors to is still the session's, so the placement is unchanged.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    assert TEMP_FILE_SUBHEADING in text, "the temp-file rules lost their heading"
    assert not any(
        h.startswith("## Temp File Rules") for h in _h2_headings(text)
    ), "the temp-file rules are a `## ` section again, not a subsection of the session topic"
    assert TEMP_FILE_SUBHEADING in _section(text, SESSION_HEADING), (
        "the temp-file rules are no longer inside the session section they annotate"
    )


def test_a_split_section_reads_as_split() -> None:
    """The instrument's control: the reading above must notice the shape it exists for.

    A check that reports the healthy answer whatever it is given is not a check. This feeds the
    extractors a sample in which the two halves are under two headings again — the mutation the
    report names — and requires the split to be visible.
    """
    sample = (
        "## Memory\n\n**Memory Hygiene**\n- a rule\n\n"
        "## Memory Management\n\n**Memory file format**\n- a format\n"
    )
    memory_headings = [h for h in _h2_headings(sample) if h.startswith("## Memory")]
    assert len(memory_headings) == 2
    section = _section(sample, MEMORY_HEADING)
    assert HYGIENE_MARKER in section
    assert FORMAT_MARKER not in section, (
        "the region reading spans the whole sample, so it would report the healthy answer on "
        "a file that has been split again"
    )
    assert _section(sample, "## Not There") == ""


# ── the two permanent principles, character for character ────────────────────


def test_the_two_supreme_principles_are_pinned_verbatim() -> None:
    """One character changed is the defect, in the file and in the render.

    These two blocks are `MANIFESTO.md` 第四条附则二 and 第四条附则三 — permanent, and not
    subject to any evolution mechanism — so the reorganisation may add a translation beside them
    (it did) and may not edit what carries them. Both surfaces are read: a Jinja comment around
    the text would leave every word in the file and remove it from the prompt every session
    receives, which is the one way this rule could be lost while the file looked untouched.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    rendered = _render_system_prompt()
    for name, principle in (
        ("第四条附则二", STOP_PRINCIPLE),
        ("第四条附则三", UPGRADE_PRINCIPLE),
    ):
        assert principle in text, (
            f"the {name} statement in system.j2 no longer matches the text this project pinned: "
            "it is a permanent principle and the Chinese text carrying it is quoted verbatim"
        )
        assert principle in rendered, (
            f"the {name} statement is in the file but not in the rendered prompt — nothing a "
            "session reads then carries it"
        )
        assert text.count(principle) == 1, (
            f"the {name} statement appears more than once: two copies are free to drift apart"
        )


def test_the_verbatim_pin_notices_a_single_character() -> None:
    """The control: the comparison is exact, not "close enough".

    `in` is a substring test, so it holds only while the text is identical; this asserts that
    property directly rather than trusting it, because a pin that had drifted to a fuzzy
    comparison would pass on a rewritten principle.
    """
    assert STOP_PRINCIPLE in STOP_PRINCIPLE
    mutated = STOP_PRINCIPLE.replace("永久有效", "永久有效。")
    assert mutated not in STOP_PRINCIPLE and STOP_PRINCIPLE not in mutated


# ── history in the comments, anchors in the body ─────────────────────────────


def test_the_history_leaves_the_render() -> None:
    """Reading (1): a note about why a line exists belongs in a comment, not in the prompt.

    The report's other half: a session that wants the provenance can read the file, and every
    session pays for the render. Writing any one of these back into the body fails here — which
    is the mutation arm the report asks for.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    rendered = _render_system_prompt()
    gone = [item for item in HISTORY_IN_COMMENTS if item not in text]
    assert not gone, (
        f"the file no longer carries its own provenance: {gone} — moving a note into a comment "
        "must not lose it, or the next reader cannot find which PR landed the line"
    )
    leaked = [item for item in HISTORY_IN_COMMENTS if item in rendered]
    assert not leaked, (
        f"{leaked} is in the rendered prompt: engineering history is a note about why a line "
        "exists, so it belongs in a comment the render does not carry — writing one back into "
        "the body spends prompt on every session for a fact no session acts on"
    )


def test_a_host_ruling_keeps_its_anchor_in_the_body() -> None:
    """Reading (2), and the exemption the reading above needs to be honest.

    A host ruling is not history: its date is how the message is re-found, and the public record
    beside it is the half that stays resolvable on a host that never recorded the message, so
    both stay in the body. This is what makes the "no `PR #n` in the render" acceptance item
    unreachable as written — see the module docstring — and it is asserted here rather than left
    as an exception nobody states.
    """
    rendered = _render_system_prompt()
    for anchor in HOST_RULING_ANCHORS:
        assert anchor in rendered, (
            f"the host ruling lost its anchor {anchor!r} from the rendered prompt: a timestamp "
            "indexes one machine's store, and the record spelled beside it is what a reader "
            "elsewhere can resolve"
        )
