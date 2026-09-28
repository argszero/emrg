"""The in-process file-effect policy: what ``write``/``edit`` may touch, per tier.

**Where this module is not.** It is deliberately *not* under ``emrg/sandbox/``, which is
the **fence** side — the writable roots the kernel-boundary tool runs under, plus the
providers that express them. Two reasons, and the second is mechanical: the divergent
question this module answers ("what may an in-process ``open()`` touch") is a tool-side
gate, not a fence, so filing it with the fence would imply it takes part in a policy it
measurably does not; and the guard that pins host decision D5
(``tests/test_prompt_templates.py::_sandbox_defines``) reads every ``ast`` function name
under ``emrg/sandbox/`` and refuses one that mentions a ``trusted_write_zone`` — measured
by this move: filing this file there turned that guard red with
``file_policy.py:trusted_write_zones``, i.e. it read the *move* as D5's mechanism coming
back. The grant is real and is named below; where it lives is what keeps the fence's
statement true.

**Why this module exists.** The two tool families enforce a file-effect policy
through two different mechanisms, and until now only one of them had a home:

* the process-boundary tool (``bash_tool_v2`` / ``pwsh_tool_v2``) is confined by
  the kernel — its writable roots come from :func:`emrg.sandbox.roots.writable_roots`
  and become a Seatbelt profile / bwrap bind set / Windows restricted token;
* the file tools run **inside** the daemon: they call ``open()`` in-process, where
  no kernel boundary can be applied, so their only gate is a predicate. That
  predicate lived in ``emrg/tools/bash_tool.py`` — the legacy shell tool, which
  P7 (issue #1675) deletes. Deleting the legacy file without moving the predicate
  would have deleted the only gate the file tools have.

This module is that predicate's home. It is the reason the legacy file's last
non-test importer can go away.

**What this module is not.** It is *not* the fence's policy, and the two answer
differently at ``read-only`` — measured, both halves with pure predicates
(2026-09-25, unchanged here):

============================  ==========================  =======================
target                        these predicates             v2 fence
============================  ==========================  =======================
inside the workspace          BLOCK                        no root granted
outside the workspace         allow                        no root granted
============================  ==========================  =======================

At ``workspace-write`` the two also differ by exactly one root: the trusted zone
:func:`trusted_write_zones` names, which
:func:`emrg.sandbox.roots.writable_roots` does not grant.

**Which side should change is an open decision**, owned by issue #1553 and carried
by #1675: (A) one policy for both families, so a tier means one thing for every
tool — which requires re-taking the extra-root decision — or (B) two written
policies, this one included, with their divergence stated instead of implied. The
predicates below are moved **verbatim**, behaviour unchanged, so that decision is
taken against a working module rather than a plan.

**Callers.** ``emrg/tools/write_tool.py`` and ``emrg/tools/edit_tool.py`` at every
tier other than ``danger-full-access``; the legacy command scanner imports
:func:`protected_paths`, :func:`trusted_write_zones`, :func:`temp_write_roots`,
:func:`is_absolute_path` and :func:`is_within` for its own target walks, and those
usages retire with it.
"""

from __future__ import annotations

import os
import re
import tempfile

#: ``os.name == "nt"`` — whether the *guard* is running on Windows. It is read to
#: keep the path rules below from depending on which ``os.path`` (and therefore
#: which ``os.sep``) the guard happens to run under, which is what makes issue
#: #1261's containment rule verifiable off Windows.
WINDOWS_SHELL = os.name == "nt"

#: Drive-rooted: `C:\…` / `C:/…`. UNC (`\\server\share`) needs no pattern — it
#: already reads as rooted through the `\` test in :func:`is_absolute_path`.
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

#: Daemon state files a sandboxed tool call must never write, at any tier. Each is
#: a file the host owns and the daemon reads at startup.
PROTECTED_FILES = (
    "~/.emrg/config.toml",
    "~/.emrg/emrgd.token",
    "~/.emrg/tasks.yml",
    "~/.emrg/projects.yml",
    "~/.emrg/rants.jsonl",
)


def protected_paths() -> list[str]:
    """Canonicalized (realpath) protected daemon state files."""
    out: list[str] = []
    for p in PROTECTED_FILES:
        try:
            out.append(os.path.realpath(os.path.expanduser(p)))
        except OSError:
            pass
    return out


def trusted_write_zones() -> list[str]:
    """Canonicalized write roots that a ``workspace-write`` session may target.

    These are trusted alongside the injected workspace root and the OS temp
    area — targets inside them are never blocked by the ``workspace-write``
    boundary:

    - ``~/.emrg/evolution/.emrg/`` — the evolution module's own data root
      (cycle records under ``memory/``, ``sessions/`` scratch). The evolution
      task runs at ``workspace-write`` with ``workspace`` = the repo checkout
      (``~/.emrg/evolution/emrg``), so its own record writes to
      ``~/.emrg/evolution/.emrg/memory/*.md`` fall OUTSIDE ``workspace`` and
      would be blocked — a self-regression from PR #1092 (issue #1093). Trust
      this root like the daemon's own ``~/.emrg`` state, so the evolution
      module can still record its history.

    The root this names was **archived by host decision on 2026-09-28**
    (``~/.emrg/evolution/.emrg/`` → ``~/.emrg/evolution/archive/evolution-root-20260928/``),
    so this grant is currently unreachable in practice: no writer is left that
    targets it. It is kept *as moved* because dropping it is a behaviour change
    and therefore part of the open #1553 decision, not of this move.
    """
    out: list[str] = []
    evo_data = os.path.expanduser("~/.emrg/evolution/.emrg")
    try:
        out.append(os.path.realpath(evo_data))
    except OSError:
        pass
    return out


def temp_write_roots() -> set[str]:
    r"""Canonicalized OS-temp write roots, normalized for the ``Temp\<suffix>``
    discrepancy (issue #1093 proposal #3).

    ``tempfile.gettempdir()`` may return ``C:\\...\\AppData\\Local\\Temp\\2``
    (8.3 short name + ``\\2`` suffix) on Windows, while helpers/scripts are
    written to the plain ``...\\Temp\\`` root. When ``gettempdir()`` points at
    a ``Temp``-named parent with a suffix child, also trust the parent ``Temp``
    root so both spellings are allowed.
    """
    roots: set[str] = set()
    t = tempfile.gettempdir()
    try:
        roots.add(os.path.realpath(t))
    except OSError:
        pass
    parent = os.path.dirname(t)
    if os.path.basename(parent).lower() == "temp":
        try:
            roots.add(os.path.realpath(parent))
        except OSError:
            pass
    return roots


def is_absolute_path(p: str) -> bool:
    """True when ``p`` is absolute (or drive-less rooted, e.g. ``/etc/hosts``
    on Windows — ntpath.isabs returns False for those, but they still do not
    resolve under the cwd, so the sandbox must treat them as absolute).

    A drive-rooted spelling (`C:\\…`, `C:/…`) counts as absolute when the shell
    that will run the command is a Windows shell. `ntpath.isabs` already answers
    True for it there, so on that platform this arm is redundant — it is here so
    the rule does not depend on which `os.path` the *guard* happens to be
    running under, which is what makes issue #1261's branch verifiable off
    Windows. On a POSIX shell the spelling is a relative name and is left alone.
    """
    return (
        os.path.isabs(p)
        or p.startswith("/")
        or p.startswith("\\")
        or bool(WINDOWS_SHELL and WINDOWS_DRIVE_RE.match(p))
    )


def resolve_file_target(file_path: str, workspace: str | None) -> str:
    """The path the in-process file tools will actually touch (issue #1558).

    One home for the base a relative ``file_path`` is joined onto, because the
    defect this exists to close is precisely a disagreement: the predicates
    below treated a relative target as "in the workspace" — a *spelling* — while
    the write resolved it against the **daemon process's cwd**, a different tree.
    The gate answered a question about one file and the write touched another, so
    no gate downstream could refuse anything (there is no fence for in-process
    writes, unlike bash, which is kernel-confined by the Seatbelt/bwrap profile).

    So both callers read the same reduction here: the target joined onto the
    directory the write happens in — ``workspace``, the session cwd the daemon
    injects into ``write``/``edit`` exactly as it injects ``workdir`` into bash.
    This is the same rule #1353 installed for the command scan (`os.path.join`
    of the target onto the base the child starts in, then a realpath containment
    test); the in-process tools have no child, so their base is the declared
    workspace.

    An absolute target is returned as spelled, and a relative one with **no**
    workspace has no base to join onto — it is returned as spelled too, which is
    the pre-existing behaviour for a caller that declared no boundary (the tools
    are then fail-open, as their docstrings say).

    :param file_path: the target as the model spelled it.
    :param workspace: the session cwd, or ``None`` when none was declared.
    :returns: the absolute target to judge and to write.
    """
    expanded = os.path.expanduser(file_path)
    if not expanded or is_absolute_path(expanded) or not workspace:
        return expanded
    return os.path.join(os.path.expanduser(workspace), expanded)


def is_within(path: str, root: str) -> bool:
    """True when ``path`` (absolute) is inside ``root`` (absolute) or equals it.

    Under a Windows shell the comparison canonicalises the separator before the
    prefix test. That is a uniform substitution on both sides, so it cannot
    change which of two real paths contains the other — but it does stop the
    test from depending on which `os.path` (and therefore which ``os.sep``) the
    *guard* is running under, which is what makes issue #1261's containment
    verifiable off Windows. POSIX shells are untouched.
    """
    try:
        rp = os.path.realpath(path)
        rr = os.path.realpath(root)
        if WINDOWS_SHELL:
            rp = rp.replace("\\", "/")
            rr = rr.replace("\\", "/").rstrip("/")
            return rp == rr or rp.startswith(rr + "/")
        return rp == rr or rp.startswith(rr + os.sep)
    except OSError:
        return False


def check_read_only_file_write(file_path: str, workspace: str | None = None) -> str | None:
    """Read-only sandbox check for the write/edit tools (community issue #979).

    Returns a block reason when the target file is inside the task's workspace
    (the host's working tree — protected by the structural dirty-tree guard) or
    is a protected daemon state file; returns None when allowed.

    Writes OUTSIDE the workspace (memory dir, logs, OS temp) stay allowed so a
    read-only cycle can still record state and write its own artifacts — the
    guard protects the host's uncommitted work, not the agent's own scratch
    space.

    This does **not** mirror the bash tool's read-only semantics, and the
    sentence that said it did was the only statement anywhere of what the file
    tools are supposed to do at this tier (issue #1553). Measured on this tree
    (2026-09-25), both halves with pure predicates, nothing spawned or written:

    ============================  =========================  ================
    target                        this function              v2 fence
    ============================  =========================  ================
    inside the workspace          BLOCK                      (no root granted)
    outside the workspace         allow                      (no root granted)
    ============================  =========================  ================

    The fence's allow-list is :func:`emrg.sandbox.roots.writable_roots` —
    empty for every mode but ``workspace-write`` — and the process-boundary tool
    derives its profile from it, so at ``read-only`` bash can write nowhere at
    all while these tools may write anywhere outside the workspace. The two
    layers disagree in both directions at once, and which semantics the file
    tools should have is the open decision P7 carries (#1553): replacing this
    function with the fence would deny a read-only cycle the very write named
    above, its own cycle record.

    A relative target is joined onto ``workspace`` first (issue #1558): this used
    to realpath the spelling as given, i.e. against the **daemon's cwd** — a base
    the caller never designated — so the containment answer was about a path
    outside the workspace the caller declared, and one file's two spellings split
    their verdict (the relative spelling allowed at a tier where the absolute
    spelling of that same file was blocked). The write followed the same base, so
    it landed outside the declared workspace rather than inside it: what was wrong
    is *which* file was judged, not a write this function never saw. The judgement
    and the write now name the file the caller named.
    See :func:`resolve_file_target`.
    """
    path = os.path.realpath(resolve_file_target(file_path, workspace))
    if workspace:
        ws = os.path.realpath(os.path.expanduser(workspace))
        if is_within(path, ws):
            return (
                f"read-only sandbox: blocked file write inside workspace {path!r} "
                "(dirty-tree guard, community issue #979)"
            )
    if path in protected_paths():
        return (
            f"read-only sandbox: blocked write to protected daemon file {path!r}"
        )
    return None


def check_workspace_write(file_path: str, workspace: str | None = None) -> str | None:
    """workspace-write sandbox check for the write/edit tools (rant 2026-09-01T15:10:23).

    Returns a block reason when the target file is a protected daemon state file,
    is ``~/.emrg`` itself, or is an absolute path outside the workspace root, the
    OS-temp roots and the trusted zones named below; returns None when allowed.

    The allowed list is the workspace root, the OS-temp roots, and **one root the
    blueprint's derivation does not have**: :func:`trusted_write_zones` —
    ``~/.emrg/evolution/.emrg``, the evolution module's own data root, trusted
    because the task runs at this tier and its cycle records land outside the
    workspace (issue #1093, a self-regression from PR #1092). So this is not
    exactly dsh's workspace + temp allow-list, and it is not identical to the v2
    fence either: ``emrg.sandbox.roots.writable_roots`` grants the workspace and
    the temp areas and nothing else, so at this tier the process-boundary tool
    cannot write the evolution root these tools may. The divergence is measured
    in :func:`check_read_only_file_write`'s docstring; which side should change is
    the decision P7 carries (issue #1553).

    Relative paths are joined onto ``workspace`` and then judged exactly like
    absolute ones (issue #1558) — they used to return early on the assumption
    "cwd = the workspace root", which is not where a write resolves when the
    caller passes the spelling the model gave it. That asymmetry inside one
    function was the hole: the absolute branch below realpaths both sides and
    requires containment, and the relative branch reached none of it. A relative
    target with no declared workspace still has no base to join onto, so it keeps
    the old reading — and both tools pass the joined target, so in daemon use
    (``workspace`` always injected) the base is never in doubt.

    Without this check the write/edit tools let ``workspace-write`` sessions write
    anywhere outside the session cwd, while the bash tool is correctly blocked —
    the asymmetric hole this function closes.
    """
    if not file_path:
        return None
    expanded = resolve_file_target(file_path, workspace)
    if not is_absolute_path(expanded):
        # No workspace was declared, so there is no base to join onto and no
        # boundary to require containment against (non-daemon use; the tools are
        # fail-open there by construction).
        return None
    real = os.path.realpath(expanded)
    if real in protected_paths():
        return (
            f"workspace-write sandbox: blocked write to protected daemon file {file_path!r}"
        )
    emrg_home = os.path.realpath(os.path.expanduser("~/.emrg"))
    if real == emrg_home:
        return (
            f"workspace-write sandbox: blocked destructive write to {file_path!r} "
            "(would erase the daemon's data directory)"
        )
    workspace_real = (
        os.path.realpath(os.path.expanduser(workspace)) if workspace else None
    )
    # Allow: inside workspace, inside the OS-temp roots (normalized), or inside
    # a trusted zone (~/.emrg/evolution/.emrg — the evolution module's own data
    # root, issue #1093 self-regression). Everything else is blocked.
    allowed_srcs = [workspace_real] if workspace_real else []
    allowed_srcs += list(trusted_write_zones())
    allowed_srcs += list(temp_write_roots())
    if not any(
        src and (is_within(real, src) or real == src) for src in allowed_srcs
    ):
        return (
            f"workspace-write sandbox: blocked write outside workspace {file_path!r}"
            + (f" (resolves to {real!r})" if real != os.path.expanduser(file_path) else "")
        )
    return None
