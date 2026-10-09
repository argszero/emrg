"""The application-level logs live in ``~/.emrg/logs/`` (rant 2026-10-09T14:20:18).

Why this file exists
--------------------
The host's config root had collected nine log files while ``~/.emrg/logs/``
already existed and was already a log home, so the rule was half-applied. The
five application-level logs moved — same names, one derivation — and a move is
exactly the kind of change that is *read* as done and never measured: a writer
left pointing at the old root keeps working (it writes somewhere, the host just
does not look there), and every tail-reader silently reads an empty file.

So this file asserts the two things that make the move real, in the two
directions the rant asks for:

* **behaviour** — with `HOME` stubbed, each Python writer's file lands under
  `logs/`, and its old location stays empty;
* **a static guard** — no source file builds a log path under the config root.
  The migration list (`emrg.logfiles.APP_LOG_FILES`) is the only exemption and it
  is enumerated, not a `*.log` pattern: a log file added to a writer without
  being added to that tuple is reported, which is the whole point of writing the
  guard as a list rather than a wildcard.

What is deliberately not here
-----------------------------
The cwd-relative client logs (`<cwd>/.emrg/emrg-client.log`) are outside the
rant's boundary and unchanged. No test here starts, stops or restarts a daemon,
and none touches the upgrade chain: the writers are called directly, with the
host's own `HOME` replaced by a temporary one.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.client import daemon_manager as dm  # noqa: E402
from emrg.config import logs_dir  # noqa: E402
from emrg.logfiles import APP_LOG_FILES, legacy_log_names, migrate_legacy_logs  # noqa: E402
from emrg.server import daemon as srv  # noqa: E402
from emrg.server import __main__ as server_main  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGFILES_MODULE = REPO_ROOT / "emrg" / "logfiles.py"


def _stub_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point every home lookup at `home`, on both platforms.

    `Path.home()` is read per call (it is `os.path.expanduser("~")` underneath),
    so setting the variables is enough — and both spellings are set because
    Windows' `ntpath` reads `USERPROFILE` while POSIX reads `HOME`, so a test
    that set one would silently exercise the host's real directory on the other.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@contextlib.contextmanager
def _root_handlers_cleared():
    """Let `logging.basicConfig` install a handler while this block runs.

    `basicConfig` is a no-op when the root logger already has handlers, and
    pytest's logging plugin puts one there for every test — so without this the
    writer under test would compute its path, install nothing, and report green
    while having written no file at all.
    """
    saved_handlers = logging.root.handlers[:]
    saved_level = logging.root.level
    logging.root.handlers = []
    try:
        yield
    finally:
        for handler in logging.root.handlers:
            with contextlib.suppress(Exception):
                handler.close()
        logging.root.handlers = saved_handlers
        logging.root.level = saved_level


# ── the list, and the one derivation ───────────────────────────────────────


def test_the_migration_list_is_the_five_application_logs_exactly():
    """Pinned literally: dropping a name must be a red, not a quieter migration."""
    assert APP_LOG_FILES == (
        "emrgd.log",
        "emrgd-crash.log",
        "emrgd-exit.log",
        "emrgd-start.err",
        "emrg-gui.log",
    )


def test_the_migration_looks_for_every_listed_name_and_its_rotations():
    names = set(legacy_log_names())
    for base in APP_LOG_FILES:
        assert base in names, f"{base} is not migrated"
    assert "emrgd.log.1" in names and "emrgd.log.3" in names


# ── behaviour: each writer, with a stubbed HOME ────────────────────────────


def test_the_daemon_log_handler_writes_under_logs(tmp_path, monkeypatch):
    _stub_home(monkeypatch, tmp_path)
    with _root_handlers_cleared():
        server_main._configure_logging()
        installed = [
            h for h in logging.root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        ] if hasattr(logging, "handlers") else []
    under = tmp_path / ".emrg" / "logs" / "emrgd.log"
    assert under.exists(), "the daemon's rotating handler did not write under logs/"
    assert not (tmp_path / ".emrg" / "emrgd.log").exists(), "the old root copy is still written"
    if installed:
        assert Path(installed[0].baseFilename) == under


def test_the_crash_log_path_is_under_logs(tmp_path, monkeypatch):
    _stub_home(monkeypatch, tmp_path)
    assert server_main._crash_log_path() == tmp_path / ".emrg" / "logs" / "emrgd-crash.log"


def test_the_crash_log_is_really_opened_under_logs(tmp_path):
    """The writer re-points `sys.stdout`/`sys.stderr`, so it runs in a subprocess.

    Calling it in-process would leave this interpreter's own streams aimed at a
    file the test then closes — which breaks pytest's capture for every test that
    runs afterwards, a false red invented by the guard. A child process has no
    such reach, and it exercises the real function rather than a paraphrase of it.
    """
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.run(
        [
            sys.executable, "-c",
            "from emrg.server.__main__ import _redirect_std_streams as f; f(); print('reached')",
        ],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    written = tmp_path / ".emrg" / "logs" / "emrgd-crash.log"
    assert written.exists(), f"nothing was written to {written} (stderr: {proc.stderr!r})"
    assert "reached" in written.read_text(encoding="utf-8")
    assert not (tmp_path / ".emrg" / "emrgd-crash.log").exists()


def test_the_client_log_paths_are_under_logs(tmp_path, monkeypatch):
    _stub_home(monkeypatch, tmp_path)
    assert dm._log_path() == tmp_path / ".emrg" / "logs" / "emrgd.log"
    assert dm._start_stderr_path() == tmp_path / ".emrg" / "logs" / "emrgd-start.err"
    # and the child's stderr file really opens there, creating the directory
    handle = dm._truncate_start_stderr(dm._start_stderr_path())
    assert handle is not None
    handle.close()
    assert (tmp_path / ".emrg" / "logs" / "emrgd-start.err").exists()


def test_the_exit_record_is_under_logs():
    """Read as a value: the constant is resolved at import, like the writers' own."""
    assert srv._EXIT_RECORD_PATH == logs_dir() / "emrgd-exit.log"


# ── the migration ──────────────────────────────────────────────────────────


def _legacy_root(tmp_path: Path) -> Path:
    root = tmp_path / ".emrg"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


def test_legacy_logs_move_into_the_logs_directory(tmp_path, monkeypatch):
    _stub_home(monkeypatch, tmp_path)
    root = _legacy_root(tmp_path)
    (root / "emrgd.log").write_text("old daemon log\n", encoding="utf-8")
    (root / "emrgd.log.1").write_text("rotation\n", encoding="utf-8")
    (root / "emrg-missing.log").write_text("not in the list\n", encoding="utf-8")

    moved = migrate_legacy_logs()

    assert sorted(moved) == ["emrgd.log", "emrgd.log.1"]
    assert (root / "logs" / "emrgd.log").read_text(encoding="utf-8") == "old daemon log\n"
    assert (root / "logs" / "emrgd.log.1").exists()
    assert not (root / "emrgd.log").exists()
    # a file that is not on the list is not touched — the migration is not a sweep
    assert (root / "emrg-missing.log").exists()


def test_the_migration_is_idempotent(tmp_path, monkeypatch):
    _stub_home(monkeypatch, tmp_path)
    root = _legacy_root(tmp_path)
    (root / "emrgd.log").write_text("old\n", encoding="utf-8")

    assert migrate_legacy_logs() == ["emrgd.log"]
    assert migrate_legacy_logs() == []


def test_the_migration_never_overwrites_a_log_that_is_already_there(tmp_path, monkeypatch):
    """The destination wins: it is the newer file, and a migration may not lose data."""
    _stub_home(monkeypatch, tmp_path)
    root = _legacy_root(tmp_path)
    (root / "emrgd.log").write_text("stale copy\n", encoding="utf-8")
    (root / "logs" / "emrgd.log").write_text("the live log\n", encoding="utf-8")

    moved = migrate_legacy_logs()

    assert moved == []
    assert (root / "logs" / "emrgd.log").read_text(encoding="utf-8") == "the live log\n"
    assert (root / "emrgd.log").read_text(encoding="utf-8") == "stale copy\n", (
        "the leftover must be left where it is, never deleted"
    )


# ── the static guard ───────────────────────────────────────────────────────

#: How a source file can build a log path whose directory is the config root.
#: Each is a *construction*, not a mention: a docstring or a host-facing message
#: naming `~/.emrg/logs/emrgd.log` is the point of the change, not a defect.
_ROOT_LOG_PATTERNS = (
    ('python `Path.home() / ".emrg" / <name>`',
     re.compile(r'Path\.home\(\)\s*/\s*"\.emrg"\s*/\s*"(?P<name>[^"]+)"')),
    ("python `config_dir() / <name>`",
     re.compile(r'config_dir\(\)\s*/\s*"(?P<name>[^"]+)"')),
    ('js `path.join(os.homedir(), ".emrg", <name>)`',
     re.compile(r'path\.join\(\s*os\.homedir\(\)\s*,\s*"\.emrg"\s*,\s*"(?P<name>[^"]+)"')),
)

#: What counts as a log file name. A rotation suffix is admitted (`emrgd.log.1`)
#: because that is the shape the migration has to recognise; the detector is a
#: pattern over *names*, which is not the same thing as the exemption — the
#: exemption is `APP_LOG_FILES`, spelled out.
_LOG_NAME = re.compile(r"^.+\.(?:log|err)(?:\.\d+)?$")

_SCAN_SUFFIXES = {".py", ".js"}
_SCAN_SKIP_PARTS = {"test", "tests", "node_modules", "dist", "__pycache__"}


def _root_log_hits(text: str) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pattern in _ROOT_LOG_PATTERNS:
            for match in pattern.finditer(line):
                if _LOG_NAME.match(match.group("name")):
                    hits.append((lineno, label, match.group("name")))
    return hits


def _source_files() -> list[Path]:
    files: list[Path] = []
    for root in (REPO_ROOT / "emrg", REPO_ROOT / "scripts"):
        for path in sorted(root.rglob("*")):
            if path.suffix not in _SCAN_SUFFIXES or not path.is_file():
                continue
            if _SCAN_SKIP_PARTS & set(path.relative_to(REPO_ROOT).parts):
                continue
            files.append(path)
    return files


def test_the_detector_fires_on_a_root_log_path_and_not_on_another_file(tmp_path):
    """The instrument's control: a scan that matches nothing proves nothing."""
    assert _root_log_hits('log_file = Path.home() / ".emrg" / "emrgd.log"') == [
        (1, 'python `Path.home() / ".emrg" / <name>`', "emrgd.log")
    ]
    assert _root_log_hits('log_file = config_dir() / "emrg-gui.log"') == [
        (1, "python `config_dir() / <name>`", "emrg-gui.log")
    ]
    assert _root_log_hits('const x = path.join(os.homedir(), ".emrg", "emrgd-start.err");') == [
        (1, 'js `path.join(os.homedir(), ".emrg", <name>)`', "emrgd-start.err")
    ]
    # a non-log file under the config root is not this guard's business
    assert _root_log_hits('token = config_dir() / "emrgd.token"') == []


def test_no_source_builds_a_log_path_under_the_config_root():
    offenders: list[str] = []
    scanned = 0
    for path in _source_files():
        scanned += 1
        for lineno, label, name in _root_log_hits(path.read_text(encoding="utf-8", errors="replace")):
            if path == LOGFILES_MODULE and name in APP_LOG_FILES:
                continue  # the migration list is the only exemption, and it is explicit
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} [{label}] {name!r}")
    assert scanned > 50, (
        f"the scan visited {scanned} file(s); a scan that reads nothing passes for the "
        "wrong reason (a moved directory would silently pass this guard)"
    )
    assert not offenders, (
        "these source lines build a log path under the config root, where nothing "
        "should still write one (rant 2026-10-09T14:20:18): the log directory is "
        "`emrg.config.logs_dir()`, and the migration in emrg/logfiles.py is the only "
        "place allowed to name a root log — and only for a name in APP_LOG_FILES: "
        + "; ".join(offenders)
    )
