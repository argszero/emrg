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


# ── what the writer tells its caller ──────────────────────────────────────
#
# Measured 2026-10-02 (cycle cyc20261002-163118): the whole file above passed with
# the writer made non-atomic — every leg asserts content, and content is what
# `write_text` also produces. Two holes were behind that:
#
# * `mkdir` and `tempfile.mkstemp` sat *above* the `try`, so the first failure a host
#   really meets (a directory that cannot be made, a temp file that cannot be created
#   — permission, or ENOSPC) escaped as an OSError while every later failure was
#   swallowed. With the config dir read-only, `PermissionError` came out of
#   `task_create`.
# * the function returned None either way, so a caller could not tell "wrote the
#   file" from "warned in the log" — and `task_create` answered "created" for a task
#   that was never persisted.
#
# These legs pin both: the report, and the swap that makes the write atomic (which
# is the thing the class name promises and no leg above would notice).


class TestTheWriteIsReported:
    def test_a_write_that_landed_reports_true(self, tmp_path: Path):
        target = tmp_path / "log.yml"
        assert atomic_write_yaml([{"a": 1}], target) is True
        assert yaml.safe_load(target.read_text(encoding="utf-8")) == [{"a": 1}]

        blob = tmp_path / "token"
        assert atomic_write_bytes("s3cret", blob) is True
        assert blob.read_text(encoding="utf-8") == "s3cret"

    def test_a_parent_that_cannot_be_made_reports_false(self, tmp_path: Path):
        """`mkdir` is the first thing that can fail, and it is inside the guard now."""
        blocked = tmp_path / "a-file"
        blocked.write_text("not a directory", encoding="utf-8")
        target = blocked / "log.yml"

        assert atomic_write_yaml([{"a": 1}], target) is False
        assert not target.exists()
        assert blocked.read_text(encoding="utf-8") == "not a directory"

    def test_a_temp_file_that_cannot_be_made_reports_false(self, tmp_path: Path, monkeypatch):
        """The mkstemp hole itself: it raised out of the function before this."""
        import emrg.server.atomic as mod

        def refuse(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(mod.tempfile, "mkstemp", refuse)
        assert atomic_write_yaml([{"a": 1}], tmp_path / "l.yml") is False
        assert atomic_write_bytes("x", tmp_path / "t.txt") is False
        assert list(tmp_path.iterdir()) == [], "a failed write left something behind"

    def test_a_replace_that_cannot_happen_reports_false_and_keeps_the_old_file(
        self, tmp_path: Path
    ):
        """The other half: the temp file is written, and the swap is what fails."""
        target = tmp_path / "occupied.yml"
        target.mkdir()
        (target / "inside").write_text("still here", encoding="utf-8")

        assert atomic_write_yaml([{"a": 1}], target) is False
        assert (target / "inside").read_text(encoding="utf-8") == "still here"
        assert [p.name for p in tmp_path.iterdir()] == ["occupied.yml"], (
            "the temp file of a failed write must be cleaned up"
        )

    def test_no_reader_ever_sees_a_half_written_file(self, tmp_path: Path, monkeypatch):
        """The property the module is named for, stated as an observation.

        At the instant of the swap the target still holds the **old, complete**
        document: that is what "a reader cannot see a partial file" means, and it is
        what an in-place `write_text` cannot offer — there the target is truncated
        first, so a reader in that window sees an empty or half a file. An in-place
        writer makes this leg fail on its first assertion, because it never swaps.
        """
        import os as os_mod

        target = tmp_path / "log.yml"
        atomic_write_yaml([{"generation": 1}], target)
        seen_at_swap: list[str] = []
        real_replace = os_mod.replace

        def watched(src, dst, *args, **kwargs):
            seen_at_swap.append(Path(dst).read_text(encoding="utf-8"))
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os_mod, "replace", watched)
        assert atomic_write_yaml([{"generation": 2}], target) is True

        assert seen_at_swap, "the write never swapped a file into place — it is not atomic"
        assert yaml.safe_load(seen_at_swap[0]) == [{"generation": 1}], (
            "a reader during the write saw the new content before the swap: the "
            "target was written in place"
        )
        assert yaml.safe_load(target.read_text(encoding="utf-8")) == [{"generation": 2}]

    def test_a_reported_success_is_what_the_disk_says(self, tmp_path: Path):
        """The control for the failure legs: True must not be handed out for free."""
        target = tmp_path / "log.yml"
        assert atomic_write_yaml([{"a": 1}], target) is True
        target.unlink()
        blocked = tmp_path / "f"
        blocked.write_text("x", encoding="utf-8")
        assert atomic_write_yaml([{"a": 1}], blocked / "log.yml") is False
