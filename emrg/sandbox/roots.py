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
from typing import TYPE_CHECKING

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
      it stays deleted belongs to the tests, not to a spare field here.
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
    spellings = [policy.workspace_root, "/tmp"]
    spellings.extend(_platform_temp_sources())
    out: list[str] = []
    seen: set[str] = set()
    for spelling in spellings:
        canonical = canonical_path(spelling)
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


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
