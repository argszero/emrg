"""Content guards for emrg/server/competition_prompt.md (rant 2026-09-12T14:57:33).

The competition task prompt is the operational contract for entering online
competitions (Tianchi / Kaggle / DataFountain / HuggingFace). The host's hard
constraint is that **only fully online** competitions may be entered, and that
the judgment must be an executable procedure rather than agent discretion.
These tests pin the online-only gate (the offline signal words, the award-
ceremony exception, "reject by default when the rules text is unobtainable",
"read doubt conservatively") plus the compute-feasibility limit and the
"stop and ask the host" rule for identity verification — so a future edit
cannot silently relax the gate the host explicitly set.

Two levels, two questions
-------------------------
`competition_prompt.md` is a Jinja2 template: `TaskHandler._build_evolution_prompt`
renders it with `undefined=jinja2.Undefined` and sends the **result** to the round. So
the file-level assertions below answer "does the source that ships carry this?", and the
render-level ones answer "is this what a round is sent?" — neither implies the other. A
Jinja construct (`{# … #}`, `{% if false %}`) removes text from the prompt while the
file keeps every character, and a file read cannot see that at all: measured 2026-10-08
(`cyc20261008-082424`, issue #1904), wrapping §0.0 in `{# … #}` left all five
load-bearing phrases in the file, every file-level assertion green, and the render 2,301
characters shorter with none of them — a hand-written mutation arm reported `SURVIVED`
for exactly that one. The same shape was fixed for `emrg/server/prompts/vibe_check.j2`
in issue #1902.
"""

import re
from pathlib import Path

import pytest
import yaml

from emrg.protocol import InstanceIdentity
from emrg.server import scheduler as mod
from tests.task_handler_factory import make_handler

PROMPT = (
    Path(__file__).resolve().parent.parent
    / "emrg"
    / "server"
    / "competition_prompt.md"
)

#: The agent the render fixture asks the builder for — a name that appears nowhere in
#: the template, so "the render carries it" proves a placeholder resolved rather than a
#: string being copied through.
PROMPT_PROJECT = "demoproj"


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> str:
    """The competition prompt as a round receives it, rendered by the real builder.

    `TaskHandler._build_evolution_prompt` is the only thing that turns this template
    into a prompt, so it is what these tests render through: a fresh `jinja2.Environment`
    here could differ in `trim_blocks`, `lstrip_blocks` or the loader and the artifact
    under test would then be a prompt no round is ever sent.

    The context is the one a real records-driven call produces (a agent and nothing
    else); `config_dir` is redirected at a tmp tree — the shape
    `tests/test_evolution_prompt_index_rule.py` uses — so the `agents.yml` read lands
    on a file this test made, never on the host's `~/.emrg`.
    """
    tmp_path = tmp_path_factory.mktemp("competition-prompt")
    agent_dir = tmp_path / PROMPT_PROJECT
    agent_dir.mkdir(exist_ok=True)
    (tmp_path / "agents.yml").write_text(
        yaml.safe_dump([{"name": PROMPT_PROJECT, "path": str(agent_dir)}]),
        encoding="utf-8",
    )
    original = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        handler = make_handler(
            name="competition-task",
            type="competition",
            config={"agent": PROMPT_PROJECT},
            identity=InstanceIdentity(),
            template_path=PROMPT,
        )
        return handler._build_evolution_prompt()
    finally:
        mod.config_dir = original


def test_competition_prompt_exists():
    assert PROMPT.is_file(), (
        "competition_prompt.md missing — the competition task cannot resolve "
        "its template"
    )


def test_online_only_is_the_stated_hard_constraint():
    text = PROMPT.read_text(encoding="utf-8")
    assert "participate only in **fully online** competitions" in text
    assert "If a competition has an offline component, do not enter it" in text
    # The gate is a procedure, not a principle: it must say so.
    assert "executable procedure" in text


def test_offline_signal_words_are_all_present():
    text = PROMPT.read_text(encoding="utf-8")
    # Chinese signals
    for word in [
        "线下",
        "现场",
        "决赛答辩",
        "答辩",
        "路演",
        "决赛",
        "集训",
        "现场评审",
        "差旅",
        "差旅报销",
    ]:
        assert word in text, f"offline signal word missing: {word}"
    # English signals
    for word in [
        "on-site",
        "onsite",
        "offline round",
        "in-person",
        "final presentation",
        "pitch event",
        "demo day",
        "venue",
        "travel",
    ]:
        assert word in text, f"offline signal word missing: {word}"
    # Any single hit disqualifies
    assert "any single hit disqualifies the competition" in text


def test_award_ceremony_is_an_explicit_exception():
    text = PROMPT.read_text(encoding="utf-8")
    # Without this the gate would exclude essentially every prize competition.
    assert "award ceremony" in text
    assert "award banquet" in text
    assert "领奖仪式" in text
    assert "颁奖典礼" in text
    assert "do not disqualify" in text
    # The scope itself is stated: the *process* must be offline to disqualify.
    assert "the participation/evaluation process must be offline" in text


def test_gate_requires_positive_online_evidence():
    text = PROMPT.read_text(encoding="utf-8")
    assert "at least one piece of positive online evidence" in text
    for token in ["在线评测", "leaderboard", "submission", "在线提交"]:
        assert token in text, f"positive online evidence token missing: {token}"


def test_rules_text_unavailable_defaults_to_rejection():
    text = PROMPT.read_text(encoding="utf-8")
    # The dangerous default is "assume it is online" — pin the safe direction.
    assert "default to not participating" in text
    assert "never default to passing" in text
    assert "rules text unavailable, online-only status cannot be verified" in text


def test_doubt_is_read_conservatively():
    text = PROMPT.read_text(encoding="utf-8")
    assert "treat it as having an offline component" in text
    assert "Doubt resolves against participation" in text


def test_evidence_must_be_quoted_verbatim():
    text = PROMPT.read_text(encoding="utf-8")
    assert "verbatim" in text
    assert "no paraphrase, no inference, no bare conclusion" in text


def test_all_three_rule_pages_are_fetched():
    text = PROMPT.read_text(encoding="utf-8")
    # Many competitions state the offline component only on the schedule page.
    assert "rules" in text and "schedule / timeline" in text and "prize" in text
    assert "Many competitions state their offline component only on the schedule page" in text


def test_identity_verification_stops_and_asks_the_host():
    text = PROMPT.read_text(encoding="utf-8")
    assert "real-name verification" in text
    assert "do not retry, do not work around it" in text
    assert "Never create accounts or enter credentials" in text
    # Bypassing verification is explicitly forbidden.
    assert "Never attempt to bypass real-name / phone / payment verification" in text


def test_compute_feasibility_limit_is_stated():
    text = PROMPT.read_text(encoding="utf-8")
    # Host machine: Apple M4 Pro, 48GB unified memory, no NVIDIA GPU.
    assert "no NVIDIA GPU" in text
    assert "CPU-solvable" in text
    assert "LLM-API-solvable" in text


def test_phase_state_machine_is_complete():
    text = PROMPT.read_text(encoding="utf-8")
    for phase in [
        "Phase A — Discovery and screening",
        "Phase B — Registration",
        "Phase C — Data and baseline",
        "Phase D — Iteration",
        "Phase E — Deadline wrap-up",
        "Phase F — Archive",
    ]:
        assert phase in text, f"phase missing: {phase}"
    # One phase per round *per competition*, and the baseline anchor rule. The old
    # sentence here ("A round advances one phase — do not do several unrelated things in
    # one round") sat next to §0.0 item 2, which requires every round to work all active
    # competitions, and the round resolved that conflict the wrong way on 2026-10-07
    # (one round touched 1 of 8; the host corrected it, 2026-10-07T11:52:53). The rule the
    # test pins is now the replacement's: the round's unit of work is the *set*.
    assert '"One phase per round" means one phase PER COMPETITION' in text
    assert "A round advances one phase — do not do several unrelated things" not in text, (
        "the sentence that read as 'one competition per round' is back — it contradicts "
        "§0.0 item 2 and was the mis-reading the host corrected on 2026-10-07"
    )
    assert "Submit at least once successfully and obtain a leaderboard score" in text


def test_the_round_covers_every_active_competition():
    """§1's affirmative half: the round's unit of work is the set, not one competition.

    Rant 2026-10-09T14:35:15 (§1). §0.0 item 2 requires every round to work all active
    competitions and this section's old first sentence required "exactly one phase ...
    do not do several unrelated things in one round"; a round that read the second as
    "one competition" produced a round that touched 1 of 8 (2026-10-07), and the host
    corrected it. Three clauses carry the rule, and each closes a different dodge: the
    traversal, the mandatory increment, and the ledger that forces a stated reason.
    """
    text = PROMPT.read_text(encoding="utf-8")
    assert "The round's unit of work is the set of ACTIVE competitions" in text
    assert "Every round must traverse ALL active competitions" in text
    assert "Every competition must show an increment each round" in text
    # The dodge list has to name the excuses it rejects, or each reads as a reason.
    assert "a per-competition submission-quota ledger" in text
    for excuse in (
        "I was busy with another competition",
        "it was waiting",
        "it was already submitted",
        "Waiting for evidence",
        "no point submitting",
        "did not get to it",
    ):
        assert excuse in text, (
            f"the rejected ground {excuse!r} is gone from §1 — a rule that does not name "
            "the excuse it refuses is argued around by it"
        )


def test_the_workspace_hygiene_section_is_placed_before_the_phase_machine():
    """§0.7, and *where* it sits: a tidy-up rule read after the work has started is late.

    Rant 2026-10-09T14:35:15 (§0.7). Measured in `competitions/532510-zhihu-tables/` on
    2026-10-07: 466 files at the top level and the competition's single living document
    present but visually buried, so the host could not find it. The section belongs with
    the other preparation steps, above §1 and the phase table.
    """
    text = PROMPT.read_text(encoding="utf-8")
    assert "#### 0.7 Workspace hygiene" in text
    for rule in (
        "Root-level budget",
        "No `.bak` litter",
        "Predictable paths",
        "Verify after reorganising",
    ):
        assert rule in text, f"§0.7 lost the {rule!r} rule"
    # The four rules are load-bearing in their specifics, not just their headings: the
    # budget names what may stay, the litter rule caps the copies, and the last one exists
    # because a tidy-up can break a live pipeline silently.
    assert "`主文档.md`" in text
    assert "**at most one**, or none" in text
    assert "must never silently break a live pipeline" in text
    assert text.index("#### 0.7 Workspace hygiene") < text.index("### 1. Assess progress"), (
        "§0.7 drifted below §1 — the rule binds the round's own housekeeping, and a round "
        "that has already begun working meets it too late"
    )
    assert text.index("#### 0.7 Workspace hygiene") < text.index("#### Phase A"), (
        "§0.7 drifted below the phase table, so a round is told to tidy up only after it "
        "has produced the files the rule is about"
    )


def test_rejected_competitions_are_not_re_evaluated():
    text = PROMPT.read_text(encoding="utf-8")
    assert "never re-evaluate them in later rounds" in text
    assert "verbatim reason quoted from the rules page" in text


def test_goal_line_covers_both_prize_and_standing():
    text = PROMPT.read_text(encoding="utf-8")
    assert "win the prize" in text
    assert "leaderboard standing / percentile" in text


def test_the_scoring_rule_is_read_each_round(rendered):
    """What decides the score is a per-round reading, and hand-set state goes stale.

    Both levels are asked, and they are different questions. The file read answers "does the
    source that ships carry this?" — the render read answers "is this what a round is sent?".
    A rule wrapped in `{# … #}` keeps every word in the file and renders as nothing, so a
    file-level assertion alone would report a rule no round ever reads; measured on this very
    template in issue #1904 for §0.0. The render is therefore the reading that matters here,
    and the file assertion is kept because a rule deleted from the source must also fail.

    Whitespace is collapsed before the search, because these phrases are longer than the
    template's wrap column: pinning a line break would make the guard fail on a reflow that
    changed no word, and a guard that reports formatting as a missing rule teaches the next
    reader to edit the assertion rather than the text.

    What this does not measure: whether a round obeyed it. That is a reading of the round's
    closing summary, not something a text assertion can answer.
    """
    def flat(text: str) -> str:
        return re.sub(r"\s+", " ", text)

    source = flat(PROMPT.read_text(encoding="utf-8"))
    prompt = flat(rendered)

    for phrase, why in (
        ("What decides the score is a reading",
         "the rule that the metric's own definition is re-read each round, rather than reused "
         "as background or carried across platforms"),
        ("which submissions count",
         "the first of the four things that decide a score — without it the rule states a "
         "principle and leaves the round nothing to check"),
        ("when the standings freeze",
         "the fourth check; a rule that lists only where submissions land has read a quarter "
         "of what decides the score"),
        ("Anything set by hand once goes stale",
         "the half that decays in silence: a selection or freeze marker made in an early round "
         "does not follow the better work later rounds produce"),
    ):
        assert phrase in prompt, (
            f"the rendered competition prompt must carry {why}; {phrase!r} is not in what a "
            f"round is sent"
        )
        assert phrase in source, (
            f"{phrase!r} renders but is not in the source — these two readings are supposed to "
            f"agree, so one of them is being produced by something other than the template"
        )


def test_preparation_and_closing_summary_are_mandatory():
    text = PROMPT.read_text(encoding="utf-8")
    assert "0. Preparation (MUST run first every round)" in text
    assert "Do not skip the preparation step (even when \"everything looks fine\")" in text
    # The per-round reflection used to be appended to a `*_reflections.md` diary;
    # the rant 2026-09-14T14:35:47 sweep moved it into the round's final message.
    assert "closing summary in your final message" in text
    assert "a round that ends without a summary strands the next one" in text
    assert "Seven questions the closing summary must answer" in text
    # Rant scan must match the agent field exactly, like the other templates.
    assert "exactly `{{ task.agent }}`" in text


def test_error_handling_and_forbidden_sections_exist():
    text = PROMPT.read_text(encoding="utf-8")
    assert "### Error Handling" in text
    assert "### Forbidden" in text
    assert "Do not modify `~/.emrg/config.toml`" in text
    # The offline gate is the first forbidden item: never enter an offline one.
    assert "**Never enter a competition with an offline component**" in text


def _signal_list(text: str, prefix: str) -> list[str]:
    """One §3.2 signal list as the prompt prints it.

    Parsed from the prompt rather than duplicated here, so the tests track the
    live list instead of a copy that can drift away from it.
    """
    for line in text.splitlines():
        if line.startswith(prefix):
            listed = line.split(":", 1)[1]
            return [w.strip().strip("`").strip() for w in listed.split("、") if w.strip()]
    return []


def _english_signals(text: str) -> list[str]:
    return _signal_list(text, "- English:")


def _chinese_signals(text: str) -> list[str]:
    return _signal_list(text, "- Chinese:")


def test_english_signals_cover_the_bare_adjective():
    """The list must contain the bare adjective, not only its compounds.

    §3.1 defines the disqualifying class *semantically* — "the
    participation/evaluation process must be offline" — while §3.2 enumerates
    the signal words. Since a single hit disqualifies, a class member that is
    absent from the enumeration is not merely under-detected: it reads as *no
    offline component found* and the agent proceeds.

    Measured 2026-09-12: the English list held only compounds (`offline round`,
    `on-site`, …), so "The final round will be held offline." and "Final
    evaluation is offline." — the natural way to state the requirement — both
    missed, while the Chinese list caught every equivalent because `线下` is
    listed bare. A test that asserts each listed word *is present* can never
    see this: it pins the list against removals and is blind to omissions.
    """
    text = PROMPT.read_text(encoding="utf-8")
    signals = _english_signals(text)
    assert signals, "§3.2 has no English signal line"
    for bare in ("offline", "off-line"):
        assert bare in signals, (
            f"bare '{bare}' missing from the English list {signals} — the "
            f"compound-only enumeration misses the requirement's own phrasing"
        )
    # The class is about the process, so at least one attendance form is needed
    # to catch "participants must attend an on-site event" phrased without
    # any of the venue/round nouns.
    assert any(w in signals for w in ("on-site", "onsite", "in-person",
                                      "physical attendance", "must attend")), signals


def test_hyphenated_signals_are_listed_in_both_spellings():
    """Every hyphenated signal must also be listed with a space.

    Hyphenation is optional in English and prose uses both spellings
    interchangeably: a rules page saying "the final will be judged on site"
    states an offline evaluation requirement in the same sense as "on-site
    judging". A list that carries only the hyphenated spelling therefore misses
    the space-separated one, which is the bare-`offline` gap one spelling down.

    Measured 2026-09-13 (cycle cyc20260913-094149) on head `0244b77b`:
    `Finalists will be evaluated on site.`, `Final judging takes place on
    site.`, `Winners are required to present in person.` and `Top teams present
    in person at the awards.` each matched **none of the signals the list held
    then**, while their hyphenated spellings hit. The failure direction is the
    dangerous one — a missed hit lets the agent enter a competition with an
    offline component, whereas a spurious hit only costs an entry.

    Asserted as a rule over the parsed list rather than as three presence
    checks, so a *future* hyphenated entry added without its twin fails here.
    """
    signals = _english_signals(PROMPT.read_text(encoding="utf-8"))
    assert signals, "§3.2 has no English signal line"
    missing = [w for w in signals if "-" in w and w.replace("-", " ") not in signals]
    assert not missing, (
        f"hyphenated signal(s) {missing} have no space-separated spelling in "
        f"the list {signals} — English hyphenation is optional, so only the "
        f"hyphenated form misses the space-separated wording of the same "
        f"requirement"
    )


def test_a_stated_signal_count_matches_the_parsed_list():
    """A count the prompt states for a signal list must be that list's length.

    The list's length is a *derived* fact: `_english_signals` /
    `_chinese_signals` parse it from the prompt's own `- English:` line. A
    hand-written copy of it has nothing keeping the two in sync, and the copy
    is the one a reader believes.

    Measured 2026-09-13 (cycle cyc20260913-094149): the §3.2 note said the four
    sample phrasings "each matched **0 of the 13** signals above", and then the
    same commit that added three space twins to the list left the number at 13
    over a 16-entry list. The note's own subject is an incomplete enumeration, so
    a stale count there is read as the list being shorter than it is — and no
    test could see it, because every other test in this file reads the
    `- English:` line and never the prose around it.

    The rule is "a stated count is the current one", not "no counts": the point
    is to fail loudly the moment a signal is added or removed, with the numbers
    on both sides in the message. A historical measurement should be phrased as
    one ("none of the signals the list held then") rather than as a bare count.
    """
    text = PROMPT.read_text(encoding="utf-8")
    lengths = {len(_english_signals(text)), len(_chinese_signals(text))}
    # `\**` because the defect's number was bolded ("**0 of the 13** signals
    # above") and a pattern requiring the digit to touch the word missed it —
    # caught by mutating the fixed text back and watching this test stay green.
    stated = [int(n) for n in
              re.findall(r"(\d+)\**\s+(?:offline\s+)?signals?\b", text)]
    stale = [n for n in stated if n not in lengths]
    assert not stale, (
        f"the prompt states {stale} signal(s), but the lists it prints hold "
        f"{sorted(lengths)} entries (English / Chinese) — a hand-written count "
        f"of a parsed list drifts silently the next time the list changes"
    )


def test_signal_list_verdicts_on_sample_requirements():
    """Coverage check: sample requirements in, expected verdicts out.

    Presence assertions cannot prove completeness, but a table of sentences
    with the verdict the gate must reach *does* fail when a listed form stops
    matching the way the requirement is normally phrased — which is the failure
    that actually occurred. Verdicts are computed from the live list, so the
    test moves with the prompt.
    """
    text = PROMPT.read_text(encoding="utf-8")
    signals = _english_signals(text)
    zh_line = next((ln for ln in text.splitlines() if ln.startswith("- Chinese:")), "")
    zh_signals = [w.strip().strip("`").strip() for w in
                  zh_line.split(":", 1)[1].split("、") if w.strip()]

    cases = [
        # (sentence, language, must_be_disqualified)
        ("The final round will be held offline.", "en", True),
        ("Final evaluation is offline.", "en", True),
        ("Offline judging will take place.", "en", True),
        ("Participants must attend an offline event.", "en", True),
        ("The final round will be held on-site.", "en", True),
        ("Finalists will be evaluated on site.", "en", True),
        ("Winners are required to present in person.", "en", True),
        ("Teams must travel to Shanghai for the final.", "en", True),
        ("This is a completely online competition.", "en", False),
        ("Submissions are scored on a public leaderboard.", "en", False),
        ("本次比赛为线下比赛", "zh", True),
        ("决赛在线上进行，需线下提交纸质材料", "zh", True),
        ("参赛者需现场参加评审", "zh", True),
        ("全程线上提交，在线评测", "zh", False),
    ]
    wrong: list[str] = []
    for sentence, lang, must_hit in cases:
        words = zh_signals if lang == "zh" else signals
        # §3.2 requires a case-insensitive match: prose capitalises these
        # freely ("Offline judging…"), and matching only the lowercase
        # spelling of one's own list is the same enumeration gap one level
        # down — so the check lowercases both sides.
        low = sentence.lower()
        hit = any(w and w.lower() in low for w in words)
        if hit != must_hit:
            wrong.append(f"{sentence!r} ({lang}): expected "
                         f"{'disqualify' if must_hit else 'allow'}, got "
                         f"{'disqualify' if hit else 'allow'}")
    assert not wrong, "offline-signal coverage failures:\n  " + "\n  ".join(wrong)


# --- negation: the case a substring list structurally cannot decide ---------
#
# Measured on the head that introduced this gate (`7d56e34`, cycle
# cyc20260912-174026): all six sentences below were disqualified, and every one
# of them is *positive evidence for online-only*. The consequence is not a lost
# round — §4 files a rejection as `never re-evaluated`, so a mechanical hit
# permanently excludes a legitimate online competition.
#
# The negation is unbounded (any list word × any negation form), so §3.2.1
# cannot be an extra word list: enumerating negated spellings is the same
# enumeration gap one level up. What is pinned here is therefore the *prompt's*
# obligation — that the clause exists, names these forms, and cannot be dropped
# — plus a reference implementation showing the two directions separate.

NEGATION_FORMS = [
    # negations (the requirement is absent)
    "no ", "without ", "not required", "-free", "无需", "没有", "取消",
    # reclassifications (the requirement is satisfied online instead)
    "改为线上", "moved online", "replaced by online", "virtual ", "remotely",
    "online ",
]


def _negated_context(sentence: str, word: str) -> bool:
    """Whether `word`'s hit in `sentence` sits inside a cancelling construction.

    A reference implementation of §3.2.1, deliberately window-based rather than
    grammar-based: the clause is a *reading instruction* for the agent, and a
    test that pretended to parse natural language would assert precision this
    procedure does not claim. What it must get right is that a negation
    **near** the hit cancels it.

    **Sentence-bounded, and that bound is the point** (`cyc20260912-180719`).
    §3.2.1 removes a hit only where the negation **cancels** the requirement -
    "never hits where it merely sits nearby" - so a negation belonging to an
    earlier sentence must not reach across and cancel this one:

        There is no issue with the entry requirements. Finalists must travel to
        Shanghai for the award gala.

    The `no ` negates *issue*, and the travel requirement stands, so this must
    disqualify. A pure window (`idx-40`) reaches across the sentence boundary and
    calls it cancelled - i.e. it cancels on exactly the "merely nearby" reading
    the clause forbids. Measured: with the window alone this sentence read as an
    allowance, and narrowing §3.2.1 in the prompt to a proximity rule left all
    21 tests passing, so nothing pinned the distinction.
    """
    low = sentence.lower()
    idx = low.find(word.lower())
    if idx < 0:
        return False
    head = low[max(0, idx - 40): idx]
    # Keep only the hit's own sentence: a negation in an earlier one is a
    # different statement about a different thing.
    for sep in (". ", "? ", "! ", "; ", "。", "！", "？", "；"):
        if sep in head:
            head = head.rsplit(sep, 1)[1]
    window = head + low[idx: idx + len(word) + 25]
    return any(form in window for form in NEGATION_FORMS)


def test_negation_clause_exists_in_the_gate():
    """The clause itself must be stated, not merely implemented by luck.

    A future edit could delete §3.2.1 while every remaining test still passed —
    the positive-direction tests above are blind to it, which is exactly the
    shape of the defect this clause fixes.
    """
    text = PROMPT.read_text(encoding="utf-8")
    assert "negated" in text, "§3.2.1 (negation handling) was removed"
    assert "do not disqualify" in text
    # The reclassification direction (offline -> moved online) is part of it.
    assert "改为线上" in text
    # And the asymmetry: a doubt between a negation and a requirement resolves
    # to *offline*, so the clause cannot be read as a general relaxer.
    assert "treat as offline" in text
    # The clause cancels only where the negation *cancels the requirement*, never
    # where it "merely sits nearby". Measured (`cyc20260912-180719`): rewriting
    # §3.2.1 into a proximity rule ("the negation applies only when it appears
    # immediately before the signal word") left all 21 tests passing, because
    # nothing asserted this sentence - so the narrowing was invisible. A window
    # of characters is not the clause's rule; the sentence the hit sits in is.
    assert "merely sits nearby" in text, (
        "§3.2.1 must keep the 'never merely sits nearby' restriction - without it "
        "the clause can be narrowed to a proximity rule and a negation in an "
        "earlier sentence would cancel a real requirement"
    )


def test_negated_hits_are_not_disqualifiers():
    """Both directions at once, on real sentences (cycle cyc20260912-174026)."""
    text = PROMPT.read_text(encoding="utf-8")
    signals = _english_signals(text)
    zh_signals = [w.strip().strip("`").strip() for w in
                  text.split("- Chinese: ", 1)[1].split("\n")[0].split("、")
                  if w.strip()]

    cases = [
        # (sentence, lang, must_be_disqualified)
        # online-only statements that contain a list word — must be ALLOWED
        ("No travel required - the competition is fully online.", "en", False),
        ("must attend the online webinar", "en", False),
        ("The virtual venue is our Discord server.", "en", False),
        ("No on-site component; submissions are online only.", "en", False),
        ("Prizes are awarded without any in-person ceremony.", "en", False),
        ("线下比赛改为线上进行", "zh", False),
        # the same words stated as a REQUIREMENT — must still DISQUALIFY
        ("Teams must travel to Shanghai for the final.", "en", True),
        ("Participants must attend the offline final.", "en", True),
        ("The final round will be held at the venue in Beijing.", "en", True),
        ("参赛者需现场参加评审", "zh", True),
        # negation that does NOT cancel the requirement (ambiguous) → offline
        ("The final is on-site, though no travel support is provided.", "en", True),
        # A negation in an EARLIER sentence must not reach across and cancel this
        # one: §3.2.1 removes only hits where the negation *cancels* the
        # requirement, "never hits where it merely sits nearby".
        #
        # `travel` is the raw hit and none of the override phrases below appear,
        # so the verdict here is decided by the negation model and nothing else.
        # The first draft of these cases used sentences like "Finalists must
        # travel to Shanghai", where `must travel` is itself an override phrase -
        # they passed whatever the model said, and removing the model's
        # sentence-bounding still left the suite green (measured). These do not.
        ("There is no fee. Finalists travel to Shanghai for the award gala.", "en", True),
        ("No fee is charged. Selected teams travel to Shanghai in June.", "en", True),
        ("The portal is free. Winners travel to Shanghai for the ceremony.", "en", True),
    ]

    wrong: list[str] = []
    for sentence, lang, must_hit in cases:
        words = zh_signals if lang == "zh" else signals
        low = sentence.lower()
        raw = next((w for w in words if w and w.lower() in low), None)
        # §3.2 + §3.2.1: a hit disqualifies unless the hit is inside a
        # cancelling construction — except when the sentence *also* states a
        # requirement, which §3.2.1 resolves to offline.
        if raw is None:
            verdict = False
        elif _negated_context(sentence, raw) and not any(
            req in low for req in ("is on-site", "will be held at", "must travel",
                                   "must attend an offline", "must attend the offline",
                                   "现场", "需现场")
        ):
            verdict = False
        else:
            verdict = True
        if verdict != must_hit:
            wrong.append(f"{sentence!r}: expected "
                         f"{'disqualify' if must_hit else 'allow'}, got "
                         f"{'disqualify' if verdict else 'allow'} (raw hit {raw!r})")
    assert not wrong, "negation-gate failures:\n  " + "\n  ".join(wrong)


def test_machine_rejection_is_not_permanent():
    """A gate-word false positive must not be filed as `never re-evaluated`.

    §4 is what turns a mechanical hit into a permanent exclusion, so the split
    is part of the fix, not cosmetic.

    Asserted against the **§4 section block** rather than the whole document: the
    first version of this test checked only that the phrase occurred somewhere,
    and phase A (§, "does **not** go in that section") mentions it in prose — so
    renaming the actual section heading away survived the test unchanged. A
    presence check that can be satisfied by a mention of the thing is the same
    class of blindness this cycle is fixing, one level up.

    §4 was a fenced ```markdown state-file template until rant 2026-09-14T14:35:47
    removed the state file; the same two rejection entries now live as memory-entry
    forms in prose, and the block is parsed by section.
    """
    text = PROMPT.read_text(encoding="utf-8")
    block = text.split("### 4. Cross-round state lives", 1)[1].split("\n---", 1)[0]
    assert "**Rejected (never re-evaluated)**" in block, (
        "§4 lost its permanent-rejection entry form"
    )
    assert "**Rejected — needs a human read" in block, (
        "§4 has no re-checkable rejection entry form, so a §3.2.1 negation "
        "override would be frozen as permanent"
    )
    # Phase A must route to the right one of the two.
    assert "does **not** go in that section" in text


def test_the_host_facing_half_of_the_summary_is_required():
    """The host asked for these readings twice; the second ask is why they are pinned.

    Measured from the host's own messages in this task's session
    (`scripts/find-host-message.py --pattern '自说自话|卡点'` finds both):
    2026-10-01T15:58:48 asked what the competition is, what is being done, what the
    blocker is and what the score is; 2026-10-02T07:56:20 asked again, after that
    answer did not land, adding how likely the goal is, what is still missing, and
    what to do about the blocker. A request made twice is a requirement, so each
    half is pinned separately — a summary that answers the round's own seven
    questions and none of these passed before.
    """
    text = PROMPT.read_text(encoding="utf-8")
    block = text.split("**Three more the host asked for by name.", 1)
    assert len(block) == 2, (
        "§5 lost the host-facing half of the closing summary: the second ask "
        "(2026-10-02T07:56:20) is not reflected anywhere"
    )
    host_half = block[1].split("\n---", 1)[0]
    assert "How likely is the goal to be reached, and on what evidence?" in host_half, (
        "the closing summary does not have to state the odds, which is the first "
        "thing the host asked for the second time"
    )
    assert "What is still missing between here and the goal?" in host_half, (
        "the closing summary does not have to state the gap to the goal"
    )
    assert "What does the host have to decide or do?" in host_half, (
        "the closing summary does not have to name what the host must decide"
    )
    # A probability without its basis is what "don't talk to yourself" rejected, so
    # the basis is part of the requirement, not decoration.
    assert "with the basis it is estimated from" in host_half
    # The question stayed, its answer's form changed: the round rules forbid writing
    # that the goal is unlikely, so §5 asks for the reading and the gap in the
    # competition's own metric rather than a bare probability. Pinned here so the
    # pair cannot be split in silence — see
    # `test_a_discouraging_reading_is_written_as_the_gap_not_as_a_verdict`.
    assert "the current reading" in host_half and "the gap to the payout line" in host_half
    # And the reader it is written for — the host reads this, not only the next round.
    assert "not read the previous round's messages" in host_half


def test_the_blocker_rule_has_all_three_cases_and_keeps_the_forbidden_list_above_it():
    """Three cases, and the third one is the load-bearing half.

    The first two cases are tempting to over-apply: "bypass it yourself" reads as a
    licence, and a later edit could drop the "no method found → ask the host" case
    or leave it available to the hard constraints. Both directions are pinned —
    every case must still be named, and the Forbidden list must be stated to win
    over a score-neutral bypass.
    """
    text = PROMPT.read_text(encoding="utf-8")
    block = text.split("**The blocker rule**", 1)
    assert len(block) == 2, (
        "the host's blocker rule (2026-10-02T07:56:20) is gone: with no case rule, "
        "a round either bypasses blindly or strands the blocker"
    )
    rule = block[1].split("\n### ", 1)[0]
    assert "does not touch the competition's score" in rule, "the first case is missing"
    assert "would affect the score" in rule, "the second case is missing"
    assert "No way past is found" in rule and "as a question to the host" in rule, (
        "the third case is missing — that is the one the host asked to be consulted "
        "on ('如果你找不到方法，就和我讨论')"
    )
    # Both directions of case 1 and 2: the bypass is permitted only where it is
    # score-neutral, and it must be recorded rather than asserted.
    assert "Take it yourself" in rule and "why it was score-neutral" in rule
    assert "Do not take it" in rule
    assert "The Forbidden list is not a blocker to be bypassed" in rule
    for forbidden_that_a_bypass_may_not_step_over in (
        "real-name / phone / payment verification",
        "the offline gate",
    ):
        assert forbidden_that_a_bypass_may_not_step_over in rule, (
            "the blocker rule does not exclude the hard constraints, so "
            "'bypassing does not affect the score' can be read as covering them"
        )


# --- §0.0: the cadence rule the host asked to be written *in this file* --------------------------
#
# Measured 2026-10-07 (cycle cyc20261007-130240). The host asked, by name, for the
# no-slowdown rule to live in `emrg/server/competition_prompt.md` — the message is
# quotable (`find-host-message.py --pattern '如果有闲置的资源'` ->
# 2026-10-06T10:40:46) — and the file never carried it: the rule existed only in the
# tree the running daemon renders from (`~/.emrg/install/source/emrg/server/`), which is
# not under version control and is overwritten by the next install/upgrade. A rule that
# lives in a derived copy is a rule that survives until the next build; these tests are
# what keeps it in the source that ships.
#
# Scope note, the same shape as the §4 block below: each assertion is taken against the
# `### 0.0` block rather than the whole file, because the file discusses the vibe check
# and cadence in other sections too — a whole-document substring check would stay green
# after the block was deleted, which is a failure to measure rather than a pass.


def _block_0_0() -> tuple[str, str]:
    """The `### 0.0` cadence block, and the whole text it came from."""
    text = PROMPT.read_text(encoding="utf-8")
    parts = text.split("### 0.0 ", 1)
    assert len(parts) == 2, (
        "the `### 0.0` cadence block is gone from competition_prompt.md — the host asked for "
        "this rule *in this file* (2026-10-06T10:40:46) and it may not live only in the "
        "installed copy, which the next upgrade overwrites"
    )
    block = parts[1].split("\n### Current State", 1)[0]
    assert block.strip(), "the `### 0.0` heading is there but its body is gone"
    return block, text


def test_the_cadence_block_is_read_before_the_round_starts():
    """Placement, not just presence: §0.0 sits above the first step it constrains.

    A rule parked at the bottom of the file would be found by a reader looking for it and
    missed by a round that is already running — and the round is what it binds.
    """
    _block, text = _block_0_0()
    assert text.index("### 0.0 ") < text.index("### Current State"), (
        "the cadence block drifted below the header block — it must be read before the round begins"
    )
    assert text.index("### 0.0 ") < text.index("### 0. Preparation"), (
        "the cadence block drifted below §0. Preparation — the rule binds the round it precedes"
    )


def test_the_rejected_slowdown_grounds_are_named_in_the_block():
    """The rule lists the grounds it forbids, so it cannot be read as leaving room for them.

    "Never recommend a slowdown" alone is defeated by a plausible reason; the three the
    host rejected by name are the ones a round reached for on 2026-10-06.
    """
    block, _text = _block_0_0()
    assert "`recommend_slowdown` must be `false`, always" in block
    for ground in (
        "no lever left this season",
        "nothing to do but wait for the score",
        "submission quota has not refreshed",
    ):
        assert ground in block, (
            f"the rejected slowdown ground {ground!r} is gone from §0.0 — without it the rule "
            "reads as a preference a round can argue its way around"
        )


def test_waiting_is_not_idle_and_idle_resources_go_to_more_competitions():
    """The two affirmative halves: where the round turns, and what idle compute buys."""
    block, _text = _block_0_0()
    assert '"Waiting" is not "idle"' in block
    assert "turn to another live competition immediately" in block
    assert "Phase A" in block, (
        "the fallback to discovery is gone: a round with every competition waiting has nothing "
        "left to do in §0.0's terms and would idle"
    )
    assert "Idle resources go into entering more competitions" in block
    # The order of gates is load-bearing: expansion is not a licence to skip the
    # online-only gate or the compute check the earlier sections make mandatory.
    assert block.index("§3 online-only gate") < block.index("§0.6 compute feasibility"), (
        "the expansion clause no longer runs the platforms through the online gate before the "
        "compute check — the hard constraint is not bypassed by an idle CPU"
    )


def test_only_the_host_may_slow_the_task_down():
    """The single exception is named, so no in-round judgement can invent another."""
    block, _text = _block_0_0()
    assert "Only the host may slow this task down" in block
    assert "only by explicitly asking for it" in block


def test_think_before_acting_covers_the_irreversible_actions():
    """A quota-consuming action states its case first — the four parts, all present."""
    block, _text = _block_0_0()
    assert "Think before acting" in block
    for part in (
        "what will be done",
        "on what evidence",
        "the expected result",
        "the cost of failure",
    ):
        assert part in block, f"the pre-action statement is missing {part!r}"


def test_the_cadence_block_carries_the_host_messages_it_rests_on():
    """A rule recorded as the host's is a message that can be pointed at (R7).

    Quoted verbatim, in the host's own words, each with its timestamp — so the next
    cycle can re-run `find-host-message.py` on either quote instead of trusting this file.
    """
    block, _text = _block_0_0()
    for quote, stamp in (
        (
            "我看现在又降频了，为什么，不是要求"
            "evolution/emrg/emrg/server/competition_prompt.md禁止降频了吗？",
            "2026-10-06T10:40:46",
        ),
        ("如果有闲置的资源，则应该参加更多比赛来赢得更多奖金", "2026-10-06T10:40:46"),
        ("每轮对每个比赛都要做工作，不是每轮只做一个比赛", "2026-10-07T11:52:53"),
    ):
        assert quote in block, (
            f"the host's own words are gone from §0.0: {quote!r} — a paraphrase cannot be "
            "looked up, and the rule would rest on this instance's inference instead"
        )
        assert stamp in block, f"the quote {quote!r} no longer carries its timestamp {stamp}"


# --- §0.0, second level: the prompt a round is actually sent --------------------------------------
#
# The block above measures the source that ships — the concern that the rule must not live
# only in the installed copy the next upgrade overwrites (measured 2026-10-07,
# cyc20261007-130240). These measure the prompt that arrives: `competition_prompt.md` is a
# Jinja2 template and only its render reaches the round, so a Jinja construct can take the
# rule out of every round while the file keeps it whole. Same defect as issue #1902, in a
# different carrier; `emrg/server/prompts/vibe_check.j2` is where that one was fixed.


def _rendered_block_0_0(rendered: str) -> str:
    """The `### 0.0` cadence block as the round reads it — or a failure to measure.

    Fails rather than passing quietly: a missing block must never read as "nothing to
    assert", which is the shape a guard takes when its subject is deleted.
    """
    parts = rendered.split("### 0.0 ", 1)
    assert len(parts) == 2, (
        "the rendered competition prompt has no `### 0.0` cadence block — the round is "
        "sent a prompt without the host's no-slowdown rule (2026-10-06T10:40:46), whether "
        "or not the file still carries it"
    )
    block = parts[1].split("\n### Current State", 1)[0]
    assert block.strip(), (
        "the rendered `### 0.0` heading is there but its body is empty — a template "
        "construct kept the title and sent nothing underneath it"
    )
    return block


def test_the_cadence_block_reaches_the_round_and_not_merely_the_file(rendered):
    """Every load-bearing phrase of §0.0, asserted on the prompt the agent is sent.

    The file-level tests above cannot see this failure: a Jinja comment around the block
    leaves each phrase in `competition_prompt.md` and drops all of them from the render,
    so the rule the host asked to be written *in this file* stops binding the round while
    the guard stays green.
    """
    block = _rendered_block_0_0(rendered)
    for term in (
        "Never reduce the cadence",
        "`recommend_slowdown` must be `false`, always",
        "no lever left this season",
        "nothing to do but wait for the score",
        "submission quota has not refreshed",
        '"Waiting" is not "idle"',
        "Idle resources go into entering more competitions",
        "Only the host may slow this task down",
    ):
        assert term in block, (
            f"the rendered §0.0 block does not carry {term!r}: the rule is in the file and "
            "in no render, which is the one failure a file-level guard cannot see — a Jinja "
            + "construct (`{{# … #}}`, `{{% if false %}}`) removes it from every round's "
            + "prompt while the file keeps every phrase"
        )


def test_the_rendered_cadence_block_still_precedes_the_round_it_binds(rendered):
    """Placement has to survive rendering, which is where the reader meets it.

    The file-level placement test can hold while the render moves or drops the block
    entirely — this is the ordering the round actually walks past.
    """
    assert rendered.index("### 0.0 ") < rendered.index("### Current State"), (
        "the rendered cadence block drifted below the header block — the round meets its "
        "agent and time anchor before it meets the rule that governs the round"
    )
    assert rendered.index("### 0.0 ") < rendered.index("### 0. Preparation"), (
        "the rendered cadence block drifted below §0. Preparation, so a round that has "
        "started working has not been told the rule that binds it"
    )


def test_the_render_substitutes_values_and_carries_no_template_syntax(rendered):
    """The converse: a render must not be a file read wearing a render's name.

    Two ways a render assertion goes vacuous, both closed here. If the block were read
    with its syntax intact — a `{% raw %}` wrapper, or the text copied out of a literal —
    the round would be sent `{{ task.agent }}` and would be told nothing; and if what
    this test examined were the file, the same assertion catches it. Then the substituted
    value has to arrive, so "it rendered" is measured by what the round can read rather
    than by the absence of braces alone.
    """
    assert "{{" not in rendered and "{%" not in rendered, (
        "the rendered competition prompt still carries Jinja syntax — the round is sent "
        "template source rather than the prompt it names"
    )
    assert PROMPT_PROJECT in rendered, (
        "the render does not carry the agent name "
        f"{PROMPT_PROJECT!r}: "
        "`{{ task.agent }}` resolved to nothing (`jinja2.Undefined` renders empty), so "
        "the round is told its agent is blank"
    )


# --- the 2026-10-09 rant's two sections, at the level a round actually reads ----------------------
#
# Same two levels as above, for the same reason. §0.7 and §1's first sentence are new text
# in a Jinja2 template: a `{# … #}` around either removes it from every round's prompt while
# the file keeps every character, and that is the one failure a file-level guard cannot see
# (measured 2026-10-08, `cyc20261008-082424`, issue #1904 — a hand-written mutation arm
# reported SURVIVED for exactly that one).


def test_the_coverage_and_hygiene_rules_reach_the_round_and_not_merely_the_file(rendered):
    """The new sentences, asserted on the prompt a round is sent."""
    for term in (
        "The round's unit of work is the set of ACTIVE competitions",
        "Every round must traverse ALL active competitions",
        '"One phase per round" means one phase PER COMPETITION',
        "Every competition must show an increment each round",
        "a per-competition submission-quota ledger",
        "I was busy with another competition",
        "#### 0.7 Workspace hygiene",
        "Root-level budget",
        "No `.bak` litter",
        "Predictable paths",
        "Verify after reorganising",
        "must never silently break a live pipeline",
    ):
        assert term in rendered, (
            f"the rendered competition prompt does not carry {term!r}: the rule is in the "
            "file and in no render, so every round is sent a prompt without it"
        )


def test_the_rendered_hygiene_section_still_precedes_the_round_it_binds(rendered):
    """Placement has to survive rendering, which is where the round meets it."""
    assert rendered.index("#### 0.7 Workspace hygiene") < rendered.index(
        "### 1. Assess progress"
    ), (
        "the rendered §0.7 drifted below §1 — a round that has already started working is "
        "told to tidy up after the fact"
    )
    assert "'s unit of work is the set of ACTIVE competitions" in rendered, (
        "§1's first sentence did not survive the render, so the round is sent the section "
        "without the rule that fixes its mis-reading"
    )


def test_phase_d_establishes_the_answer_on_a_sample_before_changing_the_class():
    """§Phase D must state how the right answer is established, not only that a round improves.

    "Local validation → submit → record the delta" is a valid unit only when what was adjusted
    is uniform. On a class of cases handled wrongly, a whole-class change mixes the cases it
    fixed with the cases it broke into one unattributable score, so the round that made it can
    neither keep the change nor revert it — and the submissions it spent bought nothing. The
    four steps below are what separates a round that improved something from a round that
    classified, counted or observed and stopped; the last one is the step most easily left out,
    which is why it is asserted by name rather than implied.
    """
    text = PROMPT.read_text(encoding="utf-8")
    start = text.index("#### Phase D — Iteration")
    end = text.index("#### Phase E — Deadline wrap-up")
    phase_d = text[start:end]
    for step in (
        "**Classify** the cases that are not right",
        "**Draw a small sample from one class and submit it as a probe**",
        "**Read the probe's outcome as that class's answer",
        "**Turn the answer into one concrete change**",
    ):
        assert step in phase_d, f"§Phase D is missing the step {step!r}"
    assert "not the whole class" in phase_d, (
        "§Phase D must say the probe is a sample rather than the whole class — that is the "
        "difference between an attributable reading and one number with two opposite causes"
    )
    assert "Step 4 is the one most easily left out" in phase_d, (
        "§Phase D must name the step most easily skipped, or a round that only observed reads "
        "as if it had improved something"
    )


def test_a_probe_is_read_against_an_absolute_baseline():
    """§Phase D must say what a probe is read *against*, not only that it is read.

    The four steps read as complete while naming no baseline, and the baseline decides
    whether the reading is decidable at all. A probe compared with the best submission
    leaves a zero delta with two opposite meanings — the cases it targeted came back, or
    it touched none of them — and one number is printed for both, so the submission
    bought no fact. An absolute floor makes each probe a yes/no question. Asserted by
    name for the same reason step 4 is: the rule is reasoning, and a reader who is not
    given the reasoning has no ground for spending a submission on a floor.
    """
    text = PROMPT.read_text(encoding="utf-8")
    start = text.index("#### Phase D — Iteration")
    end = text.index("#### Phase E — Deadline wrap-up")
    phase_d = text[start:end]
    assert "against an absolute baseline rather than against your best score so far" in phase_d, (
        "§Phase D does not name the baseline a probe is read against, so a zero delta "
        "reads the same for a case that came back and one that was never touched"
    )
    assert "deliberately wrong answer in the same submission format" in phase_d, (
        "§Phase D must say the floor submission is graded rather than rejected, or the "
        "floor itself buys nothing and the round is back to an ambiguous zero"
    )


def test_a_discouraging_reading_is_written_as_the_gap_not_as_a_verdict(rendered):
    """§5 asks how likely the goal is; the answer has one accepted form.

    The question and the round rules used to point in opposite directions: §5 asked for a
    probability, while the round rules forbid writing that the goal is unlikely and accept
    only the current reading, the gap in the competition's own metric, the levers not yet
    tried and the next attempt. A round reading both had to pick one silently, and a lean
    estimate was the case where the two collide.

    So the question is answered **in the metric**, and a discouraging answer is the gap
    plus the untried levers — never a verdict that the goal cannot be reached. Asserted on
    the render, because that is what a round receives: a `{# … #}` around the bullet leaves
    the file intact and the round without the rule.
    """
    assert "answered in the competition's own metric" in rendered, (
        "§5 must send the probability question back to the competition's own metric"
    )
    assert "never as a verdict that the goal cannot be reached" in rendered, (
        "§5 must say what a discouraging reading is written as, or the round is left to "
        "choose between this question and its own no-conclusion rule"
    )
    assert "the levers not yet tried" in rendered
    assert "a probability for the goal" not in rendered, (
        "asking for a bare probability is the instruction the round rules forbid answering — "
        "the pair must not come back on one side only"
    )


def test_a_best_score_is_a_reading_and_a_round_brings_an_untried_attempt(rendered):
    """Phase D must say what a recorded score is, and what iteration requires.

    Without this, a round can record a best score, re-state it next round, and meet every
    stated requirement — nothing distinguished a reading from a conclusion, and nothing
    asked for an attempt the previous round had not tried.
    """
    assert "A recorded best score is a reading, not a conclusion" in rendered
    assert "at least one attempt it had not tried before" in rendered, (
        "Phase D must ask for an attempt the last round had not tried, or re-measuring "
        "counts as iteration"
    )
    assert "has recorded a reading without iterating" in rendered


# --- the general rules the template used to leave to an installation's extra_prompt ----------------
#
# `competition_prompt.md` keeps a slot for the installation's own additions, and the rules reported
# missing from that slot do not split evenly. Measured on this file before the change, three of them
# were already stated in the body under other words — the scoring rule is re-read each round (§2),
# the round's unit of work is the *set* of active competitions (§1), and the browser is for
# login-walled pages while anything genuinely public is read over plain HTTP/API (§0.1) — and four
# were absent: a claim's provenance, two limits on what a measurement can carry, and the hand-set
# state a round has to name. Those four are added here; the three that already stood gain a pin
# where they had none.
#
# Each is asserted against the section that owns its subject rather than against the whole document,
# for the reason §4's and §0.0's guards already give: this file discusses scoring, measurement and
# provenance in several places, so a whole-document substring check stays green after the section
# itself is deleted — a failure to measure rather than a pass.


def _flat(text: str) -> str:
    """One space for every run of whitespace, so a reflow is not a missing rule."""
    return re.sub(r"\s+", " ", text)


def _section(text: str, heading: str, end: str) -> str:
    """One section's body, or a failure to measure.

    Fails rather than returning `""`: a guard whose subject is gone has to say so, because an empty
    block turns every `in` assertion built on it into a report about wording.
    """
    parts = text.split(heading, 1)
    assert len(parts) == 2, f"{heading!r} is gone from the competition prompt"
    body = parts[1].split(end, 1)[0]
    assert body.strip(), f"{heading!r} is there but its body is empty"
    return body


def _goal_line(text: str) -> str:
    return _section(text, "### 2. Goal line and prize reading", "\n---")


def _phase_d(text: str) -> str:
    return _section(text, "#### Phase D — Iteration", "#### Phase E")


def _environment(text: str) -> str:
    return _section(text, "#### 0.1 Environment verification", "\n#### 0.2")


def test_a_claim_about_the_rules_carries_its_provenance(rendered):
    """§2: a "not allowed / at the ceiling / untried" claim is read from the rules, with a source.

    The three kinds are the ones a round can actually hold — official wording quoted, a reading it
    took, its own assumption labelled as one. The rule exists because the fourth thing a round
    reaches for (an index line, an earlier summary, another competitor's phrasing) is none of them
    and reads exactly like evidence. It is a claim about the *rules*, so it is answered where the
    rules are written, which is §2 rather than the offline gate.

    Asserted on both readings for the reason `test_the_scoring_rule_is_read_each_round` gives: a
    `{# … #}` keeps every word in the file and sends the round none of them.
    """
    for label, text in (("source", PROMPT.read_text(encoding="utf-8")), ("render", rendered)):
        block = _flat(_goal_line(text))
        for phrase, why in (
            ("A conclusion about what the rules allow is a reading with a source",
             "the provenance rule itself"),
            ("the platform's official wording, quoted",
             "the only kind a later round or the host can check by re-reading the rules"),
            ("a reading this round took",
             "the middle kind — the round's own measurement rather than someone's memory"),
            ("an assumption written into a prompt",
             "the kind that reads like a rule and has nothing behind it"),
            ("from an earlier summary or from another competitor's phrasing",
             "the sources that are not evidence, named so the rule cannot be read as accepting them"),
            ("is re-read before it is acted on",
             "what a round does with an inherited claim: re-read it, not act on it"),
        ):
            assert phrase in block, (
                f"the {label} of §2 does not carry {why}; {phrase!r} is missing, so a round is "
                f"left to inherit 'we are not allowed' from whatever it read last"
            )


def test_a_resolution_finer_than_the_lever_leaves_the_comparison_undecidable(rendered):
    """Phase D: a zero delta is undecidable when the metric is coarser than the effect.

    Phase D's probe-and-floor passage argues one instance of this — a zero delta read against a
    best score — so the general rule was absent, and a round working a coarse platform metric could
    write "no difference" as a conclusion. The two causes of a zero (nothing changed; the change is
    below what the platform reports) print the same number, and only the stated resolution
    separates them.
    """
    for label, text in (("source", PROMPT.read_text(encoding="utf-8")), ("render", rendered)):
        block = _flat(_phase_d(text))
        for phrase in (
            "A local resolution finer than the lever makes the comparison undecidable",
            "a delta of zero has two causes",
            "an unread measurement, not a conclusion",
            "the size of the effect it was looking for",
            "the resolution it measures with",
        ):
            assert phrase in block, (
                f"the {label} of Phase D does not carry {phrase!r}: a zero delta reads as a "
                f"result whatever the platform's resolution is"
            )


def test_an_agreement_in_counts_cannot_establish_a_candidate(rendered):
    """Phase D: agreement is a *non-contradiction*, and the rule says what would establish a fact.

    Counting is the cheapest reading available and the most tempting to stop at: two instruments
    that agree are read as confirmation, when all they show is that neither contradicted the other.
    The rule names the test that distinguishes the two — an outcome that would have differed had
    the fact been otherwise — so "the counts match" cannot be written as evidence for a candidate.
    """
    for label, text in (("source", PROMPT.read_text(encoding="utf-8")), ("render", rendered)):
        block = _flat(_phase_d(text))
        for phrase in (
            "An agreement in counts can exclude a candidate, never establish one",
            "they do not show that the thing was observed",
            "would have differed had the fact been otherwise",
            "names what it would have seen under the alternative",
        ):
            assert phrase in block, (
                f"the {label} of Phase D does not carry {phrase!r}: agreeing counts are read as "
                f"confirmation of the candidate they were supposed to exclude"
            )


def test_a_derived_artefact_is_evidence_only_once_it_is_rebuilt(rendered):
    """Phase D: a pipeline's product is a claim about the pipeline until it is rebuilt.

    A submission table, a merged file or a generated summary is derived, and a derived thing
    carries whatever the code that produced it carries. Rebuilding it from its inputs and diffing
    the two line by line is what turns it into evidence; without that step a stale artefact is
    indistinguishable from a fresh one in every reading a round takes of it.
    """
    for label, text in (("source", PROMPT.read_text(encoding="utf-8")), ("render", rendered)):
        block = _flat(_phase_d(text))
        for phrase in (
            "A derived artefact is evidence only after it is rebuilt and compared line by line",
            "rebuilt from its inputs and diffed against the first copy",
            "does not reproduce byte for byte",
            "the pipeline's output rather than the data's",
        ):
            assert phrase in block, (
                f"the {label} of Phase D does not carry {phrase!r}: a derived artefact is used as "
                f"a reading of the data without anything having rebuilt it"
            )


def test_the_round_names_the_state_it_set_by_hand(rendered):
    """§2: re-reading the hand-made state is not enough — the round says what it made by hand.

    The rule already asked the round to re-read a manual selection, a pending queue or a freeze
    marker. A round satisfied that by reading the selection it had made, without ever saying it had
    made one, so the half that decays in silence had no place in the summary and nothing after the
    fact could date the decision.

    The reverse pin is the retired sentence itself: the version that asked only for a re-read must
    not stand beside the one that also asks for the naming, because two versions of one instruction
    leave the round picking the weaker.
    """
    source = PROMPT.read_text(encoding="utf-8")
    for label, text in (("source", source), ("render", rendered)):
        assert "say what this round itself set or selected by hand" in _flat(_goal_line(text)), (
            f"the {label} of §2 does not ask the round to name the state it set by hand, so a "
            f"manual selection is re-read but never reported as this round's own act"
        )
    assert "Re-read that state each round, and treat a mismatch" not in _flat(source), (
        "the sentence that asked only for a re-read is back beside the one that also asks the "
        "round to name what it set by hand — two versions of one instruction, and the weaker one "
        "is what a round will follow"
    )


def test_the_browser_is_scoped_to_login_walls(rendered):
    """§0.1's two halves, pinned where a round reads them — there was no guard on either.

    "Browser first" and "prefer the cheaper path when it is genuinely public" are one instruction
    only while both stand. The heading alone reads as a mandate to drive a browser at a public
    leaderboard; the exemption alone reads as a licence to skip a login-walled rules page, which
    §3.5 turns into a rejection of the competition. Nothing pinned either half, so this scope could
    be re-settled by deleting one sentence in silence.
    """
    for label, text in (("source", PROMPT.read_text(encoding="utf-8")), ("render", rendered)):
        block = _flat(_environment(text))
        for phrase, why in (
            ("Browser first", "the default route, which is where the login state lives"),
            ("the login state exists only in the real browser",
             "the reason the browser is the default at all"),
            ("does **not** need the browser",
             "the exemption that keeps the heading from reading as a mandate"),
            ("prefer the cheaper path when it is genuinely public",
             "the rule the exemption rests on: public means HTTP/API, not a browser"),
        ):
            assert phrase in block, (
                f"the {label} of §0.1 does not carry {why}; {phrase!r} is missing, so 'browser "
                f"first' and the cheaper path are no longer one instruction"
            )


# --- the rules a round needs that the template used to leave to an installation --------------------
#
# Each was paid for in a lost submission or a misread score, and each lived only in an installation's
# own `extra_prompt` — a field no other installation can read. They are ported here as method, in the
# template's own words, into the three sections that own their subjects:
#
#   * the iteration phase, which decides how a submission is spent;
#   * the goal-line section, which decides how a score is read;
#   * the recording section, which requires the closing summary.
#
# The pins below are section-scoped for the reason §4's and §0.0's guards already give: this file
# discusses measurement, scoring and self-review in several places, so a whole-document substring
# check stays green after the section that owns the rule is deleted. A rule stated nowhere and a rule
# stated in the wrong section read the same to a round that is looking for it where it belongs.


def _recording(text: str) -> str:
    return _section(text, "### 5. Recording", "\n### Error Handling")


def _assess(text: str) -> str:
    return _section(text, "### 1. Assess progress", "\n### Phases")


# (i) how a submission is spent — the iteration phase
_VOTE_ECONOMY_RULES = (
    "A constant answer is a free estimator of the mean and of nothing else",
    "An option is free only when its expectation is positive",
    "A dead path is revived only after proving it is not an identity transform",
    "A probe's order is its amplitude times its prior hit rate",
    "One knob per vote while votes are scarce",
    "Several columns in one submission means a per-column report",
    "A number is read only after the line that printed it is identified",
)

# (ii) how a reading travels — the goal-line section
_RULER_RULES = (
    "A reading travels no further than the ruler that took it",
    "What a manual selection costs depends on how the platform aggregates",
    "The platform's free readings are exhausted before a slot is spent",
)

# (iii) the gate lines the closing summary owes — the recording section
_GATE_LINES = (
    "Translation",
    "Scope",
    "Coverage and counterexamples",
    "Resolution",
    "The explanation least wanted",
    "Reverse self-defence",
)

# (iv) how a resetting allowance is spent — the assess section's quota ledger
_QUOTA_SPEND_RULE = (
    "A quota that resets is spent across its period, not at the start of it",
    "states the share it is taking now as a fraction of the allowance",
    "the most one batch may carry",
    "the gap it will leave before the next",
    "the last batch is placed so its result can still be read before the next reset",
)


def test_a_submission_is_a_purchase_and_the_rules_that_decide_it_are_in_the_iteration_phase() -> None:
    """The seven vote-economy rules sit where the round decides how to spend a slot."""
    phase_d = _flat(_phase_d(PROMPT.read_text(encoding="utf-8")))

    for rule in _VOTE_ECONOMY_RULES:
        assert _flat(rule) in phase_d, (
            f"{rule!r} is not stated in the iteration phase, so a round deciding how to spend a "
            "submission reads past it — the phase is where that decision is made"
        )


def test_the_ruler_rules_are_stated_where_a_score_is_read() -> None:
    """The three rules about how far a reading travels sit under the goal line."""
    goal = _flat(_goal_line(PROMPT.read_text(encoding="utf-8")))

    for rule in _RULER_RULES:
        assert _flat(rule) in goal, (
            f"{rule!r} is not stated under the goal line, so the section that decides what a score "
            "establishes does not carry it"
        )


def _gate_rows(text: str) -> list[str]:
    """The gate lines the recording section lists, one entry per bold first cell."""
    recording = _recording(text)
    table = recording.split("| Line | The question it answers |", 1)
    assert len(table) == 2, (
        "the recording section no longer carries the gate-line table, so a count of its rows would "
        "be a count of nothing"
    )
    return re.findall(r"^\| \*\*(.+?)\*\* \|", table[1], re.M)


def test_the_gate_line_count_is_the_number_the_section_says_it_is() -> None:
    """The stated count and the table it introduces are one fact, so they are read as one.

    The table is the single home of which lines there are; the sentence states how many, and is
    therefore a claim this guard measures rather than a second copy a reader has to reconcile.
    """
    text = PROMPT.read_text(encoding="utf-8")
    rows = _gate_rows(text)
    assert rows, "the gate-line table lists no lines, so a count taken from it compares against nothing"

    stated = re.search(r"carries the (\w+) gate lines", _flat(_recording(text)))
    assert stated, (
        "the recording section no longer states how many gate lines the summary owes, so a round "
        "cannot tell whether it has finished writing them"
    )
    assert stated.group(1) == "six", (
        f"the section now claims {stated.group(1)!r} gate lines; if that is the intended number, "
        "the table and this assertion move together"
    )
    assert len(rows) == 6, (
        f"the section claims six gate lines and lists {len(rows)} — {rows!r}. The count and the "
        "table have drifted apart, so a round trusting the sentence writes the wrong number of them"
    )
    assert tuple(rows) == _GATE_LINES, (
        f"the gate lines are not the ones named: read {rows!r}, expected {list(_GATE_LINES)!r}"
    )


def test_the_quota_is_spent_across_its_period_rather_than_at_the_start_of_it() -> None:
    """The distribution rule sits in the assess section, beside the ledger that opens the round.

    The section is the one place a round decides what today's allowance is for, so a rule kept
    anywhere else is read after the decision it governs has been made.
    """
    assess = _flat(_assess(PROMPT.read_text(encoding="utf-8")))

    for part in _QUOTA_SPEND_RULE:
        assert _flat(part) in assess, (
            f"{part!r} is not stated beside the per-competition quota ledger, so a round opening "
            "that ledger has nothing telling it to keep part of the allowance for later"
        )

# --- the two counts §5 states about its own lists ------------------------------------------------
#
# The defect class #2576 fixed for the promote template's reflection heading, found here by a
# section-scoped survey of spelled-out counts: §5 says "Seven questions" and "Three more", and until
# this pin nothing compared either sentence against the list it introduces. The sentence is the only
# instruction a round reads before it starts answering, so a list edited without it — or the reverse —
# leaves a round owing the wrong number with nothing to notice.
#
# The slice is `_recording`, the helper the section-scoped pins above already use. One name for one
# boundary: a second spelling of the same slice is how the two drift apart.

_NUMBER_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

_SELF_REVIEW_CLAIM = r"\*\*(\w+) questions the closing summary must answer\*\*"
_HOST_CLAIM = r"\*\*(\w+) more the host asked for by name\."
_HOST_ANCHOR = "the host asked for by name"


def _as_count(word: str) -> int:
    """A spelled-out number as an int, refusing a word this pin cannot read.

    Refusing rather than defaulting is the point: a claim rewritten as a digit ("7 questions") or as
    a vague quantity would otherwise make every assertion built on it compare against nothing while
    still reading green. "one" is deliberately absent — it is as often a pronoun as a count.
    """
    try:
        return _NUMBER_WORDS[word.lower()]
    except KeyError:
        raise AssertionError(
            f"§5 states its count as {word!r}, which this pin cannot read as a number. State it as "
            f"one of {sorted(_NUMBER_WORDS)} so the sentence and its list stay comparable"
        ) from None


def _numbered(recording: str) -> list[str]:
    """The numbered self-review questions, one entry per question."""
    return re.findall(r"^\d+\. \*\*(.+?)\*\*", recording, re.M)


def _bulleted_after_host_questions(recording: str) -> list[str]:
    """The bulleted questions §5 attributes to the host: the bullet list the sentence introduces.

    The bound is the **contiguous run of bullets** that follows the sentence, not the rest of the
    section. The section is one home for several subjects, and once it carried a second list the
    wider bound read that one too: the count stayed right only because the other subject's items are
    table rows rather than bullets, so a single bullet added there would redden this pin and blame
    the host's list for a change that never touched it. A false red is how a guard gets weakened, so
    the slice names the list it claims and stops where that list stops.
    """
    parts = recording.split(_HOST_ANCHOR, 1)
    assert len(parts) == 2, (
        f"§5 no longer carries the sentence naming the host's own questions ({_HOST_ANCHOR!r}), so a "
        "count taken from that part of the section would be a count of the wrong list"
    )
    listed: list[str] = []
    for line in parts[1].split("\n"):
        found = re.match(r"^- \*\*(.+?)\*\*", line)
        if found:
            listed.append(found.group(1))
        elif listed:
            break
    return listed


def _claim(section: str, pattern: str) -> int:
    """The count a sentence states, as an int, or a failure naming what is missing."""
    found = re.search(pattern, section)
    assert found, (
        f"§5 no longer states the count matching {pattern!r}. The sentence is what tells a round how "
        "many items it owes, so a section without one cannot be read as owing any"
    )
    return _as_count(found.group(1))


def test_the_closing_summary_counts_the_self_review_questions_it_lists() -> None:
    """The sentence and the numbered list it introduces are one fact, read as one."""
    recording = _recording(PROMPT.read_text(encoding="utf-8"))
    listed = _numbered(recording)
    assert listed, (
        "§5 lists no numbered self-review questions, so a count taken from it compares against "
        "nothing"
    )
    claimed = _claim(recording, _SELF_REVIEW_CLAIM)
    assert claimed == len(listed), (
        f"§5 claims {claimed} self-review questions and lists {len(listed)} — the count and the "
        f"list have drifted apart: {listed!r}"
    )


def test_the_host_questions_count_is_the_number_the_section_says() -> None:
    """The second count is measured apart from the first: it introduces a different list."""
    recording = _recording(PROMPT.read_text(encoding="utf-8"))
    listed = _bulleted_after_host_questions(recording)
    assert listed, (
        "§5 states how many questions the host asked for and then lists none, so the claim stands "
        "without a subject"
    )
    claimed = _claim(recording, _HOST_CLAIM)
    assert claimed == len(listed), (
        f"§5 claims {claimed} questions the host asked for and lists {len(listed)} — {listed!r}"
    )


def test_the_closing_summary_count_pins_discriminate_in_both_directions() -> None:
    """The failure the live template cannot supply, applied to synthetic text.

    A pin that only reads the live file passes today whether or not it could ever fail. These arms
    supply both errors it exists for — a list edited without its sentence, and a sentence edited
    without its list — plus the opposite error: a count that quietly widened to a neighbouring list
    would read the first two arms green while measuring the wrong thing.
    """
    section = (
        "### 5. Recording\n\n"
        "**Two questions the closing summary must answer**:\n\n"
        "1. **First?**\n"
        "2. **Second?**\n\n"
        "**Two more the host asked for by name.**\n\n"
        "- **Third?**\n"
        "- **Fourth?**\n"
    )

    assert len(_numbered(section)) == 2
    assert _claim(section, _SELF_REVIEW_CLAIM) == len(_numbered(section))
    assert len(_bulleted_after_host_questions(section)) == 2
    assert _claim(section, _HOST_CLAIM) == len(_bulleted_after_host_questions(section))

    # a list grows and its sentence does not
    grown = section.replace("2. **Second?**\n", "2. **Second?**\n3. **Third?**\n")
    assert len(_numbered(grown)) == 3
    assert _claim(grown, _SELF_REVIEW_CLAIM) != len(_numbered(grown))

    # the sentence grows and the list does not
    claim_only = section.replace("**Two questions", "**Three questions")
    assert _claim(claim_only, _SELF_REVIEW_CLAIM) != len(_numbered(claim_only))

    # the host's list grows and its sentence does not
    host_grown = section + "- **Fifth?**\n"
    assert len(_bulleted_after_host_questions(host_grown)) == 3
    assert _claim(host_grown, _HOST_CLAIM) != len(_bulleted_after_host_questions(host_grown))

    # a bullet placed BEFORE the host sentence belongs to no counted list, so it moves nothing
    before = section.replace(
        "**Two more the host", "- **A decoy bullet**\n\n**Two more the host"
    )
    assert len(_bulleted_after_host_questions(before)) == 2
    assert _claim(before, _HOST_CLAIM) == len(_bulleted_after_host_questions(before))

    # a bullet in a LATER subject of the same section belongs to that subject, not this list.
    # This is the arm that pins the bound: read to the end of the section, it would count three and
    # redden the host's pin for a change that never touched it.
    later = section + "\n**A different subject, in the same section.**\n\n- **Not a host question?**\n"
    assert len(_bulleted_after_host_questions(later)) == 2, (
        "a bullet belonging to a later subject in the same section was counted as one of the host's "
        "questions — the slice is reading past the list its sentence introduces"
    )
    assert _claim(later, _HOST_CLAIM) == len(_bulleted_after_host_questions(later))

    # a count this pin cannot read is refused rather than defaulted
    with pytest.raises(AssertionError, match="cannot read as a number"):
        _claim(section.replace("**Two questions", "**7 questions"), _SELF_REVIEW_CLAIM)
