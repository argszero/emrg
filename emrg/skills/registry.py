"""Installable-skills catalog — parse ``~/.emrg/skills/skill-catalog.md``.

Revised design (rant 2026-08-08T10:14:29, supersedes the 10:11:35
registry design): the recommended-skills list is itself a normal skill
file named ``skill-catalog.md``. The existing skill loader already picks
it up (name + description → one line in the system prompt's Available
Skills), and the LLM reads its body for install/update guidance. No new
meta-mechanism, no system.j2 change.

The frontmatter carries both loader fields (``name``/``description``,
which the loader reads and ignores everything else) and machine
metadata for ``/skills available/install/update`` (a ``skills:`` list of
name/description/repo/install/dest/check — same 5 metadata fields as the
original design, still no file lists). Install/update state lives in
``~/.emrg/skills/.state.json`` — ``managed: true`` marks catalog-managed
files that the 24h update check may refresh; unmarked files are treated
as host-modified and never touched.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from emrg.config import config_dir

# The "is this path a regular file" rule is the tool layer's, reached at a path. Imported
# rather than restated, for the reason `emrg/memory.py` gives at the same import: the
# boundary is one fact, and a second spelling of it is a second boundary that can drift.
from emrg.tools.base import non_regular_kind

logger = logging.getLogger(__name__)

# Canonical baseline — must match emrg/skills/skill-catalog.md
# (tests/test_skills_registry.py asserts byte-equality so the embedded
# fallback never drifts from the shipped source file).
BASELINE_CATALOG_MD = """---
name: skill-catalog
description: "Catalog of optional installable skills (browser-harness, etc.). Read this file when a task needs a capability you don't have — it lists what is installable, how to install, and how updates are checked."
skills:
  - name: browser-harness
    description: "Direct browser control via CDP: automation, scraping, testing, site work."
    repo: browser-use/browser-harness
    install: self-publishing
    dest: ~/.emrg/skills/
    check: github_release
---

# Installable Skills

This catalog lists optional skills that are NOT installed by default. When
a task needs one (e.g. browser interaction), install it on demand:

## browser-harness

- **What it does**: Direct browser control via CDP — automation, scraping, testing, site work.
- **How to install**: `/skills install browser-harness`
- **Source repo**: browser-use/browser-harness
- **Install method**: self-publishing (CLI's own `skill` command emits the skill files)
- **Install destination**: `~/.emrg/skills/`
- **Update check**: GitHub release tag (api.github.com)

(New recommended skills get a section appended here — adding a skill only
touches this file, the system prompt never changes.)
"""

# Frontmatter keys a valid catalog entry must carry.
REQUIRED_ENTRY_FIELDS = ("name", "description", "repo", "install", "dest", "check")

CATALOG_FILENAME = "skill-catalog.md"


def catalog_path() -> Path:
    """Path of the installable-skills catalog file."""
    return config_dir() / "skills" / CATALOG_FILENAME


def state_path() -> Path:
    """Path of the skills install/update state file."""
    return config_dir() / "skills" / ".state.json"


def ensure_catalog_file() -> Path:
    """Write the bundled baseline catalog if missing (daemon startup fallback).

    Covers upgrades and user deletions: a clean install gets the catalog
    from the packaging bundle, but the daemon re-writes it from the
    embedded baseline when it is absent (log INFO, never disturbs the
    host).
    """
    path = catalog_path()
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(BASELINE_CATALOG_MD, path)
            logger.info("wrote baseline skill catalog: %s", path)
        except OSError:
            logger.warning("could not write %s", path, exc_info=True)
    return path


def parse_skills_frontmatter(text: str) -> list[dict]:
    """Parse the ``skills:`` list from the catalog frontmatter.

    Handles the simple indented list-of-maps format used by the baseline
    (avoids pulling in a YAML dependency for this fixed structure):

    .. code-block:: yaml

        skills:
          - name: browser-harness
            description: "..."
            repo: ...
            install: ...
            dest: ...
            check: ...

    Top-level ``name``/``description`` (loader fields) are ignored.
    Returns a list of dicts (5 metadata fields each); malformed entries
    are skipped.
    """
    if not text.startswith("---"):
        return []
    parts = text.split("---", 2)
    if len(parts) < 3:
        return []
    fm_lines = parts[1].splitlines()

    entries: list[dict] = []
    current: dict[str, str] | None = None
    in_skills = False
    for raw in fm_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "skills:":
            in_skills = True
            continue
        if not in_skills:
            continue
        if stripped.startswith("- "):  # new entry: "- name: X"
            if current is not None:
                entries.append(current)
            current = {}
            key, _, value = stripped[2:].partition(":")
            current[key.strip()] = _strip_quotes(value.strip())
        elif current is not None and ":" in stripped:
            key, _, value = stripped.partition(":")
            current[key.strip()] = _strip_quotes(value.strip())
    if current is not None:
        entries.append(current)

    # Keep only entries with all required fields
    return [e for e in entries if all(k in e for k in REQUIRED_ENTRY_FIELDS)]


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def load_catalog_skills() -> list[dict]:
    """Read the catalog's installable-skill list (empty when missing).

    The subject's **kind** is asked before it is opened, because opening it is what decides
    whether this call returns at all: `read_text` on a FIFO blocks at the *open* until a
    writer appears, so the `except OSError` below cannot bound the consequence — nothing is
    raised and the call simply never returns. This runs on the daemon's event loop (the
    `skills_available` and `skills_install` handlers), so the block is the whole server.

    Measured 2026-10-11 (`cyc20261011-100220`): a `mkfifo` at `~/.emrg/skills/skill-catalog.md`
    made this function **not return in 8 s**, where the same fixture with a regular file
    returned in 0.00 s.

    The path is the directory this instance installs skills into, and `load_skills` already
    kind-checks the `*.md` files beside it (#2084) — this file sits in the same directory and
    was the one left unguarded.

    Asked **instead of** the `path.exists()` this function used to lean on, not in addition
    to it: `exists()` is true for a FIFO, which is exactly the reading that let the block
    through, so it is removed rather than kept as a second opinion.
    """
    path = catalog_path()
    try:
        kind = non_regular_kind(path)
    except OSError:
        # A missing (or unstattable) path: the same empty answer this function has always
        # given for one.
        return []
    if kind:
        logger.debug("cannot read %s — it is %s, not a regular file", path, kind)
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.debug("cannot read %s", path)
        return []
    return parse_skills_frontmatter(text)


def find_catalog_skill(name: str) -> dict | None:
    """Look up a single catalog entry by name."""
    for entry in load_catalog_skills():
        if entry.get("name") == name:
            return entry
    return None


# ── install/update state (.state.json) ───────────────────────────────

def read_state() -> dict[str, dict]:
    """Read skill state: {name: {version, installed_at, managed, ...}}.

    Asks the subject's **kind** before opening it, for the reason
    :func:`load_catalog_skills` states at length: `.state.json` lives in the same directory
    (`~/.emrg/skills/`), `read_text` on a FIFO blocks at the open and raises nothing, and
    both this function and that one run on the daemon's event loop. Measured 2026-10-11
    (`cyc20261011-100220`): a `mkfifo` at `~/.emrg/skills/.state.json` made this **not
    return in 8 s** where a regular file returned in 0.00 s.

    The `path.exists()` this used to lean on is gone rather than kept: `exists()` is true
    for a FIFO, and a caller that answers "does it exist" when it means "can I open it" is
    how the block got through.
    """
    path = state_path()
    try:
        kind = non_regular_kind(path)
    except OSError:
        # Missing or unstattable: the empty mapping this function already answers with.
        return {}
    if kind:
        logger.debug("cannot parse %s — it is %s, not a regular file", path, kind)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("cannot parse %s — treating as empty", path)
    return {}


def write_state(state: dict[str, dict]) -> None:
    """Atomically write skill state."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        path,
    )


def skill_is_managed(name: str) -> bool:
    """True when a skill is tracked by the catalog update check."""
    return bool(read_state().get(name, {}).get("managed"))


def _atomic_write_text(data: str, target: Path) -> None:
    """Write text via temp file + os.replace (atomic, no partial reads)."""
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=".atomic_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp_path, target)
    except OSError:
        logger.warning("atomic write failed for %s", target, exc_info=True)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
