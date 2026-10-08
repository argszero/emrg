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

Why the render-level legs exist (2026-10-08)
--------------------------------------------
The legs above read the file as text, and ``tests/test_prompt_templates.py`` does
render every template — but with a *minimal* config, which for this template takes the
``{% if task.get('role', '') == 'editor' %}`` / ``{% elif … == 'author' %}`` dispatch to
its **else**: neither work cycle renders, and that guard's ``len(rendered) > 500`` still
passes on the header alone (measured 2026-10-08: 18,441 of 58,528 characters). So the
role a task is configured with decides which half of the file a round is sent, and
nothing read it per role. These legs render through the real builder
(``TaskHandler._build_evolution_prompt``, the single render site for every task template)
**for each role the host configures**, and read the cycle the role selects — fail-to-
measure included, because a render that carries no cycle is a failure, not a pass.
"""

from pathlib import Path

import yaml

from emrg.protocol import InstanceIdentity
from emrg.server import scheduler as mod
from tests.task_handler_factory import make_handler

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


# --- Render-level legs (2026-10-08) --------------------------------------------
#
# The class, measured in a different carrier first: a guard that reads a task template
# as text measures the *file*, not the prompt a round receives, and the daemon renders
# every task prompt through jinja2 — so the file is not what is sent. #1903
# (`prompts/vibe_check.j2`) and #1905 (`competition_prompt.md` §0.0) added the missing
# leg for their carriers. This template has the same exposure and a sharper edge: its
# whole work cycle sits behind a role dispatch, so `config.role` decides which half of a
# 58,528-character file a round is sent.

# The work-cycle heading each role selects, and the same string the guard reads.
ROLE_HEADS = {"editor": "### 1. Editor Work Cycle", "author": "### 1. Author Work Cycle"}

# The fail-closed clause the dispatch renders when neither branch matches, kept as one
# string so the template and this guard name the same thing.
NO_ROLE_STOP = "this task's `role` is neither `editor` nor `author`"


def _render_journal(tmp_path: Path, monkeypatch, role: str | None) -> str:
    """The journal template as a round receives it, through the real builder.

    Real env, real context, ``config_dir`` on a tmp tree so no host path is read or
    written; ``make_handler`` hands the task record, so the production derivation runs.
    """
    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    project_dir = tmp_path / "demoproj"
    project_dir.mkdir(exist_ok=True)
    (tmp_path / "projects.yml").write_text(
        yaml.safe_dump([{"name": "demoproj", "path": str(project_dir)}]), encoding="utf-8"
    )
    config: dict = {"project": "demoproj", "author_id": "demo-inst"}
    if role is not None:
        config["role"] = role
    return make_handler(
        name="demo-task",
        config=config,
        interval=300,
        identity=InstanceIdentity(),
        template_path=PROMPT,
    )._build_evolution_prompt()


def _cycle_of(rendered: str, role: str) -> str:
    """The work cycle the render carries, or a failure to measure — never a pass."""
    heading = ROLE_HEADS[role]
    assert heading in rendered, (
        f"the rendered journal prompt carries no {heading!r} — this test cannot measure "
        f"what a round with role={role!r} is sent, which is a failure to measure, not a pass"
    )
    return rendered


def test_the_editor_work_cycle_reaches_the_round_and_not_merely_the_file(tmp_path, monkeypatch):
    """The editor's operational contract, read on the prompt a round receives."""
    rendered = _cycle_of(_render_journal(tmp_path, monkeypatch, "editor"), "editor")
    # The discipline section the file-level test pins, on the render this time.
    assert "#### Verification discipline — how a claim is discharged" in rendered, (
        "the editor render lost the verification-discipline section — the file carries it, "
        "but the round that must apply it is not sent it"
    )
    for action in _SIX_ACTIONS:
        assert action in rendered, f"the editor render lost the discipline action: {action!r}"
    # Each editor phase sends the reader there, checked per phase (a count would be
    # satisfied by four mentions in one paragraph).
    chunks = rendered.split("\n#### ")
    for heading in ("Phase A: Triage", "Phase B: Decision",
                    "Phase C: Follow-up", "Phase D: Ops"):
        chunk = next((c for c in chunks if c.startswith(heading)), None)
        assert chunk is not None, f"editor {heading} is missing from the render"
        assert "§Verification discipline" in chunk, (
            f"editor {heading} does not send the reader to the discipline section in the render"
        )


def test_the_author_work_cycle_reaches_the_round_and_not_merely_the_file(tmp_path, monkeypatch):
    """The author's contract, and that the dispatch stays exclusive."""
    rendered = _cycle_of(_render_journal(tmp_path, monkeypatch, "author"), "author")
    for marker in ("Phase A: Research (in-preparation)", "Phase B: Submit",
                   "Phase C: Revision", "Phase D: Track"):
        assert marker in rendered, f"the author render lost {marker!r}"
    assert ROLE_HEADS["editor"] not in rendered, (
        "an author round is sent the editor's cycle as well — the dispatch is not exclusive"
    )


def test_the_journal_render_substitutes_values_and_leaves_no_template_syntax(tmp_path, monkeypatch):
    """The converse: an assertion only the file could satisfy is a file read in disguise."""
    for role in ROLE_HEADS:
        rendered = _render_journal(tmp_path, monkeypatch, role)
        for tag in ("{{", "{%", "{#"):
            assert tag not in rendered, (
                f"an unrendered {tag!r} reaches a {role} round — the agent would read "
                f"template syntax as instruction"
            )
        assert "demo-inst" in rendered, (
            f"the configured author_id does not reach the {role} render"
        )


def test_a_role_the_dispatch_does_not_name_stops_the_round(tmp_path, monkeypatch):
    """Fail closed: the whole operational contract vanishes when `role` names nothing.

    Measured 2026-10-08 (cyc20261008-085636): a role that is neither `editor` nor `author`
    rendered **neither** work cycle — 18,457 of 58,528 characters — and every file-level
    test stayed green. The file's own idiom for a missing required config is to fail closed
    (the `author_id` STOP banner), so the dispatch does the same.
    """
    rendered = _render_journal(tmp_path, monkeypatch, "reviewer")
    assert NO_ROLE_STOP in rendered, (
        "a role the dispatch does not name rendered no work cycle and no stop clause — the "
        "round would improvise a procedure the journal does not have"
    )
    for heading in ROLE_HEADS.values():
        assert heading not in rendered, (
            f"{heading!r} renders under an unnamed role — the dispatch is not exclusive"
        )
