"""The path-shaping helpers the in-process file policy is stated in.

**What this module holds now.** The *mechanics* of judging a file target — where
a relative spelling resolves (:func:`resolve_file_target`), what "inside" means
(:func:`is_within`), the platform's absolute-path rule
(:func:`is_absolute_path`), and the host's protected daemon files
(:func:`protected_paths`) — and no policy. The policy is ``emrg/sandbox/``: the
fence (:func:`emrg.sandbox.fence.file_refusal`) asks the same
:func:`emrg.sandbox.roots.writable_roots` the Seatbelt/bwrap profiles are built
from, so ``write``/``edit`` and the shell tools cannot answer differently at one
tier (host ruling 2026-09-28T21:50, answering issue #1553's decision half).

**What it no longer holds.** Two gates, ``check_read_only_file_write`` and
``check_workspace_write``, which were the file tools' own answer and disagreed
with the fence in **both** directions at ``read-only`` (bash could write nowhere
while the file tools could write anywhere outside the workspace) and by one root
at ``workspace-write`` (:func:`trusted_write_zones`). The ruling chose one
policy for both families, so both gates are **deleted** rather than moved a
second time: the two tools now ask the fence directly. Deleting them here is
what keeps a third answer from growing back in the file the move came from.

**The extra deployer root.** :func:`trusted_write_zones` is kept *as moved* —
host decision D5 deleted it and the root itself was archived (issue #1703) — and
no production path reads it: the fence grants the workspace and the platform
temp areas and nothing else. It stays because dropping a grant is a behaviour
change with its own review, and because the guard that pins D5
(``tests/test_prompt_templates.py::_sandbox_defines``) reads every function name
under ``emrg/sandbox/`` and refuses one mentioning it as a *name*: filing this
module with the fence would read its survival as the mechanism coming back.
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


