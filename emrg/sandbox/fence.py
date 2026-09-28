"""The in-process file-effect fence — one answer for one file path.

``emrg/tools/bash_tool_v2.py`` and its PowerShell twin spawn a child and let the
kernel confine it: the Seatbelt/bwrap profile is built from
:func:`emrg.sandbox.roots.writable_roots`, so under ``read-only`` a bash command
can write nowhere at all.  The ``write`` and ``edit`` tools do not spawn
anything — they call ``open()`` inside the daemon, where no kernel boundary can
be applied — so until P7 they answered from their own rules
(``emrg/tools/file_policy.check_read_only_file_write`` /
``check_workspace_write``), and the two sides disagreed in **both** directions
at the same tier:

========================  ==========================  ==================
target                    the file tools              the v2 fence
========================  ==========================  ==================
inside the workspace      BLOCK (``read-only``)       no root granted
outside the workspace     allow (``read-only``)       no root granted
========================  ==========================  ==================

**Host ruling (2026-09-28T21:50, answering issue #1553's decision half): the
two tools share one policy.**  So this module asks the same
:func:`~emrg.sandbox.roots.writable_roots` derivation the profile is built
from, and the answer a file write gets cannot depend on which tool asked it.
Two consequences are written down here rather than discovered later:

* **At ``read-only`` the fence refuses everything**, including the memory root a
  read-only cycle would write its own record into.  ``read-only`` grants nothing
  by definition (``roots.writable_roots`` returns ``[]``), and it is the tier a
  cycle runs at only when the daemon's dirty-tree recovery *errored*
  (``scheduler.TaskHandler._resolve_sandbox``; a recoverable dirty tree is
  pinned and stashed and the cycle keeps its normal tier).  The exit from that
  state is a host turn — which carries no tier and therefore resolves to
  ``policy.DEFAULT_MODE`` = ``danger-full-access``, i.e. stays unconfined, as it
  did before.
* **At ``workspace-write`` the fence grants the workspace root and the platform
  temp areas**, which retires the extra deployer root the file tools used to
  grant aside — the root host decision D5 deleted and then archived
  (issue #1703).  The protected daemon state files (``PROTECTED_FILES``) are
  still refused, by containment: none of them is inside a session's workspace
  root, and a second rule that re-listed them here would be the second allow-list
  this module exists to remove.

Nothing here is an enforcement boundary: an in-process fence can only refuse a
call before it is made.  It is the *policy* half of P7 — the same policy the
kernel-enforced dialects read — and it is what makes the two tools' answers
agree by construction rather than by maintenance.
"""

from __future__ import annotations

import os

from emrg.sandbox.policy import DANGER_FULL_ACCESS, SandboxPolicy
from emrg.sandbox.roots import canonical_path, writable_roots
from emrg.tools.file_policy import protected_paths


def file_refusal(file_path: str, policy: SandboxPolicy) -> str | None:
    """Answer one file target against the policy every dialect shares.

    The question is containment in :func:`writable_roots` and nothing else, so
    the set of paths a ``write``/``edit`` call may touch is the set a spawned
    command may write — one derivation, two readers.

    :param file_path: the target, already joined onto the caller's workspace
        where it was relative (``emrg.tools.file_policy.resolve_file_target``);
        this function canonicalizes it, which is the identity the Seatbelt
        filters and the ACL runner compare as well.
    :param policy: the call's resolved policy — mode, workspace root, session.
    :returns: ``None`` when the target is writable under the policy, otherwise
        the refusal text, naming the tier and the roots it grants.  The text is
        returned rather than raised because every caller renders it to the model
        as the tool result (the shape ``command_scan.command_refusal`` has).
    """
    if policy.mode == DANGER_FULL_ACCESS:
        # The tier that checks nothing still checks nothing (host's rule): a call
        # that carries no tier resolves here, which is what keeps a host session
        # unconfined.
        return None
    target = canonical_path(file_path)
    if target in protected_paths():
        # The host's own daemon state (``~/.emrg/config.toml`` and its four
        # neighbours).  Containment already refuses all five in every deployment
        # this repo runs, because none of them sits inside a session's workspace
        # or a temp area — but "already" is a fact about today's geometry, and a
        # session whose cwd is ``~`` would make the OS fence grant them.  So this
        # is the one place the in-process fence is **stricter** than the kernel
        # profile rather than identical to it: it can refuse a write bash would
        # be allowed to make, never the reverse.  The list stays in
        # ``emrg/tools/file_policy.py`` because the legacy scanner reads it too.
        return f"{policy.mode} sandbox: blocked write to protected daemon file {file_path!r}"
    roots = writable_roots(policy)
    if any(_is_within_root(target, root) for root in roots):
        return None
    granted = (
        ", ".join(repr(root) for root in roots)
        if roots
        else "no root — this tier grants none"
    )
    # The spelling *and* the path it resolves to, when they differ: the property
    # issue #1558 bought is that a judgment names the file it judged, and a
    # relative spelling that climbs out is exactly the case where the two are not
    # the same file. Compared as the canonical identity both sides of the
    # comparison use, so one assertion holds on either platform.
    resolved = f" (resolves to {target!r})" if target != file_path else ""
    return (
        f"{policy.mode} sandbox: blocked file write to {file_path!r}{resolved} — "
        f"outside every root this tier grants ({granted})"
    )


def _is_within_root(target: str, root: str) -> bool:
    """Whether a canonical target is the root or sits under it.

    A prefix test on a canonical pair, which is the comparison the providers
    make (``providers/darwin.py`` matches its filters the same way).  ``root``
    arrives canonical, so the separator is normalised once here rather than in
    each caller — a root spelled ``/`` (the filesystem root, which no tier
    grants but a policy could) must not become ``//``.

    :param target: the canonical target path.
    :param root: one canonical granted root.
    :returns: whether the target is inside the granted root.
    """
    return target == root or target.startswith(root.rstrip(os.sep) + os.sep)
