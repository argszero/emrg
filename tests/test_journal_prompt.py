"""Content guards for emrg/server/journal_prompt.md (rant 2026-08-24T12:17:11).

The journal task prompt is the operational contract for the AI-run academic
journal (silicon-science-cs). These tests pin the top-conference review bar
(semantic Novelty, baseline comparison, reproduction verification) so a future
edit cannot silently relax the standards back to "file existence + checklist".
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
