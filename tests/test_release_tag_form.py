"""A release tag's *form* is part of the release, and the instruction is what produced it.

Measured defect (2026-09-20): `v0.2.97` reached GitHub as a **lightweight** tag while every
release since `v0.2.94` is **annotated** — it was the only tag in the series with no `^{}`
dereference line (`git ls-remote --tags origin`). Nothing was wrong with the build that
followed; the artifact simply did not match the line it belonged to. The command that
produced it was the one this repository taught: `Agent.md` §Releasing step 2 and both
next-step messages in `scripts/bump-version.py` said `git tag v<x.y.z>`, which is exactly
a lightweight tag.

Two halves, both mechanised here rather than left as prose:

* the **instruction** carries the annotated form (this file), so it cannot drift back to the
  bare spelling without a red test;
* the **artifact** is checked where it arrives — `build-release.yml`'s `verify-tag` job reads
  the tag object's type from the API before any platform builds. That check's discriminating
  signal was measured in both states against real tags, because a test cannot make that call
  offline: `git/ref/tags/<tag>` → `object.type` is `tag` for `v0.2.97`/`v0.2.96` (annotated)
  and `commit` for `v0.2.92`/`v0.2.93` (lightweight). This file pins the wiring of that job —
  that it exists, reads that field, and is actually a prerequisite of `build` — so it cannot
  be detached or emptied and stay quiet.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.is_file(), f"{rel} is missing — the guard cannot measure what it guards"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{rel} is empty — a vacuous read must not read as a pass"
    return text


def _release_step_two() -> str:
    """The `2. **Tag**` line of `Agent.md`'s Releasing section, or a loud failure.

    A missing line must fail here rather than yield `""` and let every `in` assertion below
    pass on nothing — the deletion of the step is the largest drift this guard exists to catch.
    """
    text = _read("Agent.md")
    section = re.search(r"^## Releasing\n(.*?)(?=^## )", text, re.S | re.M)
    assert section, "Agent.md has no `## Releasing` section"
    lines = [ln for ln in section.group(1).splitlines() if ln.startswith("2. **Tag**")]
    assert len(lines) == 1, f"expected exactly one `2. **Tag**` step, got {len(lines)}"
    return lines[0]


def test_agent_md_teaches_the_annotated_tag() -> None:
    step = _release_step_two()
    # The **taught command** is the thing that produced the defect, so pin that and not the
    # whole line: the line legitimately mentions the bare spelling while naming it as wrong,
    # and a global negative would make explaining the defect impossible.
    taught = re.search(r"`([^`]*git tag[^`]*)`", step)
    assert taught, f"the tag step carries no backticked command: {step}"
    command = taught.group(1)
    assert command.startswith("git tag -a v<x.y.z> -m "), (
        f"Agent.md's taught tag command is not annotated: {command}"
    )
    assert "git push origin v<x.y.z>" in command, f"the taught step does not push: {command}"


def test_bump_version_prints_the_annotated_tag_in_both_sites() -> None:
    text = _read("scripts/bump-version.py")
    # The module docstring's numbered flow and the message printed after a real bump are two
    # separate sites; a fix applied to one of them is the shape this asserts against.
    assert 'git tag -a vX.Y.Z -m "emrg vX.Y.Z"' in text, "the docstring's step 3 is not annotated"
    assert "git tag -a v{args.version} -m" in text, (
        "the printed next-step message is not annotated"
    )
    assert "git tag vX.Y.Z &&" not in text, "the docstring still teaches the lightweight form"
    assert "git tag v{args.version} &&" not in text, "the printed message still does"


def test_build_release_checks_the_tag_form_before_building() -> None:
    wf = _read(".github/workflows/build-release.yml")
    assert re.search(r"^  verify-tag:\n", wf, re.M), "build-release.yml has no `verify-tag` job"
    assert "object.type" in wf, "the verify-tag job does not read the tag object's type"
    assert re.search(r'needs:\s*verify-tag', wf), (
        "`build` does not depend on `verify-tag` — the check is orphaned and cannot fail a build"
    )
    # Skipping on a branch dispatch is what keeps `workflow_dispatch` runs buildable.
    assert "startsWith(github.ref, 'refs/tags/')" in wf, (
        "the tag check is not gated on a tag ref — a branch dispatch would fail it"
    )
