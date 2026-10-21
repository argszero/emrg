#!/usr/bin/env python3
"""The only route by which a task may message a third party on the host's account.

The class this exists for
-------------------------
A prompt can say "ask the host before messaging anyone else", and the model can talk itself
out of it: reword the question, decide the case was covered, reuse an answer given to a
different message. A rule whose enforcement is the model's own reading of a sentence is a
rule with a bypass. So the rule lives here instead — a mechanically executable permission —
and the prompt keeps only the *instructions for using it*.

The rule
--------
Sending to the host's own account is always allowed. Sending to **anyone else** requires an
approval the host gives for **that one message**: one approval, one recipient, one body,
spent on use.

Four verbs, the last one read-only:

    request --to <ou_xxx> --text-file <f>    ask; lock the recipient and the body
    send    --token <t>                      spend one approval; no --to, no body
    self    --text-file <f>                  message the host himself; no approval
    list    [--status pending]               read the ledger

Why `send` takes no recipient and no body
-----------------------------------------
Because "approved A, sent B" then cannot be expressed at the argument level. The recipient
and the body are recorded when the request is made, and `send` re-reads them from the ledger
and compares the body's sha256 before anything leaves. The approval the host types is a
reply to a message that already carried the exact text, so what he approved and what is sent
are the same bytes by construction rather than by good intentions.

How consent is decided
----------------------
`+chat-messages-list` reports `create_time` at **minute** resolution, so two messages inside
one minute cannot be ordered by it, and "the reply came after the request" cannot be read off
timestamps. Consent is therefore positional: the request message's id is recorded when the
request is sent, and a reply counts only when it **appears later in the stream than that
message** — the same list, read in order.

Spent before it is sent
-----------------------
"Spent on use" is enforced by writing the spend **before** the CLI is invoked, not after it
answers. The record has to be older than the thing it records: a gate that marks the row
`sent` only once the CLI has accepted the message leaves the row `pending` through any death
in between — a crash, a kill, a machine that sleeps past the CLI's own patience — and the
next `send` then passes every check and delivers the same message a second time.

So `send` moves the row to `sending` and rewrites the ledger first, and calls the CLI after
that. Every verb refuses a row that is not `pending`, so the claim is permanent unless the
process that wrote it lives to release it. Whether it releases is a choice, and the choice is
made by whether the failure can be read:

* **A refusal it can read** — the CLI could not be run, or it exited non-zero and said why —
  proves nothing left, so the row goes back to `pending` and the approval stays usable. A
  fault with no consequence must not cost the host a second round of asking.
* **An answer it cannot read** — a timeout, output that is not JSON, an answer carrying no
  message_id — proves nothing either way, so the row stays `sending` and that approval is
  gone. Failing closed costs one new request; failing open costs a second message to a third
  party, which is the one thing this gate exists to prevent.

A `sending` row is therefore the visible trace of an interrupted send: `list` prints it like
any other row, and `send` refuses it with the remedy rather than as an unexplained state.

What this cannot do, stated rather than left unsaid
---------------------------------------------------
It stops misuse, not deliberate forgery. The gate runs as the host's own user, and that
identity can itself post as the host. It makes reusing an old approval, redirecting a send,
swapping the body, or bypassing the gate impossible at the argument and ledger level; forging
a "host reply" under the same identity cannot be prevented in principle. The prompt forbids
reaching the CLI directly for a third party, and that prohibition is deliberate rather than
mechanical — the same identity means the tool cannot check it.

Everything the gate writes lives inside the workspace it runs in, at
`<workspace>/.emrg/feishu/` (`requests.jsonl` plus `bodies/<token>.txt`). Nothing is written
outside it.

One transport limit, measured rather than assumed
-------------------------------------------------
The text travels to the CLI as a **single command-line argument**, and the request it builds
is several lines. That is fine for a native program on either platform and for any program on
POSIX. It is not fine on Windows for a `.cmd`/`.bat` shim — which is what npm installs — because
the command interpreter parses the line before the shim runs and a newline inside an argument
ends it: measured on the Windows CI leg, the CLI received `[EMRG approval request]` and nothing
after it. So a Windows host must give `--cli` a native executable, and where it can only offer
a shim the text would arrive truncated. Handing the CLI a file instead would need an interface
this gate has no way to verify, so the limit is stated here rather than guessed around.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: The CLI's argv shapes, in one place because the gate's safety depends on them: the
#: `--receive-id` of `send` is the *recorded* recipient, never a command-line value.
DEFAULT_CLI = "lark-cli"
LIST_ARGS = ("+chat-messages-list",)
SEND_ARGS = ("+chat-messages-create",)
AUTH_ARGS = ("auth", "status")


class Refusal(Exception):
    """The gate declined, and nothing was sent."""


class OutcomeUnknown(Refusal):
    """The CLI ran, and the gate could not read what it answered.

    A `send` that ends here proves nothing either way: the message may have left before the
    CLI went quiet. It is its own class because the caller's next move differs from the one a
    plain `Refusal` allows — see "Spent before it is sent" in this module's docstring.
    """


# ── the workspace's own state ────────────────────────────────────────────


def state_dir(workspace: Path) -> Path:
    return workspace / ".emrg" / "feishu"


def ledger_path(workspace: Path) -> Path:
    return state_dir(workspace) / "requests.jsonl"


def body_path(workspace: Path, token: str) -> Path:
    return state_dir(workspace) / "bodies" / f"{token}.txt"


def read_ledger(workspace: Path) -> list[dict]:
    """Every row, oldest first. A missing ledger is an empty one — a fresh workspace."""
    path = ledger_path(workspace)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def append_ledger(workspace: Path, row: dict) -> None:
    path = ledger_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def rewrite_ledger(workspace: Path, rows: list[dict]) -> None:
    path = ledger_path(workspace)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    tmp.replace(path)


def write_body(workspace: Path, token: str, text: str) -> str:
    """Store the exact bytes an approval will cover, and answer their sha256."""
    path = body_path(workspace, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_row(rows: list[dict], token: str) -> dict:
    for row in rows:
        if row.get("token") == token:
            return row
    raise Refusal(f"no request carries the token {token!r}")


# ── the CLI ──────────────────────────────────────────────────────────────


def run_cli(cli: str, args: tuple[str, ...] | list[str]) -> dict:
    """Run one CLI call in the foreground and return its JSON answer.

    Foreground, one at a time: a backgrounded send would be an approval spent with nobody
    watching its result, which is the opposite of what this gate is for.
    """
    argv = [cli, *args]
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, check=False,
        )
    except OSError as exc:
        # Nothing ran, so nothing left: this is a refusal the caller may undo.
        raise Refusal(f"the CLI could not be run ({cli}): {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # The timeout is the gate's own patience, not the CLI's verdict: a `send` that
        # outlived it may well have gone out.
        raise OutcomeUnknown(f"the CLI did not answer within its time: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise Refusal(
            f"the CLI exited {done.returncode}: {detail[-1] if detail else 'no output'}"
        )
    try:
        return json.loads(done.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise OutcomeUnknown(f"the CLI answered something that is not JSON: {exc}") from exc


def host_open_id(cli: str) -> str:
    """Who the host himself is, read at run time so no account constant is stored."""
    data = run_cli(cli, [*AUTH_ARGS, "--json"])
    try:
        open_id = data["identities"]["user"]["openId"]
    except (KeyError, TypeError) as exc:
        raise Refusal(f"auth status carried no identities.user.openId: {data!r}") from exc
    if not isinstance(open_id, str) or not open_id.strip():
        raise Refusal("auth status carries an empty host open id")
    return open_id


def list_messages(cli: str, chat_id: str) -> list[dict]:
    data = run_cli(cli, [*LIST_ARGS, "--chat-id", chat_id, "--json"])
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise Refusal(f"the message list carried no items: {data!r}")
    return items


def send_message(cli: str, to: str, text: str) -> str:
    data = run_cli(
        cli, [*SEND_ARGS, "--receive-id", to, "--text", text, "--json"]
    )
    message_id = data.get("message_id") if isinstance(data, dict) else None
    if not isinstance(message_id, str) or not message_id:
        raise OutcomeUnknown(f"the send answered no message_id: {data!r}")
    return message_id


def message_id_of(item: dict) -> str | None:
    for key in ("message_id", "messageId", "id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def message_text_of(item: dict) -> str:
    body = item.get("body")
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                return content
            if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
                return parsed["text"]
            return content
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            return content["text"]
    for key in ("text", "content"):
        if isinstance(item.get(key), str):
            return item[key]
    return ""


def message_sender_of(item: dict) -> str | None:
    for key in ("sender_id", "senderId", "sender"):
        value = item.get(key)
        if isinstance(value, dict):
            for inner in ("open_id", "openId", "id"):
                if isinstance(value.get(inner), str):
                    return value[inner]
        elif isinstance(value, str):
            return value
    return None


# ── the verbs ────────────────────────────────────────────────────────────


def read_text_arg(args: argparse.Namespace) -> str:
    if args.text_file and args.text is not None:
        raise Refusal("give either --text or --text-file, not both")
    if args.text_file:
        try:
            return Path(args.text_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise Refusal(f"the body file could not be read: {exc}") from exc
    if args.text is None:
        raise Refusal("a body is required (--text or --text-file)")
    return args.text


def cmd_request(args: argparse.Namespace) -> None:
    text = read_text_arg(args)
    if not text.strip():
        raise Refusal("an empty message is not a message")
    cli = args.cli
    host = host_open_id(cli)
    if args.to == host:
        raise Refusal(
            "that recipient is the host himself; `self` needs no approval and is the "
            "command for it"
        )
    workspace = Path(args.workspace)
    token = secrets.token_hex(4)
    digest = write_body(workspace, token, text)
    request_text = (
        f"[EMRG approval request]\n"
        f"token: {token}\n"
        f"to: {args.to}\n"
        f"--- exact text ---\n{text}\n--- end ---\n"
        f"Reply to this message with the token to approve sending it."
    )
    request_message_id = send_message(cli, host, request_text)
    append_ledger(workspace, {
        "token": token,
        "to": args.to,
        "sha256": digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "request_message_id": request_message_id,
        "status": "pending",
    })
    print(json.dumps({"token": token, "to": args.to,
                      "request_message_id": request_message_id}, ensure_ascii=False))


def consent_found(items: list[dict], request_message_id: str, host: str, token: str) -> bool:
    """Positional consent: a host message that appears after the request, carrying the token.

    Position, not `create_time`, because the CLI reports whole minutes: two messages inside
    one minute are indistinguishable by timestamp, and "after" has to mean after.
    """
    position_of_request = None
    for index, item in enumerate(items):
        if message_id_of(item) == request_message_id:
            position_of_request = index
    if position_of_request is None:
        return False
    for item in items[position_of_request + 1:]:
        if message_sender_of(item) != host:
            continue
        if token in message_text_of(item):
            return True
    return False


def cmd_send(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace)
    rows = read_ledger(workspace)
    row = find_row(rows, args.token)

    if row.get("status") == "sent":
        raise Refusal(
            f"the token {args.token!r} was already spent on {row.get('sent_message_id')!r}; "
            f"one approval covers one message"
        )
    if row.get("status") != "pending":
        detail = f"the token {args.token!r} is {row.get('status')!r}, not pending"
        if row.get("status") == "sending":
            detail += (
                "; a send that was interrupted before its answer left the approval spent — "
                "whether the message went out is unknown, so ask the host again rather than "
                "retrying this token"
            )
        raise Refusal(detail)

    created = datetime.fromisoformat(row["created_at"])
    if datetime.now(timezone.utc) - created > timedelta(seconds=args.max_age):
        raise Refusal(
            f"the request expired after {args.max_age}s; send a new one rather than reviving "
            f"an old approval"
        )

    path = body_path(workspace, args.token)
    if not path.exists():
        raise Refusal(f"the body for {args.token!r} is gone from {path}")
    current = hashlib.sha256(path.read_bytes()).hexdigest()
    if current != row.get("sha256"):
        raise Refusal(
            "the body changed after the host was asked; what he approved is not what would "
            "be sent"
        )

    cli = args.cli
    host = host_open_id(cli)
    if not consent_found(
        list_messages(cli, host), row["request_message_id"], host, args.token
    ):
        raise Refusal(
            f"the host's stream carries no reply after the request containing the token "
            f"{args.token!r}; nothing was sent"
        )

    # The spend is written before the CLI is invoked, so that the record is older than the
    # send it records: a process that dies in between leaves a row every verb refuses, not
    # one the next `send` accepts. See "Spent before it is sent" in the module docstring for
    # which failures release the claim and why.
    row["status"] = "sending"
    row["claimed_at"] = datetime.now(timezone.utc).isoformat()
    rewrite_ledger(workspace, rows)

    try:
        message_id = send_message(cli, row["to"], path.read_text(encoding="utf-8"))
    except OutcomeUnknown:
        # Nothing here proves the message stayed: the claim stands, and this approval is gone.
        raise
    except Refusal:
        # The CLI refused and said so — nothing was sent, so the approval stays spendable.
        row["status"] = "pending"
        row.pop("claimed_at", None)
        rewrite_ledger(workspace, rows)
        raise

    row["status"] = "sent"
    row.pop("claimed_at", None)
    row["sent_at"] = datetime.now(timezone.utc).isoformat()
    row["sent_message_id"] = message_id
    rewrite_ledger(workspace, rows)
    print(json.dumps({"sent": True, "to": row["to"], "message_id": message_id},
                     ensure_ascii=False))


def cmd_self(args: argparse.Namespace) -> None:
    text = read_text_arg(args)
    if not text.strip():
        raise Refusal("an empty message is not a message")
    cli = args.cli
    host = host_open_id(cli)
    message_id = send_message(cli, host, text)
    append_ledger(Path(args.workspace), {
        "kind": "self",
        "to": host,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "sent_message_id": message_id,
    })
    print(json.dumps({"sent": True, "to": host, "message_id": message_id},
                     ensure_ascii=False))


def cmd_list(args: argparse.Namespace) -> None:
    rows = read_ledger(Path(args.workspace))
    if args.status:
        rows = [r for r in rows if r.get("status") == args.status]
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    if not rows:
        print("(no rows)", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    # `allow_abbrev=False` is load-bearing, not tidiness: with abbreviations on, the `--to`
    # of a redirect attempt is read as a prefix of `--token` and silently **overwrites the
    # token**, so `send --token A --to B` asked about `B` instead of refusing. A gate whose
    # arguments can be reinterpreted is a gate with a second spelling for every misuse, so
    # every parser here — the parent and each verb — refuses abbreviations.
    parser = argparse.ArgumentParser(
        prog="feishu-gate.py",
        allow_abbrev=False,
        description=(
            "The only route by which a task may message a third party on the host's "
            "account. `request` asks, `send` spends one approval, `self` messages the host "
            "himself, `list` reads the ledger."
        ),
    )
    parser.add_argument("--workspace", default=".", help="where .emrg/feishu/ lives")
    parser.add_argument("--cli", default=DEFAULT_CLI,
                        help=f"the Feishu CLI (default: {DEFAULT_CLI} on PATH)")
    parser.add_argument("--max-age", type=int, default=24 * 60 * 60,
                        help="seconds a pending request stays usable (default: 86400)")
    sub = parser.add_subparsers(dest="verb", required=True)

    p_request = sub.add_parser("request", help="ask the host to approve one message", allow_abbrev=False)
    p_request.add_argument("--to", required=True, help="the recipient's open_id")
    p_request.add_argument("--text")
    p_request.add_argument("--text-file")
    p_request.set_defaults(handler=cmd_request)

    # `send` deliberately has neither `--to` nor a body argument: the recipient and the
    # bytes were locked when the host was asked, so "approved A, sent B" cannot be
    # expressed here at all.
    p_send = sub.add_parser("send", help="send the message one approval covers", allow_abbrev=False)
    p_send.add_argument("--token", required=True)
    p_send.set_defaults(handler=cmd_send)

    p_self = sub.add_parser("self", help="message the host himself; no approval needed", allow_abbrev=False)
    p_self.add_argument("--text")
    p_self.add_argument("--text-file")
    p_self.set_defaults(handler=cmd_self)

    p_list = sub.add_parser("list", help="read the ledger", allow_abbrev=False)
    p_list.add_argument("--status")
    p_list.set_defaults(handler=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cli == DEFAULT_CLI and shutil.which(DEFAULT_CLI) is None:
        print(
            f"feishu-gate: {DEFAULT_CLI} is not on PATH; pass --cli with its absolute path "
            f"(the template's extra_prompt section names it)",
            file=sys.stderr,
        )
        return 2
    try:
        args.handler(args)
    except Refusal as exc:
        print(f"feishu-gate: refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
