"""Tests for `scripts/gh_read.py` - which channel reads a GitHub path, and what it costs.

Why these tests exist
---------------------
The module exists because the queue tools are unmeasurable on a tokenless host while the
data is public (`cyc20261004-074903`). So the two things that must be pinned are the two
directions of that sentence: **the fallback happens when `gh` refuses**, and **it does not
happen when `gh` answers** - the second because an always-on fallback would spend the
host's 60/hour anonymous budget on requests `gh` already answered, and would hide a `gh`
that is failing.

The failure mode this module could reintroduce is subtler than "it did not fall back": a
fallback that changes *what the caller reads* while appearing to replace only *which
channel read it*. Two of those are pinned here as the silent direction - a `--jq` filter
that would be dropped, and a non-`api` argv (`pr view --json headRefOid`) that would need
field renaming to translate. Both are refused with a message, never approximated: an
approximation is not a failure, it is a wrong answer that looks like a right one.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gh_read.py"


def _load(name="gh_read"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load()


#: What `gh` really prints on a host with no token - measured 2026-10-04, and quoted
#: rather than described because the *words* are what a reader of the refusal sees.
GH_REFUSED = (
    "To get started with GitHub CLI, please run:  gh auth login\n"
    "Alternatively, populate the GH_TOKEN environment variable with a GitHub API "
    "authentication token.\n"
)


def gh_says(mod, monkeypatch, *, rc: int, out: str = "", err: str = "", record=None):
    """Drive the module's own `subprocess.run`, at its seam.

    Not `subprocess.run` globally: `mod.subprocess` is the shared stdlib module, so
    patching its attribute would replace it for everything else in the process (the
    reason the sibling suites' fakes are documented).
    """

    class _Proc:
        def __init__(self):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def fake_run(argv, **kwargs):
        if record is not None:
            record.append(argv)
        assert argv[0] == "gh", argv
        return _Proc()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)


class FakeResponse:
    def __init__(self, payload, link: str = ""):
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {"Link": link}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def public_says(mod, monkeypatch, routes: dict[str, object], record=None):
    """Route `urlopen` by URL, recording every URL asked for."""

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        if record is not None:
            record.append(url)
        if url not in routes:
            raise AssertionError(f"unexpected anonymous URL: {url}")
        value = routes[url]
        if isinstance(value, Exception):
            raise value
        return FakeResponse(*value) if isinstance(value, tuple) else FakeResponse(value)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)


# --- the channel choice -----------------------------------------------------


def test_gh_answering_is_returned_and_the_public_api_is_not_asked(mod, monkeypatch):
    """Control leg: with a working `gh` nothing else is touched.

    `transports_used()` is asserted to be exactly `["gh"]` as well as the body, because a
    fallback that fired *after* gh succeeded would still return gh's text - it would only
    show up as a spent request and a misnamed basis.
    """
    gh_says(mod, monkeypatch, rc=0, out='{"ok": true}')
    asked: list[str] = []
    # The route is provided even though a correct implementation never asks: with an empty
    # table the fake raises "unexpected anonymous URL" *before* the assertion below runs,
    # so an arm that makes the fallback fire was reported UNJUDGEABLE rather than KILLED -
    # the fixture, not the rule, was what failed (measured 2026-10-04).
    public_says(
        mod,
        monkeypatch,
        {"https://api.github.com/repos/argszero/emrg": {"unexpected": True}},
        record=asked,
    )
    body = mod.api_text(["api", "repos/argszero/emrg"])
    # The claim of this leg first, and for the same reason: an arm that makes the fallback
    # fire is judged on this assertion, not on whichever body comparison comes first.
    assert asked == [], "a working gh must not be second-guessed"
    assert body == '{"ok": true}'
    assert mod.transports_used() == ["gh"]


def test_a_refusing_gh_is_followed_by_the_public_api(mod, monkeypatch):
    """The whole point: the request `gh` declined to make, made without credentials."""
    gh_says(mod, monkeypatch, rc=4, err=GH_REFUSED)
    public_says(
        mod,
        monkeypatch,
        {"https://api.github.com/repos/argszero/emrg": {"full_name": "argszero/emrg"}},
    )
    text = mod.api_text(["api", "repos/argszero/emrg"])
    assert json.loads(text) == {"full_name": "argszero/emrg"}
    assert mod.transports_used() == ["api.github.com (no token)"]


def test_both_channels_refusing_names_both_reasons(mod, monkeypatch):
    """`gh`'s refusal and the anonymous `403` are different facts and must both survive.

    On this host they are exactly what a caller sees for a job log: "no token here" from
    one channel, "this endpoint is not public" from the other. Collapsing them into one
    sentence is the defect the family exists to remove.
    """
    gh_says(mod, monkeypatch, rc=4, err=GH_REFUSED)
    url = "https://api.github.com/repos/argszero/emrg/actions/jobs/1/logs"
    public_says(mod, monkeypatch, {url: mod.PublicApiRefused("HTTP 403")})
    with pytest.raises(RuntimeError) as excinfo:
        mod.api_text(["api", "repos/argszero/emrg/actions/jobs/1/logs"])
    message = str(excinfo.value)
    assert "gh auth login" in message, "the proxy's own words are carried, not paraphrased"
    assert "HTTP 403" in message, "and the anonymous status with them"


def test_a_missing_gh_binary_falls_back_rather_than_crashing(mod, monkeypatch):
    """The other way `gh` can fail to answer, and it must reach the same fallback.

    The old wrapper in `check-issue-links.py` caught `FileNotFoundError` and let every
    other failure raise, so the two paths to "ask the next channel" were written twice and
    could drift. Here they are one path.
    """

    def raise_missing(argv, **kwargs):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(mod.subprocess, "run", raise_missing)
    public_says(mod, monkeypatch, {"https://api.github.com/x": [1]})
    assert json.loads(mod.api_text(["api", "x"])) == [1]


# --- what the fallback must refuse rather than approximate ------------------


def test_a_jq_filter_is_refused_not_dropped(mod, monkeypatch):
    """Dropping `--jq` does not fail - it changes what the caller reads, silently."""
    gh_says(mod, monkeypatch, rc=4, err=GH_REFUSED)
    asked: list[str] = []
    public_says(mod, monkeypatch, {}, record=asked)
    with pytest.raises(RuntimeError) as excinfo:
        mod.api_text(["api", "repos/argszero/emrg/pulls/1/reviews", "--jq", ".[] | .body"])
    assert "--jq" in str(excinfo.value)
    assert asked == [], "the unfiltered payload must not be fetched and handed back as if filtered"


def test_a_non_api_argv_is_refused_by_name(mod, monkeypatch):
    """`pr view --json …` needs field renaming, and a wrong rename corrupts a merge gate.

    Refusing is the honest half of the two: the caller keeps `gh`, and the refusal says
    which shape could not be translated rather than returning a payload whose `headRefOid`
    is missing because REST calls it `head.sha`.
    """
    gh_says(mod, monkeypatch, rc=4, err=GH_REFUSED)
    asked: list[str] = []
    public_says(mod, monkeypatch, {}, record=asked)
    with pytest.raises(RuntimeError) as excinfo:
        mod.api_text(["pr", "view", "1841", "--json", "state,mergedAt"])
    message = str(excinfo.value)
    assert "only the" in message and "api" in message
    assert asked == []


# --- paging -----------------------------------------------------------------


def test_the_next_link_is_read_by_its_relation(mod):
    """The relation decides, and it is not the first URL in the header.

    GitHub sends `prev`, `next` and `last` in one comma-separated `Link` value; on a first
    page the `last` relation can precede `next`, so taking the first URL would jump to the
    end of the listing - and on a middle page it would page backwards. Both directions are
    asserted, including the one where there is no `next` at all.
    """
    header = (
        '<https://api.github.com/x?page=5>; rel="last", '
        '<https://api.github.com/x?page=3>; rel="next", '
        '<https://api.github.com/x?page=1>; rel="prev"'
    )
    assert mod._next_link(header) == "https://api.github.com/x?page=3"
    assert mod._next_link('<https://api.github.com/x?page=1>; rel="prev"') == ""
    assert mod._next_link("") == ""


def test_paginate_follows_the_next_link_and_merges(mod, monkeypatch):
    """`gh api --paginate` prints one merged array; so must this, or callers drift.

    The `Link` header carries `prev`, `next` and `last` in one comma-separated string, so
    taking the first URL in it would page *backwards* from page two onward - hence the
    relation is parsed rather than assumed.
    """
    gh_says(mod, monkeypatch, rc=4, err=GH_REFUSED)
    page2 = "https://api.github.com/repos/argszero/emrg/issues?page=2"
    page3 = "https://api.github.com/repos/argszero/emrg/issues?page=3"
    public_says(
        mod,
        monkeypatch,
        {
            "https://api.github.com/repos/argszero/emrg/issues": (
                [{"number": 1}],
                f'<{page3}>; rel="last", <{page2}>; rel="next"',
            ),
            # A middle page carries **all three** relations, which is what the real header
            # does and the reason the relation is parsed rather than assumed: an earlier
            # version of this fixture had no `next` on the middle page, so it asserted a
            # shape GitHub never sends and the pager stopped a page early.
            page2: (
                [{"number": 2}],
                f'<{page2}>; rel="prev", <{page3}>; rel="next", <{page3}>; rel="last"',
            ),
            page3: ([{"number": 3}], f'<{page2}>; rel="prev"'),
        },
    )
    merged = json.loads(mod.api_text(["api", "repos/argszero/emrg/issues", "--paginate"]))
    assert merged == [{"number": 1}, {"number": 2}, {"number": 3}]


def test_without_paginate_one_page_is_the_whole_answer(mod, monkeypatch):
    """Control leg for the pager: `gh api` without `--paginate` returns one page, and a
    fallback that silently followed links would return more rows than the caller asked
    for - a listing that no longer matches what the same call does with a token."""
    gh_says(mod, monkeypatch, rc=4, err=GH_REFUSED)
    page2 = "https://api.github.com/repos/argszero/emrg/issues?page=2"
    asked: list[str] = []
    public_says(
        mod,
        monkeypatch,
        {
            "https://api.github.com/repos/argszero/emrg/issues": (
                [{"number": 1}],
                f'<{page2}>; rel="next"',
            ),
            page2: ([{"number": 2}], ""),
        },
        record=asked,
    )
    assert json.loads(mod.api_text(["api", "repos/argszero/emrg/issues"])) == [{"number": 1}]
    assert asked == ["https://api.github.com/repos/argszero/emrg/issues"]


def test_a_paging_loop_that_would_not_end_is_reported(mod, monkeypatch):
    """A partial listing reported as the whole one is the family's cardinal sin.

    Driven by a `Link: rel="next"` that never stops - the shape a server can produce by
    accident, and the one thing an unbounded `while next_url` would turn into a hang.
    """
    gh_says(mod, monkeypatch, rc=4, err=GH_REFUSED)
    url = "https://api.github.com/x"
    public_says(mod, monkeypatch, {url: ([{"n": 1}], f'<{url}>; rel="next"')})
    monkeypatch.setattr(mod, "MAX_PAGES", 3)
    with pytest.raises(mod.PublicApiRefused) as excinfo:
        mod._public_api_text("x", True)
    assert "3 pages" in str(excinfo.value)


def test_a_body_that_is_not_json_is_refused(mod, monkeypatch):
    """A proxy or an error page that is not JSON must not become "[]"."""
    gh_says(mod, monkeypatch, rc=4, err=GH_REFUSED)

    class NotJson:
        headers = {"Link": ""}

        def read(self):
            return b"<html>rate limited</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda request, timeout=None: NotJson())
    with pytest.raises(RuntimeError) as excinfo:
        mod.api_text(["api", "repos/argszero/emrg"])
    assert "not JSON" in str(excinfo.value)


# --- the call site this cycle wired -----------------------------------------


def test_check_issue_links_reads_through_this_module():
    """The rewire, pinned: the tool's `_gh` must be this module's `api_text`.

    Without this leg the next edit could put the old `gh`-only wrapper back and every test
    in the two files would still pass - the reading would just stop working on a tokenless
    host, which is the state this cycle removed.
    """
    spec = importlib.util.spec_from_file_location(
        "check_issue_links_for_gh_read", REPO_ROOT / "scripts" / "check-issue-links.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.gh_read.__name__ == "gh_read"
    source = (REPO_ROOT / "scripts" / "check-issue-links.py").read_text(encoding="utf-8")
    body = source.split("def _gh(args: list[str]) -> str:", 1)[1].split("\n\n\ndef ", 1)[0]
    assert "gh_read.api_text(args)" in body, (
        "the tool must read through the shared channel-choosing module; a wrapper that "
        "runs `gh` itself is unmeasurable on a host with no token"
    )
    assert "subprocess.run" not in body, "and must not keep a second gh implementation"
