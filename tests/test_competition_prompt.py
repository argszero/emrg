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
"""

from pathlib import Path

PROMPT = (
    Path(__file__).resolve().parent.parent
    / "emrg"
    / "server"
    / "competition_prompt.md"
)


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


def test_preparation_and_reflection_are_mandatory():
    text = PROMPT.read_text(encoding="utf-8")
    assert "0. Preparation (MUST run first every round)" in text
    assert "Do not skip the preparation step (even when \"everything looks fine\")" in text
    assert "MUST end with a reflection appended" in text
    # Rant scan must match the project field exactly, like the other templates.
    assert "exactly `{{ task.project }}`" in text


def test_error_handling_and_forbidden_sections_exist():
    text = PROMPT.read_text(encoding="utf-8")
    assert "### Error Handling" in text
    assert "### Forbidden" in text
    assert "Do not modify `~/.emrg/config.toml`" in text
    # The offline gate is the first forbidden item: never enter an offline one.
    assert "**Never enter a competition with an offline component**" in text


def _english_signals(text: str) -> list[str]:
    """The English offline signals as listed in §3.2 of the prompt.

    Parsed from the prompt rather than duplicated here, so this test tracks the
    live list instead of a copy that can drift away from it.
    """
    for line in text.splitlines():
        if line.startswith("- English:"):
            listed = line.split(":", 1)[1]
            return [w.strip().strip("`").strip() for w in listed.split("、") if w.strip()]
    return []


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
    """
    low = sentence.lower()
    idx = low.find(word.lower())
    if idx < 0:
        return False
    window = low[max(0, idx - 40): idx + len(word) + 25]
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

    Asserted against the **template block** rather than the whole document: the
    first version of this test checked only that the phrase occurred somewhere,
    and phase A (§, "does **not** go in that section") mentions it in prose — so
    renaming the actual section heading away survived the test unchanged. A
    presence check that can be satisfied by a mention of the thing is the same
    class of blindness this cycle is fixing, one level up.
    """
    text = PROMPT.read_text(encoding="utf-8")
    block = text.split("```markdown", 1)[1].split("```", 1)[0]
    assert "## Rejected (never re-evaluated)" in block, (
        "the state-file template lost its permanent-rejection section"
    )
    assert "## Rejected — needs a human read" in block, (
        "the state-file template has no re-checkable rejection section, so a "
        "§3.2.1 negation override would be frozen as permanent"
    )
    # Phase A must route to the right one of the two.
    assert "does **not** go in that section" in text
