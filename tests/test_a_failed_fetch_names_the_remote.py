"""A failed PR-head fetch names the remote it asked (2026-10-03).

Background (cycle `cyc20261003-095256`)
---------------------------------------
Five merge gates fetch a PR's real head with

    git fetch --quiet origin +pull/<N>/head:refs/<gate>/pr<N>

and each reported a failure as `could not fetch PR #<N>: <git's sentence>`. On this
host the sentence was

    fatal: couldn't find remote ref pull/1826/head

about PR **#1826, which was open**, with head `c6eff015` on GitHub - measured while
running `check-merge-plan-suite.py 1826` as this cycle's merge gate. Nothing in the
gate's output points at the cause: `.git/config` here carries

    url.C:/Users/Administrator/.emrg/evolution/emrg/.insteadof https://github.com/argszero/emrg.git

so `origin` is a **local directory**, and `refs/pull/<N>/head` exists on the remote
the PR lives on, not in that directory. A name that reads as "the GitHub remote" is
the whole reason the message misleads: the reader is told the fetch failed and never
told where it went.

The rewrite is not the defect - it is this host's offline fallback, and it is what
makes a local `git fetch` work at all while git-over-https is down. The silence was.
So the failure now names the URL `git remote get-url origin` answers, the one
instrument that expands the rewrite: a reader sees a filesystem path and knows the
fetch never left the machine.

Both directions on every gate, and they are the point: the message must carry the
URL that was really answered - a GitHub URL when that is the answer, a filesystem
path when *that* is - so a message that hardcoded either one fails one of the two
arms. The control half of each arm is the *other* URL: each asserts its own is
present **and the other's is absent**, which is what separates "reports where the
fetch went" from "prints a string that happened to be nearby". The third arm keeps
git's own sentence: naming the remote must not replace the diagnostic the site
already carried (`test_check_merge_order.py` pins that half at its own site).

The last group is the class, not a site. Five gates spell this message, and the
repair for one of them is what a sixth would copy: the walker asks every gate that
fetches a `pull/<N>/head` whether it names the remote, and the walker's two
directions are themselves fixture-tested - a synthetic sixth site that copies the
old message is reported, one that names the remote is not.

Hermetic: no git, no network, no repository. Every runner is replaced.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

# (module name, file, does `_fetch_head` also take the repo?) - the fifth gate's
# signature differs from the other four's, and the tests must not paper over that.
GATES = (
    ("check_merge_plan_suite", "check-merge-plan-suite.py", False),
    ("check_merge_tree_health", "check-merge-tree-health.py", False),
    ("check_merge_landing_diff", "check-merge-landing-diff.py", False),
    ("check_merge_sequence", "check-merge-sequence.py", False),
    ("check_merge_order", "check-merge-order.py", True),
)

GITHUB_URL = "https://github.com/argszero/emrg.git"
LOCAL_URL = "C:/Users/Administrator/.emrg/evolution/emrg/"
FETCH_FAILURE = "fatal: couldn't find remote ref pull/7/head"

#: The refspec each gate builds. One spelling across all five - that is what makes a
#: single walker able to find the sites at all.
FETCH_REFSPEC = '"+pull/{number}/head:{ref}"'
NAMES_THE_REMOTE = "merge_tree.remote_url('origin'"


def _load(name: str, filename: str):
    """Load a gate by path - its directory name has hyphens and cannot be imported."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=GATES, ids=[gate[0] for gate in GATES])
def gate(request):
    return _load(request.param[0], request.param[1])


def _fetch_failure(gate_module, monkeypatch, url: str) -> str:
    """The message a gate produces when its fetch fails and its remote answers `url`.

    The runner is replaced, never executed: the two commands a fetch site runs are
    `git fetch` (fails) and, on that path, `git remote get-url` (answers `url`). Any
    other argv is a failure of this fixture, not of the gate.
    """
    takes_repo = dict((g[0], g[2]) for g in GATES)[gate_module.__name__]

    def run(argv, *args, **kwargs):
        if list(argv[:3]) == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 0, url + "\n", "")
        assert "fetch" in argv, f"an unanticipated command was run: {argv}"
        return subprocess.CompletedProcess(argv, 1, "", FETCH_FAILURE)

    monkeypatch.setattr(gate_module, "_run", run)
    with pytest.raises(Exception) as excinfo:
        if takes_repo:
            gate_module._fetch_head("argszero/emrg", 7)
        else:
            gate_module._fetch_head(7)
    return str(excinfo.value)


def test_a_github_remote_is_named_as_the_github_remote(gate, monkeypatch) -> None:
    message = _fetch_failure(gate, monkeypatch, GITHUB_URL)
    assert GITHUB_URL in message
    assert LOCAL_URL not in message


def test_a_rewritten_remote_is_named_as_what_it_really_is(gate, monkeypatch) -> None:
    """The measured case: `origin` is a local directory, and the message says so."""
    message = _fetch_failure(gate, monkeypatch, LOCAL_URL)
    assert LOCAL_URL in message
    assert GITHUB_URL not in message


@pytest.mark.parametrize("url", [GITHUB_URL, LOCAL_URL])
def test_the_remote_is_added_to_gits_own_sentence_never_instead_of_it(
    gate, monkeypatch, url: str
) -> None:
    message = _fetch_failure(gate, monkeypatch, url)
    assert FETCH_FAILURE in message
    assert f"could not fetch PR #7" in message


def test_an_unreadable_remote_is_unknown_never_an_empty_string() -> None:
    """The helper's own direction: a URL it cannot read is `unknown`, not a guess."""
    merge_tree = _load("merge_tree", "merge_tree.py")

    def refusing(argv, *args, **kwargs):
        return subprocess.CompletedProcess(argv, 128, "", "error: No such remote 'origin'")

    assert merge_tree.remote_url("origin", refusing) == "unknown"


@pytest.mark.parametrize("stdout", ["", "\n", "   \n"])
def test_an_empty_answer_is_unknown(stdout: str) -> None:
    merge_tree = _load("merge_tree", "merge_tree.py")

    def blank(argv, *args, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    assert merge_tree.remote_url("origin", blank) == "unknown"


def test_a_runner_that_cannot_spawn_is_unknown() -> None:
    """This runs *inside* an error path: it must annotate a failure, never replace it."""
    merge_tree = _load("merge_tree", "merge_tree.py")

    def absent(argv, *args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    assert merge_tree.remote_url("origin", absent) == "unknown"


def _sites_that_do_not_name_the_remote(scripts: Path) -> list[str]:
    """Gates that fetch a PR head without saying which remote they asked.

    Keyed on the refspec, not on a function's name: the property is "a fetch of a
    `pull/<N>/head` reports where it fetched from", and `_fetch_head` is only what
    the five gates happen to call their fetch site today.
    """
    findings = []
    for path in sorted(scripts.glob("check-merge-*.py")):
        source = path.read_text(encoding="utf-8")
        if FETCH_REFSPEC not in source:
            continue
        if NAMES_THE_REMOTE not in source:
            findings.append(path.name)
    return findings


def test_every_pull_head_fetch_names_the_remote_it_asked() -> None:
    assert _sites_that_do_not_name_the_remote(SCRIPTS) == []


def test_the_walker_sees_every_fetch_site() -> None:
    """A walker that finds nothing is green and says nothing - pinned here."""
    seen = sorted(
        path.name
        for path in SCRIPTS.glob("check-merge-*.py")
        if FETCH_REFSPEC in path.read_text(encoding="utf-8")
    )
    assert len(seen) == 5, f"the walk found {len(seen)} fetch sites, expected 5: {seen}"


def test_the_walker_reports_a_sixth_site_that_copies_the_old_message(tmp_path) -> None:
    """A new gate copying the message must fail the walker, not survive until read."""
    (tmp_path / "check-merge-new.py").write_text(
        'raise MeasurementError(f"could not fetch PR #{number}: {detail}")\n'
        f"ref = f'{FETCH_REFSPEC}'\n",
        encoding="utf-8",
    )
    assert _sites_that_do_not_name_the_remote(tmp_path) == ["check-merge-new.py"]


def test_the_walker_passes_a_site_that_names_the_remote(tmp_path) -> None:
    (tmp_path / "check-merge-new.py").write_text(
        f"ref = f'{FETCH_REFSPEC}'\n"
        f'raise MeasurementError(f"{{{NAMES_THE_REMOTE}, _run}})")\n',
        encoding="utf-8",
    )
    assert _sites_that_do_not_name_the_remote(tmp_path) == []
