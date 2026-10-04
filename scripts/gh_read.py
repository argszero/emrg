#!/usr/bin/env python3
"""Read a GitHub REST path, with `gh` when there is a token and without one when there is not.

Why this module exists
----------------------
The readings in this family (`check-issue-links.py`, and the three other queue tools that
follow it) shell out to `gh`. On a host with no token, `gh` does **not** attempt the
request at all - it prints

    To get started with GitHub CLI, please run:  gh auth login
    Alternatively, populate the GH_TOKEN environment variable ...

and exits non-zero - while `api.github.com` answers the *same public paths* anonymously.
Measured 2026-10-04 (`cyc20261004-072223`, `-073846`, `-074903`): `/repos/.../pulls`,
`/issues`, a PR's `/reviews`, a commit's `/check-runs`, a run's `/jobs` and a job's
`/check-runs/<id>/annotations` all answer `200` with no credentials; a job's `/logs` is the
exception, `403`.

So on such a host every queue reading reported "could not measure" (`rc=2`) about data it
could have read, which is the failure the family's own rule forbids from the other side:
an unreadable reading is not a pass, but a *readable* one reported unreadable is the same
defect wearing the opposite mask. This module is the **single** place that decides which
channel answers, so the next tool does not have to re-solve it: `read-run-failure.py`
learned it first, in its own file (`cyc20261004-073846`), and a rule with two homes is a
rule free to drift apart (R9).

The contract
------------
:func:`api_text` takes the argv a caller would hand `gh` *after* the program name and
returns the text `gh` would have printed, whichever channel produced it:

* `gh` first. Any successful exit is an answer and is returned unchanged.
* a read made without credentials second, when `gh` refused - the request `gh` declined to
  make: for an `api` argv the same path, for a `pr view` argv the same pull request
  projected back into the field names the caller asked for.
* both reasons raised together when both refuse, because they are not interchangeable:
  `gh`'s refusal means "this host has no token", the anonymous `403` means "this endpoint
  is not public" (which is exactly the job log's answer).

Two limits, stated because they are limits
------------------------------------------
* **Two shapes are translated; every other `gh` composition is refused.** `api` (a REST
  path, with the query string it carries) and `pr view <n> -R <repo> --json <fields>` are
  translated - the second through a **per-field** table (:data:`_PR_VIEW_FIELDS`) because
  `gh pr view` is `gh`'s own projection of one REST payload and three of its fields do not
  agree with REST on the *value*, not merely the name (the table below). Everything else -
  `pr list --json`, `run view`, the rest of the CLI's compositions - is refused **by
  name**: a translation that renames a field *wrongly* would corrupt a merge gate's
  reading, a worse outcome than the honest refusal it replaces. A field with no measured
  translation is refused the same way, even inside a shape that is translated.
* **A `--jq` filter is refused rather than dropped.** Returning the unfiltered payload
  where a filter was asked for does not fail - it changes what the caller reads, silently,
  which is the class of defect this whole family exists to remove. `read-run-failure.py`
  keeps its own two channels for the same reason: they are the ones its paths need.

The three value translations, which are not renames
---------------------------------------------------
`pr view` and the REST payload it is built from agree on **keys** for some fields and
disagree on **values** for three others. Measured 2026-10-04 (`cyc20261004-081356`) against
`/repos/argszero/emrg/pulls/<n>` for an open, a merged and a closed-unmerged PR:

===========  ====================  =====================================
`gh` field   REST source           the disagreement
===========  ====================  =====================================
`state`      `state` + `merged`    REST says `open` / `closed` only; a merged PR is
                                   `closed` **with** `merged: true`, so `MERGED` has to
                                   be reconstructed or every merged PR reads as `CLOSED`
`mergeable`  `mergeable`           REST is a **boolean**; `gh` is the string
                                   `MERGEABLE` / `CONFLICTING` / `UNKNOWN`. Handing a
                                   boolean to the vote counter makes `blocked` false for
                                   a conflicting PR - it compares against `"CONFLICTING"`
`mergeStateStatus`  `mergeable_state`  REST is lower case (`clean`, `dirty`); `gh` is
                                   upper (`CLEAN`, `DIRTY`), and the counter's non-clean
                                   set is spelled in upper case
===========  ====================  =====================================

A field with no measured translation is **refused by name**, never passed through
unchanged: a payload whose `mergeable` is `True` where the caller compares strings is
exactly the silent corruption this module exists to prevent.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

#: Identifies this tool to api.github.com, which refuses requests without one.
USER_AGENT = "emrg-gh-read"
#: One anonymous request's budget. Anonymous reads share a 60/hour bucket with every other
#: unauthenticated client on the host, so a hung request is not worth waiting out.
TIMEOUT = 30
#: A paging loop that cannot end is worse than a short reading: a ceiling on pages, said
#: out loud rather than left implicit, and reported when it is hit.
MAX_PAGES = 20

#: Which channels have answered, so a reading can name its own basis. A set: one reading
#: can straddle a `gh` call and a fallback by design.
_TRANSPORTS: set[str] = set()


def transports_used() -> list[str]:
    """The channels that answered, sorted - the reading's basis, printed beside it."""
    return sorted(_TRANSPORTS)


def _note(which: str) -> None:
    _TRANSPORTS.add(which)


class PublicApiRefused(RuntimeError):
    """`api.github.com` answered, and refused: the status is the answer, not a hiccup."""


def _gh_text(args: list[str]) -> tuple[int, str, str]:
    """`gh <args>` as `(returncode, stdout, stderr)`, never raising on a bad exit.

    A missing `gh` binary is reported as a return code rather than an exception, because
    the caller treats "no gh here" and "gh here but refused" identically - both mean *ask
    the next channel* - and a caller that had to catch two exception types to reach the
    same fallback would eventually forget one (the shape `check-issue-links.py`'s old
    `_gh` had: it caught `FileNotFoundError` and let every other failure raise).
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        return 127, "", f"gh is not available on this machine ({exc})"
    return proc.returncode, proc.stdout, proc.stderr


def _api_path_and_flags(args: list[str]) -> tuple[str, bool, list[str]]:
    """`(path, paginate, refused_flags)` out of an `api` argv.

    The path is positional and carries its own query string. `--paginate` is honoured by
    this module's own pager; any other flag is returned in `refused_flags` so the caller
    can refuse the fallback rather than reinterpret the request.
    """
    if not args or args[0] != "api":
        raise ValueError(f"not an `api` invocation: {args!r}")
    rest = args[1:]
    path = ""
    paginate = False
    refused: list[str] = []
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "--paginate":
            paginate = True
        elif item in ("--jq", "-q") and index + 1 < len(rest):
            refused.append(item)
            index += 1
        elif item.startswith("-"):
            refused.append(item)
        elif not path:
            path = item
        else:
            refused.append(item)
        index += 1
    if not path:
        raise ValueError(f"no path in an `api` invocation: {args!r}")
    return path, paginate, refused


def _next_link(header: str) -> str:
    """The `rel="next"` URL out of a `Link` header, or `""`.

    Parsed rather than assumed: the header carries several relations
    (`prev`, `next`, `last`) in one comma-separated string, and taking the first URL would
    page backwards on a page after the first.
    """
    for part in header.split(","):
        if 'rel="next"' in part:
            start = part.find("<")
            end = part.find(">")
            if start != -1 and end > start:
                return part[start + 1 : end]
    return ""


def _http_json(url: str) -> tuple[object, str]:
    """One anonymous GET, as `(payload, next_url)`.

    `next_url` is read from the `Link` header so a caller can page without knowing how
    this function works.
    """
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body), _next_link(response.headers.get("Link") or "")
    except urllib.error.HTTPError as exc:
        raise PublicApiRefused(
            f"api.github.com answered HTTP {exc.code} for {url} anonymously"
        ) from exc
    except json.JSONDecodeError as exc:
        raise PublicApiRefused(
            f"api.github.com answered {url} with a body that is not JSON: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a report, not a crash
        raise PublicApiRefused(
            f"api.github.com could not be reached for {url} anonymously: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _public_api_text(path: str, paginate: bool) -> str:
    """The `api` path read with no credentials, as the text `gh` would have printed.

    The merged-array shape matches what `gh api --paginate` prints with no `--jq` (one
    JSON array on one line - measured and recorded in `check-issue-links.py`'s own
    docstring), so a caller that parses gh's output parses this one unchanged.
    """
    url = f"https://api.github.com/{path}"
    first, next_url = _http_json(url)
    if not paginate:
        return json.dumps(first)
    if not isinstance(first, list):
        return json.dumps(first)
    merged = list(first)
    pages = 1
    while next_url and pages < MAX_PAGES:
        payload, next_url = _http_json(next_url)
        if not isinstance(payload, list):
            break
        merged.extend(payload)
        pages += 1
    if next_url:
        raise PublicApiRefused(
            f"the anonymous read of {path} stopped after {MAX_PAGES} pages with more to "
            "come - a partial listing reported as the whole one is the reading this "
            "module exists to keep from happening"
        )
    return json.dumps(merged)


class Untranslatable(RuntimeError):
    """This argv is not a shape this module knows how to read without `gh`."""


def api_text(args: list[str]) -> str:
    """`gh <args>` if that answers, else the same read made anonymously.

    Raises `RuntimeError` naming **both** channels' reasons when neither answers, so a
    caller reporting "could not measure" can say why without a second call.
    """
    rc, out, err = _gh_text(args)
    if rc == 0:
        _note("gh")
        return out
    gh_reason = err.strip() or f"gh exited {rc} with no message"

    try:
        text = _translated(args)
    except PublicApiRefused as exc:
        raise RuntimeError(f"gh failed (rc={rc}): {gh_reason} | and {exc}") from exc
    except Untranslatable as exc:
        raise RuntimeError(f"gh failed (rc={rc}): {gh_reason} - and {exc}") from exc
    _note("api.github.com (no token)")
    return text


def _translated(args: list[str]) -> str:
    """The anonymous substitution for `args`, or a refusal naming what could not be done.

    Three outcomes, and telling them apart is the point: this **returns** the text when the
    shape is known and the anonymous channel answered; raises `PublicApiRefused` when the
    shape was known and the *endpoint* refused (the status is the answer - a job log is the
    one path that does this, and reporting it as a translation failure would hide that); and
    raises `Untranslatable` when the shape itself is not known, which is not a network fact
    at all.

    The argv is kept as the caller wrote it rather than replaced by a function call, so a
    tool swapping its transport changes one line - the one that says where the payload comes
    from - and its request stays reviewable against what it asked for before.
    """
    if args[:2] == ["pr", "view"]:
        try:
            number, fields, repo = _pr_view_argv(args)
            payload = pr_view(number, fields, repo)
        except PublicApiRefused:
            raise
        except RuntimeError as exc:
            raise Untranslatable(f"that `pr view` call cannot be translated ({exc})") from exc
        return json.dumps(payload)
    if args[:1] == ["api"]:
        path, paginate, refused = _api_path_and_flags(args)
        if refused:
            raise Untranslatable(
                "the anonymous fallback would have to drop "
                f"{sorted(set(refused))} from the request, which changes what the caller "
                "reads rather than replacing the channel that read it"
            )
        return _public_api_text(path, paginate)
    shape = args[0] if args else "(no arguments)"
    raise Untranslatable(
        f"it translates the `api` and `pr view` shapes only, and this is {shape!r}. "
        "A shape it cannot translate is refused rather than guessed at: `pr list --json` "
        "and every other `gh` composition rename fields, and a wrong rename corrupts a "
        "merge gate's reading instead of failing"
    )


def _pr_view_argv(args: list[str]) -> tuple[int, list[str], str]:
    """`(number, fields, repo)` out of a `pr view <n> -R <owner/repo> --json f1,f2` argv.

    Raises `RuntimeError` when the shape is recognised but not translatable - a missing PR
    number, no `--json`, no `-R`, or an argument this module does not know. That is
    deliberately *not* the same answer as "not a `pr view` at all" (the caller checks
    `args[:2]` for that): a `pr view` argv that lost its `-R` must be refused with a reason,
    never fall through to "unknown shape" and be retried as something else.

    `-R` is **required**, unlike `gh`, which infers the repository from the working
    directory's remote. This module reads the pull request by REST path (`repos/<repo>/…`),
    so it needs the repository named; inferring one from the cwd would make the same argv
    answer about different repositories depending on where it ran.
    """
    rest = args[2:]
    number = 0
    fields: list[str] = []
    repo = ""
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "--json" and index + 1 < len(rest):
            fields = [f.strip() for f in rest[index + 1].split(",") if f.strip()]
            index += 2
            continue
        if item in ("-R", "--repo") and index + 1 < len(rest):
            repo = rest[index + 1]
            index += 2
            continue
        if not number:
            try:
                number = int(item)
            except ValueError as exc:
                raise RuntimeError(f"{item!r} is not a PR number") from exc
            index += 1
            continue
        raise RuntimeError(f"{item!r} is an argument this module does not translate")
    if not number:
        raise RuntimeError("no PR number")
    if not fields:
        raise RuntimeError("no `--json <fields>`")
    if not repo:
        raise RuntimeError(
            "no `-R <owner/repo>` - this module reads a pull request by REST path, so it "
            "needs the repository named rather than inferred from the working directory"
        )
    return number, fields, repo


# ── the `pr view` shape: a REST payload projected into `gh`'s field names ────

#: `gh --json` field name → how to build it from the REST pull payload. Each entry is a
#: function rather than a key, because three of them do not agree with REST on the *value*
#: (the module docstring's table) and a rename-only table would pass those three through
#: silently wrong.
#:
#: Every entry here was measured against a real payload for an open, a merged and a
#: closed-unmerged PR (`cyc20261004-081356`). A field that is not in this table is
#: **refused by name** by `pr_view`: the caller asked for something this module has no
#: measured translation for, and inventing one is the corruption it exists to prevent.
def _key(name: str):
    """A field that agrees with REST on name **and** shape - the only kind passed through.

    A function rather than a bare string so every entry in the table has one signature
    (`payload -> value`): a mixture of keys and callables is how a table like this ends up
    projecting the whole payload into every field it was not written for.
    """
    return lambda payload: payload.get(name)


def _nested(*path):
    def read(payload):
        node = payload
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node

    return read


def _state(payload):
    """`gh`'s `state`: `OPEN` / `MERGED` / `CLOSED`, reconstructed from REST.

    REST reports only `open` or `closed`, and carries the merge as a separate `merged`
    flag, so a merged PR is `closed` **with** `merged: true`. Passing REST's `closed`
    through would report every merged PR as merely closed - and the two are not the same
    reading: `check-vote-count.py`'s `mark` prints `MERGED` or `CLOSED` because the
    remedies differ, and `check-merge-freshness.py` reports "merged at <time>" for one and
    "closed without merging" for the other. Measured 2026-10-04: #1834 and #1836 are
    `closed`+`merged=true`, #1710 is `closed`+`merged=false`.
    """
    raw = str(payload.get("state") or "").lower()
    if raw == "open":
        return "OPEN"
    if raw == "closed":
        return "MERGED" if payload.get("merged") is True else "CLOSED"
    return raw.upper()


def _mergeable(payload):
    """`gh`'s `mergeable`: the string, from REST's boolean.

    REST answers `true` / `false` / `null` (not computed); `gh` answers
    `MERGEABLE` / `CONFLICTING` / `UNKNOWN`. The direction that matters is `false`:
    `check-vote-count.py`'s `blocked` compares `mergeable == "CONFLICTING"`, so a bare
    `False` would make a conflicting PR read as **not blocked**, and `ok` could then
    answer True for a PR that cannot merge - the failure its own docstring records being
    fixed once already. Measured 2026-10-04: #1710 is `mergeable=False` and gh calls it
    `CONFLICTING`; #1834 (merged) is `None` and gh calls it `UNKNOWN`.
    """
    value = payload.get("mergeable")
    if value is True:
        return "MERGEABLE"
    if value is False:
        return "CONFLICTING"
    return "UNKNOWN"


def _merge_state(payload):
    """`gh`'s `mergeStateStatus`: REST's `mergeable_state`, upper-cased.

    REST spells it lower (`clean`, `dirty`, `behind`, `blocked`, `draft`, `unstable`,
    `unknown`); the counter's `_NON_CLEAN_STATES` is a dict keyed in upper case, and its
    `CLEAN` comparison is upper too. Measured 2026-10-04: #1841 is `clean`, #1710 is
    `dirty`, #1834 is `unknown`.
    """
    return str(payload.get("mergeable_state") or "UNKNOWN").upper()


#: The projection. Keys are what a caller asks `gh` for; values build them from REST.
_PR_VIEW_FIELDS = {
    "number": _key("number"),
    "title": _key("title"),
    "state": _state,
    "draft": _key("draft"),
    # `gh`'s `mergedAt` is REST's `merged_at` - the one entry here that is a genuine
    # rename *and* passes its value through, which is why the table holds no bare strings:
    # a key-only table would have to spell this one `mergedAt` and answer `None` forever.
    "mergedAt": _key("merged_at"),
    "headRefOid": _nested("head", "sha"),
    "headRefName": _nested("head", "ref"),
    "baseRefName": _nested("base", "ref"),
    "mergeable": _mergeable,
    "mergeStateStatus": _merge_state,
}


def pr_view(number: int, fields: list[str], repo: str = "argszero/emrg") -> dict:
    """One pull request, shaped exactly as `gh pr view <n> --json <fields>` shapes it.

    `fields` is the caller's own list, in `gh`'s spelling - the call site keeps its
    request unchanged and this module answers it. That is what makes the swap safe to
    review: the only line that changes in the tool is the one that says where the payload
    comes from.

    **This reads the anonymous channel and no other.** It is reached from `_translated`,
    which runs only after `gh` has already refused the same call, so asking `gh` again here
    would be a second apology for a question that has been answered - and on a host *with*
    a token `api_text` returns `gh`'s own output and never arrives. A caller that wants the
    channel choice made for it calls `api_text(["pr", "view", …])`; that is the entry point
    the tools use.

    The REST payload is a superset of what is projected, so no field's absence is a
    payload problem; an **unknown** field name is a translation this module does not have
    and is refused by name, because the alternative - returning the field under a guessed
    shape - is the corruption the module exists to keep from a merge gate.
    """
    unknown = sorted(set(fields) - set(_PR_VIEW_FIELDS))
    if unknown:
        raise RuntimeError(
            f"no measured translation for {unknown} - `gh pr view --json` fields this "
            f"module knows: {sorted(_PR_VIEW_FIELDS)}. A field passed through under a "
            "guessed shape is a corrupted reading, not a fallback"
        )
    payload = _http_json(f"https://api.github.com/repos/{repo}/pulls/{int(number)}")[0]
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"the pull request payload for #{number} is {type(payload).__name__}, not an "
            "object - the reading was not answered"
        )
    if payload.get("message") and "state" not in payload:
        # Every real pull payload carries a `state`; an error object carries only `message`.
        # Tested on the field rather than on `message` alone, so a future payload with an
        # unrelated `message` field is projected instead of being reported as an error.
        raise RuntimeError(f"#{number} could not be read: {payload['message']}")
    return {field: _PR_VIEW_FIELDS[field](payload) for field in fields}


def main(argv: list[str] | None = None) -> int:
    """A one-line probe: which channel answers this path here?

    Exists because the question "can this host read GitHub at all, and how" is asked at the
    top of every reading and answered nowhere in the family - and because the answer on a
    given host changes without notice (a token is added, or expires).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(
            "usage: gh_read.py <api path>   e.g. gh_read.py repos/argszero/emrg",
            file=sys.stderr,
        )
        return 2
    try:
        text = api_text(["api", *args])
    except RuntimeError as exc:
        print(f"could not read: {exc}", file=sys.stderr)
        return 2
    print(f"transport: {', '.join(transports_used())}")
    print(text[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
