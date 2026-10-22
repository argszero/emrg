"""The typed writable-grant model every enforcement dialect reads.

Issue #2492 (rant 2026-10-22T20:12:00). The writable surface used to be a list of
**paths** and nothing else, so two things could not be said at all:

* an object that is not a path. ``lark-cli`` keeps its renewing token in
  ``HKCU\\Software\\LarkCli\\keychain`` on Windows, which no path operation
  reaches, so a confined task there can read a message but cannot renew the
  identity it read it with;
* a grant a host does not have to type again in every session. Named-by-hand
  roots are per session (``policy.extra_roots``), so a tool that keeps state
  outside the workspace needs the same ``/sandbox add`` in every session that
  runs it.

A grant is therefore a ``(kind, target, source)`` triple.

**``kind``** says what kind of object is being made writable, and it is written
down once — by whoever names the grant — instead of being asked of the disk at
every call. That is requirement 1 of the issue, and it fixes a defect as it goes:
the Seatbelt profile distinguishes a directory (``subpath``) from a file
(``literal``), and it used to decide that with ``os.path.isdir`` at
profile-build time, so a name the host granted as one file silently widened to
the whole tree beneath it the day that name became a directory. Recorded here,
the grant keeps meaning what was asked for however the disk changes.

**``target``** is what the grant names: a path for the two filesystem kinds, a
registry key spelling for ``registry``. The field is not called ``path`` because
a registry key is not one, and no path operation — canonicalization, containment,
``isdir`` — applies to it.

**``source``** says who named it: ``session`` for a root the host typed with
``/sandbox add``, ``default`` for an entry of :data:`DEFAULT_MANIFEST`. The
distinction is requirement 3: a default that could be neither shown nor withdrawn
would be the deployment constant host decision D5 deleted, back under a new name
(``emrg/sandbox/roots.py`` records that decision and this module is the boundary
between the two mechanisms).

Which kinds a platform can *enforce* is a property of the platform and not of the
model, and that is what lets the model accept a kind it cannot honour without
lying about it. :data:`ENFORCED_KINDS` names what has an implementation;
:func:`enforcement_refusal` produces the one sentence a caller shows when a kind
is not there, and every dialect refuses such a grant **by name** rather than
dropping it — dropping it would report a boundary the run does not have, and
passing it on as a path would grant something else entirely. Today ``registry``
is in no platform's set: the model carries the kind, the writer refuses it with
that sentence, and the enforcement itself — a DACL on a registry key, in
``emrg/sandbox/win32/`` — is Stage 2 of issue #2492.
"""

from __future__ import annotations

import os
import sys
from typing import NamedTuple

#: The kinds of object the writable surface can name. ``directory`` and ``file``
#: are the two a filesystem can enforce; ``registry`` is a key.
GRANT_DIRECTORY = "directory"
GRANT_FILE = "file"
GRANT_REGISTRY = "registry"
GRANT_KINDS: tuple[str, ...] = (GRANT_DIRECTORY, GRANT_FILE, GRANT_REGISTRY)

#: Who named a grant. ``session`` is a root the host typed; ``default`` is one
#: this module's manifest supplies so a host who types nothing still has the
#: state their tools need writable.
SOURCE_SESSION = "session"
SOURCE_DEFAULT = "default"
GRANT_SOURCES: tuple[str, ...] = (SOURCE_SESSION, SOURCE_DEFAULT)

#: The kinds **this platform's dialects implement**, per platform family.
#:
#: ``registry`` is deliberately in none of them: the kind is modelled and
#: :func:`enforcement_refusal` names the reason, but the code that applies a DACL
#: to a key does not exist yet (issue #2492 Stage 2). A set rather than a comment
#: because two callers read it — the refusal sentence, and the guard that keeps
#: :data:`DEFAULT_MANIFEST` from claiming a kind its platform cannot honour.
ENFORCED_KINDS: dict[str, tuple[str, ...]] = {
    "darwin": (GRANT_DIRECTORY, GRANT_FILE),
    "linux": (GRANT_DIRECTORY, GRANT_FILE),
    "win32": (GRANT_DIRECTORY, GRANT_FILE),
    "other": (GRANT_DIRECTORY, GRANT_FILE),
}

#: The default manifest: what is writable in every session without a command.
#:
#: Host-requested (rant 2026-10-22T20:12:00), and laid out per platform because
#: the locations genuinely differ. ``lark-cli`` renews its user identity by
#: taking a lock and writing a token; these are the directories it uses.
#:
#: Two properties this table owes, both asserted by tests rather than trusted:
#:
#: * **a row's kind is one its platform can enforce** — :data:`ENFORCED_KINDS` is
#:   the reader, so the Windows registry keys the rant also names are absent
#:   here until Stage 2 can enforce them. A manifest promising a grant the run
#:   does not have would be the lying boundary this module refuses elsewhere;
#: * **the table is data, not a code path** — an entry whose directory does not
#:   exist on this host is simply absent from the derivation
#:   (:func:`default_grants`), so a deployment that has never run ``lark-cli``
#:   is not an error and not a grant.
#:
#: ``%APPDATA%`` is expanded with :func:`os.path.expandvars`, which understands
#: the ``%NAME%`` form on Windows only; on POSIX the two darwin/linux rows carry
#: ``~`` alone and the Windows row is never read.
DEFAULT_MANIFEST: dict[str, tuple[tuple[str, str], ...]] = {
    "darwin": (
        (GRANT_DIRECTORY, "~/Library/Application Support/lark-cli"),
        (GRANT_DIRECTORY, "~/.lark-cli"),
    ),
    "linux": (
        (GRANT_DIRECTORY, "~/.local/share/lark-cli"),
        (GRANT_DIRECTORY, "~/.lark-cli"),
    ),
    "win32": (
        (GRANT_DIRECTORY, "~/.lark-cli"),
        (GRANT_DIRECTORY, "%APPDATA%/Microsoft/Protect"),
    ),
}


def platform_family(platform: str | None = None) -> str:
    """The family key :data:`ENFORCED_KINDS` and :data:`DEFAULT_MANIFEST` use.

    Read from ``sys.platform`` unless a caller names one, so a test can measure a
    platform it is not running on without inventing an environment.

    :param platform: the platform string to classify; defaults to ``sys.platform``.
    :returns: ``darwin``, ``linux``, ``win32``, or ``other``.
    """
    name = sys.platform if platform is None else platform
    for family in ("darwin", "linux", "win32"):
        if name.startswith(family):
            return family
    return "other"


def enforced_kinds(platform: str | None = None) -> tuple[str, ...]:
    """The grant kinds this platform's dialects can enforce.

    :param platform: the platform string to classify; defaults to ``sys.platform``.
    :returns: the enforced kinds, shortest-first by :data:`GRANT_KINDS` order.
    """
    allowed = ENFORCED_KINDS.get(platform_family(platform), ())
    return tuple(kind for kind in GRANT_KINDS if kind in allowed)


def enforcement_refusal(kind: str, target: str = "", platform: str | None = None) -> str | None:
    """The sentence for a kind this platform cannot enforce, or ``None``.

    One producer for two readers — the writer that refuses a host's
    ``/sandbox add --registry`` and the derivation that refuses to hand a dialect
    a grant it could only drop — because two spellings of one refusal is how a
    host comes to read two different explanations of one rule.

    :param kind: the grant kind in question.
    :param target: the grant's target, quoted when there is one.
    :param platform: the platform to judge; defaults to ``sys.platform``.
    :returns: the refusal text, or ``None`` when the kind is enforceable here.
    """
    allowed = enforced_kinds(platform)
    if kind in allowed:
        return None
    # Quoted plainly rather than with `!r`: a registry key is a Windows spelling
    # full of backslashes, and `repr` doubles every one of them, so the sentence a
    # host reads would name a key that looks nothing like the one they typed.
    named = f" '{target}'" if target else ""
    return (
        f"a {kind} grant{named} cannot be enforced on {platform_family(platform)} — this "
        f"platform enforces {', '.join(allowed)}. Dropping it would report a boundary the "
        f"run does not have, and honouring it as a path would grant a different object, so "
        f"it is refused instead (issue #2492 Stage 2 adds {kind} enforcement)"
    )


class GrantKindNotEnforced(RuntimeError):
    """A grant names a kind no dialect on this platform implements.

    Raised by ``emrg/sandbox/roots.py::named_grants`` — the single place every
    dialect's derivation passes through — so that a kind this platform cannot
    enforce is refused **by name** instead of being dropped by one dialect and
    honoured by another. The message is
    :func:`enforcement_refusal`'s, the same sentence the writer shows a host who
    types ``/sandbox add --registry``: one rule, one wording.
    """

    def __init__(self, grant: "RootGrant") -> None:
        self.grant = grant
        self.reason = enforcement_refusal(grant.kind, grant.target) or (
            f"a {grant.kind} grant cannot be enforced on {platform_family()}"
        )
        super().__init__(self.reason)


class RootGrant(NamedTuple):
    """One typed writable grant: what kind of object, which one, and who named it.

    A ``NamedTuple`` rather than a dataclass because it crosses the wire (as a
    plain dict, :meth:`as_wire`) and is logged in refusal messages; being a tuple
    also makes it hashable, so the deduplication in
    ``emrg/sandbox/roots.py`` is a set membership test rather than a loop over
    attributes.
    """

    #: One of :data:`GRANT_KINDS`.
    kind: str
    #: A path for ``directory``/``file``, a key spelling for ``registry``.
    target: str
    #: One of :data:`GRANT_SOURCES`.
    source: str = SOURCE_SESSION

    def as_wire(self) -> dict[str, str]:
        """The grant as a JSON-ready dict, the shape the daemon injects.

        :returns: a three-key mapping a client or a tool can read back with
            :meth:`from_wire`.
        """
        return {"kind": self.kind, "target": self.target, "source": self.source}

    @classmethod
    def from_wire(cls, value: object) -> "RootGrant | None":
        """Read one injected grant, or ``None`` when it is not one.

        Tolerant on purpose, and only in the narrowing direction: the daemon is
        the sole writer of this payload (``_inject_tool_arguments`` replaces any
        value the model put in the call), and a tool that cannot read an entry
        must end up with a **smaller** writable surface, never a larger one. So
        a malformed entry is dropped rather than guessed at, and a kind this
        module does not know is dropped rather than passed on — the enforcement
        refusal is for a grant that was *named*, not for a string that is not a
        grant at all.

        :param value: the wire value: a mapping, or a bare path string for the
            two filesystem kinds (a caller building a policy by hand).
        :returns: the grant, or ``None``.
        """
        if isinstance(value, RootGrant):
            return value
        if isinstance(value, str):
            return cls.from_path(value)
        if not isinstance(value, dict):
            return None
        kind = value.get("kind")
        target = value.get("target")
        if not isinstance(kind, str) or kind not in GRANT_KINDS:
            return None
        if not isinstance(target, str) or not target.strip():
            return None
        source = value.get("source", SOURCE_SESSION)
        if source not in GRANT_SOURCES:
            source = SOURCE_SESSION
        return cls(kind=kind, target=target, source=source)

    @classmethod
    def from_path(cls, target: str, source: str = SOURCE_SESSION) -> "RootGrant":
        """A filesystem grant whose kind is read from the disk **once**, here.

        The kind of a path is a fact about the disk, not a spelling convention,
        so it is measured with ``os.path.isdir`` at the moment the grant is made
        — the daemon's ``/sandbox add`` judge has already required the path to
        exist, and a caller that skipped that judge still gets the conservative
        answer (anything not a directory is granted as the one file it names,
        never as the tree above it).

        :param target: the path the grant names.
        :param source: who named it.
        :returns: the grant.
        """
        try:
            is_dir = os.path.isdir(target)
        except OSError:  # pragma: no cover - isdir swallows its own errors
            is_dir = False
        return cls(kind=GRANT_DIRECTORY if is_dir else GRANT_FILE, target=target, source=source)


def default_grants(platform: str | None = None) -> list[RootGrant]:
    """The manifest's entries for this platform, as grants that really apply here.

    **Total**, in the sense ``emrg/sandbox/roots.py`` uses for the temp sources: a
    manifest entry whose directory this host does not have is *absent* from the
    answer, never an error. ``lark-cli`` may never have run here, and a boundary
    that refused to be computed over a tool nobody installed would take a working
    sandbox down with it.

    The ``source`` is :data:`SOURCE_DEFAULT` for every entry: that is what lets
    a host tell a grant they typed from one the installation supplied, which is
    the whole difference between this table and the deployment constant host
    decision D5 deleted.

    :param platform: the platform to read; defaults to ``sys.platform``.
    :returns: the manifest's grants that exist on this host, in table order.
    """
    family = platform_family(platform)
    out: list[RootGrant] = []
    for kind, spelling in DEFAULT_MANIFEST.get(family, ()):
        target = os.path.expandvars(os.path.expanduser(spelling))
        try:
            exists = os.path.isdir(target) if kind == GRANT_DIRECTORY else os.path.exists(target)
        except OSError:  # pragma: no cover - both calls swallow their own errors
            exists = False
        if not exists:
            continue
        out.append(RootGrant(kind=kind, target=target, source=SOURCE_DEFAULT))
    return out


def manifest_targets(platform: str | None = None) -> list[str]:
    """Every manifest target on this platform, whether or not it exists here.

    The withdrawal path reads this rather than :func:`default_grants`: a host who
    withdraws a default grants nothing by doing so, and the answer they need —
    "that was one of the installation's entries" — does not depend on the
    directory being present at the instant they ask.

    :param platform: the platform to read; defaults to ``sys.platform``.
    :returns: the expanded targets, in table order.
    """
    family = platform_family(platform)
    return [
        os.path.expandvars(os.path.expanduser(spelling))
        for _kind, spelling in DEFAULT_MANIFEST.get(family, ())
    ]
