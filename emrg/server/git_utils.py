"""Shared git utilities — used by both daemon and scheduler."""

from __future__ import annotations

import subprocess


def _detect_git_remote(cwd: str) -> str:
    """Detect the origin remote (owner/repo) from a git repository.

    Returns '' if detection fails.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
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
