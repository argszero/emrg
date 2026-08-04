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
    target = tmp_path / "emrgd.port"
    atomic_write_bytes("49152\ns3cret-token", target)

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "49152\ns3cret-token"


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 0600 semantics differ on Windows")
def test_atomic_write_bytes_mode_600(tmp_path: Path):
    """Writes with mode 0o600 by default (token file must be private)."""
    target = tmp_path / "emrgd.port"
    atomic_write_bytes("49152\ntoken", target)

    mode = stat.S_IMODE(os.stat(str(target)).st_mode)
    assert mode == 0o600


def test_atomic_write_bytes_creates_parent_dir(tmp_path: Path):
    """Creates parent directories if they don't exist."""
    target = tmp_path / "deep" / "nested" / "emrgd.port"
    atomic_write_bytes("1\nt", target)

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "1\nt"


def test_atomic_write_bytes_overwrites(tmp_path: Path):
    """Overwrites existing file atomically."""
    target = tmp_path / "emrgd.port"
    target.write_text("old", encoding="utf-8")

    atomic_write_bytes("new", target)

    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_bytes_no_temp_leak(tmp_path: Path):
    """Verifies no temp files remain after write."""
    before = set(os.listdir(str(tmp_path)))
    target = tmp_path / "emrgd.port"
    atomic_write_bytes("1\nt", target)
    after = set(os.listdir(str(tmp_path)))

    assert "emrgd.port" in after
    assert after - before == {"emrgd.port"}


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
