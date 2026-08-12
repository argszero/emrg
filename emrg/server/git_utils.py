"""Shared git utilities — used by both daemon and scheduler."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from emrg._win import win32_no_window_kwargs
from emrg.config import config_dir

INSTALL_BIN = Path.home() / ".emrg" / "install" / "bin"
INSTALL_INFO = config_dir() / "install-info.json"


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

    if git or gh:
        _cache_tool_paths(git, gh)
    return git, gh


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
