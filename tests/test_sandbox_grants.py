"""The typed writable-grant model — what a grant is, before any dialect reads it.

Issue #2492. The writable surface used to be ``list[str]``: a path and nothing
else, so a kind could not be *stated* and had to be *asked of the disk* by each
dialect, and an object that is not a path could not be named at all. A grant is a
``(kind, target, source)`` triple now (``emrg/sandbox/grants.py``), and this file
measures the claims that model makes rather than the dialects that consume it —
those are measured in ``test_sandbox_extra_roots.py`` §4 and
``test_bash_v2_win32.py``.

The layout follows the issue's requirements: §1 the kind is explicit (§1), §2 the
manifest supplies grants nobody typed (§2), §3 it is visible and withdrawable
(§3), §4 it never widens a tier that grants nothing (§4), §5 a kind no platform
enforces is refused by name rather than dropped (§5).

Two readings this file is written around:

* the autouse fixture in ``tests/conftest.py`` pins the deployment manifest
  **empty**, so every test here installs its own table (``pin_default_manifest``)
  — a test that instead read the real rows would be measuring whether this host
  happens to run ``lark-cli``;
* the one test whose subject *is* the real table takes a snapshot of it at
  **import**, before any fixture can patch it. That is the "reads it directly,
  past the fixture" the conftest docstring promises, and it is the only way to
  measure the table that ships.
"""

from __future__ import annotations

import json
import sys

import pytest

from emrg.sandbox import grants as grants_mod
from emrg.sandbox.fence import file_refusal
from emrg.sandbox.grants import (
    DEFAULT_MANIFEST,
    ENFORCED_KINDS,
    GRANT_DIRECTORY,
    GRANT_FILE,
    GRANT_KINDS,
    GRANT_REGISTRY,
    SOURCE_DEFAULT,
    SOURCE_SESSION,
    GrantKindNotEnforced,
    RootGrant,
    default_grants,
    enforced_kinds,
    enforcement_refusal,
    manifest_targets,
)
from emrg.sandbox.policy import grants_from_arguments, resolve_policy
from emrg.sandbox.providers.darwin import sbpl_string, seatbelt_profile_args
from emrg.sandbox.providers.linux import bwrap_profile_args
from emrg.sandbox.roots import (
    canonical_path,
    canonical_target,
    granted_grants,
    named_grants,
    stored_grants,
    writable_roots,
)
from emrg.sandbox.win32.registry import RegistryKeyError, split_key
from emrg.session import Session

#: The shipped manifest, captured at import — before the autouse fixture can pin
#: it empty for the duration of a test. Only the test that measures the *real*
#: table reads this.
_SHIPPED_MANIFEST = DEFAULT_MANIFEST
_SHIPPED_ENFORCED = ENFORCED_KINDS


def _write(name: str, tmp_path) -> str:
    """A real file outside the workspace: the path form of a ``file`` grant."""
    target = tmp_path / name
    target.write_text("x", encoding="utf-8")
    return str(target)


# ── §1 the kind is explicit ────────────────────────────────────────────────


def test_a_recorded_kind_wins_over_the_disk(tmp_path):
    """A name granted as one file stays one file the day it becomes a directory.

    This is the defect the typed model removes, and it is not hypothetical: the
    Seatbelt profile chose ``subpath`` (a whole tree) or ``literal`` (this name)
    with ``os.path.isdir`` at profile-build time, so a grant that had become a
    directory silently widened to everything beneath it. The kind is recorded
    now, so the disk is not asked again.
    """
    became_a_directory = tmp_path / "suite"
    became_a_directory.mkdir()
    grant = RootGrant(kind=GRANT_FILE, target=str(became_a_directory), source=SOURCE_SESSION)
    policy = resolve_policy(
        mode="workspace-write",
        workspace_root=str(tmp_path),
        extra_grants=(grant,),
    )

    profile = " ".join(seatbelt_profile_args(policy))

    # The expected clause is spelled by the provider's own ``sbpl_string``, never by
    # quoting the path here. The profile escapes every backslash, so a clause written
    # from the raw path is found on a host whose paths have none and is *false* on a
    # Windows host with the grant visibly present — which is what ``test-windows``
    # reported for this file (`2 failed, 5179 passed, 499 skipped`). Both directions
    # take the same spelling, so the negative one is a measurement there too rather
    # than a string that could never match.
    clause = sbpl_string(canonical_path(str(became_a_directory)))

    assert f"(literal {clause})" in profile
    assert f"(subpath {clause})" not in profile, (
        "the recorded kind was overridden by the disk — the widening this change removes"
    )


def test_one_path_named_as_two_kinds_is_two_grants(tmp_path):
    """De-duplication is by ``(kind, target)``, so a kind is never silently picked.

    Collapsing the two into one would have to choose which was meant, and the
    choice would be invisible to the host who named both.
    """
    target = str(tmp_path)
    policy = resolve_policy(
        mode="workspace-write",
        workspace_root=str(tmp_path / "ws"),
        extra_grants=(
            RootGrant(kind=GRANT_DIRECTORY, target=target),
            RootGrant(kind=GRANT_FILE, target=target),
        ),
    )

    kinds = [
        grant.kind for grant in granted_grants(policy)
        if grant.target == canonical_path(target)
    ]

    assert kinds == [GRANT_DIRECTORY, GRANT_FILE], kinds


def test_the_path_projection_has_no_key_to_a_registry_grant(tmp_path):
    """A registry key is not a path, so the path projection contributes none.

    ``writable_roots`` is a view of ``granted_grants`` for the readers that need
    only paths. Handing a key spelling to one of them would grant something else
    entirely — the identity bug requirement 1 of issue #2492 names.

    The workspace root is the fixture's own directory, not a literal ``/tmp``: the
    policy refuses a root that is not absolute *in the execution world*, and ``/tmp``
    is not one on a Windows host — where this test then failed on the setup rather
    than on the claim it makes. The claim is about a kind that is not a path, so the
    root it is made under must not itself be a platform statement.
    """
    grant = RootGrant(kind=GRANT_REGISTRY, target="HKCU\\Software\\LarkCli")
    policy = resolve_policy(
        mode="workspace-write",
        workspace_root=str(tmp_path),
        extra_grants=(grant,),
    )

    assert canonical_target(grant) == "HKCU\\Software\\LarkCli", (
        "realpath over a key spelling would rewrite the key into a path"
    )
    with pytest.raises(GrantKindNotEnforced):
        writable_roots(policy)


# ── §2 the manifest supplies grants nobody typed ───────────────────────────


def test_a_default_applies_without_a_command(pin_default_manifest, tmp_path):
    """Requirement 2: a host who never types ``/sandbox add`` still gets the state.

    The tool whose identity expires renews by writing outside the workspace, so a
    session that grants only its workspace cannot run it. The default manifest is
    what closes that, and the grant it produces is labelled ``default`` so a
    reader can tell it from one the host named.
    """
    state = tmp_path / "state"
    state.mkdir()
    pin_default_manifest(state)
    policy = resolve_policy(mode="workspace-write", workspace_root=str(tmp_path / "ws"))

    grants = granted_grants(policy)

    assert RootGrant(
        kind=GRANT_DIRECTORY, target=canonical_path(str(state)), source=SOURCE_DEFAULT
    ) in grants
    assert canonical_path(str(state)) in writable_roots(policy)


def test_a_default_the_host_named_is_reported_as_the_host_s(pin_default_manifest, tmp_path):
    """A name both the host and the manifest supply is one grant, with one source.

    The provenance is what lets the display say «you named this» — and what
    decides that withdrawing it also withdraws the default underneath.
    """
    state = tmp_path / "state"
    state.mkdir()
    pin_default_manifest(state)
    policy = resolve_policy(
        mode="workspace-write",
        workspace_root=str(tmp_path / "ws"),
        extra_grants=(RootGrant(kind=GRANT_DIRECTORY, target=str(state)),),
    )

    matching = [
        grant for grant in granted_grants(policy)
        if grant.target == canonical_path(str(state))
    ]

    assert [grant.source for grant in matching] == [SOURCE_SESSION], matching


def test_a_directory_the_host_does_not_have_is_absent_not_an_error(pin_default_manifest, tmp_path):
    """A manifest row for a tool nobody installed is absent, never fatal.

    ``lark-cli`` may never have run on this host; a boundary that refused to
    compute over a name that is not there would take a working sandbox down with
    it, which is why the derivation is total over its inputs.
    """
    absent = tmp_path / "never-installed"
    pin_default_manifest(absent)

    assert default_grants() == []
    assert manifest_targets() == [str(absent)], (
        "the withdrawal path reads the table, not the disk — a name that is not "
        "there yet is still a default a host may withdraw"
    )


# ── §3 the manifest is visible and withdrawable ────────────────────────────


def test_stored_grants_show_a_default_while_the_tier_grants_nothing(pin_default_manifest, tmp_path):
    """Requirement 3 meets requirement 4: shown at every tier, granted at one.

    ``stored_grants`` is the display reader and does not consult the tier — a
    host who asks what is on the list sees the defaults even at ``read-only``,
    which is what makes one of them withdrawable instead of invisible.
    """
    state = tmp_path / "state"
    state.mkdir()
    pin_default_manifest(state)
    policy = resolve_policy(mode="read-only", workspace_root=str(tmp_path / "ws"))

    shown = stored_grants(policy)
    granted = granted_grants(policy)

    assert any(grant.source == SOURCE_DEFAULT for grant in shown), shown
    assert granted == [] and writable_roots(policy) == []


def test_a_withdrawn_default_is_not_granted(pin_default_manifest, tmp_path):
    """Withdrawal is state, and the derivation reads it."""
    state = tmp_path / "state"
    state.mkdir()
    pin_default_manifest(state)
    policy = resolve_policy(
        mode="workspace-write",
        workspace_root=str(tmp_path / "ws"),
        withheld_roots=(canonical_path(str(state)),),
    )

    assert canonical_path(str(state)) not in writable_roots(policy)
    assert all(
        grant.target != canonical_path(str(state)) for grant in stored_grants(policy)
    )


# ── §4 a default never widens a tier that grants nothing ───────────────────


def test_the_manifest_grants_nothing_at_read_only(pin_default_manifest, tmp_path):
    """Requirement 4, with a manifest that really names something.

    The property ``writable_roots`` has today is ``[]`` for ``read-only``; a
    default that applied regardless would be a widening nobody typed, in the one
    tier whose definition is that nothing is writable.
    """
    state = tmp_path / "state"
    state.mkdir()
    pin_default_manifest(state)

    for mode in ("read-only",):
        policy = resolve_policy(mode=mode, workspace_root=str(tmp_path / "ws"))
        assert granted_grants(policy) == []
        assert writable_roots(policy) == []


# ── §5 a kind no platform enforces is refused by name ─────────────────────


def test_one_platform_enforces_a_registry_key_and_the_other_three_refuse_it():
    """The staging boundary has moved, and it is now asymmetric on purpose.

    A registry key's DACL is read and rewritten through ``RegGetKeySecurity`` /
    ``RegSetKeySecurity``, which exist on Windows and nowhere else. So the kind is
    enforceable on exactly one family, and the other three must keep saying so
    rather than dropping the grant — a platform that cannot enforce a kind and
    silently accepts it is the lying boundary this module refuses everywhere else.
    """
    assert GRANT_REGISTRY in enforced_kinds("win32")
    for family in ("darwin", "linux", "other"):
        assert GRANT_REGISTRY not in enforced_kinds(family), family


def test_the_windows_registry_rows_name_keys_the_applier_can_address():
    """The shipped table's registry rows, held to the parser that has to act on them.

    A row naming a spelling ``split_key`` refuses would be a default grant that can
    never be applied — the lying manifest this module refuses elsewhere, one layer
    down. Comparing the two halves is what keeps the table and the applier in step;
    nothing here opens a key, so it measures on every platform.
    """
    rows = _SHIPPED_MANIFEST["win32"]
    keys = [row for row in rows if row[0] == GRANT_REGISTRY]
    assert len(keys) >= 2, (
        f"the Windows manifest ships {len(keys)} registry row(s) — the parent and the key "
        "are both load-bearing, so fewer than two means nothing was measured"
    )
    for kind, spelling in keys:
        _root, subkey = split_key(spelling)
        assert subkey, spelling
        assert kind in _SHIPPED_ENFORCED["win32"], (kind, spelling)
    # The other direction, and it is what makes this a guard rather than a tally:
    # re-typing a key row as a path drops it out of `keys`, so every assertion
    # above would pass over the row that changed. A path row must therefore *not*
    # carry a key's spelling.
    for kind, spelling in rows:
        if kind == GRANT_REGISTRY:
            continue
        with pytest.raises(RegistryKeyError):
            split_key(spelling)


def test_a_registry_row_this_host_does_not_have_is_absent_not_an_error():
    """The manifest's totality covers a key, not only a directory.

    ``lark-cli`` may never have run here, and on a host with no registry it cannot
    have — so the row is *absent* from the derivation, never a failure, and the
    directory rows beside it are unaffected.
    """
    if sys.platform == "win32":
        pytest.skip("on Windows the answer depends on this runner's own registry")
    kinds = [grant.kind for grant in default_grants("win32")]
    assert GRANT_REGISTRY not in kinds, kinds


def test_a_registry_grant_is_either_enforced_here_or_refused_by_name(tmp_path):
    """Requirement 5: neither silently ignored nor reported as granted.

    Dropping it would let the run claim a boundary it does not have; passing the
    key to a path consumer would grant something else. So the derivation either
    hands the grant on — on the one platform that can enforce a key — or raises,
    and the sentence names the kind, the target and what this platform can do.
    The test asks ``enforced_kinds()`` rather than ``sys.platform``, so one body
    measures both answers without asserting which platform it runs on.
    """
    grant = RootGrant(kind=GRANT_REGISTRY, target="HKCU\\Software\\X")
    policy = resolve_policy(
        mode="workspace-write",
        workspace_root=str(tmp_path),
        extra_grants=(grant,),
    )

    if GRANT_REGISTRY in enforced_kinds():
        assert named_grants(policy) == [grant], "an enforceable key grant must survive typed"
        return

    with pytest.raises(GrantKindNotEnforced) as refused:
        named_grants(policy)

    assert "registry" in refused.value.reason
    assert "HKCU\\Software\\X" in refused.value.reason
    assert "Stage 2" in refused.value.reason


def test_every_dialect_refuses_a_registry_grant_rather_than_losing_it(tmp_path):
    """One rule, both profile builders: the same policy, the same refusal.

    The fence answers in text (its callers render a sentence to the model) while
    the profile builders let the exception out, so a confined command fails
    closed instead of running under a profile that quietly omitted a grant.

    Scoped to the platforms that do not enforce the kind: on Windows a registry
    grant *is* enforceable, and Seatbelt and bubblewrap are not the dialects a run
    there reaches — the chain picks a provider by platform — so the refusal they
    own has nothing to answer.
    """
    if GRANT_REGISTRY in enforced_kinds():
        pytest.skip(
            "this platform enforces a registry key; the Seatbelt and bubblewrap "
            "builders are not the dialects a run here reaches"
        )
    policy = resolve_policy(
        mode="workspace-write",
        workspace_root=str(tmp_path),
        extra_grants=(RootGrant(kind=GRANT_REGISTRY, target="HKCU\\Software\\X"),),
    )

    with pytest.raises(GrantKindNotEnforced):
        seatbelt_profile_args(policy)
    with pytest.raises(GrantKindNotEnforced):
        bwrap_profile_args(policy)

    refusal = file_refusal(str(tmp_path / "file.txt"), policy)
    assert refusal is not None and "registry" in refusal


# ── the model at the wire and in a tool call ───────────────────────────────


def test_a_grant_round_trips_through_the_wire_shape(tmp_path):
    """``as_wire``/``from_wire`` are each other's inverse, and a bad entry narrows.

    Tolerant reading is deliberate and only in one direction: an entry a tool
    cannot parse is **dropped**, because a malformed grant that widened a
    boundary would be the failure mode the field exists to prevent.
    """
    grant = RootGrant(kind=GRANT_FILE, target=_write("f.txt", tmp_path), source=SOURCE_DEFAULT)

    assert RootGrant.from_wire(grant.as_wire()) == grant
    assert RootGrant.from_wire(grant) == grant
    assert RootGrant.from_wire("relative/but-readable") == RootGrant.from_path(
        "relative/but-readable"
    )
    for malformed in (
        None, 42, {}, {"kind": "socket", "target": "/tmp"}, {"kind": GRANT_FILE},
        {"kind": GRANT_FILE, "target": "   "},
    ):
        assert RootGrant.from_wire(malformed) is None, malformed
    assert RootGrant.from_wire({"kind": GRANT_FILE, "target": "/tmp/x", "source": "bogus"}) == (
        RootGrant(kind=GRANT_FILE, target="/tmp/x", source=SOURCE_SESSION)
    ), "an unknown source falls back to the narrower claim: the host named it"


def test_a_tool_call_reads_the_typed_grants_and_falls_back_to_paths(tmp_path):
    """One reader for the two injected keys, and the typed one wins.

    A tool that read only ``writable_roots`` would re-decide each kind from the
    disk — the defect again, one call further in. The path form stays readable for
    a caller that was never told a kind (a hand-built call, a test).
    """
    (tmp_path / "suite").mkdir()
    target = str(tmp_path / "suite")
    typed = RootGrant(kind=GRANT_FILE, target=target, source=SOURCE_SESSION)

    assert grants_from_arguments({"writable_grants": [typed.as_wire()]}) == [typed]
    assert grants_from_arguments(
        {"writable_grants": [typed.as_wire()], "writable_roots": ["/elsewhere"]}
    ) == [typed]
    assert grants_from_arguments({"writable_roots": [target]}) == [
        RootGrant(kind=GRANT_DIRECTORY, target=target, source=SOURCE_SESSION)
    ], "a path with no kind is read from the disk once, here"
    assert grants_from_arguments({}) == []


# ── the shipped table, measured as written ────────────────────────────────


def test_the_shipped_manifest_only_promises_kinds_its_platform_enforces():
    """The real table, read past the fixture that empties it for other tests.

    A manifest row promising a grant the run cannot enforce is the lying boundary
    this module refuses everywhere else — so the table is held to
    :data:`ENFORCED_KINDS`, which is why the Windows registry keys the issue also
    names are absent from it until Stage 2 can enforce them.
    """
    assert _SHIPPED_MANIFEST, "the shipped manifest is empty — nothing was measured"
    for family, rows in _SHIPPED_MANIFEST.items():
        assert rows, family
        for kind, spelling in rows:
            assert kind in GRANT_KINDS, (family, kind)
            assert kind in _SHIPPED_ENFORCED[family], (
                f"{family} ships a {kind} grant ({spelling!r}) that no dialect on "
                "that platform enforces"
            )
            assert spelling, (family, kind)


# ── what the daemon stores ────────────────────────────────────────────────


def test_the_session_keeps_a_grant_s_typed_kind_across_a_reload(tmp_path):
    """The kind is session state, not a derived reading of the disk.

    A restart used to re-decide every root's kind with ``isdir`` — so a grant
    could change meaning without anyone asking. The typed list is persisted, and
    the path projection is written beside it from the same producer.
    """
    session = Session.create_with_id("s_typed", tmp_path)
    named = RootGrant(
        kind=GRANT_FILE, target=_write("token", tmp_path), source=SOURCE_SESSION
    )

    session.set_sandbox_grants([named])

    meta = json.loads(session._meta_path.read_text(encoding="utf-8"))
    reloaded = Session.load("s_typed", tmp_path)

    assert meta["sandbox_grants"] == [named.as_wire()]
    assert meta["sandbox_roots"] == [named.target], "the projection, from the same producer"
    assert reloaded.sandbox_grants == [named]
    assert reloaded.sandbox_roots == [named.target]
