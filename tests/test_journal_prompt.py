"""Content guards for emrg/server/journal_prompt.md (rants 2026-08-24T12:17:11 +
2026-09-02T20:24:48).

The journal task prompt is the operational contract for the AI-run academic
journal (silicon-science-cs). These tests pin the top-conference review bar
(semantic Novelty, baseline comparison, reproduction verification — and since
2026-09-02: a Significance dimension with a forced "whose belief/decision
changes" test, an N3 cap on reusing the journal's own census pipeline with a
domain swap, prior-belief registration, and top-conference (not
measurement-archive) positioning) so a future edit cannot silently relax the
standards back to "file existence + checklist".
"""

from pathlib import Path

PROMPT = Path(__file__).resolve().parent.parent / "emrg" / "server" / "journal_prompt.md"


def test_journal_prompt_exists():
    assert PROMPT.is_file(), "journal_prompt.md missing — journal task cannot resolve its template"


def test_review_quality_bar_has_top_conference_standards():
    text = PROMPT.read_text(encoding="utf-8")
    # Semantic novelty scoring with reject lean
    assert "Score Novelty semantically" in text
    assert "5 = groundbreaking" in text
    assert "4 = substantive new contribution" in text
    assert "novelty ≤ 2 or a missing related-work comparison → lean REJECT" in text
    # Baseline comparison requirement (self before/after does NOT count)
    assert "comparing the system to its own before/after state does NOT count" in text
    # Stochastic systems: >=3 independent runs with variance/CI
    assert "≥3 independent runs reporting mean ± variance / confidence interval" in text
    # Overclaiming is a REJECT basis
    assert "overclaiming goes into weaknesses and can alone justify REJECT" in text
    # ACCEPT criteria: all dims >=3 + reproduction passed + no unresolved major concern
    assert "ACCEPT criteria (all must hold)" in text
    assert "every dimension scored ≥ 3" in text
    assert "reproduction verification passed" in text
    assert "no unresolved major concern" in text


def test_triage_has_c2_graded_reproduction_verification():
    text = PROMPT.read_text(encoding="utf-8")
    assert "reproduction verification (C2-graded, mandatory)" in text
    assert "file existence alone is NOT sufficient" in text
    # Light experiments: actually run the README one-command reproduction
    assert "Light experiments" in text
    assert "actually run the README one-command reproduction" in text
    assert "verify the core numbers reproduce within the stated tolerance" in text
    # Heavy experiments: downgrade to script-integrity verification + record reason
    assert "Heavy experiments" in text
    assert "script-integrity verification" in text
    assert "record the reason for not actually running" in text
    # Unreproducible + author did not supplement within 1 revision round -> failed
    assert "Unreproducible" in text
    assert "reproduction verdict **failed**" in text


def test_review_template_has_reproducibility_dimension():
    # Both templates (editor review + author Phase Review-Other) must carry it
    assert PROMPT.read_text(encoding="utf-8").count(
        "**Reproducibility**: success | partial | failed — observed deviation"
    ) == 2


def test_decision_rule_blocks_accept_on_failed_reproduction():
    text = PROMPT.read_text(encoding="utf-8")
    assert "reproduction-failed / partial and unexplained" in text
    assert "the decision CANNOT be ACCEPT — only REVISION or REJECT" in text


def test_author_submission_quality_bar_synced():
    text = PROMPT.read_text(encoding="utf-8")
    assert "Baseline comparison required" in text
    assert "≥3 independent runs with mean ± variance / confidence interval" in text
    assert "one-command reproduction + expected output / tolerance" in text
    assert "heavy experiments must attach real run logs and random seeds" in text


def test_common_rules_quality_bar_synced():
    text = PROMPT.read_text(encoding="utf-8")
    assert "baseline comparison" in text
    assert "one-command reproducibility spec with expected output/tolerance" in text
    assert "reproduction verification" in text


def test_significance_is_scored_dimension_in_both_review_templates():
    # Rant 2026-09-02T20:24:48: "so what / whose belief or decision changes"
    # must be answered explicitly — both review comment templates (editor +
    # author Phase Review-Other) carry the Significance score + forced test.
    text = PROMPT.read_text(encoding="utf-8")
    assert text.count("Novelty: <n> | Significance: <n>") == 2
    assert text.count("**Significance check** (name a community") == 2
    # Decision synthesis aggregates Significance too
    assert "Novelty / Significance / Technical soundness / Writing / Experimental rigor / Reproducibility, each 1–5" in text


def test_significance_forced_question_and_reject_path():
    text = PROMPT.read_text(encoding="utf-8")
    assert "name a community — if this result is true, how do their beliefs or decisions change?" in text
    assert "an unanswered \"so what\" can alone justify REJECT" in text
    # ACCEPT criteria still requires every dimension >= 3
    assert "every dimension scored ≥ 3" in text


def test_census_pipeline_reuse_capped_at_n3():
    text = PROMPT.read_text(encoding="utf-8")
    # The journal's own mature pipeline (head_sha-pinned corpus + multi-channel
    # classifier + Wilson CI + byte-identical reproduction) with a domain swap
    # is capped at N3 in the review bar, with explicit exemption conditions.
    assert "capped at Novelty N3" in text
    assert "head_sha-pinned corpus + multi-channel classifier + Wilson CI + byte-identical reproduction" in text
    assert "a new measurement instrument or a new construct" in text
    assert "contradict an explicit registered prior belief" in text
    assert "decision-relevance argument connects the measurement to a named stakeholder" in text
    # Pure cross-sectional snapshots of a new domain are N3 at most
    assert "A pure cross-sectional snapshot of a new domain through an unchanged pipeline is N3 at most" in text


def test_author_side_house_pipeline_reuse_rule():
    text = PROMPT.read_text(encoding="utf-8")
    # Submission quality bar: Nth census-family application needs longitudinal /
    # panel design or a new construct; snapshot-only reuse is not submittable.
    assert "The Nth application of the census family MUST add" in text
    assert "longitudinal/panel design" in text
    assert "repeated measurement of an already-measured corpus over time" in text
    assert "introduce a **new construct / new measurement instrument**" in text
    assert "a pure cross-sectional snapshot of a new domain through an unchanged pipeline is not a publishable contribution" in text


def test_prior_belief_registration_required():
    text = PROMPT.read_text(encoding="utf-8")
    # Registration (Author Phase A) must include a prior-belief paragraph;
    # vendor hype is explicitly not a valid justification.
    assert "**Prior-belief registration (mandatory" in text
    assert "states the expected direction/effect before running the study" in text
    assert "\"Vendor hype says X is the future\" or \"this direction seems interesting\" are NOT valid justifications" in text
    # Manuscript must report whether results confirm/contradict registered priors
    assert "Prior-belief reporting" in text
    assert "whether the results confirm it, contradict it, or leave it unresolved" in text


def test_journal_positioned_as_top_conference_not_measurement_archive():
    text = PROMPT.read_text(encoding="utf-8")
    # (e) positioning: the journal is a top-conference-quality empirical journal,
    # not a measurement archive collecting snapshot studies; CfP must stay aligned.
    assert "top-conference-quality empirical journal" in text
    assert "NOT a measurement archive" in text
    assert "CfP wording must not invite" in text
    # The old "top-level contributions are a bonus, NOT the default bar" framing
    # is gone — quality bar no longer depends on contribution level.
    assert "NOT the default bar" not in text
