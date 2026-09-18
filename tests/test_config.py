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
from emrg import config as cfg_mod
from emrg.server import config_reload as cr


def test_config_dir():
    d = config_dir()
    assert isinstance(d, Path)
    assert d.name == ".emrg"


def test_config_path():
    p = config_path()
    assert isinstance(p, Path)
    assert p.name == "config.toml"
    assert p.parent.name == ".emrg"


def test_the_default_config_path_is_redirected_into_the_scratch_tree(tmp_path):
    """`conftest` redirects the default path; this pins that it is in force.

    Read through the *module attribute*, not through this file's own
    `from emrg.config import config_path` — a by-value import is a separate name
    that a fixture patching `emrg.config` cannot reach, which is the trap this
    whole change is about (and the reason this test failed the first time it was
    written: it called its own copy and got the host's path back).

    `test_config_path` above asserts the *shape* of the default resolution, and
    the redirection keeps the shape (`config.toml` under a `.emrg` directory), so
    it cannot tell a redirected run from an unredirected one. Without this pin the
    fixture could be deleted and the only symptom would be the suite quietly
    reading the host's real `~/.emrg/config.toml` again — 202 reads per run,
    measured with a spy on `open`/`io.open` (2026-09-18) and 0 after the
    redirection. Attribution of the 202: `daemon.py:259` (the `ConfigReloader`
    the server builds) 201, `daemon.py:650` (a poll that ticks) 1.
    """
    p = cfg_mod.config_path()
    assert p.parent.name == ".emrg"
    assert p.parent.parent == tmp_path, (
        "the suite resolved the default config path outside the test's scratch "
        "tree — the host's real ~/.emrg/config.toml is being read again"
    )


def test_the_reloader_resolves_the_config_path_inside_the_scratch_tree(tmp_path):
    """The same redirection for the other module that **binds** the name.

    `config_reload.py` does `from emrg.config import ... config_path ...`, so it
    holds its own reference; a fixture that re-pointed only `emrg.config` would
    leave `ConfigReloader.__init__` fingerprinting the host's file. Pinned
    separately because the two are separate names, and the difference is exactly
    the class of defect this suite has hit twice (`tests/test_ws_e2e.py`'s
    module-level binding, `tests/test_config_reload.py`'s 14 host reads).
    """
    assert cr.config_path() == tmp_path / ".emrg" / "config.toml"


def test_llm_config_defaults():
    cfg = LlmConfig()
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.api_key == ""
    assert cfg.model == "gpt-4o-mini"
    assert cfg.max_tokens == 8192
    assert cfg.temperature == 0.7
    assert cfg.max_tool_rounds == 270
    assert cfg.context_window == 131072
    assert cfg.auto_compact_threshold == 0.0


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
    assert cfg.llm.max_tokens == 8192  # default


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
    content = config_file.read_text(encoding="utf-8")
    assert "[llm]" in content
    assert "deepseek-chat" in content


def test_ensure_config_noop_when_exists(tmp_path, monkeypatch):
    """ensure_config is a no-op when config already exists."""
    config_file = tmp_path / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("custom")
    monkeypatch.setattr("emrg.config.config_path", lambda: config_file)
    ensure_config()
    assert config_file.read_text(encoding="utf-8") == "custom"  # unchanged


def test_load_config_with_models(tmp_path, monkeypatch):
    """load_config parses [[llm.models]] list for /model switching."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("""[llm]
api_key = "sk-test-123"
model = "deepseek-chat"

[[llm.models]]
name = "deepseek-chat"
context_window = 131072

[[llm.models]]
name = "deepseek-reasoner"
context_window = 131072
""")
    monkeypatch.setattr("emrg.config.config_path", lambda: config_file)
    cfg = load_config()
    assert cfg.llm.model == "deepseek-chat"
    assert len(cfg.llm.models) == 2
    assert cfg.llm.models[0]["name"] == "deepseek-chat"
    assert cfg.llm.models[0]["context_window"] == 131072
    assert cfg.llm.models[1]["name"] == "deepseek-reasoner"


def test_load_config_no_models_is_empty(tmp_path, monkeypatch):
    """When [[llm.models]] is absent, models list is empty."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("""[llm]
api_key = "sk-test-123"
model = "gpt-4"
""")
    monkeypatch.setattr("emrg.config.config_path", lambda: config_file)
    cfg = load_config()
    assert cfg.llm.models == []


# ── vision resolution (rant 2026-09-17T16:53:02) ───────────────────────────


def test_load_config_vision_prefers_the_matching_entry(tmp_path, monkeypatch):
    """The entry's own `vision` is not dead config: startup resolves it.

    Before this, startup read only the top-level `[llm] vision`, so a model whose
    `[[llm.models]]` entry declared `vision = true` still started as blind.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text("""[llm]
api_key = "sk-test-123"
model = "deepseek-v4-flash"
vision = false

[[llm.models]]
name = "flash"
model = "deepseek-v4-flash"
vision = true
""")
    monkeypatch.setattr("emrg.config.config_path", lambda: config_file)
    cfg = load_config()
    assert cfg.llm.vision is True, "the entry's flag must win over the top-level key"
    assert cfg.llm.vision_default is False, "the fallback stays the top-level value"


def test_load_config_vision_falls_back_to_the_top_level_key(tmp_path, monkeypatch):
    """An entry without `vision` falls back to the top-level key — an answer,
    not a silence — and the default is kept separately for runtime switches."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("""[llm]
api_key = "sk-test-123"
model = "deepseek-chat"
vision = true

[[llm.models]]
name = "deepseek-chat"
context_window = 131072
""")
    monkeypatch.setattr("emrg.config.config_path", lambda: config_file)
    cfg = load_config()
    assert cfg.llm.vision is True
    assert cfg.llm.vision_default is True


def test_load_config_vision_no_matching_entry_is_the_top_level_default(
    tmp_path, monkeypatch
):
    config_file = tmp_path / "config.toml"
    config_file.write_text("""[llm]
api_key = "sk-test-123"
model = "gpt-4o"
vision = true

[[llm.models]]
name = "other"
model = "deepseek-chat"
vision = false
""")
    monkeypatch.setattr("emrg.config.config_path", lambda: config_file)
    cfg = load_config()
    assert cfg.llm.vision is True, "no matching entry → the top-level default decides"


def test_resolve_model_vision_source_names_the_decider():
    from emrg.config import resolve_model_vision

    models = [{"name": "a", "vision": True}, {"name": "b"}]
    assert resolve_model_vision(models, "a", False) == (True, "entry")
    assert resolve_model_vision(models, "b", True) == (True, "top-level-default")
    assert resolve_model_vision(models, "missing", True) == (True, "top-level-default")
    assert resolve_model_vision([], "a", False) == (False, "top-level-default")
    assert resolve_model_vision(None, "a", False) == (False, "top-level-default")


def test_find_model_entry_matches_by_name_or_by_api_id():
    """The one matcher every entry lookup goes through.

    It is asserted here as the *matcher*, apart from any caller, because that is
    what makes the drift impossible: a caller that spells the rule itself is a
    second copy, and the two copies disagreed (the vision resolution matched
    name-or-model, the switch path's `context_window` lookup matched `name` only,
    so a switch by API id inherited the previous model's window silently).
    """
    from emrg.config import find_model_entry

    models = [{"name": "qwen-max", "model": "qwen3.8-max-preview", "context_window": 262144}]
    assert find_model_entry(models, "qwen-max") is models[0]
    assert find_model_entry(models, "qwen3.8-max-preview") is models[0]
    assert find_model_entry(models, "qwen3") is None
    assert find_model_entry([], "qwen-max") is None
    assert find_model_entry(None, "qwen-max") is None


def test_find_model_entry_skips_a_malformed_row_without_raising():
    """The file is user-edited: one bad row must not take the lookup down.

    A row that is not a mapping has no `name`/`model` to match on. Raising here
    would put a `TypeError` on the `/model` path — a switch that dies is worse
    than a row that is skipped, and the row after it still has to be found.
    """
    from emrg.config import find_model_entry

    models = ["not-a-mapping", {"name": "good", "model": "x"}]
    assert find_model_entry(models, "good") is models[1]
    assert find_model_entry(models, "not-a-mapping") is None
