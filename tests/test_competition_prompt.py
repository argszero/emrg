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
characters shorter with none of them — `scripts/run-mutation-arm.py` reported `SURVIVED`
for exactly that arm. The same shape was fixed for `emrg/server/prompts/vibe_check.j2`
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

#: The project the render fixture asks the builder for — a name that appears nowhere in
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

    The context is the one a real records-driven call produces (a project and nothing
    else); `config_dir` is redirected at a tmp tree — the shape
    `tests/test_evolution_prompt_index_rule.py` uses — so the `projects.yml` read lands
    on a file this test made, never on the host's `~/.emrg`.
    """
    tmp_path = tmp_path_factory.mktemp("competition-prompt")
    project_dir = tmp_path / PROMPT_PROJECT
    project_dir.mkdir(exist_ok=True)
    (tmp_path / "projects.yml").write_text(
        yaml.safe_dump([{"name": PROMPT_PROJECT, "path": str(project_dir)}]),
        encoding="utf-8",
    )
    original = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        handler = make_handler(
            name="competition-task",
            type="competition",
            config={"project": PROMPT_PROJECT},
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
    # One phase per round, and the baseline anchor rule.
    assert "A round advances one phase" in text
    assert "Submit at least once successfully and obtain a leaderboard score" in text


def test_rejected_competitions_are_not_re_evaluated():
    text = PROMPT.read_text(encoding="utf-8")
    assert "never re-evaluate them in later rounds" in text
    assert "verbatim reason quoted from the rules page" in text


def test_goal_line_covers_both_prize_and_standing():
    text = PROMPT.read_text(encoding="utf-8")
    assert "win the prize" in text
    assert "leaderboard standing / percentile" in text


def test_preparation_and_closing_summary_are_mandatory():
    text = PROMPT.read_text(encoding="utf-8")
    assert "0. Preparation (MUST run first every round)" in text
    assert "Do not skip the preparation step (even when \"everything looks fine\")" in text
    # The per-round reflection used to be appended to a `*_reflections.md` diary;
    # the rant 2026-09-14T14:35:47 sweep moved it into the round's final message.
    assert "closing summary in your final message" in text
    assert "a round that ends without a summary strands the next one" in text
    assert "Seven questions the closing summary must answer" in text
    # Rant scan must match the project field exactly, like the other templates.
    assert "exactly `{{ task.project }}`" in text


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
        "project and time anchor before it meets the rule that governs the round"
    )
    assert rendered.index("### 0.0 ") < rendered.index("### 0. Preparation"), (
        "the rendered cadence block drifted below §0. Preparation, so a round that has "
        "started working has not been told the rule that binds it"
    )


def test_the_render_substitutes_values_and_carries_no_template_syntax(rendered):
    """The converse: a render must not be a file read wearing a render's name.

    Two ways a render assertion goes vacuous, both closed here. If the block were read
    with its syntax intact — a `{% raw %}` wrapper, or the text copied out of a literal —
    the round would be sent `{{ task.project }}` and would be told nothing; and if what
    this test examined were the file, the same assertion catches it. Then the substituted
    value has to arrive, so "it rendered" is measured by what the round can read rather
    than by the absence of braces alone.
    """
    assert "{{" not in rendered and "{%" not in rendered, (
        "the rendered competition prompt still carries Jinja syntax — the round is sent "
        "template source rather than the prompt it names"
    )
    assert PROMPT_PROJECT in rendered, (
        "the render does not carry the project name "
        f"{PROMPT_PROJECT!r}: "
        "`{{ task.project }}` resolved to nothing (`jinja2.Undefined` renders empty), so "
        "the round is told its project is blank"
    )
