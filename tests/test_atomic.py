"""Unit tests for emrg.server.atomic — atomic write utilities."""

from __future__ import annotations

import os
import stat
import sys

import pytest
from pathlib import Path

import yaml

from emrg.server.atomic import atomic_write_bytes, atomic_write_yaml


def test_atomic_write_and_read(tmp_path: Path):
    """Writes YAML data and reads it back."""
    target = tmp_path / "test.yml"
    data = [{"name": "emrg", "path": "/tmp/emrg"}]
    atomic_write_yaml(data, target)

    assert target.exists()
    content = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert content == data


def test_atomic_write_creates_parent_dir(tmp_path: Path):
    """Creates parent directories if they don't exist."""
    target = tmp_path / "deep" / "nested" / "data.yml"
    data = [{"key": "value"}]
    atomic_write_yaml(data, target)

    assert target.exists()
    content = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert content == data


def test_atomic_write_overwrites(tmp_path: Path):
    """Overwrites existing file atomically."""
    target = tmp_path / "config.yml"
    target.write_text("old: data", encoding="utf-8")

    data = [{"new": "content"}]
    atomic_write_yaml(data, target)

    content = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert content == data


def test_atomic_write_empty_list(tmp_path: Path):
    """Writes an empty list."""
    target = tmp_path / "empty.yml"
    atomic_write_yaml([], target)

    assert target.exists()
    content = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert content == []


def test_atomic_write_no_temp_leak(tmp_path: Path):
    """Verifies no temp files remain after write."""
    before = set(os.listdir(str(tmp_path)))
    target = tmp_path / "projects.yml"
    atomic_write_yaml([{"name": "test"}], target)
    after = set(os.listdir(str(tmp_path)))

    # Only the target file should exist, no temp leftovers
    assert "projects.yml" in after
    assert after - before == {"projects.yml"}


def test_atomic_write_bytes_basic(tmp_path: Path):
    """Writes a text blob and reads it back."""
    target = tmp_path / "emrgd.token"
    atomic_write_bytes("s3cret-token", target)

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "s3cret-token"


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 0600 semantics differ on Windows")
def test_atomic_write_bytes_mode_600(tmp_path: Path):
    """Writes with mode 0o600 by default (token file must be private)."""
    target = tmp_path / "emrgd.token"
    atomic_write_bytes("token", target)

    mode = stat.S_IMODE(os.stat(str(target)).st_mode)
    assert mode == 0o600


def test_atomic_write_bytes_creates_parent_dir(tmp_path: Path):
    """Creates parent directories if they don't exist."""
    target = tmp_path / "deep" / "nested" / "emrgd.token"
    atomic_write_bytes("t", target)

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "t"


def test_atomic_write_bytes_overwrites(tmp_path: Path):
    """Overwrites existing file atomically."""
    target = tmp_path / "emrgd.token"
    target.write_text("old", encoding="utf-8")

    atomic_write_bytes("new", target)

    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_bytes_no_temp_leak(tmp_path: Path):
    """Verifies no temp files remain after write."""
    before = set(os.listdir(str(tmp_path)))
    target = tmp_path / "emrgd.token"
    atomic_write_bytes("t", target)
    after = set(os.listdir(str(tmp_path)))

    assert "emrgd.token" in after
    assert after - before == {"emrgd.token"}


def test_atomic_write_custom_prefix(tmp_path: Path):
    """Custom prefix/suffix are respected."""
    target = tmp_path / "custom.yml"
    atomic_write_yaml([{"a": 1}], target, prefix=".my_", suffix=".bak")

    assert target.exists()
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == [{"a": 1}]


def test_atomic_write_cjk_content(tmp_path: Path):
    """Handles CJK characters correctly."""
    target = tmp_path / "chinese.yml"
    data = [{"name": "进化", "描述": "自我演化模块"}]
    atomic_write_yaml(data, target)

    content = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert content == data


# ── a writer reports whether its bytes landed ────────────────────────────────
#
# Both writers returned `None` in *both* states, so no caller could tell a write that
# happened from one that did not — and a caller that reported the outcome anyway reported
# the wrong one. Measured 2026-10-12 (`cyc20261012-115759`) one layer up: with a directory
# at `.git/info/exclude`, `os.replace` raised `IsADirectoryError`, this module's own
# handler logged it and swallowed it, and `ensure_local_exclude` answered `"added"` — a
# success report its two callers log as "this tree now ignores EMRG's runtime directory".
#
# The arms below pin the report itself, in both directions. The failing subject is a
# **directory**: it needs no `mkfifo` (every platform has directories), and it is a
# reachable shape rather than a synthetic one — `os.replace` cannot overwrite a directory
# on POSIX (`EISDIR`) or on Windows (a `PermissionError`, also an `OSError`), and the same
# branch answers a read-only mount and a full disk.


def test_atomic_write_bytes_reports_a_write_that_did_not_land(tmp_path: Path):
    """The measured defect: the target could not be replaced, so the answer is False."""
    target = tmp_path / "emrgd.token"
    target.mkdir()

    assert atomic_write_bytes("s3cret", target) is False, (
        "a write the replace could not complete must not be reported as a success"
    )
    assert target.is_dir(), "and the subject it could not overwrite is left as it was"
    assert [p.name for p in tmp_path.iterdir()] == ["emrgd.token"], (
        "the temp file it wrote into is cleaned up either way"
    )


def test_atomic_write_yaml_reports_a_write_that_did_not_land(tmp_path: Path):
    """The sibling writer, same question — one convention in this module, not two."""
    target = tmp_path / "projects.yml"
    target.mkdir()

    assert atomic_write_yaml([{"name": "emrg"}], target) is False
    assert target.is_dir()
    assert [p.name for p in tmp_path.iterdir()] == ["projects.yml"]


def test_atomic_write_bytes_reports_a_write_that_landed(tmp_path: Path):
    """The control, and the direction that keeps the pair discriminating: a guard that
    always answered False would satisfy the two arms above and break every caller."""
    target = tmp_path / "emrgd.token"

    assert atomic_write_bytes("t", target) is True
    assert target.read_text(encoding="utf-8") == "t"


def test_atomic_write_yaml_reports_a_write_that_landed(tmp_path: Path):
    """The same control for the other writer, replacing an existing file."""
    target = tmp_path / "projects.yml"
    target.write_text("old: data", encoding="utf-8")

    assert atomic_write_yaml([{"name": "emrg"}], target) is True
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == [{"name": "emrg"}]
