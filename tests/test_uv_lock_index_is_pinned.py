"""The lock's registry index must not depend on the machine that ran uv.

Why this exists (issues #1157 / #1159)
--------------------------------------
`uv.lock` records the index each package was resolved from. A machine-level
`~/.config/uv/uv.toml` - a common accelerator config, e.g. a regional PyPI
mirror - overrides the project for *every* command, so a bare `uv run` /
`uv lock` rewrites all 20 registry entries in place. Measured on this repo:
0 -> 277 rewritten lines, and the rewrite happens even when uv exits non-zero.

That is not cosmetic. Cycles run in the source checkout, and a dirty working
tree forces the next cycle into a read-only sandbox (`emrg/server/scheduler.py`
`_is_dirty_tree_sync`: non-empty `git status --porcelain` means read-only). One
bare `uv run` therefore flipped the workspace permanently read-only, and the
repair is a git write that the same sandbox blocks - seven consecutive cycles
could not write anything.

These tests pin the fix rather than the symptom. The first is the invariant the
project actually wants; the second is what makes it true on a machine whose uv
config points elsewhere.

Both are pure text checks on files in the repo - no network, no `uv` binary, no
platform dependency (they must run on the Windows CI runner too, where `uv
lock` might behave differently but the *files* are still there).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_TOML = REPO_ROOT / "uv.toml"
UV_LOCK = REPO_ROOT / "uv.lock"

CANONICAL_INDEX = "https://pypi.org/simple"


def _registry_urls() -> list[str]:
    """Every `registry = "..."` URL recorded in the lock."""
    text = UV_LOCK.read_text(encoding="utf-8")
    return re.findall(r'registry\s*=\s*"([^"]+)"', text)


def test_lock_records_the_canonical_index_only() -> None:
    """The committed lock must resolve from canonical PyPI, everywhere.

    This is the file-level invariant: whatever index a particular machine
    prefers, what is *committed* has to be one thing, or every developer's
    `uv run` shows up as a 500-line diff of environment noise (the v0.2.94
    lesson recorded in Agent.md) and re-dirties the tree.

    Skips only if the lock genuinely has no registry entries, which would mean
    the lock format changed - a reason to look, not to pass silently.
    """
    urls = _registry_urls()
    if not urls:
        pytest.skip("uv.lock records no `registry = ` entries (format changed?)")

    unexpected = sorted({u for u in urls if u != CANONICAL_INDEX})
    assert not unexpected, (
        f"uv.lock resolves from non-canonical index(es): {unexpected}. "
        f"Every entry must be {CANONICAL_INDEX}. A mirror URL in the lock is "
        "machine-specific environment noise; regenerate with the project's "
        "uv.toml in effect (or set UV_INDEX_URL only for the command you run, "
        "never for the lock you commit)."
    )


def test_project_uv_config_pins_the_canonical_index() -> None:
    """A project-level `uv.toml` must pin the index, defeating a host mirror.

    Without this file, the host's `~/.config/uv/uv.toml` wins and a bare
    `uv run` rewrites the lock - which is how the read-only deadlock started.
    The pinned value has to be the canonical index: pinning a mirror would make
    the lock machine-specific in the other direction.

    Parsed as TOML rather than matched as text, so a commented-out or
    quoted-in-prose mention cannot satisfy it (the failure mode this whole
    suite exists for: a claim about a file that the file does not make).
    """
    assert UV_TOML.is_file(), (
        "uv.toml is missing: a machine-level uv config (mirror) will then "
        "rewrite uv.lock on any bare `uv run`, re-dirtying the tree and forcing "
        "the next cycle read-only (issues #1157/#1159)"
    )

    data = tomllib.loads(UV_TOML.read_text(encoding="utf-8"))
    index_url = data.get("index-url")
    assert index_url == CANONICAL_INDEX, (
        f"uv.toml must pin index-url = {CANONICAL_INDEX!r}; found {index_url!r}. "
        "A mirror here also rewrites the lock (measured), just less visibly."
    )
