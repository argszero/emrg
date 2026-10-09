"""Session-level extra writable roots — the host's middle tier.

Rant 2026-10-09T09:43:39. Under ``workspace-write`` a session could reach a file
outside its workspace only by lifting *every* boundary
(``danger-full-access``), and ``escalation.hops_from`` lists exactly that one
hop: the model's only route out was the one that drops all confinement. These
roots are the host's narrower instrument — named per session, shown, removable —
and they join the **one** derivation point (``writable_roots``) so the three
dialects and the in-process fence cannot answer differently.

The file is laid out as the requirements are: what a path is judged to be (§1),
the single derivation (§2), the fence (§3), the dialects (§4), who may name a
root (§5), the daemon's command (§6), the model's view of the boundary (§7), and
the separation from the deployer root host decision D5 deleted (§8).

Two traps this file is written around, both measured elsewhere in the suite:

* the workspace and the extra root are **siblings under pytest's tmp base**,
  which is itself inside the temp area, so the ``outside`` fixture withholds the
  two ambient temp sources for the tests whose subject is a root that must be
  *accepted*. Without that, a path called "outside" is the one the derivation
  grants, ``judge_extra_root`` answers "already covered", and the test would pass
  while measuring nothing.
* ``protected_paths()`` is derived from the *real* home, so those cases use the
  real path as an **input to a pure predicate** — ``judge_extra_root`` decides
  before it stats anything, and no write is ever attempted.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.sandbox import roots as roots_mod
from emrg.sandbox.fence import file_refusal
from emrg.sandbox.policy import SandboxPolicy, resolve_policy
from emrg.sandbox.providers.darwin import seatbelt_profile_args
from emrg.sandbox.providers.linux import bwrap_profile_args
from emrg.sandbox.roots import canonical_path, judge_extra_root, writable_roots
from emrg.server.daemon import EmrgServer
from emrg.session import Session


class _RecordingWs:
    """A connection that records every frame the daemon sends it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))


def _server() -> EmrgServer:
    return EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))


def _meta(session: Session) -> dict:
    return json.loads(session._meta_path.read_text(encoding="utf-8"))


def _drive(server: EmrgServer, msg: dict) -> list[dict]:
    """One `_process_message` call, returning the frames the sender received."""
    ws = _RecordingWs()
    server._all_connections.add(ws)
    asyncio.run(server._process_message(msg, ws))
    return ws.sent


@pytest.fixture
def no_temp_grants(monkeypatch):
    """Withhold the two ambient temp sources, so "outside" means outside.

    ``writable_roots`` at ``workspace-write`` grants the workspace *and* the
    shared ``/tmp`` *and* ``tempfile.gettempdir()``, and pytest's own base is
    under the temp area on Linux (``/tmp/...``) and on macOS
    (``/var/folders/...``). A test whose subject is a root that must be
    *accepted* therefore has to say which sources it is testing against —
    otherwise the path it calls "outside" is the one the derivation grants, and
    ``judge_extra_root`` answers "already covered" while the test believes it
    measured acceptance.
    """
    monkeypatch.setattr(roots_mod, "_host_temp_spellings", lambda: [])
    monkeypatch.setattr(roots_mod, "_platform_temp_sources", lambda: [])


@pytest.fixture
def outside(no_temp_grants, tmp_path):
    """A workspace and one directory that is genuinely outside it."""
    ws = tmp_path / "ws"
    ws.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    return ws, elsewhere


def _grants(profile_args: list[str]) -> list[str]:
    """The path grants a Seatbelt profile makes, read back with its quoting undone."""
    profile = " ".join(profile_args)
    raw = re.findall(r'\((?:subpath|literal) ("(?:[^"\\]|\\.)*")\)', profile)
    return [r[1:-1].replace('\\"', '"').replace("\\\\", "\\") for r in raw]


def _ws_policy(workspace, extra=()) -> SandboxPolicy:
    return resolve_policy(mode="workspace-write", workspace_root=str(workspace), extra_roots=extra)


# ── §1 what a path is judged to be ────────────────────────────────────────


def test_a_directory_outside_the_workspace_is_accepted(outside):
    """The middle tier's happy path: one named directory, canonical, stored."""
    ws, elsewhere = outside

    verdict = judge_extra_root(str(elsewhere), _ws_policy(ws))

    assert verdict.refusal is None and verdict.notice is None
    assert verdict.canonical == canonical_path(str(elsewhere))


def test_a_path_inside_the_workspace_is_reported_not_refused(outside):
    """"Already mine" is not "no": the host is told why nothing changed."""
    ws, _ = outside
    (ws / "inner").mkdir()

    verdict = judge_extra_root(str(ws / "inner"), _ws_policy(ws))

    assert verdict.refusal is None
    assert verdict.notice and "already grants" in verdict.notice


def test_a_root_added_a_moment_ago_is_reported_rather_than_added_twice(outside):
    """Requirement 5: naming a root already on the list is not a silent duplicate.

    The policy carries the stored roots, which is why the daemon builds it that
    way — a re-add has to be recognised against what the session already has.
    """
    ws, elsewhere = outside
    policy = _ws_policy(ws, extra=(canonical_path(str(elsewhere)),))

    verdict = judge_extra_root(str(elsewhere), policy)

    assert verdict.refusal is None
    assert verdict.notice and "already grants" in verdict.notice


def test_the_filesystem_root_is_refused(outside):
    ws, _ = outside

    verdict = judge_extra_root("/", _ws_policy(ws))

    assert verdict.refusal and "filesystem root" in verdict.refusal


def test_the_home_directory_is_refused(outside):
    """A pure predicate: the home path is an input, and it is refused before any stat."""
    ws, _ = outside

    verdict = judge_extra_root(os.path.expanduser("~"), _ws_policy(ws))

    assert verdict.refusal and "home directory" in verdict.refusal


def test_a_relative_path_is_refused(outside):
    """A relative root would resolve against the provider's cwd, not the host's."""
    ws, _ = outside

    verdict = judge_extra_root("notes", _ws_policy(ws))

    assert verdict.refusal and "absolute" in verdict.refusal


def test_a_path_that_does_not_exist_is_refused(outside):
    """A grant is spelled for a file and for a directory differently (§4)."""
    ws, _ = outside

    verdict = judge_extra_root(str(ws.parent / "not-there-yet"), _ws_policy(ws))

    assert verdict.refusal and "does not exist" in verdict.refusal


def test_a_root_that_would_cover_a_protected_daemon_file_is_refused(outside):
    """The one refusal that is not about size: geometry would stop protecting them.

    ``~/.emrg/config.toml`` and its four neighbours are unreachable today because
    none of them sits inside a session workspace or a temp area. A host-named
    root makes them reachable as an ordinary write, so the rule has to be
    explicit — and the refusal has to name the file it is protecting.
    """
    ws, _ = outside

    verdict = judge_extra_root("~/.emrg", _ws_policy(ws))

    assert verdict.refusal, verdict
    assert "protected daemon state file" in verdict.refusal
    assert "config.toml" in verdict.refusal


def test_the_protected_file_itself_is_refused_too(outside):
    """The same rule one level down: a root *is* the file it names."""
    ws, _ = outside

    verdict = judge_extra_root("~/.emrg/projects.yml", _ws_policy(ws))

    assert verdict.refusal and "projects.yml" in verdict.refusal


# ── §2 one derivation point ───────────────────────────────────────────────


def test_the_extra_root_reaches_every_reader(outside):
    """Requirement 3, as one assertion per reader of the same policy.

    This is the test the first mutation arm has to redden: move the extra roots
    out of ``writable_roots`` and into the Seatbelt profile alone, and the shell
    tools can write where ``write``/``edit`` cannot — the asymmetry
    ``emrg/sandbox/roots.py`` exists to make impossible. Each reader is asserted
    here rather than in its own test because the *sameness* is the property.
    """
    ws, elsewhere = outside
    root = canonical_path(str(elsewhere))
    policy = _ws_policy(ws, extra=(root,))

    assert root in writable_roots(policy)
    assert root in _grants(seatbelt_profile_args(policy))
    bound = bwrap_profile_args(policy)
    at = bound.index(root)
    assert bound[at - 1:at + 2] == ["--bind", root, root], bound
    assert file_refusal(str(elsewhere / "note.txt"), policy) is None


def test_a_root_named_twice_is_granted_once(outside):
    """Deduplication happens in the derivation, so no reader sees it twice."""
    ws, elsewhere = outside
    root = canonical_path(str(elsewhere))

    assert writable_roots(_ws_policy(ws, extra=(root, root))).count(root) == 1


def test_a_relative_root_is_refused_by_the_policy_layer_too(outside):
    """The last line of defence for a caller that skips the daemon's verdict.

    The verdict answers first, with a sentence; this is what stops a relative
    spelling reaching a provider that would resolve it against its own cwd.
    """
    ws, _ = outside

    with pytest.raises(ValueError) as excinfo:
        resolve_policy(mode="workspace-write", workspace_root=str(ws), extra_roots=["notes"])

    assert "absolute" in str(excinfo.value)


# ── §3 the in-process fence ───────────────────────────────────────────────


def test_the_fence_grants_an_extra_root_and_withdraws_it_when_it_is_gone(outside):
    """Acceptance 1 and 4: writable once named, unwritable once removed."""
    ws, elsewhere = outside
    target = str(elsewhere / "note.txt")

    assert file_refusal(target, _ws_policy(ws, extra=(canonical_path(str(elsewhere)),))) is None
    # Removal is the same call with the root no longer on the policy — the
    # daemon's list *is* the policy's list, so nothing else has to be undone.
    refusal = file_refusal(target, _ws_policy(ws))
    assert refusal and "outside every root this tier grants" in refusal


def test_read_only_grants_no_root_even_when_the_session_names_one(outside):
    """Acceptance 2, and the second mutation arm's subject.

    The early return in ``writable_roots`` is what makes a stored root harmless
    at a tier that grants nothing: the list is a property of the *mode*, and a
    session's list never widens a mode that grants none. Delete that line and
    this test is the one that says so.
    """
    ws, elsewhere = outside
    policy = resolve_policy(
        mode="read-only", workspace_root=str(ws), extra_roots=(canonical_path(str(elsewhere)),)
    )

    assert writable_roots(policy) == []
    refusal = file_refusal(str(elsewhere / "note.txt"), policy)
    assert refusal and "read-only" in refusal


def test_danger_full_access_is_unchanged_by_a_named_root(outside):
    """Acceptance 2's other half: the tier that checks nothing still checks nothing."""
    ws, elsewhere = outside
    policy = resolve_policy(
        mode="danger-full-access",
        workspace_root=str(ws),
        extra_roots=(canonical_path(str(elsewhere)),),
    )

    assert writable_roots(policy) == []
    assert file_refusal(str(ws.parent / "anywhere.txt"), policy) is None


def test_the_fence_refuses_a_protected_file_even_when_a_root_covers_it(outside):
    """The deliberate asymmetry, held open on purpose.

    A root over ``~/.emrg`` is refused by ``judge_extra_root`` before it is ever
    stored, so this state is only reachable by constructing the policy directly —
    which is exactly why the fence's own check has to exist rather than relying
    on the writer. The kernel profiles would grant the file; the fence refuses.
    That split is the "user-visible strange" requirement 6 names, and the fence
    is deliberately the stricter side.
    """
    ws, _ = outside
    policy = _ws_policy(ws, extra=(canonical_path(os.path.expanduser("~/.emrg")),))

    refusal = file_refusal(os.path.expanduser("~/.emrg/config.toml"), policy)

    assert refusal and "protected daemon file" in refusal


# ── §4 the dialects ───────────────────────────────────────────────────────


def test_a_file_root_is_granted_literally_and_a_directory_as_a_tree(outside):
    """Requirement 7: ``literal`` for a file, ``subpath`` for a directory.

    Both forms match the named path, so getting this wrong is invisible for the
    path itself — it shows up as a grant over everything *under* a file's parent
    if that name ever becomes a directory. The assertion is on the profile's
    clause, because that is the grammar the kernel parses.
    """
    ws, elsewhere = outside
    a_dir = ws.parent / "a-dir"
    a_dir.mkdir()
    a_file = ws.parent / "a-file.md"
    a_file.write_text("x", encoding="utf-8")
    policy = _ws_policy(ws, extra=(canonical_path(str(a_dir)), canonical_path(str(a_file))))

    profile = " ".join(seatbelt_profile_args(policy))

    assert f'(subpath "{canonical_path(str(a_dir))}")' in profile
    assert f'(literal "{canonical_path(str(a_file))}")' in profile
    assert f'(subpath "{canonical_path(str(a_file))}")' not in profile


def test_bwrap_binds_every_extra_root(outside):
    """Requirement 7's Linux half: a mount over the read-only view, as the workspace is."""
    ws, elsewhere = outside
    root = canonical_path(str(elsewhere))
    bound = bwrap_profile_args(_ws_policy(ws, extra=(root,)))
    at = bound.index(root)

    assert bound[at - 1:at + 2] == ["--bind", root, root], bound


def test_bwrap_binds_nothing_extra_under_read_only(outside):
    """The Linux dialect reads the same field, so the mode still decides."""
    ws, elsewhere = outside
    policy = resolve_policy(
        mode="read-only", workspace_root=str(ws), extra_roots=(canonical_path(str(elsewhere)),)
    )

    assert canonical_path(str(elsewhere)) not in bwrap_profile_args(policy)


# ── §5 only the host names a root ─────────────────────────────────────────


def _inject(server, name: str, args: dict, session, req) -> dict:
    server._inject_tool_arguments(name, args, session, req)
    return args


def test_the_injected_roots_replace_a_value_the_model_put_in_the_call(outside):
    """Requirement 4: the injection is unconditional, so a model value loses.

    The tier's injection is guarded by ``req.sandbox`` because silence there
    resolves to the unconfined default and cannot widen anything. A list of roots
    has no neutral value — an honoured model-supplied list would be a sandbox
    whose boundary the sandboxed decided — so this one is not guarded.
    """
    ws, elsewhere = outside
    session = Session.create_with_id("s_inject", ws)
    session.set_sandbox_roots([str(elsewhere)])
    req = TaskRequest(session_id=session.session_id, cwd=str(ws), prompt="hi")

    args = _inject(_server(), "bash", {"command": "true", "writable_roots": ["/"]}, session, req)

    assert args["writable_roots"] == [canonical_path(str(elsewhere))]


def test_the_roots_are_injected_into_exactly_the_tools_that_can_use_them():
    """The set is the tier's set plus ``write``/``edit`` — one name, not two unions."""
    from emrg.server.daemon import _ROOT_CONSUMING_TOOLS
    from emrg.tools.shell_dialects import SHELL_TOOL_NAMES

    assert _ROOT_CONSUMING_TOOLS == SHELL_TOOL_NAMES | {"write", "edit"}


def test_a_session_that_named_nothing_injects_an_empty_list(tmp_path):
    """Silence is the empty list, not a missing key: the tool reads one shape."""
    session = Session.create_with_id("s_empty", tmp_path)
    req = TaskRequest(session_id=session.session_id, cwd=str(tmp_path), prompt="hi")

    args = _inject(_server(), "write", {"file_path": "x"}, session, req)

    assert args["writable_roots"] == []


# ── §6 the daemon's command ───────────────────────────────────────────────


def test_add_stores_the_root_and_answers_the_requester(outside):
    ws, elsewhere = outside
    session = Session.create_with_id("s_add", ws)
    server = _server()

    frames = _drive(server, {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "add",
        "path": str(elsewhere),
    })

    reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
    assert "error" not in reply, reply
    assert reply["roots"] == [canonical_path(str(elsewhere))]
    # The daemon is the only writer, and the write is on disk — a later turn in a
    # reloaded session inherits it (requirement 1).
    assert _meta(Session.load(session.session_id, ws))["sandbox_roots"] == [
        canonical_path(str(elsewhere))
    ]


def test_add_reaches_a_connection_that_did_not_ask(outside):
    """Requirement 1: the other client sees it without a reload."""
    ws, elsewhere = outside
    session = Session.create_with_id("s_add_broadcast", ws)
    server = _server()
    other = _RecordingWs()
    server._all_connections.add(other)
    asker = _RecordingWs()
    server._all_connections.add(asker)

    asyncio.run(server._process_message({
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "add",
        "path": str(elsewhere),
    }, asker))

    assert [f for f in other.sent if f.get("type") == "sandbox_roots"], other.sent
    assert other.sent[-1]["roots"] == [canonical_path(str(elsewhere))]
    # The asker got its own reply, not a second copy of it.
    assert len([f for f in asker.sent if f.get("type") == "sandbox_roots"]) == 1


def test_a_refused_path_is_not_stored(outside):
    """Acceptance 3: the refusal names the rule, and nothing is written."""
    ws, _ = outside
    session = Session.create_with_id("s_refused", ws)
    before = session._meta_path.read_text(encoding="utf-8")
    server = _server()

    for bad in ("/", os.path.expanduser("~"), "~/.emrg", "relative-thing"):
        frames = _drive(server, {
            "type": "set_sandbox_roots",
            "session_id": session.session_id,
            "cwd": str(ws),
            "op": "add",
            "path": bad,
        })
        reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
        assert reply.get("error"), (bad, reply)
        assert reply["roots"] == []

    assert session._meta_path.read_text(encoding="utf-8") == before
    assert "sandbox_roots" not in _meta(session)


def test_remove_withdraws_the_root_and_is_idempotent(outside):
    """Acceptance 4, through the command: removed is removed, and twice is not an error."""
    ws, elsewhere = outside
    session = Session.create_with_id("s_remove", ws)
    session.set_sandbox_roots([str(elsewhere)])
    server = _server()

    frames = _drive(server, {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "remove",
        "path": str(elsewhere),
    })

    reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
    assert reply["roots"] == [] and "error" not in reply
    assert Session.load(session.session_id, ws).sandbox_roots == []

    again = _drive(server, {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "remove",
        "path": str(elsewhere),
    })
    second = [f for f in again if f.get("type") == "sandbox_roots"][-1]
    assert second["roots"] == [] and "error" not in second
    assert second["notice"] and "nothing to remove" in second["notice"]


def test_list_answers_the_asker_and_announces_nothing(outside):
    """A display refresh is not a state change, so it is not broadcast."""
    ws, elsewhere = outside
    session = Session.create_with_id("s_list", ws)
    session.set_sandbox_roots([str(elsewhere)])
    server = _server()
    other = _RecordingWs()
    server._all_connections.add(other)

    frames = _drive(server, {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "list",
        "path": "",
    })

    reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
    assert reply["roots"] == [canonical_path(str(elsewhere))]
    assert not other.sent


def test_an_unknown_op_is_refused_and_nothing_is_written(outside):
    ws, _ = outside
    session = Session.create_with_id("s_bad_op", ws)
    server = _server()

    frames = _drive(server, {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "append",
        "path": "/tmp",
    })

    reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
    assert reply.get("error") and "append" in reply["error"]
    assert "sandbox_roots" not in _meta(session)


def test_the_resume_snapshot_carries_the_roots(outside):
    """A client opening a session later learns them here, or nowhere.

    ``sandbox_roots`` is a broadcast and broadcasts are never replayed, so a
    session opened after the fact would show no roots — the same reason the tier
    rides this snapshot.
    """
    ws, elsewhere = outside
    session = Session.create_with_id("s_snapshot", ws)
    session.set_sandbox_roots([str(elsewhere)])
    server = _server()
    ws_rec = _RecordingWs()

    asyncio.run(server._handle_resume_session(session.session_id, Path(str(ws)), ws_rec))

    meta = [f for f in ws_rec.sent if f.get("type") == "resume_result"][-1]["meta"]
    assert meta["sandbox_roots"] == [canonical_path(str(elsewhere))]


# ── §7 the model's view of the boundary ───────────────────────────────────


def test_the_context_line_names_the_roots_and_is_fresh_inside_the_freeze(outside):
    """Requirement 8: the boundary is stated, and the freeze does not hide it.

    Until this existed the model learned the boundary by having a write refused.
    The snapshot above is frozen for ``context_refresh_interval_ms`` so the
    prefix stays cacheable — that freeze is about the *clock*, and a host who
    names a root mid-window must not wait for it to expire. So the line is
    appended outside the frozen text, which this test pins by freezing a snapshot
    first and then changing the roots.
    """
    ws, elsewhere = outside
    session = Session.create_with_id("s_ctx", ws)
    server = _server()
    server.llm.config.context_refresh_interval_ms = 600_000

    assert server._build_context_message(session)["content"].count("[context]") == 1

    session.set_sandbox_roots([str(elsewhere)])

    text = server._build_context_message(session)["content"]

    assert canonical_path(str(elsewhere)) in text
    assert "cannot add to or widen" in text


def test_a_session_with_no_named_root_gets_no_boundary_line(tmp_path):
    """Silence by default: the tier's own boundary needs no restating every round."""
    session = Session.create_with_id("s_ctx_empty", tmp_path)
    server = _server()

    text = server._build_context_message(session)["content"]
    assert "[context] This session may also write" not in text


# ── §8 this is not the deployer root D5 deleted ───────────────────────────


def test_the_grant_comes_from_the_session_and_not_from_a_constant(outside):
    """Requirement 10: the mechanism is *named*, so it cannot be D5's come back.

    ``_trusted_write_zones()`` was deleted by host decision D5 — a deployment
    constant that nobody chose and nothing could withdraw. Its guard lives
    elsewhere and refuses the *name*. What this adds is the behavioural half the
    rant asks for ("must have a test separating the two, rather than a comment
    claiming it"): a session nobody named a root for grants **nothing** beyond
    what its tier derives, and a root appears only when the policy carries one —
    so there is no path-shaped constant to inherit, and no state a host cannot
    remove with one command.
    """
    ws, elsewhere = outside
    evo_root = canonical_path(os.path.expanduser("~/.emrg/evolution/.emrg"))
    session = Session.create_with_id("s_d5", ws)
    root = canonical_path(str(elsewhere))

    assert session.sandbox_roots == []
    derived = writable_roots(_ws_policy(ws))
    assert evo_root not in derived
    assert root not in derived

    assert root in writable_roots(_ws_policy(ws, extra=(root,)))
