#!/usr/bin/env python3
r"""Do the notarization credentials work — answered before a tag is pushed, not after.

Measured on tag `v0.3.8`, run `36956685533` (2026-10-02): the macOS **Notarize pkg** step
died three seconds after it started, the `release` job was skipped because of it, and
**nothing at all was published** — the other three platforms had built green. A three
second failure is a *refused submission*, never a notarization verdict (a verdict takes
minutes, exits 0 and reports `status=Invalid`). PR #1812 taught the step to print Apple's
own reply, so the cause is now *visible* — but only in CI, only after the tag is pushed,
which is one wasted build round for a question the host can ask at home in a second.

That is the case `evolution_prompt.md` §4 names: a check that lives only in CI needs a
host-side counterpart, or the host pays the round to learn what they could have measured
first.

The reading is deliberately the same exchange the release makes: `xcrun notarytool
history` against Apple with the three variables `build-release.yml` feeds the submit step
(its secret names, not new ones — a preflight reading a *different* credential set would
answer a question nobody asked). Measured on this host with deliberately bogus values
(2026-10-03), the refusal is:

    xcrun notarytool history --apple-id X --password Y --team-id Z --output-format json
    rc=1  stderr: "Error: HTTP status code: 401. Invalid credentials. ..."

What separates the three answers is **what replied**, not the exit code — the same
discrimination the CI step needed, because "exited non-zero" cannot tell a refused
credential from a missing `notarytool`:

* Apple answered with the JSON history it was asked for  -> `works`   (exit 0)
* Apple answered `HTTP status code: 401`/`403`           -> `refused` (exit 1)
* anything else — no `xcrun`, no credentials to send, a transport error, a timeout, or an
  exit 0 that is not the requested JSON —                 -> `unmeasurable` (exit 2)

The last bucket is the load-bearing one: a preflight that passes because it could not
reach a verdict would send the host to CI with the same false confidence this script
exists to remove, so it is exit 2 and **never 0**. The success shape is the one thing not
measurable on this host (it has no valid credentials to spend — and spending a real
submission to prove a *history* call works would be the waste this guards), so the arm is
only as strong as "the asked-for JSON came back"; nothing here reads a field inside it.

The password is read from the environment and never printed — not in the reading, and not
in a subprocess error echoed back (see `_redact`).

Exit codes, the family's contract:

    0  Apple accepted the credentials
    1  a fault: Apple refused them (HTTP 401/403). Usual causes: an expired or revoked
       app-specific password, an Apple ID or team ID that does not match, or a Developer
       Program agreement waiting to be accepted
    2  not measurable: the exchange could not be completed, or completed without a
       credentials verdict. Never 0, and the CI step must fail on it too

Usage:

    APPLE_ID=<id> MACOS_NOTARY_APP_PASSWORD=<app-specific-password> \
        MACOS_NOTARY_TEAM_ID=<team> \
        uv run --no-sync python3 scripts/check-notary-credentials.py

This docstring is **raw** - the opening triple quote carries an `r` - and the two carriers of that
command are why. A single backslash at the end of a line inside a non-raw literal is Python's own
line continuation: it is removed, and the three lines above become one before any shell reads them.
So the source can show either two backslashes, which a reader copying them out of the file runs as
an escaped backslash, or one, which the string literal then eats. Raw is the only spelling where
the backslash the reader copies is the backslash the shell continues with - the shape
`test_shell_continuations.py` reads for.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

#: The three variables `build-release.yml` feeds `notarytool` from, under that file's own
#: secret names. Read from the environment rather than taken as flags: the preflight must
#: read the credentials the release will read, and a flag is a second place to get them
#: wrong (`tests/test_check_notary_credentials.py` pins the names against the workflow).
APPLE_ID_VAR = "APPLE_ID"
PASSWORD_VAR = "MACOS_NOTARY_APP_PASSWORD"
TEAM_ID_VAR = "MACOS_NOTARY_TEAM_ID"

#: Apple's answer when it received the request and refused the authentication. Measured
#: above. 403 is included because Apple has used it for a revoked key; both mean "Apple
#: received this and said no to who is asking", which is the only thing this rejects.
#: Deliberately anchored on the status line: a 5xx, a DNS failure or a CLI error is *not*
#: a credentials verdict and must land in `unmeasurable`.
_REFUSED = re.compile(r"HTTP status code:\s*(?:401|403)\b")

#: A working `history` exchange is a couple of seconds; the measured refusal was under one.
#: Generous but bounded — an unbounded wait turns an unreachable network into a host that
#: waits forever, and "could not measure" is the honest answer here, never a hang.
_TIMEOUT_SECONDS = 60.0


def _redact(text: str, secrets: list[str]) -> str:
    """The password must not reach the terminal by any path — including an error echo."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def probe(xcrun: str | None, env: dict[str, str]) -> tuple[str, str]:
    """Ask Apple for the submission history and name what answered.

    Returns `(state, detail)` with `state` in `works` / `refused` / `unmeasurable`. The
    detail is safe to print: the password is redacted out of it.
    """
    secrets = [env.get(PASSWORD_VAR, "")]
    if xcrun is None:
        return "unmeasurable", (
            "no `xcrun` on PATH - `notarytool` ships with the Xcode command line tools, "
            "so this host cannot run the release's notarization step at all"
        )
    unset = [name for name in (APPLE_ID_VAR, PASSWORD_VAR, TEAM_ID_VAR) if not env.get(name)]
    if unset:
        return "unmeasurable", (
            f"{', '.join(unset)} not set - there is nothing to send, so this preflight has "
            f"not measured the credentials the release will use"
        )

    argv = [
        xcrun,
        "notarytool",
        "history",
        "--apple-id",
        env[APPLE_ID_VAR],
        "--password",
        env[PASSWORD_VAR],
        "--team-id",
        env[TEAM_ID_VAR],
        "--output-format",
        "json",
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "unmeasurable", (
            f"`xcrun notarytool history` did not answer within {_TIMEOUT_SECONDS:.0f}s - "
            f"Apple was not reached, so no verdict about the credentials was reached either"
        )
    except OSError as exc:
        return "unmeasurable", f"`{xcrun}` could not be run: {_redact(str(exc), secrets)}"

    if result.returncode == 0:
        # The requested format came back, which is what "Apple accepted the request" looks
        # like. An exit 0 without it is not a verdict — `xcrun` answering something else,
        # a wrapper swallowing the call — so it is unmeasurable, never a pass.
        try:
            parsed = json.loads(result.stdout)
        except ValueError:
            return "unmeasurable", (
                "the call exited 0 but did not answer with the JSON `--output-format json` "
                f"asks for, so no credentials verdict was reached. Output: "
                f"{_redact(result.stdout.strip()[:200], secrets)!r}"
            )
        count = len(parsed.get("history", [])) if isinstance(parsed, dict) else None
        where = f"{count} past submission(s) on record" if count is not None else "a JSON reply"
        return "works", f"Apple answered the submission-history request ({where})"

    combined = f"{result.stdout}\n{result.stderr}"
    if _REFUSED.search(combined):
        return "refused", (
            f"Apple refused the credentials (exit {result.returncode}). Apple's own reply: "
            f"{_redact(combined.strip()[:400], secrets)}"
        )
    return "unmeasurable", (
        f"the call exited {result.returncode} without an authentication verdict from Apple "
        f"(a 401/403 is what a refused credential looks like), so this is not a reading on "
        f"the credentials. Output: {_redact(combined.strip()[:400], secrets)}"
    )


def check(env: dict[str, str], xcrun: str | None) -> int:
    """Print the reading and return the exit code."""
    print("notary credentials preflight")
    print(f"  apple-id: {env.get(APPLE_ID_VAR) or '(unset)'}")
    print(f"  team-id:  {env.get(TEAM_ID_VAR) or '(unset)'}")
    print(f"  password: {'set' if env.get(PASSWORD_VAR) else '(unset)'}")
    print(f"  xcrun:    {xcrun or '(not on PATH)'}")
    state, detail = probe(xcrun, env)
    if state == "works":
        print(f"OK: {detail}")
        return 0
    if state == "refused":
        print(
            f"REFUSED: {detail}\n"
            "The release's notarize step would fail the same way, three seconds in, after "
            "the tag was pushed - and because it fails, nothing is published. Usual causes: "
            "an expired or revoked app-specific password, an Apple ID or team ID that does "
            "not match the account, or a Developer Program agreement waiting to be "
            "accepted. Fix it and re-run this before tagging."
        )
        return 1
    print(f"not measurable: {detail}")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ask Apple whether the notarization credentials this host releases with "
            "actually work - before a tag makes CI ask instead."
        ),
    )
    parser.add_argument(
        "--xcrun",
        default=None,
        help=(
            "the xcrun to call (default: the one on PATH; tests point this at a stand-in, "
            "which is the only way to answer the refusal without spending a credential)"
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help=(
            "read the three variables from this file (`KEY=VALUE` lines) instead of the "
            "environment, so a host can keep them out of the shell history"
        ),
    )
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.env_file is not None:
        env.update(_read_env_file(args.env_file))
    xcrun = args.xcrun if args.xcrun is not None else shutil.which("xcrun")
    return check(env, xcrun)


def _read_env_file(path: str) -> dict[str, str]:
    """`KEY=VALUE` lines; blank lines and `#` comments ignored. Not a shell script."""
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


if __name__ == "__main__":
    sys.exit(main())
