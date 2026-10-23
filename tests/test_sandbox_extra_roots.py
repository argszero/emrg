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
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.sandbox import roots as roots_mod
from emrg.sandbox.fence import file_refusal
from emrg.sandbox.grants import GRANT_REGISTRY, RootGrant
from emrg.sandbox.policy import SandboxPolicy, resolve_policy
from emrg.sandbox.providers.darwin import seatbelt_profile_args
from emrg.sandbox.providers.linux import BWRAP_BIN, bwrap_profile_args, runner_argv
from emrg.sandbox.roots import (
    canonical_path,
    judge_extra_root,
    judge_registry_key,
    judge_root_removal,
    writable_roots,
)
from emrg.server.daemon import EmrgServer
from emrg.session import Session
from emrg.tools import file_policy


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


def _grants_by_form(profile_args: list[str]) -> dict[str, list[str]]:
    """The same grants, kept under the form the profile spelled them with.

    A list of paths cannot answer "was this pinned as ``literal`` or as
    ``subpath``" — and that distinction is the whole subject of the file/dir rule,
    since both forms match their own path. So the form is read out of the profile
    with the path, and the paths are unescaped exactly as :func:`_grants` does,
    because a profile is a *parsed* language: SBPL doubles the separator, so a raw
    Windows path appears in it with its backslashes escaped and a substring test
    against the unescaped spelling reddens there and nowhere else (CI run
    37878525434).
    """
    profile = " ".join(profile_args)
    out: dict[str, list[str]] = {"subpath": [], "literal": []}
    for form, quoted in re.findall(r'\((subpath|literal) ("(?:[^"\\]|\\.)*")\)', profile):
        out[form].append(quoted[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
    return out


def _names_path(text: str, path: str) -> bool:
    """Whether a line names this path, tolerating the escaping its spelling adds.

    The codebase spells a path in model-facing text with ``repr`` (the fence's
    refusal text does, and so does the boundary line), and ``repr`` escapes the
    separators on Windows only. Asking "is this path in this text" therefore has
    to undo that escaping rather than compare a raw spelling — the same round trip
    :func:`_grants` makes for the profile, and the same rule the suite learned as
    "assert the property, not the platform's rendering of it".
    """
    return path in text or path in text.replace("\\\\", "\\")


def _ws_policy(workspace, extra=()) -> SandboxPolicy:
    return resolve_policy(mode="workspace-write", workspace_root=str(workspace), extra_roots=extra)


#: An absolute workspace root on **every** platform, derived rather than spelled.
#:
#: ``"/tmp"`` is not one: ``os.path.isabs("/tmp")`` is **False** on Windows, because
#: a drive-less rooted spelling does not resolve under the cwd, so ``SandboxPolicy``
#: refuses it before any of this file's subject runs. The registry judges below never
#: read the workspace — a key is granted as itself — so the value only has to clear
#: that precondition, and it has to clear it on the platform the test runs on. The
#: Windows leg measured the difference: ten failures, run 39881732802, every one of
#: them ``workspace_root must be an absolute execution-world path (got '/tmp')``
#: while the same tests passed on Linux.
#:
#: ``abspath`` is what makes it platform-honest: the host's own ``os.path`` answers,
#: so this is ``/`` here and ``C:\\`` on Windows, and no spelling is written down for
#: a platform nobody measured.
_ANY_WS = os.path.abspath(os.sep)


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


def test_a_drive_less_rooted_spelling_is_never_called_relative(outside):
    """"Does not resolve under the cwd" is the rule, and `isabs` is not it.

    On Windows every drive-less rooted spelling — `/`, `/tmp/x` — answers
    ``os.path.isabs(...) == False``, so a rule that reads the platform's answer
    alone calls a host's absolute path relative. This asserts the *property*
    rather than the platform: such a spelling may be refused for whatever reason
    the path deserves (here: it does not exist), but never as "not an absolute
    path".

    **This guard's power is one-legged, and saying so is the point.** It runs on
    both legs but can only redden the Windows one: on POSIX ``os.path.isabs``
    already answers True for this spelling, so the defect is invisible there.
    Measured — reverting the fix and re-running this file on macOS leaves it
    green (33 passed), while CI run 37878525434 is the Windows run where the
    platform-only rule turned a host's `/` into the wrong refusal. A guard that
    is armed on one leg is worth having and must not be described as if both
    could kill it.
    """
    ws, _ = outside

    verdict = judge_extra_root("/emrg-probe-not-a-real-path", _ws_policy(ws))

    assert verdict.refusal, verdict
    assert "not an absolute path" not in verdict.refusal, verdict.refusal


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

    verdict = judge_extra_root("~/.emrg/agents.yml", _ws_policy(ws))

    assert verdict.refusal and "agents.yml" in verdict.refusal


def test_a_root_naming_the_protected_directory_by_another_spelling_is_refused(
    outside, tmp_path, monkeypatch
):
    """The grant arm reads identity too, and the directory is where it matters.

    A root is granted as a tree, so the question is not only "is this the file"
    but "does this name a directory above one". On a volume that folds case,
    ``~/.EMRG`` beside a protected ``~/.emrg/config.toml`` is that directory, and
    the string comparison this replaces admitted it — the grant the rule exists
    to refuse (#2551, requirement 4).

    The volume is asked rather than assumed: where it keeps the two spellings
    apart they are two directories, the grant is the size the host asked for, and
    the case is skipped with that reason. The protected list is a temporary one,
    because ``protected_paths()`` derives from the real home and no test here
    touches it.
    """
    ws, _ = outside
    home = tmp_path / "home"
    (home / ".emrg").mkdir(parents=True)
    victim = home / ".emrg" / "config.toml"
    victim.write_text("host state", encoding="utf-8")
    variant = home / ".EMRG"
    if not file_policy.same_file(str(variant), str(home / ".emrg")):
        pytest.skip("this volume keeps .EMRG and .emrg apart as two directories")
    monkeypatch.setattr(
        file_policy, "protected_paths", lambda: [os.path.realpath(victim)]
    )

    verdict = judge_extra_root(str(variant), _ws_policy(ws))

    assert verdict.refusal, verdict
    assert "protected daemon state file" in verdict.refusal


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

    forms = _grants_by_form(seatbelt_profile_args(policy))

    assert canonical_path(str(a_dir)) in forms["subpath"]
    assert canonical_path(str(a_file)) in forms["literal"]
    assert canonical_path(str(a_file)) not in forms["subpath"]


def test_bwrap_binds_every_extra_root(outside):
    """Requirement 7's Linux half: a mount over the read-only view, as the workspace is."""
    ws, elsewhere = outside
    root = canonical_path(str(elsewhere))
    bound = bwrap_profile_args(_ws_policy(ws, extra=(root,)))
    at = bound.index(root)

    assert bound[at - 1:at + 2] == ["--bind", root, root], bound


def test_bwrap_binds_a_named_file_as_a_file(outside):
    """A root that is a **file**, not a directory, still gets its own mount.

    Requirement 7 asks for both, and the directory case was the only one pinned.
    ``--bind SRC DEST`` needs DEST to exist, which a host-named path does; the
    provider's own comment records the pairing as **unmeasured** rather than
    claiming it, so this is the contract half of that measurement.
    """
    ws, elsewhere = outside
    target = elsewhere / "note.txt"
    target.write_text("x", encoding="utf-8")
    root = canonical_path(str(target))
    bound = bwrap_profile_args(_ws_policy(ws, extra=(root,)))
    at = bound.index(root)

    assert bound[at - 1:at + 2] == ["--bind", root, root], bound


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap is the Linux dialect's runner")
def test_bwrap_really_binds_a_named_file_writable(outside):
    """The argv above is a claim until something runs it — this is the running.

    Requirement 7 says the single-file pairing "must be measured before support is
    claimed", and the provider's comment says so in its own words. The pair is
    asserted in both directions on one directory: the **named** file is writable
    through the mount, and the bytes written beside it never reach the host — so a
    pass cannot come from the bind being ignored, and a refusal cannot come from
    the whole directory being writable.

    **The second half is read on the host, not from the exit code**, and that is a
    deliberate correction: ``bwrap_profile_args`` mounts a private ``--tmpfs
    /tmp``, and pytest's own base is *under* it on Linux
    (``/tmp/pytest-of-runner/…``, printed by the run that reddened on the exit-code
    form of this assertion). Inside the sandbox the sibling's directory is
    therefore re-created empty inside that private tmpfs, the write succeeds, and
    the exit code says nothing about which mount granted what — a reading that
    named the environment rather than the mechanism. Where the bytes *landed*
    discriminates in both environments: a mount over the **directory** would have
    re-exposed the host file and the reading below shows it, while a mount over the
    **file** leaves it untouched.

    Skipped where there is no bubblewrap, and CI's ubuntu leg installs it, so the
    skip is a fact about this host rather than about the mechanism.
    """
    if shutil.which(BWRAP_BIN) is None:
        pytest.skip(f"{BWRAP_BIN} is not installed on this host")

    ws, elsewhere = outside
    named = elsewhere / "named.txt"
    named.write_text("host\n", encoding="utf-8")
    sibling = elsewhere / "sibling.txt"
    sibling.write_text("host\n", encoding="utf-8")

    policy = _ws_policy(ws, extra=(canonical_path(str(named)),))
    argv = [*runner_argv(policy), "--", "sh", "-c", f'echo confined >> "{named}"']

    done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=120)

    assert done.returncode == 0, done.stderr
    assert named.read_text(encoding="utf-8") == "host\nconfined\n"
    denied = subprocess.run(
        [*runner_argv(policy), "--", "sh", "-c", f'echo nope >> "{sibling}"'],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    # The reading below is "the host file is unchanged", which a sibling write that
    # never ran would satisfy for free. ``bwrap``'s own fatal printer writes the
    # ``bwrap: `` prefix this backend classifies as a *runner* failure, so its
    # absence is what says the command really ran and the reading is about it.
    assert "bwrap: " not in denied.stderr, denied.stderr
    assert sibling.read_text(encoding="utf-8") == "host\n", (
        f"a write to the sibling reached the host (exit {denied.returncode}), so the "
        f"mount granted the whole directory rather than the named file"
    )


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


# ── §5b a registry key is judged by its own rules ─────────────────────────


def test_a_key_is_validated_by_the_parser_the_applier_uses():
    """A key the judge accepts is a key the DACL path can address — same parser.

    Two questions are a key's own and this is the first: *is the spelling one
    key*. It is answered by ``registry.split_key``, the parser
    ``apply_dacl`` itself calls, so the judge cannot accept a spelling the
    enforcement then fails to resolve. The stored target is the canonical form,
    because the capability SID is derived from the spelling.
    """
    verdict = judge_registry_key(
        "HKEY_CURRENT_USER\\Software\\LarkCli\\keychain", _ws_policy(_ANY_WS), platform="win32"
    )
    assert verdict.refusal is None, verdict
    assert verdict.grant == RootGrant(GRANT_REGISTRY, "HKCU\\Software\\LarkCli\\keychain")
    assert verdict.canonical == "HKCU\\Software\\LarkCli\\keychain"
    assert verdict.store is True


@pytest.mark.parametrize("bad, phrase", [
    ("", "empty string"),
    ("HKCU", "whole hive"),
    ("HKU\\Software\\X", "not one of"),
    ("Software\\LarkCli", "not one of"),
])
def test_a_key_that_names_no_key_is_refused_with_the_parsers_own_reason(bad, phrase):
    """One parser, one wording: the refusal a host reads is `split_key`'s."""
    verdict = judge_registry_key(bad, _ws_policy(_ANY_WS), platform="win32")
    assert verdict.refusal and phrase in verdict.refusal, verdict
    # ``store`` is deliberately not asserted: :class:`ExtraRootVerdict` documents
    # it as the caller's instruction for the *accepted* outcomes — "False only
    # for a path already covered — a refusal never reaches this field" — so it
    # keeps its default ``True`` here and reading it would be reading noise.
    assert verdict.grant is None, verdict


def test_the_spelling_is_judged_before_the_platform():
    """A malformed key is malformed everywhere, so that is the sentence that wins.

    On a platform that cannot apply a key's DACL, "this key does not exist" is
    the more useful answer than "this platform has no registry" — the host would
    otherwise fix the platform question and meet the spelling one afterwards.
    """
    verdict = judge_registry_key("HKCU", _ws_policy(_ANY_WS), platform="darwin")
    assert verdict.refusal and "whole hive" in verdict.refusal, verdict


def test_a_platform_that_cannot_apply_a_key_refuses_it_with_the_platform_sentence():
    """The refusal names the platform's limit, and never a stage that has landed."""
    verdict = judge_registry_key(
        "HKCU\\Software\\LarkCli", _ws_policy(_ANY_WS), platform="darwin"
    )
    assert verdict.refusal, verdict
    assert "cannot be enforced on" in verdict.refusal
    # The sentence this replaces claimed the enforcement had not shipped, which
    # `ENFORCED_KINDS["win32"]` had already falsified by the time a Windows host
    # could reach it.
    assert "not validated yet" not in verdict.refusal
    assert verdict.grant is None


def test_naming_a_key_twice_reports_the_grant_that_is_stored():
    """A duplicate is answered, not stored twice — and case is not a difference.

    The registry namespace is case-insensitive, so ``hkcu\\software\\x`` and
    ``HKCU\\Software\\X`` are one key with one identity; storing both would put
    two grants on the session's list for one ACE.
    """
    policy = resolve_policy(
        mode="workspace-write",
        workspace_root=_ANY_WS,
        extra_grants=[RootGrant(GRANT_REGISTRY, "HKCU\\Software\\LarkCli")],
    )
    verdict = judge_registry_key(
        "HKEY_CURRENT_USER\\software\\larkcli", policy, platform="win32"
    )
    assert verdict.refusal is None, verdict
    assert verdict.store is False
    assert verdict.grant is None
    assert verdict.notice and "already granted" in verdict.notice


def test_a_key_named_twice_at_read_only_is_not_stored_twice():
    """The duplicate check reads the mode-independent list, so the tier cannot hide it.

    ``granted_grants`` answers ``[]`` for every mode but ``workspace-write``, so a
    duplicate check built on it finds nothing at ``read-only`` and stores the same
    key a second time — one key, two grants, two capability SIDs derived from one
    spelling. The session's list follows the session and not the tier, exactly as
    it does for a path, so the reader that does not consult the tier is the one
    that answers "is this already on the list".
    """
    policy = resolve_policy(
        mode="read-only",
        workspace_root=_ANY_WS,
        extra_grants=[RootGrant(GRANT_REGISTRY, "HKCU\\Software\\LarkCli")],
    )
    verdict = judge_registry_key(
        "HKEY_CURRENT_USER\\Software\\LarkCli", policy, platform="win32"
    )
    assert verdict.refusal is None, verdict
    assert verdict.store is False, "a key already on the list must not be stored again"
    assert verdict.grant is None, verdict
    assert verdict.notice and "already granted" in verdict.notice, verdict


def test_a_key_is_withdrawn_by_the_flag_that_added_it():
    """An added grant must be withdrawable, so removal has the key's own branch.

    The path rules are skipped entirely: a key is not absolute, and there is
    nothing on a disk to stat. Only the spelling can be refused here — never the
    policy, because a withdrawal is not a grant.
    """
    verdict = judge_root_removal(
        "HKEY_CURRENT_USER\\Software\\LarkCli", _ws_policy(_ANY_WS), GRANT_REGISTRY
    )
    assert verdict.refusal is None, verdict
    assert verdict.canonical == "HKCU\\Software\\LarkCli"
    refused = judge_root_removal("HKCU", _ws_policy(_ANY_WS), GRANT_REGISTRY)
    assert refused.refusal and "whole hive" in refused.refusal


# ── §5c the region above reads its own precondition ───────────────────────


def test_the_key_judges_never_spell_a_posix_workspace_root():
    """The registry judges read no workspace, so the root they are handed is derived.

    Ten tests in this section spelled ``"/tmp"`` as the workspace root, and the
    Windows leg refused every one of them inside ``SandboxPolicy.__post_init__``
    before a line of the subject ran: ``os.path.isabs("/tmp")`` is **False** on
    Windows, because a drive-less rooted spelling does not resolve under the cwd.
    The Linux leg passed, so nothing here saw it. A key is granted as itself and
    none of these judges reads the workspace, so the value is irrelevant to what is
    measured — which is exactly why it must never be a literal.

    The region is bounded by the §5b and §5c markers rather than read to the end of
    the file: a ``split(marker, 1)[0]`` bound widens to EOF, and §7's wire frame
    legitimately carries a ``/tmp`` spelling this rule is not about. The guard keeps
    its own section for a reason it measured: written inside §5b it failed on itself,
    because the spelling it forbids is quoted in the sentence forbidding it.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    start = source.index("# ── §5b a registry key is judged by its own rules")
    end = source.index("# ── §5c the region above reads its own precondition")
    region = source[start:end]

    assert "_ANY_WS" in region, "the region did not read — nothing was measured"
    assert '"/tmp"' not in region, (
        "a registry-judge test spells a POSIX workspace root; os.path.isabs('/tmp') is "
        "False on Windows, so SandboxPolicy refuses it before the subject runs"
    )
    assert os.path.isabs(_ANY_WS), (
        f"the derived root must clear the predicate the policy applies, got {_ANY_WS!r}"
    )


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


def test_a_typed_registry_kind_is_answered_without_a_stale_sentence(outside):
    """The command carries a *stated* kind, and the daemon judges it as a key.

    The answer has two legitimate shapes — a platform whose dialects can apply a
    key's DACL stores the typed grant, one that cannot says so with the platform
    sentence — and this asserts the invariant that holds in both: the refusal is
    never for a reason the model has moved past. Before this, the sentence told a
    Windows host that the enforcement had not shipped, when it had.
    """
    ws, _ = outside
    session = Session.create_with_id("s_registry", ws)
    server = _server()

    frames = _drive(server, {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "add",
        "path": "HKCU\\Software\\LarkCli",
        "kind": "registry",
    })

    reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
    # Reloaded, not read off the object this test built: the daemon loads the
    # session itself, so an assertion on ``session`` here would be about a copy
    # nothing wrote to — it would hold whatever the daemon did.
    stored = Session.load(session.session_id, ws).sandbox_grants
    refused = reply.get("error")
    if refused:
        assert "cannot be enforced on" in refused, refused
        assert "not validated yet" not in refused, refused
        assert stored == [], stored
    else:
        assert [grant.kind for grant in stored] == [GRANT_REGISTRY], stored
        assert stored[0].target == "HKCU\\Software\\LarkCli", stored


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


def test_an_add_at_read_only_is_stored_and_takes_effect_when_the_tier_rises(outside):
    """The sentence and the behaviour are one thing (the rejection on #1976, finding 1).

    Naming a root at ``read-only`` answered "stored for this session" while
    storing nothing, so the tier flip the sentence advised left the list empty —
    measured on head ``4eb69bab``. Storing is the reading this pins, and the two
    facts the design already states make it safe: the list follows the session
    (requirement 9), and the *mode* decides what has effect — ``writable_roots``
    returns ``[]`` for ``read-only``, so a stored root can never widen a tier
    that grants none.
    """
    ws, elsewhere = outside
    session = Session.create_with_id("s_ro_add", ws)
    session.set_sandbox("read-only")
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
    assert reply["notice"] and "stored for this session" in reply["notice"]
    # What that sentence claims, measured rather than read.
    assert reply["roots"] == [canonical_path(str(elsewhere))]
    assert Session.load(session.session_id, ws).sandbox_roots == [
        canonical_path(str(elsewhere))
    ]

    # And the advice it gives: raising the tier makes the root apply, with no
    # second `add` — which is the whole of what the sentence promises.
    _drive(server, {
        "type": "set_sandbox",
        "session_id": session.session_id,
        "cwd": str(ws),
        "mode": "workspace-write",
    })
    stored = Session.load(session.session_id, ws).sandbox_roots
    policy = resolve_policy(
        mode="workspace-write", workspace_root=str(ws), extra_roots=tuple(stored)
    )
    assert canonical_path(str(elsewhere)) in writable_roots(policy)


def test_a_root_added_twice_at_read_only_is_stored_once(outside):
    """The duplicate check cannot come from ``writable_roots`` at this tier.

    That derivation is empty under ``read-only``, so the loop over it sees
    nothing and a second ``add`` of the same path would be appended again. The
    session's own list is what has to be consulted — which is why the check is
    written separately rather than left to the tier's allow-list.
    """
    ws, elsewhere = outside
    session = Session.create_with_id("s_ro_twice", ws)
    session.set_sandbox("read-only")
    server = _server()
    add = {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "add",
        "path": str(elsewhere),
    }

    _drive(server, add)
    frames = _drive(server, add)

    reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
    assert reply["notice"] and "already one of this session's roots" in reply["notice"]
    assert reply["roots"] == [canonical_path(str(elsewhere))]
    assert Session.load(session.session_id, ws).sandbox_roots == [
        canonical_path(str(elsewhere))
    ]


def test_a_root_whose_path_is_gone_can_still_be_withdrawn(outside):
    """Withdrawal is judged on the path, not on whether it could be granted.

    The rejection on #1976 (finding 2): the existence rule ran on removals too,
    so a root whose directory the host deleted answered "does not exist" and
    stayed stored. A grant that cannot be withdrawn is bad on its own, and worse
    because ``writable_roots`` carries a stored spelling whether or not it
    exists — so the reach came back live the moment the path did.
    """
    ws, elsewhere = outside
    gone = elsewhere / "gone-dir"
    gone.mkdir()
    session = Session.create_with_id("s_gone_remove", ws)
    session.set_sandbox("workspace-write")
    server = _server()

    _drive(server, {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "add",
        "path": str(gone),
    })
    assert Session.load(session.session_id, ws).sandbox_roots == [canonical_path(str(gone))]

    gone.rmdir()  # the host deletes the directory, then wants the grant back

    frames = _drive(server, {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "remove",
        "path": str(gone),
    })

    reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
    assert "error" not in reply, reply
    assert reply["roots"] == []
    assert Session.load(session.session_id, ws).sandbox_roots == []


def test_a_removal_still_refuses_a_path_it_cannot_read(outside):
    """The other side of the split: reading a path is removal's own rule.

    A relative spelling would resolve against the daemon's own cwd, so the entry
    withdrawn and the entry shown could be different ones. That rule survives —
    it is a refusal not to *grant* but to *read*, and it is what keeps a removal
    from being a permissive command.
    """
    ws, elsewhere = outside
    session = Session.create_with_id("s_remove_unreadable", ws)
    session.set_sandbox_roots([str(elsewhere)])
    server = _server()
    stored = [canonical_path(str(elsewhere))]

    for bad in ("relative-thing", ""):
        frames = _drive(server, {
            "type": "set_sandbox_roots",
            "session_id": session.session_id,
            "cwd": str(ws),
            "op": "remove",
            "path": bad,
        })
        reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
        assert reply.get("error"), (bad, reply)
        assert Session.load(session.session_id, ws).sandbox_roots == stored


def test_removal_does_not_inherit_the_add_rules(outside):
    """The two judges answer two questions, and this is the difference exactly.

    ``add`` refuses a path it could not grant; ``remove`` accepts a path it can
    read. One path that does not exist is the cheapest witness of the split, and
    it is reachable without a session — so this pins the contract itself rather
    than the command that calls it.
    """
    ws, elsewhere = outside
    policy = resolve_policy(mode="workspace-write", workspace_root=str(ws))
    never_there = str(elsewhere / "never-there")

    assert judge_extra_root(never_there, policy).refusal
    assert judge_root_removal(never_there, policy).refusal is None


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


def test_list_carries_every_grant_with_its_provenance(outside, pin_default_manifest):
    """Requirement 3 of issue #2492: the defaults are shown, and they say what they are.

    Two keys, two questions, and a display needs both: ``roots`` is what the host
    named (the list every existing client speaks), ``grants`` is the whole surface
    typed, with ``source`` saying who named each one. A default that could not be
    told from a host's own grant would be unwithdrawable in practice — nobody
    removes something they did not know they had.
    """
    ws, elsewhere = outside
    installation_default = ws.parent / "installation-state"
    installation_default.mkdir()
    pin_default_manifest(installation_default)
    session = Session.create_with_id("s_provenance", ws)
    session.set_sandbox("workspace-write")
    session.set_sandbox_roots([str(elsewhere)])
    server = _server()

    frames = _drive(server, {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "list",
        "path": "",
    })

    reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
    by_target = {grant["target"]: grant for grant in reply["grants"]}

    assert reply["roots"] == [canonical_path(str(elsewhere))], (
        "`roots` is the host-named projection and must not grow the manifest's entries"
    )
    assert by_target[canonical_path(str(elsewhere))] == {
        "kind": "directory",
        "target": canonical_path(str(elsewhere)),
        "source": "session",
    }
    assert by_target[canonical_path(str(installation_default))] == {
        "kind": "directory",
        "target": canonical_path(str(installation_default)),
        "source": "default",
    }


def test_removing_a_default_withdraws_it_and_the_write_is_refused(outside, pin_default_manifest):
    """Requirement 3's second half: one command withdraws a default.

    A default applies without anyone typing anything, so "stop applying it" has
    to be recorded state rather than an absence from a list nobody wrote — and
    the acceptance is behavioural: after the withdrawal the same write is refused.
    """
    ws, elsewhere = outside
    pin_default_manifest(elsewhere)
    session = Session.create_with_id("s_withdraw", ws)
    session.set_sandbox("workspace-write")
    server = _server()
    token = elsewhere / "token"

    def current():
        return resolve_policy(
            mode="workspace-write",
            workspace_root=str(ws),
            session_id=session.session_id,
            extra_grants=Session.load(session.session_id, ws).sandbox_grants,
            withheld_roots=Session.load(session.session_id, ws).sandbox_withheld,
        )

    assert file_refusal(str(token), current()) is None, (
        "the default did not apply — the withdrawal below would prove nothing"
    )

    frames = _drive(server, {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "remove",
        "path": str(elsewhere),
    })

    reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
    assert "error" not in reply, reply
    assert "withdrawn" in (reply.get("notice") or "")
    assert Session.load(session.session_id, ws).sandbox_withheld == [
        canonical_path(str(elsewhere))
    ]
    assert file_refusal(str(token), current()) is not None, (
        "the default was still granted after the command that withdrew it"
    )


def test_adding_a_withdrawn_default_back_makes_it_the_host_s(outside, pin_default_manifest):
    """Withdrawal is not a one-way door, and the re-grant keeps its new provenance.

    Storing the grant while leaving the withdrawal in place would store something
    the derivation then subtracts: a command that reports success and changes
    nothing.
    """
    ws, elsewhere = outside
    pin_default_manifest(elsewhere)
    session = Session.create_with_id("s_readd", ws)
    session.set_sandbox("workspace-write")
    server = _server()
    for op, path in (("remove", str(elsewhere)), ("add", str(elsewhere))):
        _drive(server, {
            "type": "set_sandbox_roots",
            "session_id": session.session_id,
            "cwd": str(ws),
            "op": op,
            "path": path,
        })

    frames = _drive(server, {
        "type": "set_sandbox_roots",
        "session_id": session.session_id,
        "cwd": str(ws),
        "op": "list",
        "path": "",
    })

    reply = [f for f in frames if f.get("type") == "sandbox_roots"][-1]
    reloaded = Session.load(session.session_id, ws)

    assert reloaded.sandbox_withheld == [], "the withdrawal outlived the re-grant"
    assert [grant.source for grant in reloaded.sandbox_grants] == ["session"]
    assert {
        grant["target"]: grant["source"] for grant in reply["grants"]
    }[canonical_path(str(elsewhere))] == "session"


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

    assert _names_path(text, canonical_path(str(elsewhere)))
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
