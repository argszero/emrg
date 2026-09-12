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
