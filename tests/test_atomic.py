"""Unit tests for emrg.server.atomic — atomic write utilities."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from emrg.server.atomic import atomic_write_yaml


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
