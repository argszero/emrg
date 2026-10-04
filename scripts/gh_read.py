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
* an anonymous `api.github.com` read of the same path second, when `gh` refused - the
  request `gh` declined to make, made without credentials.
* both reasons raised together when both refuse, because they are not interchangeable:
  `gh`'s refusal means "this host has no token", the anonymous `403` means "this endpoint
  is not public" (which is exactly the job log's answer).

Two limits, stated because they are limits
------------------------------------------
* **Only the `api` shape is translated.** `pr view --json …` and `pr list --json …` are
  `gh`'s own compositions of one or more REST paths *with renamed fields*
  (`headRefOid` vs `head.sha`, `mergeStateStatus` vs `mergeable_state`), and a translation
  that renames a field wrongly would corrupt a merge gate's reading - a worse outcome than
  the honest refusal it replaces. A caller that needs those shapes keeps using `gh`, and
  the refusal names the shape it could not translate.
* **A `--jq` filter is refused rather than dropped.** Returning the unfiltered payload
  where a filter was asked for does not fail - it changes what the caller reads, silently,
  which is the class of defect this whole family exists to remove. `read-run-failure.py`
  keeps its own two channels for the same reason: they are the ones its paths need.
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


def api_text(args: list[str]) -> str:
    """`gh <args>` if that answers, else the same `api` path read anonymously.

    Raises `RuntimeError` naming **both** channels' reasons when neither answers, so a
    caller reporting "could not measure" can say why without a second call.
    """
    rc, out, err = _gh_text(args)
    if rc == 0:
        _note("gh")
        return out
    gh_reason = err.strip() or f"gh exited {rc} with no message"

    try:
        path, paginate, refused = _api_path_and_flags(args)
    except ValueError as exc:
        raise RuntimeError(
            f"gh failed (rc={rc}): {gh_reason} - and this module translates only the "
            f"`api` shape, so there is no second channel for this call ({exc})"
        ) from exc
    if refused:
        raise RuntimeError(
            f"gh failed (rc={rc}): {gh_reason} - and the anonymous fallback would have to "
            f"drop {sorted(set(refused))} from the request, which changes what the caller "
            "reads rather than replacing the channel that read it"
        )
    try:
        text = _public_api_text(path, paginate)
    except PublicApiRefused as exc:
        raise RuntimeError(
            f"gh failed (rc={rc}): {gh_reason} | and {exc}"
        ) from exc
    _note("api.github.com (no token)")
    return text


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
