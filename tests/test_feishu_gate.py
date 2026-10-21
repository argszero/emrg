"""The approval gate's own tests, entirely offline.

`scripts/feishu-gate.py` is the only route by which a task may message a third party on the
host's account, so what is tested here is not convenience but the four properties the rule
consists of: an approval covers **one** message, the recipient and the body are the ones the
host was shown, the sender of the consent must be the host himself, and the ledger never
leaves the workspace.

Every test drives the real script as a subprocess against a **fake `lark-cli`** placed on
disk by the fixture. The fake records its argv and answers fixed JSON, so no test can make a
real request; a test that needs "the host replied" appends the reply to the fake's stream
rather than waiting for one.

Why this module is POSIX-only, and what that leaves untested
------------------------------------------------------------
The gate hands the CLI its text as **one command-line argument**, and its request is several
lines. The Windows leg found both halves of the consequence. A file named `lark-cli` with a
shebang is not a program there at all: eleven tests failed with `[WinError 193] %1 is not a
valid Win32 application`, because neither a shebang nor an execute bit means anything to
`CreateProcess`. Giving the fixture a `.cmd` shim fixed that and left one failure — the CLI
received `[EMRG approval request]` and nothing after it, because the command interpreter
parses the line before the shim runs and a newline inside an argument ends the command.

So a stand-in CLI cannot carry this gate's text on Windows, and the limit is the transport's
rather than the tests': a native `--cli` executable does receive the whole argument, while a
`.cmd` shim — which is what npm installs — does not. `scripts/feishu-gate.py` states the same
limit for a reader of the script. What that leaves untested here is the gate's argv-level
behaviour on Windows; what it does not leave untested is the rule, which is what these tests
are about and which the POSIX leg exercises in full.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "feishu-gate.py"
HOST = "ou_host_self"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="a stand-in CLI cannot receive the gate's multi-line argument on "
           "Windows: a shebang file is not a program there, and a .cmd shim ends "
           "the command at the first newline (see the module docstring)",
)

FAKE_CLI = '''"""A stand-in for the Feishu CLI: records argv, answers fixed JSON, touches no network."""
import json, os, signal, sys
from pathlib import Path

argv = sys.argv[1:]
log = Path(os.environ["FAKE_LARK_LOG"])
stream_path = Path(os.environ["FAKE_LARK_STREAM"])
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\\n")

def stream():
    if stream_path.exists():
        return json.loads(stream_path.read_text(encoding="utf-8"))
    return []

def save(items):
    stream_path.write_text(json.dumps(items), encoding="utf-8")

if argv[:2] == ["auth", "status"]:
    print(json.dumps({"identities": {"user": {"openId": os.environ["FAKE_LARK_HOST"]}}}))
elif argv and argv[0] == "+chat-messages-list":
    print(json.dumps({"items": stream()}))
elif argv and argv[0] == "+chat-messages-create":
    receive = argv[argv.index("--receive-id") + 1]
    text = argv[argv.index("--text") + 1]
    if receive and receive == os.environ.get("FAKE_LARK_DECLINE_ON"):
        print("the CLI declined this message", file=sys.stderr)
        sys.exit(4)
    items = stream()
    message_id = f"om_{len(items) + 1}"
    items.append({
        "message_id": message_id,
        "receive_id": receive,
        "sender_id": os.environ["FAKE_LARK_HOST"],
        "body": {"content": json.dumps({"text": text})},
    })
    save(items)
    if receive == os.environ.get("FAKE_LARK_DIE_ON"):
        # The message is out and the gate never learns it — the death this suite is about.
        os.kill(os.getppid(), signal.SIGKILL)
        os._exit(0)
    if receive == os.environ.get("FAKE_LARK_MUMBLE_ON"):
        # The message is out and the answer carries no message_id, so the gate cannot tell.
        print(json.dumps({"receive_id": receive}))
    else:
        print(json.dumps({"message_id": message_id, "receive_id": receive}))
else:
    print("unexpected argv: %r" % (argv,), file=sys.stderr)
    sys.exit(3)
'''


@pytest.fixture()
def fake_lark(tmp_path):
    """A workspace, a fake CLI, and the two files it reads."""
    cli = tmp_path / "lark-cli"
    cli.write_text(f"#!/usr/bin/env python3\n{FAKE_CLI}", encoding="utf-8")
    cli.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {
        **os.environ,
        "FAKE_LARK_LOG": str(tmp_path / "argv.jsonl"),
        "FAKE_LARK_STREAM": str(tmp_path / "stream.json"),
        "FAKE_LARK_HOST": HOST,
    }
    return {
        "tmp_path": tmp_path,
        "cli": cli,
        "workspace": workspace,
        "env": env,
        "stream": tmp_path / "stream.json",
        "argv_log": tmp_path / "argv.jsonl",
    }


def gate(lark, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), "--cli", str(lark["cli"]), *args],
        cwd=str(cwd or lark["workspace"]),
        env=lark["env"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def argv_lines(lark) -> list[list[str]]:
    path = lark["argv_log"]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def sends(lark) -> list[list[str]]:
    return [a for a in argv_lines(lark) if a and a[0] == "+chat-messages-create"]


def third_party_messages(lark) -> list[dict]:
    """What the stand-in CLI actually delivered to a recipient that is not the host."""
    path = lark["stream"]
    if not path.exists():
        return []
    items = json.loads(path.read_text(encoding="utf-8"))
    return [item for item in items if item.get("receive_id") not in (None, HOST)]


def request_one(lark, to: str = "ou_third_party", text: str = "the verbatim body") -> str:
    done = gate(lark, "request", "--to", to, "--text", text)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["token"]


def host_replies(lark, text: str) -> None:
    """Append a message from the host to the fake's stream — the consent under test."""
    items = json.loads(lark["stream"].read_text(encoding="utf-8"))
    items.append({
        "message_id": f"om_reply_{len(items) + 1}",
        "sender_id": HOST,
        "body": {"content": json.dumps({"text": text})},
    })
    lark["stream"].write_text(json.dumps(items), encoding="utf-8")


def ledger(lark) -> list[dict]:
    path = lark["workspace"] / ".emrg" / "feishu" / "requests.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── (a) request asks the host, once, and leaves one pending row ───────────────


def test_request_asks_the_host_once_and_leaves_one_pending_row(fake_lark):
    token = request_one(fake_lark)

    assert len(sends(fake_lark)) == 1
    argv = sends(fake_lark)[0]
    assert argv[argv.index("--receive-id") + 1] == HOST, (
        "the approval request must go to the host himself — asking anyone else for "
        "permission to message them is not a gate"
    )
    assert token in argv[argv.index("--text") + 1], "the request must carry its token"
    rows = ledger(fake_lark)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["to"] == "ou_third_party"
    assert rows[0]["token"] == token


# ── (b) no reply, no send ────────────────────────────────────────────────────


def test_send_without_a_host_reply_refuses_and_sends_nothing(fake_lark):
    token = request_one(fake_lark)
    asks_before = len(sends(fake_lark))

    done = gate(fake_lark, "send", "--token", token)

    assert done.returncode == 1
    assert "no reply" in done.stderr
    assert len(sends(fake_lark)) == asks_before, "a refusal must send nothing"
    assert ledger(fake_lark)[0]["status"] == "pending"


# ── (c) the host's reply is the approval, and it sends the approved bytes ────


def test_send_after_the_hosts_reply_sends_the_recorded_recipient_and_body(fake_lark):
    token = request_one(fake_lark, to="ou_third_party", text="exactly this, verbatim ⚑")
    host_replies(fake_lark, f"yes {token}")

    done = gate(fake_lark, "send", "--token", token)

    assert done.returncode == 0, done.stderr
    outbound = sends(fake_lark)[-1]
    assert outbound[outbound.index("--receive-id") + 1] == "ou_third_party"
    assert outbound[outbound.index("--text") + 1] == "exactly this, verbatim ⚑", (
        "the body sent must be the body the host was shown, verbatim"
    )
    row = ledger(fake_lark)[0]
    assert row["status"] == "sent"
    assert row["sent_message_id"], "a spent approval must record what it sent"


def test_a_body_swapped_after_the_request_is_refused(fake_lark):
    token = request_one(fake_lark)
    host_replies(fake_lark, f"yes {token}")
    body = fake_lark["workspace"] / ".emrg" / "feishu" / "bodies" / f"{token}.txt"
    body.write_text("something else entirely", encoding="utf-8")
    asks_before = len(sends(fake_lark))

    done = gate(fake_lark, "send", "--token", token)

    assert done.returncode == 1
    assert "body changed" in done.stderr
    assert len(sends(fake_lark)) == asks_before
    assert ledger(fake_lark)[0]["status"] == "pending"


# ── (d) one approval, one message ───────────────────────────────────────────


def test_the_same_token_cannot_be_spent_twice(fake_lark):
    token = request_one(fake_lark)
    host_replies(fake_lark, f"yes {token}")
    assert gate(fake_lark, "send", "--token", token).returncode == 0
    sends_after_first = len(sends(fake_lark))

    done = gate(fake_lark, "send", "--token", token)

    assert done.returncode == 1
    assert "already spent" in done.stderr
    assert len(sends(fake_lark)) == sends_after_first, "a spent approval must not resend"


# ── the spend is recorded before the send it records ────────────────────────


def test_a_gate_that_dies_after_the_send_leaves_the_approval_unspendable(fake_lark):
    """The window a crash opens, and the reason the claim is written before the CLI runs.

    The stand-in sends, then ends the gate before it can record the send. The row must
    already have been unspendable when the CLI was invoked, so the next `send` refuses — and
    because one refusal proves nothing on its own (a gate that refused everything would pass
    the second half), the stream must hold exactly one message for the third party.
    """
    token = request_one(fake_lark)
    host_replies(fake_lark, f"yes {token}")
    fake_lark["env"]["FAKE_LARK_DIE_ON"] = "ou_third_party"

    killed = gate(fake_lark, "send", "--token", token)

    assert killed.returncode != 0, "the stand-in was to end the gate before it wrote"
    assert [row["status"] for row in ledger(fake_lark)] == ["sending"], (
        "the spend must be on disk before the CLI is invoked, so a death in between leaves a "
        "row every verb refuses rather than one the next `send` accepts"
    )
    assert len(third_party_messages(fake_lark)) == 1, "this arm needs a message that did leave"

    fake_lark["env"].pop("FAKE_LARK_DIE_ON")
    again = gate(fake_lark, "send", "--token", token)

    assert again.returncode == 1
    assert "sending" in again.stderr, (
        "the refusal must name the state and the remedy, not leave `sending` unexplained"
    )
    assert len(third_party_messages(fake_lark)) == 1, "one approval covers one message"


def test_a_cli_refusal_does_not_burn_the_approval(fake_lark):
    """The stated trade, one direction: a failure the gate can read sent nothing.

    The CLI exited non-zero and said so, so the message did not leave and the approval goes
    back to spendable — a fault with no consequence must not cost the host a second asking.
    """
    token = request_one(fake_lark)
    host_replies(fake_lark, f"yes {token}")
    fake_lark["env"]["FAKE_LARK_DECLINE_ON"] = "ou_third_party"

    declined = gate(fake_lark, "send", "--token", token)

    assert declined.returncode == 1
    assert "exited 4" in declined.stderr
    assert ledger(fake_lark)[0]["status"] == "pending", (
        "a refusal that provably sent nothing must leave the approval spendable"
    )
    assert third_party_messages(fake_lark) == []

    fake_lark["env"].pop("FAKE_LARK_DECLINE_ON")
    assert gate(fake_lark, "send", "--token", token).returncode == 0
    assert len(third_party_messages(fake_lark)) == 1


def test_an_answer_the_gate_cannot_read_keeps_the_approval_spent(fake_lark):
    """The same trade, the other direction: an unreadable answer proves nothing, so fail closed.

    The stand-in delivered the message and answered without a message_id. Retrying would risk
    a second delivery, so the row stays `sending` and the honest cost is one new request.
    """
    token = request_one(fake_lark)
    host_replies(fake_lark, f"yes {token}")
    fake_lark["env"]["FAKE_LARK_MUMBLE_ON"] = "ou_third_party"

    done = gate(fake_lark, "send", "--token", token)

    assert done.returncode == 1
    assert "no message_id" in done.stderr
    assert len(third_party_messages(fake_lark)) == 1, "this arm needs a message that did leave"
    assert ledger(fake_lark)[0]["status"] == "sending"

    fake_lark["env"].pop("FAKE_LARK_MUMBLE_ON")
    assert gate(fake_lark, "send", "--token", token).returncode == 1
    assert len(third_party_messages(fake_lark)) == 1, (
        "an answer nobody could read must not license a second delivery"
    )


# ── (e) the bad arguments cannot be expressed ───────────────────────────────


def test_send_accepts_neither_a_recipient_nor_a_body(fake_lark):
    token = request_one(fake_lark)
    for extra in (["--to", "ou_someone_else"], ["--text", "different words"]):
        done = gate(fake_lark, "send", "--token", token, *extra)
        assert done.returncode == 2, (
            f"send must refuse {extra[0]} at parse time: redirecting an approval or "
            f"swapping its body has to be inexpressible, not merely disallowed"
        )


# ── (f) an old approval is not a standing one ───────────────────────────────


def test_an_expired_request_is_refused(fake_lark):
    token = request_one(fake_lark)
    host_replies(fake_lark, f"yes {token}")
    sends_before = len(sends(fake_lark))

    done = gate(fake_lark, "--max-age", "0", "send", "--token", token)

    assert done.returncode == 1
    assert "expired" in done.stderr
    assert len(sends(fake_lark)) == sends_before


# ── (g) a different token is not consent ────────────────────────────────────


def test_another_token_in_the_hosts_message_is_not_consent(fake_lark):
    first = request_one(fake_lark)
    second = request_one(fake_lark, text="a second body")
    host_replies(fake_lark, f"yes {second}")
    sends_before = len(sends(fake_lark))

    done = gate(fake_lark, "send", "--token", first)

    assert done.returncode == 1
    assert "no reply" in done.stderr
    assert len(sends(fake_lark)) == sends_before, (
        "an approval for one message must never spend on another"
    )


# ── (h) only the host's own reply is consent ────────────────────────────────


def test_a_reply_from_anyone_but_the_host_is_not_consent(fake_lark):
    token = request_one(fake_lark)
    items = json.loads(fake_lark["stream"].read_text(encoding="utf-8"))
    items.append({
        "message_id": "om_impostor",
        "sender_id": "ou_third_party",
        "body": {"content": json.dumps({"text": f"yes {token}"})},
    })
    fake_lark["stream"].write_text(json.dumps(items), encoding="utf-8")
    sends_before = len(sends(fake_lark))

    done = gate(fake_lark, "send", "--token", token)

    assert done.returncode == 1
    assert "no reply" in done.stderr
    assert len(sends(fake_lark)) == sends_before, (
        "the token is public — it rode in the request — so consent must come from the "
        "host's identity, not from anyone who can quote it"
    )


# ── (i) messaging the host needs no approval ────────────────────────────────


def test_self_needs_no_approval_and_leaves_no_pending_row(fake_lark):
    done = gate(fake_lark, "self", "--text", "a note to the host")

    assert done.returncode == 0, done.stderr
    outbound = sends(fake_lark)[-1]
    assert outbound[outbound.index("--receive-id") + 1] == HOST
    rows = ledger(fake_lark)
    assert rows and all(row["status"] == "sent" for row in rows)
    assert not [r for r in rows if r["status"] == "pending"], (
        "self-sends are the allowed case, so they must not queue an approval nobody needs"
    )


# ── (j) nothing is written outside the workspace ────────────────────────────


def test_the_ledger_stays_inside_the_workspace(fake_lark):
    outside = fake_lark["tmp_path"] / "outside"
    outside.mkdir()
    (outside / "marker.txt").write_text("untouched", encoding="utf-8")
    token = request_one(fake_lark)
    host_replies(fake_lark, f"yes {token}")
    assert gate(fake_lark, "send", "--token", token).returncode == 0
    assert gate(fake_lark, "self", "--text", "hello").returncode == 0

    root = fake_lark["workspace"] / ".emrg" / "feishu"
    assert (root / "requests.jsonl").is_file()
    assert list((root / "bodies").glob("*.txt")), "the approved bodies must be kept there"
    assert [p.name for p in outside.iterdir()] == ["marker.txt"]
    assert not (fake_lark["tmp_path"] / ".emrg").exists(), (
        "the gate must write under its workspace, never beside it"
    )


# ── the gate is unusable without a CLI, and says so ─────────────────────────


def test_a_missing_cli_is_reported_rather_than_guessed(fake_lark, monkeypatch):
    """`lark-cli` is not on a minimal PATH, so the default must fail loudly, not silently."""
    env = {**fake_lark["env"], "PATH": str(fake_lark["tmp_path"] / "empty-bin")}
    Path(env["PATH"]).mkdir(exist_ok=True)

    done = subprocess.run(
        [sys.executable, str(GATE), "self", "--text", "hi"],
        cwd=str(fake_lark["workspace"]), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, check=False,
    )

    assert done.returncode == 2
    assert "--cli" in done.stderr, "the remedy must name the flag that supplies the path"
    assert not ledger(fake_lark)
