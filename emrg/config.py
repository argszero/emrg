"""Configuration loading for EMRG daemon.

Mirrors the Rust emrg-server/src/config.rs.
Config is stored at ~/.emrg/config.toml.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tomllib  # Python 3.11+ stdlib

from emrg.memory import NotARegularFile
from emrg.tools.base import non_regular_kind


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
    """The ``[sandbox]`` section — which shell tool this instance mounts.

    It mounted a choice until P7: ``bash_tool_v2`` (design D10) selected between
    a process-boundary executor and a frozen one that scanned the command text,
    defaulting to the boundary.  The programme's destination is the boundary, and
    a switch whose loser is about to be deleted is not a choice — so the key, its
    environment override (``EMRG_BASH_TOOL_V2``) and its rollback are gone, and
    the dialect's executor is what gets built (``emrg/tools/shell_dialects.py``
    decides which dialect).  A ``config.toml`` that still carries the key is read
    as before: unknown keys in the section are ignored.

    ``pwsh_path`` names the PowerShell executable the Windows roster mounts
    (design §14.5 item 3).  Empty means the resolution chain decides
    (``emrg/tools/pwsh_tool_v2.py``: PowerShell 7's install directory, then
    ``PATH``, then Windows PowerShell 5.1 — which every Windows host has, so
    "the executable is missing" is not a reachable state there).  A value set
    here is trusted as-is, exactly as the blueprint trusts its own ``pwshPath``.
    """

    pwsh_path: str = ""


@dataclass
class EmrgConfig:
    llm: LlmConfig = field(default_factory=LlmConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)



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


def logs_dir() -> Path:
    """Returns the directory holding EMRG's application-level log files.

    **The one derivation of that directory** (rant 2026-10-09T14:20:18): every
    writer, reader and tail-reader of `emrgd.log`, `emrgd-crash.log`,
    `emrgd-exit.log`, `emrgd-start.err` and `emrg-gui.log` takes it from here
    rather than spelling `Path.home() / ".emrg"` again — the root had collected
    nine log files before this, which is what the host asked to stop. The names
    themselves and the migration of the legacy leftovers live in
    `emrg/logfiles.py`.

    The JS side (`emrg/gui/daemon_client.js`, `emrg/gui/main.js`) cannot import
    this, so it carries its own named constant and
    `tests/test_log_dir_pairing.py` pins the two sides to the same path — the
    treatment `EMRGD_PORT` already gets.
    """
    return config_dir() / "logs"


def config_path() -> Path:
    """Returns the config file path."""
    home = Path.home()
    return home / ".emrg" / "config.toml"



def _refuse_non_regular(path: Path) -> None:
    """Refuse a config subject that is not a regular file, naming its kind (issue #2103).

    ``kind`` comes from the tool layer's path-taking reader
    (:func:`emrg.tools.base.non_regular_kind` — the one ``#2062``, ``#2076`` and the
    daemon's own readers share), and the refusal is
    :class:`emrg.memory.NotARegularFile`, which is an ``OSError`` naming the kind.

    Being an ``OSError`` is the point rather than an implementation detail: every reader
    in this module already answers a file it cannot read — a missing one (``load_config``
    raises ``FileNotFoundError``), a corrupt one (the two section loaders fall back to
    their defaults) — so routing the refusal through that same class keeps each reader's
    contract exactly as written. The only behaviour that changes is that the answer
    *arrives*.

    Measured 2026-10-11 (``cyc20261011-134612``) on master ``3f18f6a6``, one child process
    per arm under this parent's 8 s cap, the subject built under a ``TemporaryDirectory``
    with ``config_path`` re-pointed at it before any call:

        arm                          regular            directory              FIFO
        load_config(path=…)          returns            IsADirectoryError      no return, 8 s
        load_config()                returns            IsADirectoryError      no return, 8 s
        load_sandbox_config()        returns            returns defaults       no return, 8 s
        load_update_config()         returns            returns defaults       no return, 8 s
        fingerprint(path)            returns a hash     returns None           no return, 8 s
        ConfigReloader(…, path=…)    constructs         constructs             no return, 8 s
        ensure_config()              returns            returns                returns

    The **directory** column is the reading that fixes the design: the kind one over
    already produces each site's answer, so the refusal adds no new answer — and the
    ``ensure_config`` row is why this module guards three readers and not a writer:
    ``if cfg_path.exists(): return`` is its first act, ``exists()`` is **true** for a FIFO,
    so its ``write_text`` is never reached. That one is measured, not assumed.

    An **open is not a read**: on a FIFO it waits at the open until a writer appears and
    raises nothing, so no ``except (OSError, …)`` around these call sites can bound the
    consequence. Two of them run before the daemon has an event loop at all — they are
    called from ``EmrgServer.__init__`` (``daemon.py:637``/``:643``/``:656``) — and the
    reload tick reads the file on the loop every ``POLL_INTERVAL_SECONDS``.
    """
    # A missing path is not a refusal: it raises `FileNotFoundError` from the `stat`,
    # which is the answer callers of a missing config already handle. One syscall,
    # on the path the open is about to follow.
    kind = non_regular_kind(path)
    if kind:
        raise NotARegularFile(
            f"{path} is {kind}, not a regular file. This reader opens the config file "
            f"directly, and opening it blocks until a peer appears — a FIFO waits for a "
            f"writer, a socket for a connection — so the call would never return, and "
            f"neither would the daemon: two of these readers run in "
            f"`EmrgServer.__init__`, before any handler exists to time them out."
        )


def _read_config_text(path: Path) -> str:
    """``path.read_text()`` after asking its kind (``_refuse_non_regular``).

    Every read of the config file in this module goes through here, and
    ``tests/test_config.py::test_no_read_text_reaches_the_config_unasked`` parses this
    file to keep it that way: a reader that reaches its subject through ``Path``'s own
    convenience method is the shape that keeps being missed (``#2095``/``#2096``).
    """
    _refuse_non_regular(path)
    return path.read_text(encoding="utf-8")


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

    content = _read_config_text(cfg_path)
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
    pwsh_path = section.get("pwsh_path", "")
    return SandboxConfig(
        pwsh_path=pwsh_path if isinstance(pwsh_path, str) else "",
    )


def load_sandbox_config() -> SandboxConfig:
    """Load only the ``[sandbox]`` section.

    The daemon constructs its tool registry from this. Missing config file or
    missing section → the defaults (the ``pwsh_path`` chain decides), exactly as
    :func:`load_update_config` tolerates both. There is no environment override:
    the one that existed named a rollback P7 retired.
    """
    cfg = SandboxConfig()
    cfg_path = config_path()
    if cfg_path.exists():
        try:
            data = tomllib.loads(_read_config_text(cfg_path))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        cfg = _sandbox_from(data)
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
        data = tomllib.loads(_read_config_text(cfg_path))
    except (OSError, tomllib.TOMLDecodeError):
        return UpdateConfig()
    update_data = data.get("update", {})
    return UpdateConfig(
        enabled=update_data.get("enabled", True),
        delay_minutes=update_data.get("delay_minutes", 1440),
    )


#: The mode the config file must have. It holds ``api_key`` in plaintext, so it is private
#: on the same rule as the daemon's token file (``emrg.server.atomic.atomic_write_bytes``)
#: and as the GUI's own writer, which states it as ``G69`` — "mode 0o600（含明文 api_key）".
#: The two writers agreed about the file's content and disagreed about its permissions
#: (measured 2026-10-12: this function left it ``0644`` under umask 022 while the GUI wrote
#: ``0o600``), which is the defect this constant closes.
CONFIG_FILE_MODE = 0o600


def ensure_config() -> None:
    """Create the default config file if it doesn't exist, and keep it private.

    The one writer of the config file, and the one open in this module that needs no kind
    guard — measured, not assumed (issue #2103): its first act is the ``exists()`` return
    below, and ``exists()`` is **true** for a FIFO, a socket and a directory alike, so a
    non-regular subject never reaches the ``write_text``. Guarding it would be code no
    reading can reach.

    Two rules, one file. The kind rule is the paragraph above; the **permission** rule is
    :data:`CONFIG_FILE_MODE`, applied to a file this function creates *and* to one it finds
    already there — an install made before the rule existed keeps its ``0644`` otherwise,
    and the plaintext key a user adds to it is readable by every other local user for the
    life of that install. Tightening a mode is one-way-safe: the owner keeps full access,
    and nothing reads the mode.
    """
    cfg_path = config_path()

    if cfg_path.exists():
        _make_config_private(cfg_path)
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
    _make_config_private(cfg_path)
    print(f"Default config created at {cfg_path}", file=sys.stderr)
    print("Edit it to set your API key and model.", file=sys.stderr)


def _make_config_private(cfg_path: Path) -> None:
    """Tighten ``cfg_path`` to :data:`CONFIG_FILE_MODE` unless it is already that or narrower.

    Called on both of :func:`ensure_config`'s paths, so a file this function creates and one
    an earlier install left behind get the same answer. The mode is read first: a file already
    at the rule, or closed further than it, is left untouched.

    The rule is applied *after* the ``write_text`` rather than by writing through the atomic
    primitive that owns this mode for the token file. The reason is what the window between
    the two calls can hold: this function only reaches its create path when no file exists,
    and what it puts there is the placeholder template — the host's key arrives later, by an
    edit, when the mode is already the rule. So the gap never holds a secret, and the write
    the census holds for this module (issue #2103) is the one that stays.

    A failure is swallowed the way every other best-effort mode write in this tree is: on a
    filesystem that does not carry POSIX modes (a FAT volume, a Windows share) the call
    either raises or does nothing, and neither is a reason to refuse to start the daemon. The
    file's *contents* are what the caller came for.
    """
    try:
        current = stat.S_IMODE(cfg_path.stat().st_mode)
        # `0o077` is the whole test, and the arm in `tests/test_config.py` is what proves it is
        # doing the work: a file already at `CONFIG_FILE_MODE` carries none of these bits, so
        # the write is skipped for it without a second branch — and a mode narrower than the
        # rule (0400, or a set-id bit its owner meant) is left alone, because this function's
        # job is to close a file to other users, never to open one already shut further.
        # An equality branch here was written first and **measured redundant**: an arm that
        # removed it survived every test, which is how the dead line was found (2026-10-12).
        if current & 0o077:
            os.chmod(cfg_path, CONFIG_FILE_MODE)
    except OSError:
        pass
