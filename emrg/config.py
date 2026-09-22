"""Configuration loading for EMRG daemon.

Mirrors the Rust emrg-server/src/config.rs.
Config is stored at ~/.emrg/config.toml.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
    # vision: whether the model supports OpenAI vision API (image_url content type).
    # Default false — DeepSeek and most models don't support it.
    vision: bool = False
    # vision_default: the top-level `[llm] vision` — what a model's vision falls
    # back to when its `[[llm.models]]` entry has no `vision` key, or has no entry
    # at all (rant 2026-09-17T16:53:02). Kept separately from `vision` because
    # `vision` is the *effective* value and moves on every `/model` switch: a
    # fallback read from it would inherit the previous model's answer instead of
    # the configured default, which is the defect this field exists to prevent.
    vision_default: bool = False
    # stream_options: None means don't send stream_options at all (for APIs like Kimi).
    # Default is {"include_usage": False} for OpenAI/DeepSeek compatibility.
    stream_options: Optional[dict] = field(default_factory=lambda: {"include_usage": False})
    # context_refresh_interval_ms: minimum interval (ms) between dynamic-context
    # (current time) user-message injections into a tool loop. 0 = inject every
    # request (fresh time, always model-aware). >0 (e.g. 60000) = skip
    # re-injection within the window — the system prompt prefix stays
    # byte-stable for prompt caching (rant 2026-08-23T13:54:14).
    context_refresh_interval_ms: int = 0


def find_model_entry(models: Optional[list[dict]], key: str) -> Optional[dict]:
    """The ``[[llm.models]]`` entry a switch key names, or None.

    One matcher for the whole tree, because two lookups spelling the same rule
    differently drift. ``resolve_model_vision`` matched an entry by ``name``
    **or** ``model``; ``EmrgServer._handle_set_model``'s ``context_window``
    lookup matched ``name`` only — so a host who switched by the API id (which is
    what ``[llm] model`` holds and therefore what is easiest to copy out of
    ``config.toml``) silently kept the **previous** model's context window. Same
    shape as the vision flag had before its own fix (rant 2026-09-17T16:53:02),
    one key over: a value inherited from the model being left, with nothing said.

    ``key`` is what ``/model`` receives: the entry's display ``name`` or its
    ``model`` id. Non-dict entries are skipped rather than raised on — the file is
    user-edited, and one malformed row must not take the switch path down.
    """
    for m in models or []:
        if not isinstance(m, dict):
            continue
        if m.get("name") == key or m.get("model") == key:
            return m
    return None


def resolve_model_vision(
    models: Optional[list[dict]], key: str, default: bool
) -> tuple[bool, str]:
    """Resolve a model's vision flag in one place, with a stated priority.

    Rant 2026-09-17T16:53:02: the flag had two sources and neither held. Startup
    read only the top-level ``[llm] vision`` and never opened the matching
    ``[[llm.models]]`` entry; ``/model`` read only the entry and, on a missing
    key, kept the **previous model's** value rather than falling back to a
    default. So a model that cannot see images could be sent one, and a model
    that can see them could be degraded to text, both silently.

    The resolution is therefore one function, called by both paths:

    * an entry matches by its display ``name`` (what ``/model`` receives) or by
      its ``model`` (what ``[llm] model`` holds in config.toml) — the matching
      itself is ``find_model_entry``, so the switch path's other lookups (the
      entry's ``context_window``, its API id) name the same entry this does;
    * the entry's own ``vision`` wins when the key is present — source
      ``"entry"``;
    * otherwise the top-level ``[llm] vision`` applies — source
      ``"top-level-default"`` — including when no entry matches at all.

    A missing key is an answer, not a silence: the second element names which of
    the two decided, so a caller can log it and the host can read the effective
    value instead of inferring it from a failed attempt to send an image.
    """
    entry = find_model_entry(models, key)
    if entry is not None and "vision" in entry:
        return bool(entry["vision"]), "entry"
    return bool(default), "top-level-default"


@dataclass
class UpdateConfig:
    """Auto-upgrade settings (rant 2026-08-20T12:33:59 — 自动升级重构).

    enabled: master switch — when false, the daemon never checks for new
        releases and never triggers an upgrade session.
    delay_minutes: how long after a release is published before it becomes
        eligible for upgrade (default 1440 = 1 day; the host can set 1 for
        immediate). Granularity is minutes (host chose A). The check
        interval is NOT configurable — hard-coded 5 minutes in upgrade.py.
    Old fields check / ttl_hours / auto_download are removed (the
    download-installer mechanism is fully replaced by the agent-driven
    local equivalent install).
    """

    enabled: bool = True
    delay_minutes: int = 1440


@dataclass
class SandboxConfig:
    """The ``[sandbox]`` section — which bash tool this instance runs.

    ``bash_tool_v2`` chooses between the two executors that answer to the tool
    name ``bash`` (design ``bash-tool-v2-design.md`` D10).  Its default is now
    **the new one**: the boundary it enforces is the OS process boundary rather
    than a static scan of the command text, which is the whole point of the
    programme, and a boundary that is off by default is not the one being
    measured in production.  ``False`` keeps the frozen tool and remains the
    rollback: one line here, or ``EMRG_BASH_TOOL_V2=0`` for a single launch.

    Read **once at startup** by the daemon — unlike ``[llm]`` and ``[update]``
    this key is not hot-reloaded, because it decides which executor is *built*
    into the tool registry, and the registry is constructed once and read-only
    after that (``emrg/tools/registry.py``).  The environment variable
    ``EMRG_BASH_TOOL_V2`` overrides the file in both directions, so the host can
    try either executor in a session without editing ``config.toml``.
    """

    bash_tool_v2: bool = True


@dataclass
class EmrgConfig:
    llm: LlmConfig = field(default_factory=LlmConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)


#: Environment override for :attr:`SandboxConfig.bash_tool_v2`.
ENV_BASH_TOOL_V2 = "EMRG_BASH_TOOL_V2"


def _as_bool(raw: str, default: bool) -> bool:
    """Read a truthy/falsy environment spelling, or fall back.

    :param raw: the variable's text.
    :param default: the value to keep when the text is neither.
    :returns: the parsed flag.
    """
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    return default


def config_dir() -> Path:
    """Returns the EMRG data directory (~/.emrg)."""
    return Path.home() / ".emrg"


def config_path() -> Path:
    """Returns the config file path."""
    home = Path.home()
    return home / ".emrg" / "config.toml"



def load_config(path: Optional[Path] = None) -> EmrgConfig:
    """Load EMRG configuration from ~/.emrg/config.toml.

    `path` is the file to read; it defaults to `config_path()` so every
    existing caller is unchanged. It exists so the daemon's hot-reload path
    (rant 2026-09-17T16:52:57) and its tests can point the loader at a file
    they own — a test must never read the host's real config.
    """
    cfg_path = config_path() if path is None else Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"config not found at {cfg_path} — create it with [llm] section"
        )

    content = cfg_path.read_text(encoding="utf-8")
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
        vision_default=llm_data.get("vision", False),
        # The startup path goes through the same resolution `/model` uses
        # (rant 2026-09-17T16:53:02): an entry's own `vision` wins over the
        # top-level key, so a declared-per-model flag is no longer dead config.
        vision=resolve_model_vision(
            llm_data.get("models", []),
            llm_data.get("model", "gpt-4o-mini"),
            llm_data.get("vision", False),
        )[0],
        stream_options=stream_opts,
        context_refresh_interval_ms=llm_data.get("context_refresh_interval_ms", 0),
    )

    # Resolve ${ENV_VAR} placeholders in the API key
    if llm.api_key.startswith("${") and llm.api_key.endswith("}"):
        var_name = llm.api_key[2:-1]
        llm.api_key = os.environ.get(var_name, llm.api_key)

    update_data = data.get("update", {})
    update = UpdateConfig(
        enabled=update_data.get("enabled", True),
        delay_minutes=update_data.get("delay_minutes", 1440),
    )

    return EmrgConfig(llm=llm, update=update, sandbox=_sandbox_from(data))


def _sandbox_from(data: dict) -> SandboxConfig:
    """Read the ``[sandbox]`` section out of a parsed config file.

    :param data: the parsed TOML document.
    :returns: the section's value, defaulting to the new bash tool.
    """
    section = data.get("sandbox", {})
    if not isinstance(section, dict):
        return SandboxConfig()
    return SandboxConfig(bash_tool_v2=bool(section.get("bash_tool_v2", True)))


def load_sandbox_config() -> SandboxConfig:
    """Load only the ``[sandbox]`` section (design D10).

    The daemon constructs its tool registry from this. Missing config file or
    missing section → the default (the new bash tool), exactly as
    :func:`load_update_config` tolerates both. The environment override is
    applied **last**, so it wins over the file in both directions — including
    the rollback to the frozen tool without editing ``config.toml``.
    """
    cfg = SandboxConfig()
    cfg_path = config_path()
    if cfg_path.exists():
        try:
            data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        cfg = _sandbox_from(data)
    raw = os.environ.get(ENV_BASH_TOOL_V2)
    if raw is not None:
        cfg = SandboxConfig(bash_tool_v2=_as_bool(raw, cfg.bash_tool_v2))
    return cfg


def load_update_config() -> UpdateConfig:
    """Load only the [update] section (rant 2026-08-10T07:12:12).

    The daemon constructs the UpgradeManager from this helper. Missing config
    file or missing section → defaults (enabled=True, delay_minutes=1440).
    """
    cfg_path = config_path()
    if not cfg_path.exists():
        return UpdateConfig()
    try:
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return UpdateConfig()
    update_data = data.get("update", {})
    return UpdateConfig(
        enabled=update_data.get("enabled", True),
        delay_minutes=update_data.get("delay_minutes", 1440),
    )


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
# vision: set to true if model supports OpenAI vision API (image_url content type).
# This is the DEFAULT for every model that says nothing below: a [[llm.models]]
# entry with its own `vision` key wins, and an entry without one (or no entry at
# all) falls back to this value — it never inherits the previous model's answer.
# The value actually in force is what the daemon reports, not this declaration:
# the TUI shows it next to the model name in the status bar.
vision = false

# Additional models for /model switching (optional — add or remove as needed)
# model: API model name (optional — defaults to name if not set)
[[llm.models]]
name = "deepseek-v3"
model = "deepseek-chat"
context_window = 131072
vision = false

[[llm.models]]
name = "deepseek-r1"
model = "deepseek-reasoner"
context_window = 65536
vision = false
""", encoding="utf-8")
    print(f"Default config created at {cfg_path}", file=sys.stderr)
    print("Edit it to set your API key and model.", file=sys.stderr)
