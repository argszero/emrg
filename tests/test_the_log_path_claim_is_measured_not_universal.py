"""`gh run view --log`'s empty answer is a reading about a run, never a universal.

The class, and where it came from
---------------------------------
This template's step 0.4 makes reading a failed run's cause a per-cycle duty, and the command a
reader reaches for first is `gh run view <id> --log`. On this host that command answers **0 bytes
with exit 0** for a red run — a failure to measure wearing the shape of a pass — and the repo
answered it with a reading that reports its own failure (`scripts/read-run-failure.py`). Three
carriers explain that to a reader: that tool's own docstring, `scripts/check-release-published.py`'s
(the remedy it replaced), and `DEVELOPMENT.md` ("The readable path to a failed run's cause"). This
file is what keeps them honest about **what was measured**.

Measured 2026-10-04 (`cyc20261004-214803`), `gh` 2.58.0 — the version two of the carriers name —
over the eight most recent runs of this repository:

    gh run view 37203340165 --log   -> rc 0, 12828 bytes   (3/3 on a repeat)
    gh run view 37202743156 --log   -> rc 0, 0 bytes
    gh run view 37202679408 --log   -> rc 0, 0 bytes
    gh run view 37202043973 --log   -> rc 0, 0 bytes
    gh run view 37194550758 --log   -> rc 0, 0 bytes       (a red Test run)
    gh run view 37194011425 --log   -> rc 0, 0 bytes       (a red Test run)
    gh run view 36956685533 --log   -> rc 0, 0 bytes       (the v0.3.8 red build)

So the silent answer is the shape to expect — including for every *failing* run, which is the case
the carriers exist for — but it is **not** universal. The axis was not established: the
counterexample is not the event type (its neighbour is a `push` too) and not recency (that
neighbour is ten minutes older). This is the whole finding, and it is enough: **two carriers stated
it as "for every run"**, and the third (`check-release-published.py`) stated the measured set and
the only conclusion it supports — "for this run, for a green one …, and for a recent `Test` run, so
it is the host's log path and not this run's". A universal is the part a reader can falsify with
one command, and finding it false is how they discount the warning that matters; the measured set
cannot be falsified that way.

What is asserted
----------------
* **the carriers are enumerated by content, not by a list**: every Python source directly under
  `scripts/` and every Markdown file at the repository root whose text names `gh run view`. A
  carrier added later is read here the moment it names the command, which is what stops the rule
  from decaying into the absence of anyone's objection;
* **each carrier names the reading** (`read-run-failure.py`) — a warning with no remedy leaves the
  reader to reach for the silent command again, which is the defect the carriers document;
* **no carrier states the silent answer as a universal** (`every run`), and the predicate is
  asserted **in both directions** on synthetic text, so a matcher that always faults cannot pass
  the leg above.

Named limits. The search set is `scripts/*.py` and the root `*.md`, because the claim is prose
written *to a reader* and those are the two places this repo writes that prose; `emrg/**` and
`tests/**` are outside it by that reasoning rather than by oversight. `every run` is banned as a
phrase rather than parsed as a quantifier: a future carrier with a genuine universal would have to
change this file, and saying so is better than a matcher clever enough to be wrong.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The command the claim is about — the marker that makes a file a carrier.
MENTIONS = "gh run view"
#: The reading every carrier must hand the reader instead of the silent command.
READING = "read-run-failure.py"
#: The universal two carriers wrote, and `cyc20261004-214803` falsified with one run.
UNIVERSAL = "every run"


def _carriers() -> dict[str, str]:
    """Carriers by repo-relative path: non-test sources whose text names the log path."""
    files = sorted(REPO_ROOT.glob("scripts/*.py")) + sorted(REPO_ROOT.glob("*.md"))
    return {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in files
        if MENTIONS in path.read_text(encoding="utf-8")
    }


def _faults(text: str) -> list[str]:
    """What this text gets wrong about the log path; `[]` means it states the measured shape."""
    faults: list[str] = []
    if MENTIONS in text and READING not in text:
        faults.append(
            f"names `{MENTIONS}` but not the reading (`{READING}`), so the reader is left "
            "with the silent command"
        )
    if UNIVERSAL in text:
        faults.append(
            f"states the silent answer as a universal (`{UNIVERSAL}`): measured 2026-10-04 "
            "(`cyc20261004-214803`, gh 2.58.0), `gh run view 37203340165 --log` returned its "
            "whole log with rc 0 - name the runs that were measured instead"
        )
    return faults


def test_the_carriers_are_read_by_content_and_not_by_a_list() -> None:
    """The control for the enumeration itself: the known carriers, and no test file among them."""
    carriers = _carriers()

    for expected in ("scripts/read-run-failure.py", "scripts/check-release-published.py",
                     "DEVELOPMENT.md"):
        assert expected in carriers, (
            f"{expected} no longer names `{MENTIONS}`, so the leg below would pass on silence "
            "rather than on the claim being fixed"
        )
    assert not [path for path in carriers if path.startswith("tests/")], carriers


def test_each_carrier_names_the_reading_and_states_no_universal() -> None:
    faults = {path: _faults(text) for path, text in _carriers().items()}

    assert not {path: f for path, f in faults.items() if f}, faults


def test_the_predicate_condemns_the_universal_and_clears_the_measured_shape() -> None:
    """Both directions: a predicate that always faults would pass the leg above for free."""
    falsified = (
        "`gh run view <id> --log` answers 0 bytes with exit 0 for every run. "
        "Read it with `scripts/read-run-failure.py <run-id>`."
    )
    assert any("universal" in fault for fault in _faults(falsified)), _faults(falsified)

    measured = (
        "`gh run view 36956685533 --log` answered 0 bytes with exit 0 (measured 2026-10-04). "
        "Read it with `scripts/read-run-failure.py <run-id>`."
    )
    assert _faults(measured) == [], _faults(measured)


def test_a_carrier_that_names_the_command_without_the_reading_is_a_fault() -> None:
    """The other leg of the predicate, in both directions too."""
    no_remedy = "`gh run view <id> --log` answers 0 bytes with exit 0."

    assert any("not the reading" in fault for fault in _faults(no_remedy)), _faults(no_remedy)
    assert _faults("nothing about that command here") == []
