"""Configuration loading for EMRG daemon.

Mirrors the Rust emrg-server/src/config.rs.
Config is stored at ~/.emrg/config.toml.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import tomllib  # Python 3.11+ stdlib


@dataclass
class LlmConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    max_tokens: int = 8192
    temperature: float = 0.7
    # 更大的轮数允许 LLM 完成更复杂的多步骤任务（如大规模重构、深度分析）。
    # 30 轮在复杂任务中经常不够用，导致任务被截断。270 轮给予充足空间。
    # 演化系统可能会尝试将此值改小——保留此注释以说明意图。
    max_tool_rounds: int = 270
    context_window: int = 131072
    auto_compact_threshold: float = 0.0
    models: list[dict] = field(default_factory=list)  # [[llm.models]] for /model switching
    # stream_options: None means don't send stream_options at all (for APIs like Kimi).
    # Default is {"include_usage": False} for OpenAI/DeepSeek compatibility.
    stream_options: Optional[dict] = field(default_factory=lambda: {"include_usage": False})


@dataclass
class EmrgConfig:
    llm: LlmConfig = field(default_factory=LlmConfig)


def config_dir() -> Path:
    """Returns the EMRG data directory (~/.emrg)."""
    return Path.home() / ".emrg"


def config_path() -> Path:
    """Returns the config file path."""
    home = Path.home()
    return home / ".emrg" / "config.toml"



def load_config() -> EmrgConfig:
    """Load EMRG configuration from ~/.emrg/config.toml."""
    cfg_path = config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"config not found at {cfg_path} — create it with [llm] section"
        )

    content = cfg_path.read_text()
    data = tomllib.loads(content)
    llm_data = data.get("llm", {})

    # stream_options: None = no stream_options sent (for Kimi etc.)
    raw_stream_opts = llm_data.get("stream_options")
    if raw_stream_opts is None and "stream_options" not in llm_data:
        stream_opts: Optional[dict] = {"include_usage": False}  # default
    else:
        stream_opts = raw_stream_opts  # explicit None disables it

    llm = LlmConfig(
        base_url=llm_data.get("base_url", "https://api.openai.com/v1"),
        api_key=llm_data.get("api_key", ""),
        model=llm_data.get("model", "gpt-4o-mini"),
        max_tokens=llm_data.get("max_tokens", 8192),
        temperature=llm_data.get("temperature", 0.7),
        max_tool_rounds=llm_data.get("max_tool_rounds", 270),
        context_window=llm_data.get("context_window", 131072),
        auto_compact_threshold=llm_data.get("auto_compact_threshold", 0.0),
        models=llm_data.get("models", []),
        stream_options=stream_opts,
    )

    # Resolve ${ENV_VAR} placeholders in the API key
    if llm.api_key.startswith("${") and llm.api_key.endswith("}"):
        var_name = llm.api_key[2:-1]
        llm.api_key = os.environ.get(var_name, llm.api_key)

    return EmrgConfig(llm=llm)


def ensure_config() -> None:
    """Create default config file if it doesn't exist."""
    cfg_path = config_path()
    if cfg_path.exists():
        return

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("""[llm]
# OpenAI-compatible API endpoint
base_url = "https://api.deepseek.com"
api_key = "sk-..."
model = "deepseek-chat"
max_tokens = 8192
temperature = 0.7
context_window = 131072
auto_compact_threshold = 0.0

# Additional models for /model switching (optional — add or remove as needed)
[[llm.models]]
name = "deepseek-v3"
context_window = 131072

[[llm.models]]
name = "deepseek-r1"
context_window = 131072
""")
    print(f"Default config created at {cfg_path}", file=sys.stderr)
    print("Edit it to set your API key and model.", file=sys.stderr)
