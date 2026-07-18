"""Tests for emrg.config."""

import tempfile
from pathlib import Path

import pytest

from emrg.config import (
    LlmConfig,
    EmrgConfig,
    config_dir,
    config_path,
    load_config,
    ensure_config,
)


def test_config_dir():
    d = config_dir()
    assert isinstance(d, Path)
    assert d.name == ".emrg"


def test_config_path():
    p = config_path()
    assert isinstance(p, Path)
    assert p.name == "config.toml"
    assert p.parent.name == ".emrg"


def test_llm_config_defaults():
    cfg = LlmConfig()
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.api_key == ""
    assert cfg.model == "gpt-4o-mini"
    assert cfg.max_tokens == 4096
    assert cfg.temperature == 0.7
    assert cfg.max_tool_rounds == 270
    assert cfg.context_window == 131072
    assert cfg.auto_compact_threshold == 0.0
    assert cfg.evolution_interval == 1800


def test_emrg_config_defaults():
    cfg = EmrgConfig()
    assert isinstance(cfg.llm, LlmConfig)
    assert cfg.llm.model == "gpt-4o-mini"


def test_load_config_valid(tmp_path: Path, monkeypatch):
    """Load a minimal valid config file."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("""[llm]
api_key = "sk-test-123"
model = "gpt-4"
""")
    monkeypatch.setattr("emrg.config.config_path", lambda: config_file)
    cfg = load_config()
    assert cfg.llm.api_key == "sk-test-123"
    assert cfg.llm.model == "gpt-4"
    assert cfg.llm.max_tokens == 4096  # default


def test_load_config_resolves_env_var(monkeypatch, tmp_path):
    """${VAR} placeholders in api_key are resolved from environment."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("""[llm]
api_key = "${MY_API_KEY}"
""")
    monkeypatch.setattr("emrg.config.config_path", lambda: config_file)
    monkeypatch.setenv("MY_API_KEY", "env-resolved-key")
    cfg = load_config()
    assert cfg.llm.api_key == "env-resolved-key"


def test_load_config_env_var_not_set_keeps_placeholder(monkeypatch, tmp_path):
    """If env var is not set, keep the placeholder as-is."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("""[llm]
api_key = "${UNSET_VAR}"
""")
    monkeypatch.setattr("emrg.config.config_path", lambda: config_file)
    monkeypatch.delenv("UNSET_VAR", raising=False)
    cfg = load_config()
    assert cfg.llm.api_key == "${UNSET_VAR}"


def test_load_config_missing():
    """load_config raises when config.toml doesn't exist."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("emrg.config.config_path", lambda: Path("/no/such/config.toml"))
    with pytest.raises(FileNotFoundError):
        load_config()
    monkeypatch.undo()


def test_ensure_config_creates_if_missing(tmp_path, monkeypatch):
    """ensure_config creates a default config when it doesn't exist."""
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr("emrg.config.config_path", lambda: config_file)
    # Make sure parent exists but file doesn't
    config_file.parent.mkdir(parents=True, exist_ok=True)
    assert not config_file.exists()
    ensure_config()
    assert config_file.exists()
    content = config_file.read_text()
    assert "[llm]" in content
    assert "deepseek-chat" in content


def test_ensure_config_noop_when_exists(tmp_path, monkeypatch):
    """ensure_config is a no-op when config already exists."""
    config_file = tmp_path / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("custom")
    monkeypatch.setattr("emrg.config.config_path", lambda: config_file)
    ensure_config()
    assert config_file.read_text() == "custom"  # unchanged
