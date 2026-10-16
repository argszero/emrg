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


def test_editor_review_bar_requires_citation_authenticity_check():
    text = PROMPT.read_text(encoding="utf-8")
    # Rant 2026-09-10T11:28:43: the editor must spot-check citations itself
    # (never trusting the author's self-report), and fabrication alone can REJECT.
    assert "Spot-check citation authenticity independently" in text
    assert "never trust the author's self-check report alone" in text
    assert "A fabricated or unverifiable citation is a major concern and can alone justify REJECT" in text
    # The verification method is declarative (API endpoints + judgement criteria)
    assert 'curl -s "https://api.crossref.org/works/<doi>"' in text
    assert "https://api.crossref.org/works?query.bibliographic=<title>" in text


def test_reference_count_threshold_and_in_text_coverage():
    text = PROMPT.read_text(encoding="utf-8")
    # Rant 2026-09-10T11:28:43: >=100 references, all genuinely cited; padding
    # entries do not count; the bar is level-independent (case studies included).
    assert "Check the reference-count threshold and in-text coverage" in text
    assert "at least 100 references" in text
    assert "each one must be genuinely cited in the body text" in text
    assert "is padding and does not count toward the total" in text
    assert "applied to every manuscript alike (including case studies" in text
    # The citation gradient: 3 comparisons -> 100 references -> authenticity
    assert "3 comparisons (floor) → 100 references (volume) → authenticity verification (quality)" in text


def test_author_side_citation_integrity_requirements():
    text = PROMPT.read_text(encoding="utf-8")
    # Author Phase B item 12: verify EVERY reference before submitting.
    assert "Citation authenticity verification (mandatory, PR #1116, rant 2026-09-10T11:28:43)" in text
    assert "fabricated citations are academic misconduct, not a formatting slip" in text
    assert "delete it or replace it with a real reference" in text
    assert "Never submit an unverifiable citation" in text
    # The authenticity report is a committed artifact of the submission
    assert "papers/issue-<N>/reference-check.md" in text
    assert "- `reference-check.md` — citation authenticity report (item 12)" in text


def test_citation_gate_wired_into_triage_and_common_rules():
    text = PROMPT.read_text(encoding="utf-8")
    # Triage (editor Phase A step 3) verifies the gate before moving to in-review.
    assert "citation gate (PR #1116, rant 2026-09-10T11:28:43)" in text
    assert "author's authenticity report — the independent spot-check still happens at review" in text
    # Common Rules item 8 carries it too, so the bar is visible outside the phases.
    assert "Citation integrity is part of the bar (PR #1116, rant 2026-09-10T11:28:43)" in text


def test_citation_verification_row_in_both_review_templates():
    # Both review templates (editor + author Phase Review-Other) must carry the
    # independent spot-check row, mirroring the Significance-check duplication.
    text = PROMPT.read_text(encoding="utf-8")
    assert text.count("**Citation verification** (independent spot-check)") == 2
    assert text.count("total references <T> (≥100 required), uncited entries <u>") == 2


# --- Verification discipline (PR #1333; rant 2026-09-17T16:49:58) -------------
#
# The host's complaint: the editor's prompt was a skeleton (phase choice, 13-bar
# review quality, state machine, recording) with **no method** — not one line on
# what makes an assertion "verified". On the journal that produced the evidence
# (silicon-science-cs, R195→R360, 165 rounds) the method existed only as a
# session-local, git-ignored audit file, which is the shape the journal had
# already recorded as a failure mode: a rule that governs every actor, stated
# only in a carrier no other actor can see. These tests pin the durable carrier
# so a later edit cannot quietly drop the method back into a skeleton.

_SIX_ACTIONS = (
    "Read a count from the tool that produces it",
    "A copy must be as wide as the artifact it copies",
    "A claim is a receipt",
    "Every requirement needs a collector",
    "A verdict binds a version, a window and a control",
    "A state change the reader cannot see needs a named reader",
)


def test_verification_discipline_section_defines_the_six_actions():
    text = PROMPT.read_text(encoding="utf-8")
    assert "#### Verification discipline — how a claim is discharged" in text
    for action in _SIX_ACTIONS:
        assert action in text, f"the section lost action: {action!r}"
    # The diagnostic table is the half a reader applies to their own claim; the
    # six actions are the half they follow. Both are pinned, and by row name
    # rather than by row count, because a count survives a table that lost the
    # mode it was written for.
    assert "| Failure mode | Check question |" in text
    for mode in ("| A second source of truth |", "| The reading of an absence |",
                 "| An invisible rule |"):
        assert mode in text, f"the failure-mode table lost: {mode!r}"
    assert "**Read an artifact in the form it is consumed.**" in text


def test_every_editor_phase_references_the_discipline():
    """A section nobody is sent to is a section nobody applies.

    Checked per phase rather than by counting occurrences: the count would be
    satisfied by five mentions in one paragraph, which is exactly the shape
    ("a requirement in the guidance layer, no collector at the step that acts")
    the section itself is about.
    """
    text = PROMPT.read_text(encoding="utf-8")
    chunks = text.split("\n#### ")
    for heading in ("Phase A: Triage", "Phase B: Decision",
                    "Phase C: Follow-up", "Phase D: Ops"):
        chunk = next((c for c in chunks if c.startswith(heading)), None)
        assert chunk is not None, f"editor {heading} is gone"
        assert "§Verification discipline" in chunk, (
            f"{heading} does not send the reader to the discipline section"
        )


def test_state_machine_defines_the_correction_label():
    """`correction` runs in the journal while the prompt never named it."""
    text = PROMPT.read_text(encoding="utf-8")
    assert "| `correction` |" in text
    # Reopened, not closed: the clock is not the pre-publication one, which is
    # the part a reader would otherwise assume by analogy.
    assert "**`correction` reopens, it does not close**" in text
    assert "no 14-day revision deadline" in text


def test_reference_presentation_is_a_third_citation_axis():
    """Count and authenticity do not see a bibliography that renders as a wall."""
    text = PROMPT.read_text(encoding="utf-8")
    assert "Check how the references are *presented*" in text
    # Both review templates collect it, or the requirement has no slot to land in.
    assert text.count("- **Reference presentation** (read in the rendered form)") == 2


# --- The RSI failure-list gates (rant 2026-10-16T11:33:13, issue #2318) -------
#
# A 2026-08 study (Princeton; Kirgis / Kapoor et al., reported by MIT Technology
# Review) gave an agent six days, $3,000 of API budget, a GPU, an isolated VM and
# open network against two unpublished NeurIPS 2026 submissions — both were
# rejected, with the engineering judged complete and the research judged poor.
# Its five failure modes are gaps in this prompt, where the bar rewarded the
# wrong behaviour. Each gate is pinned on the side that has to execute it: a bar
# stated only for the author has no reviewer, and one stated only for the
# reviewer is discovered after the work is already done.
#
# Each pin names the *rule*, never the topic — a bare `"synthetic" in text` would
# stay green on a sentence that merely mentions the word, which is the shape
# these guards exist to refuse.


def test_gate1_a_contradiction_needs_the_studys_detectable_effect():
    """Failure mode 1: a contradiction was paid for without being tested.

    The bar called a result contradicting a registered prior "a strong novelty
    signal" and never asked whether the contradiction clears the study's own
    noise — the one reward an agent that kills its own hypotheses collects for
    free. The requirement has to sit where the reward is paid, so the census-cap
    exemption that lifts the N3 cap is conditional on it too.
    """
    text = PROMPT.read_text(encoding="utf-8")
    # Review item #15, the #7(b) exemption, and the author's #14: both sides.
    assert text.count("minimum effect this study can detect") == 3
    assert text.count('**"no information"**') == 2
    assert "Exemption (b) is conditional" in text
    assert "a contradiction inside the study's own noise is not a signal" in text
    assert "a contradiction inside the study's own noise is not a novelty signal" in text


def test_gate2_a_barren_direction_has_a_stop_go_checkpoint():
    """Failure mode 2: the only exit from a barren direction was a clock.

    `in-preparation > 60 days without submission` is an editor's administrative
    rule; the prompt had no scientific checkpoint at which the direction must be
    judged. The three outcomes are named here because "reconsider the direction"
    is not a decision.
    """
    text = PROMPT.read_text(encoding="utf-8")
    assert "**Stop-go review (mandatory checkpoint" in text
    assert "what result would **falsify** this direction" in text
    assert "**downgrade to case study**" in text
    assert "abandon, with a failure memo" in text
    # It says which clock it is not: the administrative one cannot stand in.
    assert "it is not a substitute for this judgement" in text


def test_gate3_a_revision_must_move_the_research_not_the_wording():
    """Failure mode 3: the revision step asked for a reply, not for research.

    Point-by-point responses + a change summary is a receipt for answering; the
    reviewed study's agent narrowed its claims and added caveats instead of
    fixing its method — which is exactly what those instructions permit. So the
    classification is on both sides: the author may not mark a wording-only
    round complete, and the editor must confirm a research-level change landed.
    """
    text = PROMPT.read_text(encoding="utf-8")
    assert "must NOT be marked `[revision-complete]`" in text
    assert "landed by a **research-level change**" in text
    assert "at least one requested change must be a **research-level change**" in text
    assert (
        "a revision that moved only the wording has not answered a review that "
        "asked for research" in text
    )


def test_gate4_synthetic_data_is_not_evidence_about_the_real_world():
    """Failure mode 4: nothing forbade toy data from carrying a real claim.

    The bar required a baseline and asked for n, but never said that synthetic
    data can only demonstrate a pipeline — so a hypothesis tested on a tiny
    synthetic set passed every item on the list. Both sides carry it.
    """
    text = PROMPT.read_text(encoding="utf-8")
    assert "does **not** support any conclusion about a real population" in text
    assert "It is **not** evidence about a real population" in text
    # Visible outside the phases, as the citation gate is.
    assert "synthetic data may prove a pipeline but never a claim about the real world" in text


def test_gate5_a_self_improvement_claim_has_an_admissible_shape():
    """Failure mode 5: our own bar rejected every honest self-improvement paper.

    "Comparing the system to its own before/after state does NOT count" is right
    for an ordinary experiment and fatal for a paper about a system improving
    itself — and deleting it would readmit any study that merely spent more.
    So it is *limited*: unchanged for everyone else, admissible here only with
    the four requirements, of which the same-budget control is the load-bearing
    one. Its twin is this repo's own rule that a guard must redden when the fix
    is reverted.
    """
    text = PROMPT.read_text(encoding="utf-8")
    # Unchanged for every other manuscript — the old anchor still holds.
    assert "comparing the system to its own before/after state does NOT count" in text
    # Limited on both sides, so neither actor reads it as a blanket ban.
    assert "it is not a licence to reject a paper whose subject is the system improving itself" in text
    assert "it is not a blanket ban on papers whose subject is the system's own improvement" in text
    # All four requirements, on both sides.
    assert text.count("which layer changed") == 2
    assert text.count("what triggers a round") == 2
    assert text.count("what got worse or did not move") == 2
    assert "plus** a non-self-improving control at the same budget" in text
    assert "a same-budget non-self-improving control" in text
    # Held out by definition: an evaluation set inside the loop measures the loop.
    assert text.count("**held-out set** that never entered the improvement loop") == 2

