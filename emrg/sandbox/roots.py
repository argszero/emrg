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
from typing import TYPE_CHECKING, NamedTuple

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


def writable_roots(policy: SandboxPolicy) -> list[str]:
    """The roots one confined execution may WRITE under.

    The mode's meaning as a canonical, deduplicated allow-list — ``read-only``
    (and ``danger-full-access``, which never asks) allow nothing;
    ``workspace-write`` allows the policy's workspace root, the host ``/tmp``,
    and the per-user platform temp dir (``tempfile.gettempdir()`` — the real
    temp area for the ``mkstemp`` family; omitting it would deny what the mode
    promises).

    The derivation is **total**: a temp source this host cannot report is
    *absent* from the list, never fatal (issue #1561, ``_platform_temp_sources``).
    The reading this deliberately does not take: treating a policy that cannot be
    fully computed as one that cannot be enforced, and failing at the seam. That
    reading belongs to ``contract.confine``, whose
    :class:`~emrg.sandbox.contract.SandboxUnavailableError` means "no backend on
    this platform can enforce the mode" — a missing temp *directory* is not that,
    and reporting it there would tell the reader to install bubblewrap while
    turning a host that can still confine to its workspace root into one where no
    command runs at all.

    Two things this list deliberately does NOT contain:

    * an extra deployer root.  The blueprint's allow-list is these three
      sources and nothing else (``roots.ts:52-55``); EMRG's former
      ``_trusted_write_zones()`` (``~/.emrg/evolution/.emrg/``) is deleted by
      host decision D5, not carried over under another name, so the check that
      it stays deleted belongs to the tests, not to a spare field here.  The
      host-named roots this module now *does* carry are a different mechanism
      and not that one revived: D5's root was a deployment constant nobody
      chose and nothing could withdraw, while these are named per session by
      the host, visible in ``/sandbox list`` and removable in one command.  The
      name stays dead either way — the difference is in who can name a root,
      which is why ``policy.extra_roots`` is a session's, not a config's.
    * a fourth source on Windows, beyond the ``Temp\\<suffix>`` normalization
      below — which is not a root of its own but the *parent* spelling of the
      one ``gettempdir()`` already returned (issue #1093).

    :param policy: the file-effect policy to derive the allow-list from.
    :returns: the canonical writable roots; empty for every mode but
        ``workspace-write`` (``read-only`` allows nothing by definition, and
        ``danger-full-access`` never asks).
    """
    if policy.mode != "workspace-write":
        return []
    spellings = [policy.workspace_root, *_host_temp_spellings()]
    spellings.extend(_platform_temp_sources())
    # The host-named roots join here rather than in a provider, and that
    # placement is the requirement (rant 2026-10-09T09:43:39 §3): the Seatbelt
    # profile, the bwrap mounts and the in-process fence all read this one list,
    # so an extra root cannot end up writable for a spawned command and refused
    # for `write` — the asymmetry this module exists to prevent.  A provider that
    # carried its own extra allow-list would be a second home for the same fact.
    #
    # They are appended *after* the mode's own sources and are still subject to
    # the same dedup below, so naming an already-granted root (the workspace, or
    # a temp area) changes nothing — the caller is told that at the writer, where
    # it can be reported, rather than silently repeated here.
    #
    # The `read-only` return above is what makes a stored extra root harmless at
    # a tier that grants nothing: the list is a property of the *mode*, and the
    # roots a session happens to carry never widen a mode that grants none.
    spellings.extend(policy.extra_roots)
    out: list[str] = []
    seen: set[str] = set()
    for spelling in spellings:
        canonical = canonical_path(spelling)
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


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


def judge_extra_root(path: str, policy: SandboxPolicy) -> ExtraRootVerdict:
    """Judge one host-named writable root — refuse it, or report it already covered.

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
    :param policy: the session's policy **including its stored extra roots**, so
        that re-adding one is recognised as already covered.
    :returns: the verdict — the canonical spelling, whether to store it, and any
        refusal or notice.
    """
    # Imported here rather than at module scope: ``emrg.session`` imports this
    # module for ``canonical_path``, and that happens before the tool package is
    # on the import path. The list still has one home; this is a reference.
    from emrg.tools.file_policy import protected_paths

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
    for root in writable_roots(policy):
        if canonical == root or canonical.startswith(root.rstrip(os.sep) + os.sep):
            return ExtraRootVerdict(canonical, store=False, notice=(
                f"{root!r} already grants this path under the session's tier — "
                "nothing to add"
            ))
    # The session's own list is consulted separately, because ``writable_roots``
    # only carries it under ``workspace-write``: at ``read-only`` that derivation
    # is empty by definition, so the loop above cannot see the root stored a
    # moment ago and a second ``add`` of it would be stored twice.
    stored = [canonical_path(extra) for extra in policy.extra_roots]
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
        # harmless by construction: ``writable_roots`` returns ``[]`` for this
        # mode, so a stored root can never widen a tier that grants none.
        return ExtraRootVerdict(canonical, notice=(
            "stored for this session, but the tier read-only grants no writable "
            "root — it takes effect when the tier becomes workspace-write"
        ))
    return ExtraRootVerdict(canonical)


def judge_root_removal(path: str, policy: SandboxPolicy) -> ExtraRootVerdict:
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

    :param path: the path as the host typed it.
    :param policy: unused — carried so the two judges are called alike, and so a
        later removal rule has somewhere to read the session's state from.
    :returns: the verdict — canonical spelling, and a refusal only for a path
        that could not be read.
    """
    canonical, refusal = _absolute_root_spelling(path)
    if refusal:
        return ExtraRootVerdict(canonical, refusal=refusal)
    return ExtraRootVerdict(canonical)
