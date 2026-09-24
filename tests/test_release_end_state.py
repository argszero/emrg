"""A release is complete only when GitHub says it is — and until now nothing asked it.

Measured defect (2026-09-23, tag `v0.3.1`, run `35851087374`): the `release` job's only
step is `softprops/action-gh-release`. It created the release, uploaded 8 of the 9
artifacts, and was then cut off by the peer (`##[error]other side closed`). The release
was left **draft** and **one artifact short**. The run went red, so a human could
notice — but "red" does not say what *fixed* looks like: the acceptance ("published as
Latest, not draft, not prerelease, with the full asset set") lived only as prose in
`Agent.md` §Releasing step 3, and confirming the recovery meant running `gh release
view` by hand, twice (once to find the draft, once to confirm the re-run published it).

The dangerous half is not that red run. `draft`/`prerelease` are applied when the
action **creates** a release; against one that already exists it issues an update. So
the job can finish **green having published nothing**: a draft release is invisible to
`releases/latest`, which is exactly the endpoint the auto-upgrade chain reads, and CI
would say nothing at all. `Agent.md`'s acceptance sentence is therefore mechanised here
in two halves, both needed:

* **what the job reads** — four independent signals (`.draft`, `.prerelease`,
  `.assets[].name`, `releases/latest`), read from a comment-stripped body *in argument
  position*, because the word `.draft` also occurs in the comment that explains this
  step and a substring pin stays green when the read is replaced by prose;
* **the verdict it reaches** — the body is taken from the parsed workflow and
  **executed** in five states with `gh` stubbed. Measured by arm on this tree: flattening
  every `exit 1` to `exit 0` leaves the read-pins above green, and only the executed arm
  notices. The five states are the four ways this release can be wrong (never created,
  draft, a missing asset, not Latest) plus the one that must pass.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

WORKFLOW = ".github/workflows/build-release.yml"


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.is_file(), f"{rel} is missing — the guard cannot measure what it guards"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{rel} is empty — a vacuous read must not read as a pass"
    return text


def _release_steps() -> list[dict]:
    """`release`'s steps, parsed — read from the job, never from the document.

    The wiring pinned below (`needs`, the tag gate) occurs in more than one job here, so
    a document-level match cannot say which job lost the line.
    """
    jobs = yaml.safe_load(_read(WORKFLOW)).get("jobs")
    assert isinstance(jobs, dict) and "release" in jobs, "build-release.yml has no `release` job"
    steps = jobs["release"].get("steps")
    assert isinstance(steps, list) and steps, "the `release` job carries no steps"
    return steps


def _end_state_step() -> dict:
    """The one step of `release` that is a script — there is exactly one, on purpose."""
    scripts = [s for s in _release_steps() if isinstance(s, dict) and s.get("run")]
    assert len(scripts) == 1, (
        f"expected exactly one `run:` step in `release`, got {len(scripts)} — the executed "
        "arm below runs *that* step, so a second script would make it measure an unnamed one"
    )
    return scripts[0]


def _uncommented(body: str) -> str:
    """The body with comment lines dropped — a read must be a read, not a mention of one."""
    kept = [ln for ln in body.splitlines() if not ln.strip().startswith("#")]
    assert kept, "the end-state step body is nothing but comments"
    return "\n".join(kept)


def _newest_derivation_as_a_constant(body: str) -> str:
    """The body with the `newest=` derivation replaced by the running tag.

    The edit this guard must catch: with `newest` pinned to `${tag}` the comparison can no
    longer tell "this tag is the newest published release" from "it is not", so an older tag
    rebuilt while a newer release exists would be reported as a release defect. Anchored on
    the statement's own shape and asserted, so the mutation cannot silently become a no-op
    when the derivation is rewritten.
    """
    lines = body.splitlines()
    starts = [i for i, ln in enumerate(lines) if 'newest="$(' in ln]
    assert len(starts) == 1, (
        f"expected exactly one `newest=` derivation to mutate, found {len(starts)}"
    )
    start = starts[0]
    ends = [i for i in range(start, len(lines)) if lines[i].rstrip().endswith(')"')]
    assert ends, "the `newest=` statement does not terminate with `)\"` — cannot slice it"
    statement = "\n".join(lines[start : ends[0] + 1])
    assert "releases?per_page" in statement, (
        f"the slice is not the releases-listing derivation: {statement!r}"
    )
    lines[start : ends[0] + 1] = ['newest="${tag}"']
    return "\n".join(lines)


# `--jq <expr>` (the helper's own call, and two direct ones) and `api <expr>` (the
# helper's callers) are the two positions in which this step names what it reads.
_JQ_ARG = re.compile(
    r"""(?:\b--jq\s+|\bapi\s+)(?:'([^']*)'|"(\.?[A-Za-z0-9_.\[\]|()=! ]*)"|(\.[A-Za-z0-9_.\[\]]*))"""
)


def _requested_expressions(body: str) -> set[str]:
    found = set()
    for match in _JQ_ARG.finditer(body):
        expr = next((g for g in match.groups() if g), "")
        if expr.strip():
            found.add(expr.strip())
    return found


def test_the_release_job_verifies_its_own_end_state_after_publishing() -> None:
    steps = _release_steps()
    publishers = [
        i
        for i, s in enumerate(steps)
        if isinstance(s, dict) and "action-gh-release" in str(s.get("uses", ""))
    ]
    assert publishers, "the `release` job no longer runs action-gh-release"
    assert len(publishers) == 1, f"expected one publishing step, got {len(publishers)}"
    script = _end_state_step()
    assert steps.index(script) > publishers[0], (
        "the end-state check runs BEFORE the release is published — an assertion that "
        "precedes the upload cannot measure the upload's end state"
    )
    body = _uncommented(str(script["run"]))
    expressions = _requested_expressions(body)
    for signal in (".draft", ".prerelease", ".assets[].name"):
        assert signal in expressions, (
            f"the end-state check never asks the API for `{signal}` in argument position. "
            f"It asks for: {sorted(expressions)}. (A mention of `{signal}` in the comment "
            "above the step would satisfy a substring pin — measured by arm.)"
        )
    assert "releases/latest" in body, (
        "the end-state check never asks `releases/latest` — the endpoint the upgrade "
        "chain reads, and therefore the one that decides whether 'Latest' is true"
    )
    assert "releases/tags/" in body, (
        "the end-state check does not look the release up by this run's tag"
    )
    # ⚠️ The step must run when the step ABOVE it failed — that is the state it exists to
    # report, and it is the one defect this guard was raised for (veto `cyc20260924-032325`,
    # re-measured on head `24b62492`): with no explicit `if:`, the step inherits the implicit
    # `success()` and is skipped exactly when `Create GitHub Release` fails. Read from the
    # **parsed step**, not from the workflow text: an `if:` anywhere in the document (the
    # `release` job carries one, and so do four other jobs here) would satisfy a text search.
    #
    # Named limit: this is a pin on a *string*, because GitHub's expression language has no
    # evaluator here — a spelling pin is what is available for "is this condition true on
    # failure". It accepts both spellings that are (`!cancelled()` and `always()`) by design:
    # the claim it guards is about the *failed predecessor*, and `always()` satisfies it. The
    # workflow prefers `!cancelled()` for a reason the pin does not enforce — `always()` also
    # runs on a cancelled run, where reporting "no release exists" would be a false red. What
    # this pin cannot be wrong about is the defect it was raised for, which is the *absence*
    # of the key. The verdict half is executed, below.
    condition = str(script.get("if") or "")
    assert condition, (
        "the end-state step carries no `if:` — it therefore inherits the implicit "
        "`success()` and is skipped exactly when `Create GitHub Release` fails: the state "
        "the step exists to report is the state it does not run in (measured v0.3.1, "
        "`other side closed`: the release was left a draft, one asset short, and this "
        "check never ran)"
    )
    assert "success()" not in condition, (
        f"the end-state step's `if:` requires a successful predecessor (if={condition!r}) — "
        "a check that only runs when everything already worked cannot report the failure it "
        "was written for"
    )
    assert "!cancelled()" in condition or "always()" in condition, (
        f"the end-state step's `if:` does not exempt it from a failed predecessor "
        f"(if={condition!r}) — the step would be skipped in the very state it exists for"
    )
    # The expected set is derived, never stored: a hardcoded count goes stale the moment
    # the build matrix gains or loses a platform.
    assert re.search(r"artifacts/\*", body) or re.search(r"\bexpected=", body), (
        "the expected artifact set is not derived from the downloaded files"
    )
    hardcoded = re.search(r"^\s*expected=\"?EMRG-", body, re.M)
    assert not hardcoded, f"the expected artifact set is a literal: {hardcoded.group(0)!r}"
    # The releases listing is paginated. `releases?per_page=100` returns one page, ordered
    # by *creation* time; the comparison below takes the highest by *version*, so the two
    # agree only while version order and creation order agree within a page — 102 releases
    # exist here, so page 1 is not the whole set. Pagination makes the reading independent
    # of that coincidence (raised as an external review note, adopted by the cycle that
    # answered it). Asserted on the comment-stripped body, so the word in the comment above
    # the step cannot satisfy it.
    assert "--paginate" in body, (
        "the releases listing is not paginated — the `sort -V | tail -1` over it assumes "
        "creation order and version order agree across a single page of 100 releases"
    )


_GH_STUB = '''#!/usr/bin/env python3
"""Answer the end-state check's API reads, and record what it asked."""
import json, os, pathlib, sys

pathlib.Path(os.environ["GH_ARGV_LOG"]).open("a").write(" ".join(sys.argv[1:]) + "\\n")
scenario = os.environ["GH_SCENARIO"]
tag = os.environ["GH_TAG"]
carried = json.loads(os.environ["GH_ASSETS"])
argv = sys.argv[1:]
url = argv[1] if len(argv) > 1 and argv[0] == "api" else ""
expr = argv[argv.index("--jq") + 1] if "--jq" in argv else ""


def emit(line):
    sys.stdout.write(line + "\\n")


if "releases/latest" in url:
    emit(os.environ.get("GH_LATEST_TAG", tag))
elif "releases/tags/" in url:
    if scenario == "missing":
        sys.exit(1)
    if expr == ".tag_name":
        emit(tag)
    elif expr == ".draft":
        emit("true" if scenario == "draft" else "false")
    elif expr == ".prerelease":
        emit("false")
    elif expr == ".assets[].name":
        for name in carried:
            emit(name)
    else:
        sys.exit(2)
elif "releases?per_page" in url:
    # The newest *published* release, answered from the scenario rather than from the tag.
    # A stub that always answers the tag cannot represent the one state the outer comparison
    # exists for — an older tag rebuilt while a newer release exists — and in that state
    # "replace the derivation with a constant" is invisible (measured: with the tag
    # unconditionally, that edit survived every arm; see the arm at the end of this file).
    emit(os.environ.get("GH_NEWEST_TAG", tag))
else:
    sys.exit(2)
'''


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the step body is a POSIX shell script: what this arm measures is the step's own "
        "verdict, so it is gated to the platform that can run one."
    ),
)
def test_the_end_state_check_refuses_every_way_a_release_can_be_incomplete(tmp_path) -> None:
    """The verdict, executed in five states instead of read.

    `gh` is a stub on `PATH` that answers the reads from `GH_SCENARIO` and records every
    argv it was handed — so the run makes no network call, and a stub that ignored its
    own argv could not silently satisfy a dropped `--jq`.

    `sleep` is stubbed too: the check retries a not-yet-readable release, and a real
    `sleep 10` would make this arm cost a minute to measure a loop it is not testing.
    """
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")
    body = str(_end_state_step()["run"])
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "gh"
    stub.write_text(_GH_STUB, encoding="utf-8")
    stub.chmod(0o755)
    no_sleep = bindir / "sleep"
    no_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    no_sleep.chmod(0o755)
    (tmp_path / "artifacts").mkdir()
    full = [
        "EMRG-9.9.9-macos-arm64.pkg",
        "EMRG-9.9.9-linux-aarch64.tar.gz",
        "EMRG-9.9.9-windows-x64.exe",
    ]
    for name in full:
        (tmp_path / "artifacts" / name).write_text("x", encoding="utf-8")
    argv_log = tmp_path / "argv.log"
    argv_log.write_text("", encoding="utf-8")

    def run(
        scenario: str,
        carried: list[str],
        script: str = body,
        latest: str = "",
        newest: str = "",
    ) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "GITHUB_REF_NAME": "v9.9.9",
            "GITHUB_REPOSITORY": "argszero/emrg",
            "GH_ARGV_LOG": str(argv_log),
            "GH_SCENARIO": scenario,
            "GH_TAG": "v9.9.9",
            "GH_ASSETS": json.dumps(carried),
        }
        if latest:
            env["GH_LATEST_TAG"] = latest
        if newest:
            env["GH_NEWEST_TAG"] = newest
        return subprocess.run(
            [shell, "-c", script],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    published = run("published", full)
    assert published.returncode == 0, (
        f"the check fails a published, complete, Latest release: rc={published.returncode} "
        f"{published.stdout}{published.stderr}"
    )
    # The two states the real incident produced, plus the two neighbours it could have.
    # `notlatest` and `older-tag` (below) are deliberately a **pair that differs in exactly
    # one stub answer** — what the releases listing says the newest published release is —
    # and reach opposite verdicts. That pair is what makes the outer comparison load-bearing:
    # with `newest == tag` the Latest assertion must be made (and fails here), with
    # `newest != tag` it must be skipped (and passes here), on identical everything else.
    for scenario, carried, latest, marker in (
        ("draft", full, "", "DRAFT"),
        ("partial", full[:-1], "", "does not carry"),
        ("notlatest", full, "v9.9.8", "reports Latest="),
        ("missing", full, "", "no release exists"),
    ):
        result = run(scenario, carried, latest=latest)
        assert result.returncode != 0, (
            f"the check accepts a {scenario} release — the state {scenario} is one of the "
            f"four ways this release can be wrong: rc={result.returncode} {result.stdout}"
        )
        assert marker in result.stdout + result.stderr, (
            f"the {scenario} run failed, but not for the reason it should: expected "
            f"{marker!r} in {result.stdout}{result.stderr}"
        )

    # The state the outer comparison exists for: this tag is NOT the newest published
    # release (an older tag rebuilt while a newer one exists), so the Latest assertion is
    # about the newer release and is not made here — the rebuild must not be a false red.
    # Measured on head `24b62492`: this state was unrepresentable (the stub answered the
    # running tag unconditionally), which is why replacing the derivation with a constant
    # survived every arm while a real rebuild of an old tag would have gone red.
    older = run("older-tag", full, latest="v9.9.10", newest="v9.9.10")
    assert older.returncode == 0, (
        "the check fails a release whose tag is not the newest published one — a rebuild of "
        "an older tag is allowed, and the Latest assertion belongs to the newer release: "
        f"rc={older.returncode} {older.stdout}{older.stderr}"
    )
    assert "not the newest published release" in older.stdout, (
        "the older-tag run passed without reporting WHY the Latest assertion was skipped — "
        f"it may have passed for an unrelated reason: {older.stdout}"
    )

    # …and the arm for that pair, in the same sandbox: the green run above is the control,
    # this is the mutation. Pinning `newest` to the tag removes the only thing that can skip
    # the Latest assertion, so the older-tag state must go red.
    mutated = run(
        "older-tag", full, script=_newest_derivation_as_a_constant(body),
        latest="v9.9.10", newest="v9.9.10",
    )
    assert mutated.returncode != 0, (
        "replacing the `newest` derivation with a constant leaves the older-tag state green, "
        "so the derivation is not load-bearing: a rebuild of an old tag would then be "
        f"reported as a release defect. rc={mutated.returncode} {mutated.stdout}"
    )
    assert "reports Latest=" in mutated.stdout + mutated.stderr, (
        "the mutated older-tag run failed, but not for the reason the edit causes: expected "
        f"'reports Latest=' in {mutated.stdout}{mutated.stderr}"
    )
    asked = argv_log.read_text(encoding="utf-8")
    assert "releases/latest" in asked and ".assets[].name" in asked, (
        f"the check did not ask both the asset list and `releases/latest`; it asked: {asked!r}"
    )


@pytest.mark.skipif(
    os.name == "nt",
    reason="the step body is a POSIX shell script; the arm executes it.",
)
def test_the_end_state_check_is_what_convicts_a_draft_release(tmp_path) -> None:
    """The arm: flatten the exits and the executed verdict goes green on a draft.

    This is the check's own instrument — the read-pins above stay green under this edit,
    because every `gh api` call and every jq expression is still there. What is lost is
    the answer, which is why the arm above executes the body rather than reading it.
    """
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")
    body = str(_end_state_step()["run"])
    assert "exit 1" in body, "the step carries no failing exit for the arm to flatten"
    flattened = body.replace("exit 1", "exit 0")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "gh"
    stub.write_text(_GH_STUB, encoding="utf-8")
    stub.chmod(0o755)
    no_sleep = bindir / "sleep"
    no_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    no_sleep.chmod(0o755)
    (tmp_path / "artifacts").mkdir()
    for name in ("EMRG-9.9.9-macos-arm64.pkg", "EMRG-9.9.9-windows-x64.exe"):
        (tmp_path / "artifacts" / name).write_text("x", encoding="utf-8")
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GITHUB_REF_NAME": "v9.9.9",
        "GITHUB_REPOSITORY": "argszero/emrg",
        "GH_ARGV_LOG": str(tmp_path / "argv.log"),
        "GH_SCENARIO": "draft",
        "GH_TAG": "v9.9.9",
        "GH_ASSETS": json.dumps(["EMRG-9.9.9-macos-arm64.pkg", "EMRG-9.9.9-windows-x64.exe"]),
    }
    result = subprocess.run(
        [shell, "-c", flattened],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, (
        "the flattened body still fails, so this arm cannot say what it exists to say — the "
        f"mutation did not reach the verdict: rc={result.returncode} {result.stdout}"
    )
    assert "DRAFT" in result.stdout, (
        "the flattened body did not reach the draft branch, so the arm is not measuring what "
        f"it claims: {result.stdout}"
    )
