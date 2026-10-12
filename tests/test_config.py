"""Tests for emrg.config."""

import os
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
from emrg.memory import NotARegularFile
from emrg.server import config_reload as cr
from emrg.tools.base import non_regular_kind
from tests.bounded_read import DEADLINE_SECONDS, within


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


# ── the kind-before-open guard (issue #2103) ─────────────────────────
#
# The reads below are bounded by `tests.bounded_read.within`, the one deadline this suite
# keeps. An unbounded read is the failure mode this section is about: a FIFO blocks at the
# **open** and raises nothing, so a test that calls the reader directly has only two
# outcomes — the reader returns, or the whole pytest run wedges — and a wedged run is
# unreadable as a failure (a cancelled CI job reads as `no_verdict`, whose remedy is to
# re-trigger the same head). The deadline is what turns a regression here into a failed
# test that names the cause.
#
# `_plant` is the only way a subject is made in this section, and it always builds it under
# the test's own `tmp_path`: the config path is the host's runtime state, so a test may
# only ever hand these readers a file it created itself.
#
# A FIFO needs `os.mkfifo`, so every FIFO arm is POSIX-gated; each one has a **FIFO-free**
# counterpart on a directory, which runs on both legs and is the *kind one over* whose
# answer the design copies.


def _plant(path: Path, kind: str) -> None:
    """Put a non-regular subject where a config file is expected."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "dir":
        path.mkdir()
    else:
        os.mkfifo(path)


def test_load_config_refuses_a_directory_subject(tmp_path, monkeypatch):
    """The FIFO-free control: a directory is refused by name, not opened.

    This is the reading the whole design leans on. A directory at the config path already
    produced an answer — `IsADirectoryError`, an `OSError`, out of `read_text` — so the
    refusal is the same class of answer with the kind named rather than a new outcome, and
    every caller's existing handler keeps working unchanged.
    """
    cfg_path = tmp_path / ".emrg" / "config.toml"
    _plant(cfg_path, "dir")
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)

    with pytest.raises(NotARegularFile) as excinfo:
        within(DEADLINE_SECONDS, load_config)

    assert isinstance(excinfo.value, OSError), (
        "callers answer this file's trouble with `except OSError`; the refusal has to be one"
    )
    assert "a directory" in str(excinfo.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="a FIFO needs os.mkfifo (POSIX)")
def test_load_config_refuses_a_fifo_subject(tmp_path, monkeypatch):
    """A FIFO at the config path raises, where before the call never returned at all.

    Measured 2026-10-11 (`cyc20261011-134612`) in a detached worktree of `3f18f6a6`: this
    call did **not return within 8 s**, because the open waits for a writer and raises
    nothing, so no handler around it can fire.
    """
    cfg_path = tmp_path / ".emrg" / "config.toml"
    _plant(cfg_path, "fifo")
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)

    with pytest.raises(NotARegularFile) as excinfo:
        within(DEADLINE_SECONDS, load_config)

    assert "FIFO" in str(excinfo.value)


def test_the_section_loaders_degrade_on_a_directory_subject(tmp_path, monkeypatch):
    """The readers whose contract is a default keep it: they degrade, never raise.

    `load_sandbox_config` and `load_update_config` both read through
    `except (OSError, TOMLDecodeError)` and their docstrings promise defaults for a file
    they cannot read. `NotARegularFile` **is** an `OSError`, so the refusal lands in the
    handler already written there — the answer a directory already gets, one kind over.
    A guard that raised instead would turn a documented default into a crash on a path
    the daemon builds itself in `__init__`.
    """
    cfg_path = tmp_path / ".emrg" / "config.toml"
    _plant(cfg_path, "dir")
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)

    assert within(DEADLINE_SECONDS, cfg_mod.load_sandbox_config) == cfg_mod.SandboxConfig()
    assert within(DEADLINE_SECONDS, cfg_mod.load_update_config) == cfg_mod.UpdateConfig()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="a FIFO needs os.mkfifo (POSIX)")
def test_the_section_loaders_degrade_on_a_fifo_subject(tmp_path, monkeypatch):
    """The same two readers, on the subject that used to block: both return 0.00 s.

    Measured before the guard (`3f18f6a6`, detached worktree): both **did not return
    within 8 s**. `EmrgServer.__init__` calls them (`daemon.py:637`/`:643`), so the block
    was the daemon's construction, before any event loop or handler existed.
    """
    cfg_path = tmp_path / ".emrg" / "config.toml"
    _plant(cfg_path, "fifo")
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)

    assert within(DEADLINE_SECONDS, cfg_mod.load_sandbox_config) == cfg_mod.SandboxConfig()
    assert within(DEADLINE_SECONDS, cfg_mod.load_update_config) == cfg_mod.UpdateConfig()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="a FIFO needs os.mkfifo (POSIX)")
def test_ensure_config_never_opens_a_non_regular_subject(tmp_path, monkeypatch):
    """The writer is the one open in the module that needs no guard — and this pins why.

    Measured, not assumed (`3f18f6a6`, detached worktree): `ensure_config()` **returned**
    with a FIFO at the config path, because its first act is `if cfg_path.exists(): return`
    and `exists()` is true for a FIFO. So the subject is left exactly as it was and no open
    happens — guarding this write would be code no reading can reach.
    """
    cfg_path = tmp_path / ".emrg" / "config.toml"
    _plant(cfg_path, "fifo")
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)

    assert within(DEADLINE_SECONDS, cfg_mod.ensure_config) is None
    assert non_regular_kind(cfg_path) == "a FIFO (named pipe)", (
        "the writer replaced a subject it was told to leave alone"
    )


def test_no_read_text_reaches_the_config_unasked():
    """The census leg: the shape that keeps being missed is `Path`'s own method.

    `emrg/config.py` had four opens and none of them asked. Asking the source rather than
    a reviewer's memory is what `#2095`/`#2096` did for the scheduler, for the reason it
    gave: a bare `read_text` is invisible to a grep for `open(`, and a reader added later
    must not be able to skip the guard quietly.

    The write is the measured exemption, and it is an **enumerated list** rather than a
    wildcard: `ensure_config` is the one writer, and the leg above is what holds its
    reason. Any `read_text`/`write_text`/`open(` outside its home fails here.
    """
    import ast

    source = Path(cfg_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    homes = {"read_text": "_read_config_text", "write_text": "ensure_config", "open": None}

    def enclosing(stack: list[str]) -> str:
        return stack[-1] if stack else "<module>"

    found: dict[str, set[str]] = {}
    stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in homes:
                found.setdefault(func.attr, set()).add(enclosing(stack))
            elif isinstance(func, ast.Name) and func.id == "open":
                found.setdefault("open", set()).add(enclosing(stack))
            self.generic_visit(node)

    Visitor().visit(tree)
    for shape, home in homes.items():
        callers = found.get(shape, set())
        assert callers <= {home}, (
            f"{shape} is called from {sorted(callers - {home})}, outside its home "
            f"{home!r} — every open in this module asks its subject's kind first "
            f"(issue #2103)"
        )
    # A leg that finds no call at all would pass vacuously: the two shapes that exist must
    # be seen, or this test has stopped reading the module it names.
    assert found.get("read_text") == {"_read_config_text"}
    assert found.get("write_text") == {"ensure_config"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="a FIFO needs os.mkfifo (POSIX)")
def test_the_foreground_daemon_command_reports_a_refused_config(
    tmp_path, monkeypatch, capsys
):
    """`emrg server` (foreground) says why and exits 1, instead of a traceback.

    The command's answer for a config it cannot read is one line and exit 1 — the shape it
    already had for a missing file — and the refusal is a new way to be unreadable, so it
    takes the same exit. Without the handler it escapes as an unhandled `NotARegularFile`
    traceback, which this arm kills.

    Only two outcomes are reachable here and **neither starts a daemon**: the guard
    refuses (the assertion below), or the guard is gone and `load_config` blocks at the
    open, which the deadline turns into a failed test. The subject is a FIFO, never a
    readable config, so `asyncio.run(run_server(...))` is unreachable by construction —
    the safety of this test does not rest on the guard working.

    `ensure_tool_dirs` is stubbed because it rewrites this process's `PATH`, which no test
    of a config reader should leave mutated for its neighbours.
    """
    cfg_path = tmp_path / ".emrg" / "config.toml"
    _plant(cfg_path, "fifo")
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)
    monkeypatch.setattr("emrg.tool_path.ensure_tool_dirs", lambda: None)

    from emrg.__main__ import _run_daemon

    with pytest.raises(SystemExit) as excinfo:
        within(DEADLINE_SECONDS, _run_daemon)

    assert excinfo.value.code == 1
    assert "not a regular file" in capsys.readouterr().err


class TestTheInstallerWritesTheConfigPrivate:
    """`install.sh` is the third writer of `~/.emrg/config.toml`, and the one that creates it.

    `ensure_config()` and the GUI's writers were brought onto one rule
    (`emrg.config.CONFIG_FILE_MODE`), but the installer — which runs first, on a machine where
    nothing exists yet — wrote the file through a bare `cat > "$cfg"` heredoc and no `chmod`,
    so it landed `0644` under umask 022. Measured 2026-10-12 on master by extracting
    `generate_config_template()` and running it with `HOME` pinned to a scratch tree
    (issue #2146).

    The reason that is not merely a window: the installer's own closing line tells the host to
    edit the file and paste a key into it *before* running anything, so the mode the installer
    gives the file is the mode it keeps while it holds a real secret.

    The reading is **behavioural** — the real function is extracted from `install.sh` and run —
    rather than a text search for a `chmod`, which a `chmod` that never executes would satisfy.
    The `log()` the script's preamble provides under an installed run is stubbed; that stub is
    not the subject and the function's own output is not read.

    What is asserted is the **property** (`mode & 0o077 == 0`, the mask
    `emrg.config._make_config_private` tests), not the number `0o600`: a shell script and a
    Python module cannot share one constant, so pinning the digit here would leave the two
    carriers free to drift apart one digit at a time with every test green.
    """

    INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"

    posix_only = pytest.mark.skipif(
        os.name == "nt",
        reason="Windows carries no POSIX permission bits for chmod to set, so there is "
        "nothing on that leg for this assertion to be right or wrong about",
    )

    @staticmethod
    def _function_source() -> str:
        """The `generate_config_template` body, lifted out of `install.sh`.

        Anchored on the function's own header and its closing brace at column 0, so a rename
        or a reformat is a loud failure rather than a silent empty string
        (`git ls-files`-style discovery is not available for a shell function).
        """
        import re

        text = TestTheInstallerWritesTheConfigPrivate.INSTALL_SH.read_text(encoding="utf-8")
        match = re.search(r"^generate_config_template\(\) \{.*?^\}", text, re.S | re.M)
        assert match is not None, (
            "generate_config_template() could not be lifted out of install.sh — the pattern "
            "has gone stale, and a stale pattern is not a pass"
        )
        return match.group(0)

    @posix_only
    def test_the_created_config_file_is_private(self, tmp_path):
        import stat as _stat
        import subprocess

        home = tmp_path / "home"
        home.mkdir()
        script = tmp_path / "gen.sh"
        script.write_text(
            # `log` is install.sh's own reporter; stubbed because this test reads the file's
            # mode, not the installer's output.
            "log() { :; }\n"
            + self._function_source()
            + "\ngenerate_config_template\n",
            encoding="utf-8",
        )
        env = dict(os.environ, HOME=str(home))

        subprocess.run(
            ["bash", str(script)],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )

        cfg_path = home / ".emrg" / "config.toml"
        assert cfg_path.exists(), (
            "the installer wrote no config file, so the mode below would be asserted about "
            "nothing — the extraction or the stub is wrong, not the rule"
        )
        mode = _stat.S_IMODE(os.stat(cfg_path).st_mode)
        # The **property**, in the spelling the Python-side writer uses
        # (`emrg.config._make_config_private` tests exactly this mask): nothing outside the
        # owner may read it. Asserting the number instead would pin a constant this file
        # cannot import (a shell script and a Python module cannot share one), and the two
        # would then be free to drift one digit apart with every test green.
        assert mode & 0o077 == 0, (
            f"install.sh left the config file at {oct(mode)} — readable by group or other. It "
            f"holds `api_key` in plaintext, and the installer's own closing line tells the host "
            f"to paste a key in before running anything"
        )
