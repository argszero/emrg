"""Shared git utilities — used by both daemon and scheduler."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from emrg.config import config_dir

INSTALL_BIN = Path.home() / ".emrg" / "install" / "bin"
INSTALL_INFO = config_dir() / "install-info.json"


def _detect_git_remote(cwd: str) -> str:
    """Detect the origin remote (owner/repo) from a git repository.

    Returns '' if detection fails.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=5,
        )
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
    """
    try:
        data = {}
        if INSTALL_INFO.exists():
            data = json.loads(INSTALL_INFO.read_text(encoding="utf-8"))
        data.update({"git_path": git, "gh_path": gh, "repo": "https://github.com/argszero/emrg.git"})
        INSTALL_INFO.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
    """
    git, _ = resolve_git_gh()
    exe = git or "git"
    return subprocess.run(
        [exe, *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", timeout=timeout,
    )
