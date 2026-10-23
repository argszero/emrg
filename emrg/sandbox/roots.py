"""The writable-root derivation shared by every enforcement dialect.

Mirrors ``packages/sandbox/sandbox/src/roots.ts`` in deepseek-harness (the
blueprint named in ``.emrg/designs/bash-tool-v2-design.md`` §1.1/§5.2):
``workspace-write`` means "the policy's workspace root plus the platform temp
areas", and this module is that meaning's one home.  The Seatbelt profile
(``emrg/sandbox/providers/darwin.py``) and, from P7, the in-process filesystem
fence (``emrg/sandbox/fence.py``) both derive their allow-list here, so "the
write tool cannot write /tmp but bash can" asymmetries cannot arise between
them.
"""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING, Callable, NamedTuple

from emrg.sandbox.grants import (
    GRANT_DIRECTORY,
    GRANT_FILE,
    GRANT_KINDS,
    GRANT_REGISTRY,
    GrantKindNotEnforced,
    RootGrant,
    default_grants,
    enforcement_refusal,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from emrg.sandbox.policy import SandboxPolicy


def canonical_path(path: str) -> str:
    """Resolve a granted root to the path the enforcement layer compares.

    Canonical (symlinks resolved), because both the Seatbelt filters and the
    fence's containment check match *resolved* paths — ``/tmp`` IS
    ``/private/tmp`` on darwin, and granting a root as spelled matches nothing
    (measured: design §3.5).  ``os.path.realpath`` walks the filesystem
    component by component, the same identity ``chdir``/``spawn`` use.

    Resolution failure returns the spelling as-is — the blueprint's
    ``canonicalPath`` does exactly that, and the outcome is the conservative
    one: a missing root matches nothing until it exists.  Inventing a fallback
    would grant a path the caller never named.  ``realpath`` is tolerant by
    construction (it never raises for a missing component), so this is a
    behaviour to preserve rather than an error to catch.

    :param path: the root as configured or platform-reported.
    :returns: the canonical path, or the spelling as-is.
    """
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def canonical_target(grant: RootGrant) -> str:
    """A grant's target in the identity the enforcement layer compares.

    A path is canonicalized (:func:`canonical_path`) because that is what the
    Seatbelt filters, the fence's containment test and the Windows ACL path all
    compare.  A **registry key is not a path** and is returned exactly as
    spelled: running ``realpath`` over ``HKCU\\Software\\LarkCli`` would rewrite a
    key name into a filesystem path that means nothing, which is the identity bug
    requirement 1 of issue #2492 exists to prevent.

    :param grant: the grant to canonicalize.
    :returns: the canonical path, or the key spelling for a registry grant.
    """
    if grant.kind == GRANT_REGISTRY:
        return grant.target
    return canonical_path(grant.target)


def _dedupe(grants: "list[RootGrant] | tuple[RootGrant, ...]") -> list[RootGrant]:
    """The grants, canonical, with no object granted twice, in the order given.

    Deduplication is by ``(kind, canonical target)`` rather than by target alone:
    one path can legitimately be named as a file and as a directory by two
    callers, and collapsing those two into one would have to pick a kind — the
    silent re-interpretation this model removes.  First occurrence wins, so a
    caller that lists a session's own grants before the manifest's reports a hard
    -typed root as ``session`` rather than as an installation default.

    :param grants: the grants to canonicalize and de-duplicate.
    :returns: the deduplicated list.
    """
    out: list[RootGrant] = []
    seen: set[tuple[str, str]] = set()
    for grant in grants:
        canonical = canonical_target(grant)
        key = (grant.kind, canonical)
        if key in seen:
            continue
        seen.add(key)
        out.append(grant if canonical == grant.target else grant._replace(target=canonical))
    return out


def stored_grants(policy: SandboxPolicy) -> list[RootGrant]:
    """Every grant this session names — typed, with provenance, mode-independently.

    The **display** reader. It answers ``/sandbox list`` and nothing else, and it
    deliberately does not consult the tier or the enforcement table:

    * a grant named at ``read-only`` is stored and takes effect when the tier
      rises (the behaviour ``judge_extra_root`` already documents), so a listing
      that hid it would tell the host their command did nothing;
    * a grant this platform cannot enforce is still *shown*, and only then
      refused by whoever tries to enforce it. Hiding it would be the silent
      ignore requirement 5 of issue #2492 forbids, one layer earlier.

    The session's own grants come first, then the manifest's, so a root the host
    typed that the manifest also supplies is reported as ``session`` — they named
    it, and the provenance should say so.

    :param policy: the policy to read, including its withheld targets.
    :returns: the grants, canonical and deduplicated, with their sources.
    """
    withheld = set(policy.withheld_roots)
    named = [
        grant
        for grant in (*policy.extra_grants, *default_grants())
        if grant.target not in withheld and canonical_target(grant) not in withheld
    ]
    return _dedupe(named)


def named_grants(policy: SandboxPolicy) -> list[RootGrant]:
    """The grants a dialect may actually **act on**: :func:`stored_grants` with the enforcement check.

    This is where a kind no dialect on this platform implements becomes loud.
    Raising is the deliberate choice of three:

    * silently dropping the grant would let the run report a boundary it does not
      have (the failure requirement 5 names);
    * passing a registry key to a path consumer would grant a *different* object
      somewhere on the filesystem — a widening nobody asked for;
    * so the derivation refuses, and each caller turns that into the loudest
      answer it owns: the fence returns the sentence as its refusal, a profile
      builder lets it propagate and the command fails closed.

    :param policy: the policy to read.
    :returns: the enforceable grants.
    :raises GrantKindNotEnforced: when a stored grant's kind is not in
        :func:`~emrg.sandbox.grants.enforced_kinds` for this platform.
    """
    grants = stored_grants(policy)
    for grant in grants:
        if enforcement_refusal(grant.kind, grant.target) is not None:
            raise GrantKindNotEnforced(grant)
    return grants


def _mode_grants(policy: SandboxPolicy) -> list[RootGrant]:
    """The grants the *mode* derives, before any host-named or default grant.

    The workspace root and the two temp sources, each typed from the disk once
    here (:meth:`~emrg.sandbox.grants.RootGrant.from_path`) rather than asked
    again by every dialect.

    :param policy: the policy to derive from.
    :returns: the mode's grants, uncanonicalized and not yet deduplicated.
    """
    spellings = [policy.workspace_root, *_host_temp_spellings(), *_platform_temp_sources()]
    return [RootGrant.from_path(spelling) for spelling in spellings]


def granted_grants(policy: SandboxPolicy) -> list[RootGrant]:
    """Every grant one confined execution may write under, typed.

    The **one** derivation: the mode's own sources, then the session's and the
    manifest's (:func:`named_grants`). The Seatbelt profile, the in-process
    fence, the bubblewrap mounts and the Windows ACL path all read this, so "the
    shell can write where ``write`` cannot" cannot arise between them.

    The derivation is **total** over a temp source this host cannot report: it is
    absent from the list, never fatal (issue #1561,
    :func:`_platform_temp_sources`). It is *not* total over a grant whose kind
    this platform cannot enforce — that raises (:func:`named_grants`).

    ``read-only`` (and ``danger-full-access``, which never asks) grant nothing:
    the early return below is what makes requirement 4 of issue #2492 true by
    construction rather than by a check — a default manifest cannot widen a tier
    that grants none, because this function never consults one for that tier.

    :param policy: the file-effect policy to derive the grant list from.
    :returns: the canonical, deduplicated grants; empty for every mode but
        ``workspace-write``.
    """
    if policy.mode != "workspace-write":
        return []
    return _composed(policy, named_grants)


def _composed(
    policy: SandboxPolicy, named: Callable[[SandboxPolicy], list[RootGrant]]
) -> list[RootGrant]:
    """One composition of a policy's grants: the mode's sources, then the named ones.

    The two named-list policies differ in exactly one respect — whether the
    enforcement table is consulted (:func:`named_grants` asks it,
    :func:`stored_grants` does not) — and both reach this same composition, so
    the readers of either cannot disagree about which grants exist: "the shell
    can write where ``write`` cannot" needs one derivation, not one call site.

    ``read-only`` (and ``danger-full-access``, which never asks) grant nothing:
    the early return is what makes requirement 4 of issue #2492 true by
    construction rather than by a check — a default manifest cannot widen a tier
    that grants none, because this function never consults one for that tier.

    :param policy: the file-effect policy to derive the grant list from.
    :param named: which named-list policy to append — the enforcement-checked
        one, or the display one the path projection reads.
    :returns: the canonical, deduplicated grants; empty for every mode but
        ``workspace-write``.
    """
    if policy.mode != "workspace-write":
        return []
    return _dedupe([*_mode_grants(policy), *named(policy)])


def writable_roots(policy: SandboxPolicy) -> list[str]:
    """The **paths** a policy grants — the projection path-readers use.

    A derived view, not a second allow-list: it is built by the same
    :func:`_composed` composition :func:`granted_grants` reads, and applies one
    filter to it, so it can never disagree with that derivation. It exists
    because several readers need nothing but paths — the fence's containment
    test, the confined cache relocation, and the providers' own spellings — and
    because a registry grant has no path to contribute, so a path-only reader
    must not be handed one.

    **It does not ask the enforcement table**, and that is the point of the
    split. Whether this platform can enforce a registry key is a question about
    enforcement; a key contributes no path either way. Asking it here is what
    once made this projection's own guarantee unmeasurable: while no platform
    enforced a key the projection refused before it could be asked, and the day
    one did — Windows, PR #2498 — the refusal vanished and so did the "no key
    among the paths" claim, leaving a green reading about nothing. The loud
    answer to a grant this platform cannot enforce is :func:`named_grants`'s,
    and every reader that *acts* on grants reads that one: the profile builders
    directly, and the fence by asking for the refusal itself.

    ``read-only`` still answers ``[]``, which is the property the host's rule
    rests on: at a tier that grants nothing, nothing is writable however the
    session or the manifest is configured.

    :param policy: the file-effect policy to derive the allow-list from.
    :returns: the canonical writable paths.
    """
    return [
        grant.target
        for grant in _composed(policy, stored_grants)
        if grant.kind != GRANT_REGISTRY
    ]


#: The shared POSIX temp root — one spelling, named once, read through
#: :func:`_host_temp_spellings` so it can be withheld as the ambient input it is.
_HOST_TEMP = "/tmp"


def _host_temp_spellings() -> list[str]:
    """The host's shared temp spelling — the one source that is not the probe.

    ``/tmp`` and ``tempfile.gettempdir()`` are two sources, not two names for
    one: the shared POSIX temp root is what a bare ``mkstemp`` writes to, while
    ``gettempdir()`` follows ``TMPDIR`` and is per-user (on darwin they are not
    even on the same filesystem).  Both are granted, and the docstring above
    says so.

    They are separated here because each is an **ambient** input, and a test
    that pins one while the other stays ambient is the defect class
    ``tests/test_windows_path_tokens.py`` recorded.  Measured: with only
    ``tempfile.gettempdir`` patched, ``test_write_workspace_write_blocks_outside_workspace``
    wrote the sibling it expected to be refused on the ubuntu leg (CI run
    36432808100) — pytest's temp base is ``/tmp/...`` there and
    ``/var/folders/...`` here, so this source granted exactly the target the
    test called "outside".  A literal in the caller's list could not be withheld.

    :returns: the spellings to grant — one, and the same one as before this
        function existed: the behaviour is unchanged, the input is now
        addressable.
    """
    return [_HOST_TEMP]


def _platform_temp_sources() -> list[str]:
    """The platform temp areas, or none when this host reports no usable one.

    ``tempfile.gettempdir()`` is a **probe**, not a name: CPython creates a file
    to find a writable candidate and raises ``FileNotFoundError`` when none of
    them is (measured 2026-09-24, issue #1561: reachable for real whenever
    ``TMPDIR``/``TEMP``/``TMP`` name nothing and ``/tmp``, ``/var/tmp``,
    ``/usr/tmp`` and the process cwd are all unwritable — the state of a process
    confined at ``read-only``).

    The blueprint this module ports calls Node's ``os.tmpdir()``
    (``roots.ts:52-55``), which is total — it falls back rather than raising — so
    the port has to be total too. The only way to be total without inventing a
    root is to grant the sources that resolve; ``canonical_path``'s own rule
    ("inventing a fallback would grant a path the caller never named") is why a
    fallback spelling is not substituted here.

    Both spellings are derived from the *one* probe result, so the parent
    normalization below cannot re-probe and fail separately.

    :returns: the temp spellings to grant (usually one, none on a host with no
        usable temp area).
    """
    try:
        temp_dir = tempfile.gettempdir()
    except OSError:
        return []
    return [temp_dir, *_temp_parent_spellings(temp_dir)]


def _temp_parent_spellings(temp_dir: str) -> list[str]:
    r"""The ``Temp`` parent spelling Windows reports both ways (issue #1093).

    ``tempfile.gettempdir()`` may return ``C:\...\AppData\Local\Temp\2`` (8.3
    short name plus a ``\2`` suffix) while helpers and scripts are written to
    the plain ``...\Temp\`` root.  When the temp dir sits under a ``Temp``-named
    parent, that parent is trusted too, so both spellings are allowed.  Never
    fires on POSIX, where the parent of ``/tmp``/``$TMPDIR`` is not named
    ``Temp``.

    :param temp_dir: the value ``tempfile.gettempdir()`` returned.
    :returns: extra spellings to grant (usually none).
    """
    parent = os.path.dirname(temp_dir)
    if os.path.basename(parent).lower() == "temp":
        return [parent]
    return []


class ExtraRootVerdict(NamedTuple):
    """What the daemon learns from one ``/sandbox add|remove`` path.

    Three outcomes, and they are three because the host is owed three different
    sentences: the path was **refused** (a rule forbids it, and the refusal names
    the rule), it is **already covered** (nothing to do, and the notice names the
    root that covers it), or it is **stored** (accepted, nothing to say).
    Collapsing refusal and notice into one boolean would make "I will not" and
    "you already have it" the same message, which is exactly the confusion
    requirement 6 of the rant exists to prevent.

    ``store`` is what tells the caller which of the last two happened, and it is
    a field rather than something inferred from ``notice`` because the two do not
    coincide: a root named at ``read-only`` is **stored and carries a notice**
    (it is on the session's list from now on, and takes effect when the tier
    rises), while one already covered is **not stored and carries one too**.
    Reading ``notice is None`` as "store it" stored nothing in the first case
    while the sentence the host read said "stored for this session" — measured
    2026-10-09 on head ``4eb69bab``, where the tier flip the sentence advised
    left the list empty.
    """

    #: The canonical path the verdict is about — empty only when no path was given.
    canonical: str
    #: Why the host may not name this root. ``None`` means it was not refused.
    refusal: str | None = None
    #: What to tell the host when the outcome is more than a plain acceptance.
    notice: str | None = None
    #: Whether the caller should add ``canonical`` to the session's list. False
    #: only for a path already covered — a refusal never reaches this field.
    store: bool = True
    #: The **typed** grant to store, when the verdict accepted one (issue #2492).
    #: ``None`` for a refusal and for "already covered": in both cases the caller
    #: stores nothing, and a grant here would be a second way to decide that.
    #: The kind is decided at this boundary — from the disk for a path, from the
    #: host's own word for a registry key — and never again by a dialect.
    grant: "RootGrant | None" = None


def _absolute_root_spelling(path: str) -> tuple[str, str | None]:
    """Rules 1–2: a path was named, and it is absolute (or ``~``-rooted).

    These two are about *reading* a path rather than granting one, which is why
    both ``/sandbox add`` and ``/sandbox remove`` apply them and why they live
    here instead of inside the add-only judge. Every other rule in
    :func:`judge_extra_root` answers "may this path become writable", and a
    removal is not asking that (see :func:`judge_root_removal`).

    :param path: the path as the host typed it.
    :returns: ``(canonical, None)`` when it identifies a root, else
        ``(spelling, refusal)``.
    """
    if not path or not path.strip():
        return "", "no path given — name the file or directory"
    expanded = os.path.expanduser(path.strip())
    # `os.path.isabs` is the *platform's* answer, and on Windows it calls every
    # drive-less rooted spelling relative — `/`, `/tmp/x` — which is not what it
    # means here: such a path does not resolve under the cwd, so the sandbox has
    # to read it as absolute (the same rule `emrg/tools/file_policy.is_absolute_path`
    # states for a *target*, and the reason it exists as a helper at all). One
    # spelling is deliberately still refused off POSIX: a leading backslash, which
    # on POSIX is an ordinary filename character and would resolve against the
    # daemon's own cwd — the exact accident this rule is here to prevent.
    # Measured: CI run 37878525434, where the platform-only test turned a host's
    # `/` into "not an absolute path" instead of the root refusal below.
    if not os.path.isabs(expanded) and not expanded.startswith("/"):
        return expanded, (
            f"{path!r} is not an absolute path — name it absolutely or start it "
            "with `~`, so the root named and the root shown are the same one"
        )
    return canonical_path(expanded), None


def judge_registry_key(
    key: str, policy: SandboxPolicy, platform: str | None = None
) -> ExtraRootVerdict:
    """Judge one host-named registry key — the refusal, the duplicate, or the grant.

    A third judge beside :func:`judge_extra_root` and :func:`judge_root_removal`,
    for the reason those two are separate functions: a key answers a different
    question.  None of the six granting rules has a meaning for one — a key is not
    the filesystem root, is not the home directory, has no protected daemon state
    file beneath it and nothing to ``stat`` — while two questions are its own:
    *is the spelling one key* and *can this platform apply it at all*.

    **Validation and enforcement are kept apart here**, in that order.  The
    spelling is checked first, because a malformed key is malformed on every
    platform, and it is checked with
    :func:`~emrg.sandbox.win32.registry.split_key` — the parser the DACL applier
    itself uses — so a key this judge accepts is one that path can address, and no
    second parser with a second wording exists.  Only then does the platform
    answer, through :func:`~emrg.sandbox.grants.enforcement_refusal`, which stays
    the one producer of that sentence.

    The stored target is the **canonical** spelling
    (:func:`~emrg.sandbox.win32.registry.canonical_key_spelling`), because a
    capability SID is derived from the spelling: ``HKCU`` and
    ``HKEY_CURRENT_USER`` are both accepted and would otherwise be two identities
    on one key.  Naming a key twice is therefore recognised by a case-insensitive
    comparison, since the registry namespace is case-insensitive.

    That comparison reads :func:`stored_grants` rather than
    :func:`granted_grants`, and the difference is the tier rather than the
    enforcement: the second reader answers ``[]`` for every mode but
    ``workspace-write``, so a key named at ``read-only`` would not be found and
    would be stored twice — the same defect the path judge's own second loop
    exists to prevent.  A key is granted as itself, with no containment test to
    run, so "is it on the list" is the whole question and the mode-independent
    list is the whole answer.

    :param key: the key as the host typed it.
    :param policy: the session's policy **including its stored grants**, so a key
        already granted is reported rather than stored twice.
    :param platform: the platform to judge; defaults to ``sys.platform``.  It is a
        parameter for the same reason :func:`enforcement_refusal` has one — the
        spelling rules are ordinary Python, so a POSIX leg holds them to their
        rule without a Windows host.
    :returns: the verdict — the canonical spelling, and either a refusal or the
        typed grant to store.
    """
    from emrg.sandbox.win32.registry import RegistryKeyError, canonical_key_spelling

    try:
        canonical = canonical_key_spelling(key.strip())
    except RegistryKeyError as exc:
        return ExtraRootVerdict("", refusal=str(exc))
    refusal = enforcement_refusal(GRANT_REGISTRY, canonical, platform)
    if refusal is not None:
        return ExtraRootVerdict("", refusal=refusal)
    for grant in stored_grants(policy):
        if grant.kind == GRANT_REGISTRY and grant.target.lower() == canonical.lower():
            return ExtraRootVerdict(canonical, notice=(
                f"{canonical!r} is already granted for this session — a registry namespace "
                "is case-insensitive and both accepted spellings of a root name one key, so "
                "this is the grant you already have"
            ), store=False)
    return ExtraRootVerdict(canonical, grant=RootGrant(GRANT_REGISTRY, canonical))


def judge_extra_root(
    path: str, policy: SandboxPolicy, kind: str | None = None
) -> ExtraRootVerdict:
    """Judge one host-named writable grant — refuse it, or report it already covered.

    This is the **writer-side** half of the host's ``/sandbox add`` (rant
    2026-10-09T09:43:39 §5–§6): the daemon owns the command, and this function is
    where a path is turned into the canonical spelling the enforcement layer will
    compare, or refused with a sentence a person can act on. The refusals live
    next to :func:`writable_roots` rather than in the client because a client is
    an entry point and a display — the same reason the tier is the daemon's.

    **This function answers the add question only.** Its rules 3–6 are about
    granting, and applying them to a removal is the defect
    :func:`judge_root_removal` exists to undo; the two calls are separate
    functions rather than one function with an ``op`` flag so that a caller
    cannot inherit the wrong rule set by leaving a default alone.

    Six rules, and each is here rather than at the fence for one reason: a
    refusal at the fence is a *silent* denial of a write the host thought they
    had granted, while these are answered while the host is still looking at the
    command they typed.

    1. **No path, no root** — an empty argument is not a request.
    2. **Absolute (or ``~``-rooted)** — a relative spelling would resolve against
       whatever directory the provider happened to run in, so the path granted
       and the path displayed could differ. The policy layer raises on a relative
       root for the same reason; answering here means the person sees a sentence
       instead of an exception.
    3. **Not the filesystem root** — ``/`` is exactly the reach the tier
       ``danger-full-access`` already spells, and it should be chosen as a tier,
       visibly, rather than assembled out of a file grant.
    4. **Not the home directory** — same shape, one level in. The grant is meant
       to be the size of what was named.
    5. **Never over a protected daemon state file** (:func:`~emrg.tools.file_policy.protected_paths`)
       — those five are unreachable today by *geometry* (none sits inside a
       session workspace or a temp area). A host-named root makes them reachable
       as an ordinary write, so the guard has to be explicit rather than
       incidental. The kernel profiles would grant them; the in-process fence
       would refuse (``fence.file_refusal`` is deliberately the stricter of the
       two) — that split is the "user-visible strange" the rant names, and this
       rule is what keeps it unreachable.
    6. **The path must exist** — a grant is spelled differently for a directory
       (``subpath``) and a file (``literal``), and for a name that is not there
       yet that choice has to be guessed. Refusing it means every stored root
       has a known kind, which is also what keeps a single-file ``--bind`` on
       Linux from binding nothing.

    A path that survives all six but is **already writable** is not refused: it is
    reported, and ``store`` is False so the caller adds nothing. Naming the
    workspace, a temp area, or a root added a moment ago changes nothing, and
    saying so is the requirement ("明确回报，不静默重复").

    Surviving the six at ``read-only`` is a third outcome again: the root **is**
    stored (``store`` is True), and the notice says when it will take effect.
    The host's list follows the session and the tier decides what has effect, so
    a root named now is not lost by the tier being low — it was the other way
    round before this, and the sentence promised what the code did not do.

    :param path: the path as the host typed it.
    :param policy: the session's policy **including its stored extra grants and
        the manifest's defaults**, so that re-adding one is recognised as already
        covered.
    :param kind: the kind the host asked for, when they named one
        (``/sandbox add --registry``); ``None`` means a path, whose kind is read
        from the disk once here (:meth:`~emrg.sandbox.grants.RootGrant.from_path`)
        rather than re-decided by every dialect at profile-build time.
    :returns: the verdict — the canonical spelling, the typed grant to store, and
        any refusal or notice.
    """
    # Imported here rather than at module scope: ``emrg.session`` imports this
    # module for ``canonical_path``, and that happens before the tool package is
    # on the import path. The list still has one home; this is a reference.
    from emrg.tools.file_policy import protected_paths

    if kind is not None and kind not in (GRANT_DIRECTORY, GRANT_FILE, GRANT_REGISTRY):
        return ExtraRootVerdict("", refusal=(
            f"unknown grant kind {kind!r} — expected one of {', '.join(GRANT_KINDS)}"
        ))
    if kind == GRANT_REGISTRY:
        return judge_registry_key(path, policy)

    canonical, refusal = _absolute_root_spelling(path)
    if refusal:
        return ExtraRootVerdict(canonical, refusal=refusal)
    # A filesystem root is a path that is its own parent — the definition, and
    # the reason this is not written `canonical == os.sep`: a Windows root is
    # `C:\`, which is not `os.sep`, and a host who types `/` there means the root
    # too. The spelled form is checked alongside it because `realpath` may map a
    # bare `/` somewhere other than a root on a platform whose cwd is on a drive;
    # the two spellings one idea, so neither can let a whole filesystem through.
    # `expanduser` leaves `/` alone, so the stripped spelling is its expanded one.
    if path.strip() == "/" or canonical == os.path.dirname(canonical):
        return ExtraRootVerdict(canonical, refusal=(
            f"{path!r} is the filesystem root — that reach is the tier "
            "danger-full-access, chosen as a tier rather than assembled from a "
            "narrower grant"
        ))
    if canonical == canonical_path(os.path.expanduser("~")):
        return ExtraRootVerdict(canonical, refusal=(
            f"{path!r} is the home directory — name the file or directory you "
            "mean, so the grant is the size you asked for"
        ))
    for protected in protected_paths():
        if canonical == protected or protected.startswith(canonical.rstrip(os.sep) + os.sep):
            return ExtraRootVerdict(canonical, refusal=(
                f"{path!r} would grant {protected!r}, a protected daemon state "
                "file — those are refused at every tier"
            ))
    if not os.path.exists(canonical):
        return ExtraRootVerdict(canonical, refusal=(
            f"{path!r} does not exist — a root names what is there, because a "
            "directory is granted as a tree and a file as itself"
        ))
    for grant in granted_grants(policy):
        if grant.kind == GRANT_REGISTRY:  # no containment test exists for a key
            continue
        root = grant.target
        if canonical == root or canonical.startswith(root.rstrip(os.sep) + os.sep):
            return ExtraRootVerdict(canonical, store=False, notice=(
                f"{root!r} already grants this path under the session's tier "
                f"({grant.source}) — nothing to add"
            ))
    # The grants the session names itself are consulted separately, because
    # ``granted_grants`` only carries them under ``workspace-write``: at
    # ``read-only`` that derivation is empty by definition, so the loop above
    # cannot see the grant stored a moment ago and a second ``add`` of it would
    # be stored twice. The manifest's entries are *not* consulted here: those are
    # withdrawn rather than removed, and naming one again at a tier that grants
    # nothing should store it as the host's own (the add path drops it from the
    # withheld list for exactly that reason).
    stored = [
        canonical_target(grant) for grant in policy.extra_grants
        if grant.kind != GRANT_REGISTRY
    ]
    if any(canonical == root or canonical.startswith(root.rstrip(os.sep) + os.sep)
           for root in stored):
        return ExtraRootVerdict(canonical, store=False, notice=(
            f"{canonical!r} is already one of this session's roots — nothing to add"
        ))
    if policy.mode == "read-only":
        # Stored, not merely described: the sentence says the root is on the list
        # from now on and names what makes it take effect. ``store`` is what says
        # so — reading ``notice`` as "do not store" made this branch keep nothing
        # while telling the host it had, so the tier flip it advised left the
        # list empty (measured 2026-10-09 on head ``4eb69bab``). Storing here is
        # harmless by construction: ``granted_grants`` returns ``[]`` for this
        # mode, so a stored grant can never widen a tier that grants none.
        return ExtraRootVerdict(
            canonical,
            notice=(
                "stored for this session, but the tier read-only grants no writable "
                "root — it takes effect when the tier becomes workspace-write"
            ),
            grant=RootGrant.from_path(canonical),
        )
    return ExtraRootVerdict(canonical, grant=RootGrant.from_path(canonical))


def judge_root_removal(
    path: str, policy: SandboxPolicy, kind: str | None = None
) -> ExtraRootVerdict:
    """Judge one ``/sandbox remove`` — the canonical spelling, or why it cannot be read.

    Removal asks a different question from :func:`judge_extra_root`, and this is
    a separate function so the two cannot be confused again: every rule there is
    about *granting* — the filesystem root, the home directory, the protected
    state files, and "the path must exist" (which exists because a directory is
    granted as ``subpath`` and a file as ``literal``, a choice a removal never
    makes). None of them has a meaning for a withdrawal. Running them anyway
    made a root whose path was deleted impossible to withdraw — measured
    2026-10-09 on head ``4eb69bab``: the command answered "does not exist" and
    left the root stored, and since ``writable_roots`` carries a stored spelling
    whether or not it exists, the grant came back live the moment the path did.

    Two rules remain, both from :func:`_absolute_root_spelling`, and both are
    about reading the path: one must be named, and it must be absolute. A refusal
    here therefore means "this is not a path I can read", never "the policy would
    not grant this" — a path that is simply not on the list is answered with the
    list as it stands, which is what "removal is idempotent" means.

    **A key is withdrawn by its own spelling rule and no other.** ``kind`` is the
    kind the host stated (``/sandbox remove --registry``); when it names a
    registry key the target is canonicalized the way the grant was stored, and
    none of the path rules run — there is no absolute path to require and no
    disk to stat. A grant that can be added must be withdrawable, so this branch
    can refuse only an unreadable *spelling*, never the policy.

    :param path: the path, or key, as the host typed it.
    :param policy: unused by the path rules — carried so the two judges are called
        alike, and so a later removal rule has somewhere to read the session's
        state from.
    :param kind: the kind the host stated, or ``None`` for a path.
    :returns: the verdict — canonical spelling, and a refusal only for an argument
        that could not be read.
    """
    if kind == GRANT_REGISTRY:
        from emrg.sandbox.win32.registry import RegistryKeyError, canonical_key_spelling

        try:
            return ExtraRootVerdict(canonical_key_spelling(path.strip()))
        except RegistryKeyError as exc:
            return ExtraRootVerdict("", refusal=str(exc))
    canonical, refusal = _absolute_root_spelling(path)
    if refusal:
        return ExtraRootVerdict(canonical, refusal=refusal)
    return ExtraRootVerdict(canonical)
