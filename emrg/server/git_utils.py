"""Shared git utilities — used by both daemon and scheduler."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from emrg._win import win32_no_window_kwargs
from emrg.config import config_dir
from emrg.server.atomic import atomic_write_bytes

INSTALL_BIN = Path.home() / ".emrg" / "install" / "bin"
INSTALL_INFO = config_dir() / "install-info.json"

logger = logging.getLogger(__name__)

# One-shot guard: when no git executable can be resolved at all, log the
# root cause once per process (2026-08-12 incident follow-up — the daemon
# restarted without PATH git and cycles were silently skipped for 18 min).
_GIT_MISSING_WARNED = False

# In-process memo of the last (git, gh) paths written to install-info.json —
# resolve_git_gh() is called on every git_cmd(), and each call used to do an
# atomic tmp+os.replace disk write even when nothing changed. Tool paths are
# stable within a process lifetime, so write once and skip the rest.
_LAST_CACHED_PATHS: tuple[str, str] | None = None


# ── Non-interactive subprocess environment (rant 2026-08-07T10:17:27) ──
#
# Windows GCM popup storm: the daemon is a background non-interactive
# process — any git/gh subprocess that needs credentials must FAIL FAST
# and silently, never spawn GCM GUI dialogs / askpass / terminal prompts.
# These vars are applied to every git/gh subprocess the daemon spawns
# (bash_tool child processes, scheduler clone/fetch, github_status).

def no_prompt_env() -> dict:
    """Copy of the current environment with all interactive git prompts disabled.

    - ``GIT_TERMINAL_PROMPT=0`` — git never asks on the terminal
    - ``GCM_INTERACTIVE=never`` — Git Credential Manager never shows its GUI
    - ``GIT_ASKPASS=`` — disables askpass helper popups

    macOS/Linux are unaffected (osxkeychain / credential helpers are
    non-interactive there); Windows without stored credentials now fails
    with a clear git error instead of popping a window.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_ASKPASS"] = ""
    return env


# gh auth status user extraction — output forms seen across gh versions:
#   "Logged in to github.com as octocat"
#   "Logged in to github.com account octocat"
#   "Logged in to github.com account octocat using token"
_GH_AUTH_USER_RE = re.compile(
    r"Logged in to github\.com (?:account |as )['\"]?([A-Za-z0-9][A-Za-z0-9-]*)"
)


def parse_gh_auth_user(output: str) -> str | None:
    """Extract the authenticated GitHub username from ``gh auth status`` output.

    Returns None when the output does not describe an authenticated session.
    """
    match = _GH_AUTH_USER_RE.search(output or "")
    return match.group(1) if match else None


# ── HTTPS→SSH fallback for blocked github.com:443 (2026-08-08) ───
#
# Some networks block github.com:443 (HTTPS git transport) while SSH
# (port 22) and the api.github.com REST endpoint stay reachable. Observed
# on the packaged host: `git pull` hangs ~75 s then fails with "Failed to
# connect to github.com port 443", while `ssh -T git@github.com` succeeds.
# A fresh `git clone` or any pull/push against an https origin then fails
# and the evolution workspace never syncs. These helpers convert a
# github.com https URL to its SSH form and recognise connection-type git
# errors, so the scheduler can retry via SSH. Deliberately narrow: auth
# failures / 404s / repo-specific errors never trigger a switch.

_HTTPS_GITHUB_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$")

_CONNECTION_ERROR_MARKERS = (
    "failed to connect",
    "couldn't connect",
    "could not connect",
    "connection refused",
    "connection timed out",
    "operation timed out",
    "could not resolve host",
    "network is unreachable",
    "unable to access",
    "tls handshake timeout",
    # "the remote end hung up unexpectedly" / "connection ... hung up" —
    # git's classic message when a proxy/network drops the connection
    # mid-transfer (observed on the packaged host: fetch fails with
    # "fatal: the remote end hung up unexpectedly" while https works via
    # proxy on retry). A network-level drop is exactly the case where an
    # SSH retry may succeed.
    "hung up",
)


def https_to_ssh_url(url: str) -> str | None:
    """Convert a github.com https URL to its SSH form, or None.

    ``https://github.com/owner/repo.git`` → ``git@github.com:owner/repo.git``
    Returns None for non-github / non-https URLs (SSH URLs, enterprise
    hosts, local paths) — callers must not switch those.
    """
    match = _HTTPS_GITHUB_RE.match((url or "").strip())
    if not match:
        return None
    return f"git@github.com:{match.group(1)}/{match.group(2)}.git"


def is_git_connection_error(stderr: str) -> bool:
    """True when git stderr indicates a network/connection failure.

    Does NOT match auth errors ("Authentication failed", "Permission
    denied (publickey)"), missing repos ("Repository not found") or other
    non-connection failures — switching the remote would not fix those.
    """
    text = (stderr or "").lower()
    return any(marker in text for marker in _CONNECTION_ERROR_MARKERS)


def git_origin_url(cwd: str) -> str:
    """Return the raw origin URL for a repo, '' when absent/unreadable."""
    result = git_cmd("remote", "get-url", "origin", cwd=cwd, timeout=5)
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def _detect_git_remote(cwd: str) -> str:
    """Detect the origin remote (owner/repo) from a git repository.

    Returns '' if detection fails.
    """
    try:
        # 用解析后的 git（install-info → bundled → PATH 回退，见 git_cmd），
        # 防 daemon 启动环境无 PATH git 时误判 "not a git repo"（2026-08-12 事故）。
        result = git_cmd("remote", "get-url", "origin", cwd=cwd, timeout=5)
        if result.returncode == 0:
            url = result.stdout.strip()
            # Extract owner/repo from various URL formats:
            #   git@github.com:owner/repo.git
            #   https://github.com/owner/repo.git
            #   https://github.com/owner/repo
            if ":" in url and "@" in url:
                # SSH: git@github.com:owner/repo.git
                parts = url.split(":")[-1]
            elif "github.com/" in url:
                # HTTPS: https://github.com/owner/repo
                parts = url.split("github.com/")[-1]
            else:
                return ""
            return parts.removesuffix(".git")
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass
    return ""


def _cached_tool_path(tool: str) -> str | None:
    """Return a cached tool path from install-info.json, if present."""
    try:
        data = json.loads(INSTALL_INFO.read_text(encoding="utf-8"))
        value = data.get(f"{tool}_path")
        return str(value) if value else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _cache_tool_paths(git: str, gh: str) -> None:
    """Persist resolved tool paths so later lookups are O(1).

    Also persists the EMRG repo URL (``repo``) so the evolution workspace
    self-heal (rant 2026-08-06T20:42:05) can clone on demand without
    hardcoding — packaged installs have no git remote to detect.

    The write is atomic (temp file + os.replace) so concurrent readers
    never observe a partially-written file; the read is guarded like
    ``_cached_tool_path`` so a corrupt/partial cache (e.g. a crashed or
    concurrent writer) degrades to an empty dict instead of raising
    JSONDecodeError (observed as a flaky test_daemon failure when the
    live daemon rewrote install-info.json mid-suite).
    """
    try:
        data = {}
        if INSTALL_INFO.exists():
            try:
                data = json.loads(INSTALL_INFO.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, AttributeError):
                data = {}
        data.update({"git_path": git, "gh_path": gh, "repo": "https://github.com/argszero/emrg.git"})
        tmp = INSTALL_INFO.with_name(INSTALL_INFO.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, INSTALL_INFO)
    except OSError:
        pass


def _tool_in_install(tool: str) -> str | None:
    """Return the bundled tool path under ~/.emrg/install/bin, if present.

    Windows: git lives in install/git/cmd/git.exe; gh is a single binary in
    install/bin. POSIX: both are single binaries in install/bin.
    """
    if os.name == "nt":
        git_in_install = INSTALL_BIN.parent / "git" / "cmd" / "git.exe"
        if tool == "git" and git_in_install.exists():
            return str(git_in_install)
    exe = INSTALL_BIN / (tool + (".exe" if os.name == "nt" else ""))
    return str(exe) if exe.exists() else None


def resolve_git_gh() -> tuple[str, str]:
    """Resolve git and gh executable paths for the evolution environment.

    Priority (rant #12 §6):
      1. cached install-info.json paths
      2. bundled binaries under ~/.emrg/install/bin (or install/git/cmd on Windows)
      3. shutil.which() fallback (dev / source mode)

    Returns (git_path, gh_path). Missing executables yield '' (callers decide
    how to degrade).
    """
    global _LAST_CACHED_PATHS
    git = _cached_tool_path("git")
    gh = _cached_tool_path("gh")
    if git and Path(git).exists():
        pass
    else:
        git = _tool_in_install("git") or (shutil.which("git") or "")
    if gh and Path(gh).exists():
        pass
    else:
        gh = _tool_in_install("gh") or (shutil.which("gh") or "")

    if git:
        # Write the cache only when the resolved pair changed since the last
        # write (or nothing cached yet) — avoids an atomic install-info.json
        # write on every git_cmd() call. Paths are stable per process.
        if _LAST_CACHED_PATHS != (git, gh):
            _cache_tool_paths(git, gh)
            _LAST_CACHED_PATHS = (git, gh)
    else:
        # git is the failure mode that silently disables evolution (2026-08-12
        # incident) — warn regardless of whether gh resolved. Also skip the
        # cache write so a previously valid cached git_path is not clobbered
        # with '' (review #714 note).
        _warn_git_missing_once()
    return git, gh


def _warn_git_missing_once() -> None:
    """Log one actionable WARNING when no git executable can be resolved.

    2026-08-12 incident follow-up: a daemon restart in an environment with
    neither bundled nor PATH git made every evolution git call raise
    FileNotFoundError; _is_usable_git_repo() swallowed the OSError and the
    cycle log only said "workspace not ready — skipping cycle" for 18
    minutes. Log the root cause once per process so the daemon log is
    diagnosable.
    """
    global _GIT_MISSING_WARNED
    if _GIT_MISSING_WARNED:
        return
    _GIT_MISSING_WARNED = True
    logger.warning(
        "git executable not found (install-info / ~/.emrg/install / PATH all "
        "empty) — evolution cycles will be skipped as 'not a git repo'; "
        "install git or add it to PATH, then restart the daemon"
    )


def git_cmd(*args: str, cwd: str | None = None, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a git command using the resolved git binary.

    Falls back to bare ``git`` when no bundled binary is found (dev mode).
    The prompt-free environment guarantees no GCM/askpass popups from a
    background daemon (rant 2026-08-07T10:17:27).
    """
    git, _ = resolve_git_gh()
    exe = git or "git"
    return subprocess.run(
        [exe, *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", timeout=timeout, env=no_prompt_env(),
        **win32_no_window_kwargs(),
    )


# ── EMRG's own runtime directory inside a repository EMRG works in ────────
#
# Rant 2026-09-21T10:12:01: an open-source task's clone carried `.emrg/`
# (sessions, memory, the client log — this instance's own runtime data) as
# *untracked and unignored* dirt. The structural dirty-tree guard asks
# whether a tree holds work that exists nowhere else, and EMRG's own
# bookkeeping answers "yes": 38 cycles were forced `read-only` for a
# directory EMRG itself created, and the tier that followed from it is what
# refused the git verbs that would have converged the tree.
#
# The dirt is EMRG's, not the repository's, so the repository's *local*
# ignore file is where it belongs: `.git/info/exclude` is per-clone, is
# never committed, and never touches the upstream `.gitignore` — which is
# the file a project's maintainers own.

# An anchored entry, and *relative to the repository root*: a task directory one
# level down writes its runtime data into `<root>/work/clone/.emrg`, which the
# root's own `/.emrg/` does not cover (community issue #1507 — the same marker-vs-git
# confusion as `repo_scope` below, one gate over). Anchoring is what keeps a nested
# `.emrg/` belonging to some other tool untouched: a bare `.emrg/` would be a claim
# this repository cannot make about somebody else's directory.
EXCLUDE_ENTRY = "/.emrg/"


def runtime_exclude_entry(prefix: str = "") -> str:
    """The local ignore entry covering the runtime dir of a directory ``prefix`` deep.

    ``""`` — the directory *is* the repository root — gives ``EXCLUDE_ENTRY``
    (``/.emrg/``). A directory one level down gives ``/work/clone/.emrg/``: the
    runtime data is EMRG's own in either shape, and the entry is where git reads
    it for the tree the directory actually lives in.
    """
    return EXCLUDE_ENTRY if not prefix else f"/{prefix}/.emrg/"


def repo_scope(directory: str) -> tuple[str, str] | None:
    """The repository ``directory`` is *in*, and its path inside it.

    Answers ``(root, prefix)``, where ``prefix`` is ``""`` when the directory is
    itself the repository root — or ``None`` when it is in no repository at all.

    Asked of git, because that is whose question it is. The structural guards used
    to test ``os.path.isdir(<dir>/.git)``, which answers *"is this the root of a
    checkout?"* while the guard needs *"is this directory in a tree?"* — and the
    two differ for every directory that is not a root: a package inside a
    monorepo, a docs or `work/` tree, a checkout nested in a larger repository
    (community issue #1507, measured here: `_is_dirty_tree_sync` answered False
    for a directory whose own `git status` listed the work the task was about to
    be allowed to remove).

    Resolving the root also decides the *scope* of every reader that uses it: the
    two realpaths make the prefix comparable when one of them arrived through a
    symlink (`/tmp` on macOS is the case measured here). A directory that resolves
    outside the root it reported — which git's own answer should prevent, so this
    is a guard rather than a case — keeps the wider reading, ``("", )``, because a
    narrower one derived from an inconsistent pair is the unsafe direction.
    """
    try:
        cp = git_cmd("rev-parse", "--show-toplevel", cwd=directory)
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    root = (cp.stdout or "").strip()
    if not root:
        return None
    root = os.path.realpath(root)
    here = os.path.realpath(directory)
    if here == root:
        return root, ""
    rel = os.path.relpath(here, root)
    if rel == os.curdir or rel.startswith(os.pardir):
        return root, ""
    return root, rel.replace(os.sep, "/")

_EXCLUDE_NOTE = (
    "# EMRG's own runtime directory (sessions, memory, logs). It is not part\n"
    "# of this repository and is never committed. Ignoring it locally keeps\n"
    "# EMRG's dirty-tree guard from reading its own runtime data as work that\n"
    "# exists nowhere else. Written by emrg/server/git_utils.py; delete this\n"
    "# block if you do not want it.\n"
)


def _exclude_path_of(repo_dir: str) -> Path | None:
    """The ignore file git actually reads for ``repo_dir``, or None.

    Asked of git rather than derived from a path, because deriving it is wrong
    in the case that matters: ``.git`` is a directory in a clone and a *file*
    in a linked worktree, and a worktree's excludes are read from the **common**
    git dir (`<main>/.git/info/exclude`), not from the per-worktree one
    (`<main>/.git/worktrees/<name>/info/exclude`) — measured here, where writing
    the per-worktree file left the very directory still reported as untracked.
    ``--git-path`` is git's own answer to "which file do I read for this?".
    """
    try:
        cp = git_cmd("rev-parse", "--git-path", "info/exclude", cwd=repo_dir)
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    out = (cp.stdout or "").strip()
    if not out:
        return None
    candidate = Path(out)
    return candidate if candidate.is_absolute() else (Path(repo_dir) / candidate)


def _already_excludes_runtime_dir(text: str, entry: str = EXCLUDE_ENTRY) -> bool:
    """Whether an ignore file already ignores the runtime directory ``entry`` names.

    Accepts the spellings that mean it (anchored or not, trailing slash or not)
    so the check does not rewrite a file that already says the same thing in
    another hand's style — and a bare ``.emrg`` counts for a prefixed entry too,
    because git reads an unanchored pattern at *any* depth, which is exactly the
    directory ``/work/clone/.emrg/`` names (issue #1507).
    """
    core = entry.strip("/")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.strip("/") in (core, ".emrg"):
            return True
    return False


def ensure_local_exclude(repo_dir: str) -> str:
    """Make the git dir ``repo_dir`` lives in ignore EMRG's runtime directory.

    ``repo_dir`` is the directory this instance *works in*, not necessarily a
    repository root: the entry is written for its runtime data's path relative to
    the root git reports (``repo_scope``), into the git dir git reports
    (``_exclude_path_of``). Naming either of them from the path instead of asking
    git is what community issue #1507 measured going wrong one level down.

    Idempotent, and never raises — this runs while a session or a cycle is
    being set up, and no repository state is worth failing that for:

    * ``"present"`` — already ignored, nothing written;
    * ``"added"`` — the entry was appended (existing content preserved);
    * ``"not-a-repo"`` — no git dir there, or git is unavailable;
    * ``"error: <reason>"`` — the file could not be read or written.

    Only the repository's *local* exclude is touched. The upstream
    ``.gitignore`` belongs to the project and is left alone (rant
    2026-09-21T10:12:01).
    """
    repo = Path(repo_dir)
    if not repo.is_dir():
        return "not-a-repo"
    scope = repo_scope(str(repo))
    if scope is None:
        return "not-a-repo"
    root, prefix = scope
    exclude = _exclude_path_of(root)
    if exclude is None:
        return "not-a-repo"
    try:
        existing = (
            exclude.read_text(encoding="utf-8", errors="replace")
            if exclude.is_file()
            else ""
        )
    except OSError as exc:
        return f"error: {exc}"
    if _already_excludes_runtime_dir(existing, runtime_exclude_entry(prefix)):
        return "present"
    body = existing
    if body and not body.endswith("\n"):
        body += "\n"
    body += _EXCLUDE_NOTE + runtime_exclude_entry(prefix) + "\n"
    try:
        mode = os.stat(exclude).st_mode & 0o777 if exclude.exists() else 0o644
        atomic_write_bytes(body, exclude, mode=mode)
    except OSError as exc:
        return f"error: {exc}"
    return "added"
