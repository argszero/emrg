"""`scripts/check-release-orphan-tags.py` -- the fleet reading over the release collection.

`scripts/check-release-published.py` answers one tag. The question this file exists for is
the one no cycle thought to ask: *is any tag on this repository published-less right now?*
It was answered by hand exactly once, and the hand answer was wrong by two: the cycle that
found `v0.3.51`'s skipped release job asked the repository the next cycle and got **three**
orphans -- `v0.3.51`, `v0.3.34`, `v0.3.8` (measured 2026-11-12 against `argszero/emrg`).

What is pinned here, and why each one is a separate arm:

* the **difference** is read from two paginated collections, and both keep `--paginate` and
  a full page size -- this repository holds more than one page of releases, so dropping
  either would report every tag past the first page as an orphan;
* every candidate is **confirmed** by the per-tag endpoint before it is reported, so a
  short collection cannot name a published release as missing; and a candidate the
  endpoint contradicts is `not measurable`, never a fault and never clean;
* the three unusable states (draft, prerelease, no asset) are separate faults, because a
  draft is not an orphan -- it is invisible to `releases/latest`, which is a different
  sentence with a different remedy;
* an empty tag collection is `not measurable`: a scan whose subject is empty has measured
  nothing, which is not the verdict that nothing is wrong.

The fakes never touch the network: `_gh` is replaced by a routing table keyed on the
argument list, and every payload below is shaped like the one the live endpoint returns
(measured 2026-11-12).
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-release-orphan-tags.py"

REPO = "argszero/emrg"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_release_orphan_tags", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


@pytest.fixture
def no_baseline(mod, monkeypatch):
    """An empty record, for every arm that is not about the record.

    A recorded row whose tag a fixture leaves out is audited like any other row, so an arm
    that does not mean to exercise the record has to say so -- otherwise its fixture picks
    the subject by accident, and a change to the record reddens an arm about something else
    while looking like a regression in it.
    """
    monkeypatch.setattr(mod, "ABANDONED_TAGS", {})
    return mod


class FakeGh:
    """`_gh` replaced by a routing table: prefix of argv -> verbatim text or exception."""

    def __init__(self, routes: dict[tuple[str, ...], object]) -> None:
        self.routes = routes
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        for prefix, payload in self.routes.items():
            if tuple(args[: len(prefix)]) == prefix:
                if isinstance(payload, BaseException):
                    raise payload
                return payload  # already text: both readers parse lines themselves
        raise AssertionError(f"unrouted gh call: gh {' '.join(args)}")

    def called(self, *prefix: str) -> bool:
        return any(tuple(c[: len(prefix)]) == prefix for c in self.calls)


#: The two collection reads, by the exact argv prefix each one is asked with. Keying on the
#: full prefix is what lets the pagination arms below assert on it too.
TAGS = ("api", f"repos/{REPO}/tags?per_page=100")
RELEASES = ("api", f"repos/{REPO}/releases?per_page=100")


def _tags(*names: str) -> str:
    return "".join(f"{n}\n" for n in names)


def _releases(*rows: tuple[str, str, str, str]) -> str:
    """rows are (tag, draft, prerelease, assets) -- the @tsv shape the reader parses."""
    return "".join("\t".join(row) + "\n" for row in rows)


def _published(*names: str) -> tuple:
    return tuple((n, "false", "false", "9") for n in names)


def _routes(tags: str, releases: str, extra=None) -> dict:
    routes = {TAGS: tags, RELEASES: releases}
    if extra:
        routes.update(extra)
    return routes


def _check(mod, monkeypatch, capsys, routes, only=None):
    fake = FakeGh(routes)
    monkeypatch.setattr(mod, "_gh", fake)
    code = mod.check(REPO, only)
    return code, capsys.readouterr().out, fake


def _confirmed_absent(mod, tag) -> BaseException:
    """The 404 `_gh` builds from a real call: process code 1, the status in the message."""
    return mod.GhError(1, ["api", f"repos/{REPO}/releases/tags/{tag}"],
                       "gh: Not Found (HTTP 404)", http_status=404)


def _tag_not_found(mod, tag) -> BaseException:
    """The same 404 for a tag ref that does not exist: a collection's absence, confirmed."""
    return mod.GhError(1, ["api", f"repos/{REPO}/git/ref/tags/{tag}"],
                       "gh: Not Found (HTTP 404)", http_status=404)


# --------------------------------------------------------------------------- 0


def test_every_tag_published_is_the_only_pass(mod, monkeypatch, capsys, no_baseline):
    routes = _routes(_tags("v0.3.2", "v0.3.1"), _releases(*_published("v0.3.2", "v0.3.1")))
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 0
    assert out.splitlines()[0] == f"repo: {REPO}"
    assert "tags read: 2" in out
    assert "OK: all 2 tag(s) read carry a published release" in out


# --------------------------------------------------------------------------- 1


def test_a_tag_with_no_release_is_named_with_the_per_tag_reading(
    mod, monkeypatch, capsys, no_baseline
):
    """The whole point: the orphan is named, and the remedy explains it rather than guesses."""
    routes = _routes(
        _tags("v0.3.51", "v0.3.50"),
        _releases(*_published("v0.3.50")),
        {("api", f"repos/{REPO}/releases/tags/v0.3.51"): _confirmed_absent(mod, "v0.3.51")},
    )
    code, out, fake = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "tag: v0.3.51 -- NO RELEASE" in out
    assert "v0.3.50 -- NO RELEASE" not in out, "a published tag must not be named"
    assert "scripts/check-release-published.py <tag>" in out
    assert "uv run --no-sync python3" in out, "a printed command carries its runner"
    assert fake.called("api", f"repos/{REPO}/releases/tags/v0.3.51")


def test_every_candidate_is_confirmed_so_the_collection_alone_never_convicts(
    mod, monkeypatch, capsys, no_baseline
):
    """Absence read from a collection is a screen; the per-tag call is the verdict."""
    routes = _routes(
        # Two tags outside the recorded baseline: on a recorded tag the verdict is
        # reclassified, and this arm is about the confirmation, not about the record.
        _tags("v0.3.52", "v0.3.51"),
        _releases(),
        {
            ("api", f"repos/{REPO}/releases/tags/v0.3.52"): _confirmed_absent(mod, "v0.3.52"),
            ("api", f"repos/{REPO}/releases/tags/v0.3.51"): _confirmed_absent(mod, "v0.3.51"),
        },
    )
    code, out, fake = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "tag: v0.3.52 -- NO RELEASE" in out and "tag: v0.3.51 -- NO RELEASE" in out
    assert fake.called("api", f"repos/{REPO}/releases/tags/v0.3.52")
    assert fake.called("api", f"repos/{REPO}/releases/tags/v0.3.51")


def test_a_draft_is_its_own_fault_not_an_orphan(mod, monkeypatch, capsys, no_baseline):
    routes = _routes(
        _tags("v0.3.1"), _releases(("v0.3.1", "true", "false", "8"))
    )
    code, out, fake = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "release unusable: it is a DRAFT" in out
    assert "NO RELEASE" not in out
    assert not fake.called("api", f"repos/{REPO}/releases/tags/v0.3.1"), (
        "a draft is present in the collection, so there is nothing to confirm"
    )


def test_a_prerelease_is_its_own_fault(mod, monkeypatch, capsys, no_baseline):
    routes = _routes(_tags("v0.3.1"), _releases(("v0.3.1", "false", "true", "9")))
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "PRERELEASE" in out


def test_a_release_with_no_asset_is_a_fault(mod, monkeypatch, capsys, no_baseline):
    """The v0.3.1 incident: eight of nine uploads, and `other side closed`."""
    routes = _routes(_tags("v0.3.1"), _releases(("v0.3.1", "false", "false", "0")))
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "carries no asset" in out


# --------------------------------------------------------------------------- 2


def test_an_empty_tag_collection_is_not_measurable_never_clean(mod, monkeypatch, capsys):
    routes = _routes(_tags(), _releases())
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 2
    assert "no tags at all" in out
    assert "not a pass" in out
    assert "OK" not in out


def test_a_candidate_the_endpoint_contradicts_is_not_measurable(mod, monkeypatch, capsys):
    """The collection was short of its own subject: its premise is gone, so it decides nothing."""
    routes = _routes(
        _tags("v0.3.50"), _releases(), {("api", f"repos/{REPO}/releases/tags/v0.3.50"): "{}"}
    )
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 2
    assert "did not appear in the" in out and "answers that a release exists" in out
    assert "not a pass" in out
    assert "NO RELEASE" not in out, "a contradicted candidate must never be printed as a fault"


def test_a_confirmation_that_fails_to_answer_is_not_measurable(mod, monkeypatch, capsys):
    routes = _routes(
        _tags("v0.3.51"),
        _releases(),
        {
            ("api", f"repos/{REPO}/releases/tags/v0.3.51"): mod.GhError(
                1, ["api"], "gh: Server Error (HTTP 502)", http_status=502
            )
        },
    )
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 2
    assert "not measurable" in out
    assert "FAULT" not in out


def test_gh_missing_is_not_measurable(mod, monkeypatch, capsys):
    def boom(args):
        raise mod.GhError(None, args, "gh is not available on this machine (No such file)")

    monkeypatch.setattr(mod, "_gh", boom)
    code = mod.check(REPO, None)
    out = capsys.readouterr().out
    assert code == 2
    assert "not measurable" in out and "not a pass" in out


def test_an_absent_tag_asked_for_by_name_is_not_measurable(mod, monkeypatch, capsys):
    routes = _routes(_tags("v0.3.50"), _releases(*_published("v0.3.50")))
    code, out, _ = _check(mod, monkeypatch, capsys, routes, only="v9.9.9")
    assert code == 2
    assert "not in the 1 tags read" in out
    assert "nothing was compared" in out


# ---------------------------------------------------------------- the window, which is not
# ---------------------------------------------------------------------------- the subject


def test_both_collections_are_read_paginated_at_a_full_page(
    mod, monkeypatch, capsys, no_baseline
):
    """This repo holds more than one page of releases.

    Dropping `--paginate`, or lowering the page size, makes every tag past the first page
    read as an orphan -- a loud fault about published releases, and the expensive direction
    to be wrong in. Asserted on the argv because that is the only place it is visible.
    """
    routes = _routes(_tags("v0.3.2"), _releases(*_published("v0.3.2")))
    _code, _out, fake = _check(mod, monkeypatch, capsys, routes)
    for prefix in (TAGS, RELEASES):
        call = next(c for c in fake.calls if tuple(c[:2]) == prefix)
        assert "--paginate" in call, f"{prefix} must be read paginated"
        assert "per_page=100" in prefix[1]
        assert call[2] == "--paginate"


def test_the_counts_actually_read_are_printed_before_any_verdict(
    mod, monkeypatch, capsys, no_baseline
):
    """A reading has to name its subject size; a short collection then reads as a short one."""
    routes = _routes(_tags("v0.3.2", "v0.3.1"), _releases(*_published("v0.3.2", "v0.3.1")))
    _code, out, _ = _check(mod, monkeypatch, capsys, routes)
    lines = out.splitlines()
    assert any(l.startswith("tags read: 2") for l in lines)
    assert any(l.startswith("releases read: 2") for l in lines)
    assert "drafts included" in out


def test_a_narrowed_subject_still_reads_the_whole_collection(mod, monkeypatch, capsys):
    """`--tag` narrows what is reported, never what is known: a tag outside a short list
    would otherwise read as an orphan."""
    routes = _routes(_tags("v0.3.51", "v0.3.50"), _releases(*_published("v0.3.50")),
                     {("api", f"repos/{REPO}/releases/tags/v0.3.51"):
                      _confirmed_absent(mod, "v0.3.51")})
    code, out, _ = _check(mod, monkeypatch, capsys, routes, only="v0.3.51")
    assert code == 1
    assert "tags read: 2" in out
    assert "v0.3.50 -- NO RELEASE" not in out


# ------------------------------------------------- the recorded baseline, in both directions
#
# Two tags on this repository will never carry a release: `v0.3.8` and `v0.3.34` each lost
# their build and were superseded by a published successor. A screen that faults on them for
# ever is an exit code its reader learns to ignore, so the two are recorded -- and every arm
# below pins a way the record must NOT grow: a third tag faults, a row taken away faults, a
# different fault on a recorded tag faults, and a row that stops describing the repository
# faults -- both ways it can, by being contradicted and by naming a tag that is gone.
# Suppression is one tag's verdict, never a class of them.


def test_a_recorded_abandoned_tag_is_named_and_does_not_fault(mod, monkeypatch, capsys):
    """Recorded is not skipped: the per-tag reading still runs, the baseline reclassifies it."""
    routes = _routes(
        _tags("v0.3.8", "v0.3.34", "v0.3.50"),
        _releases(*_published("v0.3.50")),
        {
            ("api", f"repos/{REPO}/releases/tags/v0.3.8"): _confirmed_absent(mod, "v0.3.8"),
            ("api", f"repos/{REPO}/releases/tags/v0.3.34"): _confirmed_absent(mod, "v0.3.34"),
        },
    )
    code, out, fake = _check(mod, monkeypatch, capsys, routes)
    assert code == 0
    assert "v0.3.8 -- ABANDONED" in out, "the record is printed, not silently dropped"
    assert "NO RELEASE" not in out
    assert "FAULT" not in out
    assert "abandoned above carry none by recorded decision" in out
    assert fake.called("api", f"repos/{REPO}/releases/tags/v0.3.8"), (
        "an abandoned tag is still confirmed by the per-tag endpoint: the baseline "
        "reclassifies a verdict it did not take a shortcut around"
    )


def test_a_third_orphan_outside_the_baseline_still_faults(mod, monkeypatch, capsys):
    """The baseline names two tags, not a class: anything else keeps the full verdict."""
    routes = _routes(
        _tags("v9.9.9", "v0.3.8", "v0.3.34"),
        _releases(),
        {
            ("api", f"repos/{REPO}/releases/tags/{t}"): _confirmed_absent(mod, t)
            for t in ("v9.9.9", "v0.3.8", "v0.3.34")
        },
    )
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "v9.9.9 -- NO RELEASE" in out
    assert "FAULT: 1 tag(s) with no release" in out, "only the unrecorded orphan is counted"
    assert "2 more carry no release by recorded decision" in out


def test_dropping_a_baseline_row_makes_that_tag_fault_again(mod, monkeypatch, capsys):
    """The other direction: the record is the only thing suppressing it."""
    monkeypatch.setattr(mod, "ABANDONED_TAGS", {})
    routes = _routes(
        _tags("v0.3.8"),
        _releases(),
        {("api", f"repos/{REPO}/releases/tags/v0.3.8"): _confirmed_absent(mod, "v0.3.8")},
    )
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "v0.3.8 -- NO RELEASE" in out
    assert "ABANDONED" not in out


def test_the_baseline_never_suppresses_a_different_fault_on_a_recorded_tag(
    mod, monkeypatch, capsys
):
    """A draft on a recorded tag is a draft, with a draft's remedy and nothing to confirm."""
    routes = _routes(
        _tags("v0.3.8", "v0.3.34"),
        _releases(("v0.3.8", "true", "false", "8")),
        {
            ("api", f"repos/{REPO}/releases/tags/v0.3.34"): _confirmed_absent(mod, "v0.3.34"),
        },
    )
    code, out, fake = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "release unusable: it is a DRAFT" in out
    assert "v0.3.8 -- ABANDONED" not in out, "the record reclassified nothing here"
    assert not fake.called("api", f"repos/{REPO}/releases/tags/v0.3.8")


def test_a_baseline_row_that_no_longer_describes_the_repository_is_a_fault(
    mod, monkeypatch, capsys
):
    """A suppression that outlives its reason is how a baseline becomes permanent rot."""
    routes = _routes(
        _tags("v0.3.8", "v0.3.34", "v0.3.50"),
        _releases(*_published("v0.3.8", "v0.3.50")),
        {
            ("api", f"repos/{REPO}/releases/tags/v0.3.34"): _confirmed_absent(mod, "v0.3.34"),
        },
    )
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "baseline row: v0.3.8 -- STALE" in out
    assert "remove it from ABANDONED_TAGS" in out
    assert "v0.3.8 -- ABANDONED" not in out, "a tag with a release is not abandoned"


def test_a_baseline_row_for_a_tag_the_collection_no_longer_has_is_a_fault(
    mod, monkeypatch, capsys
):
    """The second way a row stops describing the repository, and it faults for the same reason.

    A gone tag does not make its row true: the row names a tag the repository no longer has,
    so it suppresses nothing and can never be retired by anyone who is not told to retire it.
    The remedy is the same one line the contradicted row's remedy is, so this fault is neither
    permanent nor unactionable -- which is what an asymmetry between the two cases would have
    to rest on. The absence is confirmed on the tag's own ref first, because it was read from
    a collection.
    """
    routes = _routes(
        _tags("v0.3.50"),
        _releases(*_published("v0.3.50")),
        {
            ("api", f"repos/{REPO}/git/ref/tags/{tag}"): _tag_not_found(mod, tag)
            for tag in ("v0.3.8", "v0.3.34")
        },
    )
    code, out, fake = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "baseline row: v0.3.8 -- STALE" in out, "the row is named with its verdict"
    assert "not among the 1 tags read" in out, "the reason states what was actually read"
    assert "remove it from ABANDONED_TAGS" in out, "the one-line remedy rides the line"
    assert "v0.3.8 -- ABANDONED" not in out, "a tag with no ref is not abandoned by decision"
    assert fake.called("api", f"repos/{REPO}/git/ref/tags/v0.3.8"), (
        "the absence came from a collection, so the tag's own ref confirms it"
    )


def test_a_recorded_tag_the_collection_omits_but_its_ref_reports_is_not_measurable(
    mod, monkeypatch, capsys
):
    """The other direction of the same reading: a short collection must not convict a row.

    A tag collection can be short for a reason that has nothing to do with the tag, and this
    file's rule is that only the per-tag answer convicts. So a row whose tag the ref endpoint
    still reports is `not measurable` -- never a fault telling someone to delete a suppression
    that is still doing its job.
    """
    routes = _routes(
        _tags("v0.3.50"),
        _releases(*_published("v0.3.50")),
        {("api", f"repos/{REPO}/git/ref/tags/v0.3.8"): "{}"},
    )
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 2
    assert "not measurable" in out
    assert "answers that it exists" in out
    assert "not a pass" in out
    assert "STALE" not in out, "an unconfirmed absence must never be printed as a fault"


def test_the_baseline_is_exactly_the_two_measured_tags_and_each_carries_its_evidence(mod):
    """The record is a reading, so a reading of it is pinned: two rows, each with its run.

    Growing this dict is the decision this arm exists to make deliberate -- a third tag is a
    new permanent suppression, and it arrives as an edit to this test as well.
    """
    assert set(mod.ABANDONED_TAGS) == {"v0.3.8", "v0.3.34"}
    assert "36956685533" in mod.ABANDONED_TAGS["v0.3.8"], "the row cites its failed build"
    assert "41625081155" in mod.ABANDONED_TAGS["v0.3.34"], "the row cites its failed build"
    assert "superseded" in mod.ABANDONED_TAGS["v0.3.8"].lower()
    assert "superseded" in mod.ABANDONED_TAGS["v0.3.34"].lower()


def test_each_row_carries_the_date_its_successor_was_published(mod):
    """A reason a reader can check, not a word a reader has to trust.

    "superseded" was the whole assertion once, and a row that said its successor was
    "published the same day" passed it while the two dates were six days apart. A date is
    the smallest thing that makes the claim checkable against the release collection, so
    each row names one and this arm holds it to the successor it actually had.
    """
    assert "2026-10-08" in mod.ABANDONED_TAGS["v0.3.8"], (
        "v0.3.8 was tagged 2026-10-02 and v0.3.9 was published 2026-10-08 -- six days "
        "later, not the same day as the row once said"
    )
    assert "2026-11-03" in mod.ABANDONED_TAGS["v0.3.34"], (
        "v0.3.34 was tagged and v0.3.35 published on 2026-11-03"
    )
    for tag, reason in mod.ABANDONED_TAGS.items():
        assert "same day" not in reason.lower(), (
            f"{tag}'s row states a temporal claim a reader cannot check from the release "
            f"collection; name the successor's publication date instead"
        )


def _statement_blocks(tree):
    """Every list of statements in the module, so an exit can be read beside its siblings."""
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block


def test_every_exit_that_cannot_measure_names_its_cause():
    """A `return 2` states its own cause, so the contract cannot fall behind the code.

    The module docstring used to enumerate the ways a run can be `not measurable`. Nothing
    held that list in step with the code, and it drifted in both directions at once: it
    named a state no exit produces, and omitted two exits that exist. The property that
    survives a change sits at the exits, because that is where a cause is added -- every one
    of them prints a sentence beginning `not measurable:` before it returns 2. This arm
    holds that, and it is what the docstring's `2` entry sends a reader to.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    exits = 0
    unnamed: list[int] = []
    for block in _statement_blocks(tree):
        prints = [
            (stmt.lineno, " ".join(ast.unparse(arg) for arg in stmt.value.args))
            for stmt in block
            if isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == "print"
        ]
        for stmt in block:
            if not (
                isinstance(stmt, ast.Return)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value == 2
            ):
                continue
            exits += 1
            if not any(
                line < stmt.lineno and "not measurable" in text for line, text in prints
            ):
                unnamed.append(stmt.lineno)
    assert exits, "the module has no `return 2` - this arm would pass on nothing"
    assert not unnamed, (
        "these `return 2` exits print no `not measurable:` sentence of their own, so a "
        f"reader of the run cannot tell which measurement was missing: line(s) {unnamed}"
    )
