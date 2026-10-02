"""A step's captured output must survive `set -e`, or the capture is decoration.

`.github/workflows/build-release.yml` runs its bash steps as
`bash --noprofile --norc -e -o pipefail {0}` (the runner prints that line itself). Under
that shell, `VAR="$(cmd 2>&1)"` whose `cmd` exits non-zero aborts the step **at the
assignment**: the `echo "$VAR"` on the next line never runs, so the output the author
deliberately captured - and the `::error::` that reads it - are both discarded, and the job
log shows a bare "exit code 1" that cannot be told from "the tool is missing".

Measured 2026-10-03 (cyc20261003-021304) with `bash --noprofile --norc -e -o pipefail`,
the exact shell the runner uses:

    OUT="$(printf 'the reason\\n'; exit 1)"              -> nothing after it, rc 1
    OUT="$(printf 'the reason\\n'; exit 1)" || RC=$?     -> "the reason" printed, rc captured
    ID="$(... | grep X | head -1 ...)"                   -> nothing after it, rc 1
    ID="$(... | grep X | head -1 ... || true)"           -> the empty value is handled below

The notary step already carries the `|| NOTARY_RC=$?` form (#1811/#1812, measured on run
36956685533 - the v0.3.8 failure whose log was a bare "exit code 1"). The two
`security import` assignments in the same file did not: their `2>&1` capture and the
`::error::` that reads it were unreachable for the same reason, which is the defect this
file guards.

The rule is narrow on purpose - only assignments that **capture merged output** are
scanned. That is the shape whose whole point is to print what it captured, so an abort at
the assignment is unambiguously wrong, and a reader of a red row has nothing to argue with.
An assignment whose empty value a following check handles (`|| true` on a probe) is a
different, equally legitimate form and is accepted here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: `VAR="$(cmd ...` - the assignment and the start of a command substitution.
_ASSIGNMENT = re.compile(r"""^(?P<indent>\s*)(?P<var>[A-Za-z_][A-Za-z0-9_]*)="\$\(""")

#: What makes an assignment a *capture*: merged output, so the author means to echo it.
_MERGED = "2>&1"

#: A guard is any `||` on the assignment's own span. `|| RC=$?` and `|| true` are both fine
#: here: the first preserves the reason, the second hands an empty value to the check below.
_GUARD = "||"


class _Hit:
    def __init__(self, line: int, var: str, body: str, guarded: bool) -> None:
        self.line = line
        self.var = var
        self.body = body
        self.guarded = guarded

    def __repr__(self) -> str:  # pragma: no cover - only used in failure output
        return f"line {self.line}: {self.var}=$(...2>&1) guarded={self.guarded}"


def _assignment_span(lines: list[str], start: int) -> list[str]:
    """The assignment's own text: from its first line to the one holding the closing `\")"`.

    Terminating on `)"` is what the conventional shell in this repo writes; a substitution
    whose *body* contains `)"` would widen the span, and widening can only add a guard or a
    `2>&1` to the span, never hide one.
    """
    span = [lines[start]]
    index = start
    while ')"' not in span[-1] and index + 1 < len(lines):
        index += 1
        span.append(lines[index])
    return span


def captures(text: str) -> list[_Hit]:
    """Every command-substitution assignment capturing merged output in one `run:` block."""
    hits: list[_Hit] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        span = _assignment_span(lines, i)
        if ')"' not in span[-1]:
            # The substitution never closed inside the block: not an assignment this rule
            # can judge, and guessing would be a false positive.
            continue
        body = "\n".join(span)
        if _MERGED not in body:
            continue
        hits.append(_Hit(i + 1, match.group("var"), body, _GUARD in body))
    return hits


def unguarded_merged_captures(text: str) -> list[_Hit]:
    return [hit for hit in captures(text) if not hit.guarded]


def _bash_steps(path: Path) -> list[tuple[str, str]]:
    """(name, run) for every bash step, so a red row names the step and not just a line."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            run = step.get("run")
            if not isinstance(run, str):
                continue
            if "bash" in str(step.get("shell", "")):
                out.append((f"{job_name}/{step.get('name', '?')}", run))
    return out


def test_no_bash_step_captures_output_its_own_failure_can_discard() -> None:
    offenders: list[str] = []
    scanned = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, run in _bash_steps(path):
            scanned += 1
            for hit in unguarded_merged_captures(run):
                offenders.append(f"{path.name} {name}:{hit.line} {hit.var}=$(...2>&1)")
    assert scanned, "no bash step was read at all - the scan answered nothing"
    assert not offenders, (
        "these assignments capture merged output with no `||`, so a failure aborts the step "
        "before the line that would print what they captured (`|| RC=$?` keeps the reason, "
        "`|| true` hands an empty value to the check below):\n  " + "\n  ".join(offenders)
    )


def test_the_scan_reaches_the_capture_that_was_fixed_first() -> None:
    """A scan that read nothing would pass vacuously - prove it sees a known capture.

    The notary step's `NOTARY_OUT` is the one this rule was generalised from (#1811/#1812),
    and it is guarded. Finding it - guarded - is what makes an empty offender list mean
    "nothing unguarded" rather than "nothing read".
    """
    seen = [
        (name, hit)
        for name, run in _bash_steps(WORKFLOWS / "build-release.yml")
        for hit in captures(run)
        if hit.var == "NOTARY_OUT"
    ]
    assert seen, "the notary capture was not found at all - the scan is reading the wrong thing"
    assert all(hit.guarded for _, hit in seen), (
        "the notary capture is the shape this rule exists for and it is no longer guarded: "
        f"{[str(h) for _, h in seen]}"
    )


def test_the_scan_sees_an_unguarded_capture() -> None:
    """Positive control: the pre-fix text of the notary step must be caught."""
    pre_fix = (
        'NOTARY_OUT="$(xcrun notarytool submit "$PKG" \\\n'
        '  --apple-id "$APPLE_ID" \\\n'
        "  --wait --output-format json 2>&1)\"\n"
        'echo "$NOTARY_OUT"\n'
    )
    hits = unguarded_merged_captures(pre_fix)
    assert [hit.var for hit in hits] == ["NOTARY_OUT"], (
        "the scanner must catch the exact shape that shipped a bare 'exit code 1': "
        f"{[str(h) for h in hits]}"
    )


def test_the_scan_accepts_an_unguarded_capture_that_captures_nothing() -> None:
    """A guard on a *probe* (no `2>&1`) is a different rule and must not be forced here.

    `ID="$(find-identity | grep X | head -1 ...)"` is guarded with `|| true` in
    `build-release.yml` because an empty value is the answer it wants - but an assignment
    with no merged output has nothing it meant to print, so this scanner stays out of that
    judgement rather than inventing a second rule it cannot evidence.
    """
    assert captures('ID="$(a | b | head -1)"\n') == []


def test_the_scan_treats_both_guarded_forms_as_guarded() -> None:
    for guarded in (
        'OUT="$(cmd 2>&1)" || RC=$?\necho "$OUT"\n',
        'OUT="$(cmd 2>&1 || true)"\n',
    ):
        assert unguarded_merged_captures(guarded) == [], guarded
        assert [hit.var for hit in captures(guarded)] == ["OUT"]


@pytest.mark.parametrize(
    "text",
    [
        'OUT="$(cmd 2>&1)"\n',
        'A="$(one 2>&1)"\nB="$(two 2>&1)"\n',
    ],
)
def test_the_scan_reports_every_unguarded_capture_in_a_block(text: str) -> None:
    assert len(unguarded_merged_captures(text)) == text.count("2>&1")
