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
from emrg.logfiles import (  # noqa: E402
    APP_LOG_FILES,
    _ASIDE_SUFFIXES,
    legacy_log_names,
    migrate_legacy_logs,
)
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
    """The destination wins — and the leftover is renamed aside, never left, never deleted.

    The first half was always true; the second half is the fix (review
    cyc20261009-154611). Leaving the leftover in place is not a corner case: it is
    the ordinary one, because both clients open `logs/emrgd-start.err` and the GUI
    opens `logs/emrg-gui.log` **before** the daemon runs the migration — so on the
    first start after an upgrade those two destinations already exist and the
    leftovers stayed in the root for good, which is the state the host complained
    about. The requirement authorises discarding or renaming (「丢弃或改名，不得覆盖
    新日志」); deleting is what this test forbids.
    """
    _stub_home(monkeypatch, tmp_path)
    root = _legacy_root(tmp_path)
    (root / "emrgd.log").write_text("stale copy\n", encoding="utf-8")
    (root / "logs" / "emrgd.log").write_text("the live log\n", encoding="utf-8")

    moved = migrate_legacy_logs()

    assert moved == ["emrgd.log"], "the leftover still has to leave the root"
    assert (root / "logs" / "emrgd.log").read_text(encoding="utf-8") == "the live log\n", (
        "the live log must keep its own content"
    )
    assert not (root / "emrgd.log").exists(), (
        "the leftover must not stay in the config root — that is the whole point"
    )
    assert (root / "logs" / "emrgd.log.legacy").read_text(encoding="utf-8") == "stale copy\n", (
        "the old content must survive under the aside name — renamed, never deleted"
    )


def test_the_migration_clears_the_root_in_the_ordering_an_upgrade_actually_has(
    tmp_path, monkeypatch
):
    """The two names whose destination exists first, in the order a start really creates them.

    Measured on the head before the fix (2026-10-09): with the root holding
    `emrgd.log`, `emrg-gui.log` and `emrgd-start.err`, and `logs/` already holding
    the two files its clients open at spawn, the migration answered
    `moved: ['emrgd.log']` and left `<root>/emrg-gui.log` and
    `<root>/emrgd-start.err` behind — the 15 MB GUI log among them.
    """
    _stub_home(monkeypatch, tmp_path)
    root = _legacy_root(tmp_path)
    for name, text in (
        ("emrgd.log", "old daemon log\n"),
        ("emrg-gui.log", "old gui log\n"),
        ("emrgd-start.err", "old start err\n"),
    ):
        (root / name).write_text(text, encoding="utf-8")
    # what the clients create before the daemon starts
    (root / "logs" / "emrg-gui.log").write_text("live gui log\n", encoding="utf-8")
    (root / "logs" / "emrgd-start.err").write_text("live start err\n", encoding="utf-8")

    moved = migrate_legacy_logs()

    assert sorted(moved) == ["emrg-gui.log", "emrgd-start.err", "emrgd.log"]
    assert [p.name for p in sorted(root.iterdir())] == ["logs"], (
        f"the config root still holds {sorted(p.name for p in root.iterdir())!r}; after this "
        "migration it may hold nothing but the logs directory and the host's own data"
    )
    assert (root / "logs" / "emrg-gui.log").read_text(encoding="utf-8") == "live gui log\n"
    assert (root / "logs" / "emrgd-start.err").read_text(encoding="utf-8") == "live start err\n"
    assert (root / "logs" / "emrg-gui.log.legacy").read_text(encoding="utf-8") == "old gui log\n"
    assert (root / "logs" / "emrgd-start.err.legacy").read_text(encoding="utf-8") == "old start err\n"


def test_the_migration_reports_a_leftover_it_cannot_place(tmp_path, monkeypatch):
    """Every aside name taken: report and keep the file, never overwrite, never delete.

    The bounded suffix list is what stops a start from spinning on a name hunt; the
    cost of running out is that a leftover stays — which must be *said*, because a
    silent version of this is how "the root still has a log in it" becomes
    undiagnosable.
    """
    _stub_home(monkeypatch, tmp_path)
    root = _legacy_root(tmp_path)
    (root / "emrgd.log").write_text("stale copy\n", encoding="utf-8")
    (root / "logs" / "emrgd.log").write_text("the live log\n", encoding="utf-8")
    for suffix in _ASIDE_SUFFIXES:
        (root / "logs" / f"emrgd.log{suffix}").write_text("taken\n", encoding="utf-8")

    assert migrate_legacy_logs() == []
    assert (root / "emrgd.log").read_text(encoding="utf-8") == "stale copy\n"
    assert (root / "logs" / "emrgd.log").read_text(encoding="utf-8") == "the live log\n"


# ── the static guard ───────────────────────────────────────────────────────

#: The suffixes in which a bare `~/…` path is a command or a redirect rather than
#: prose. Measured 2026-10-09, when the shape was first added: applied to every
#: file it fired on three comments that *cite* the log path as evidence —
#: `emrg/gui/daemon_client.js`, `emrg/gui/preload.js`, `emrg/server/scheduler.py`
#: — which is exactly the "a mention is not a construction" line this guard has
#: always drawn. `$HOME` and `%USERPROFILE%` are unambiguous wherever they appear,
#: so those two keep the whole walk.
_SHELL_SUFFIXES = {".sh", ".cmd", ".bat", ".ps1"}

#: How a source file can build a log path whose directory is the config root.
#: Each is a *construction*, not a mention: a docstring or a host-facing message
#: naming `~/.emrg/logs/emrgd.log` is the point of the change, not a defect. The
#: third element is the suffixes a shape is meaningful in, or `None` for all.
_ROOT_LOG_PATTERNS = (
    ('python `Path.home() / ".emrg" / <name>`',
     re.compile(r'Path\.home\(\)\s*/\s*"\.emrg"\s*/\s*"(?P<name>[^"]+)"'), None),
    ("python `config_dir() / <name>`",
     re.compile(r'config_dir\(\)\s*/\s*"(?P<name>[^"]+)"'), None),
    ('js `path.join(os.homedir(), ".emrg", <name>)`',
     re.compile(r'path\.join\(\s*os\.homedir\(\)\s*,\s*"\.emrg"\s*,\s*"(?P<name>[^"]+)"'), None),
    # The shell and Windows shapes, added with the walk that reads them. On
    # 2026-10-09 a peer review found `packaging/smoke-test.sh` still tailing
    # `$HOME/.emrg/emrgd.log` after the move — a reader on the release workflow's
    # own failure path — which a scan of `.py`/`.js` under `emrg/` and `scripts/`
    # could not see. The claim this guard backs is about the *source*, and the
    # source of a release is shell scripts as much as Python.
    ("shell `$HOME/.emrg/<name>`",
     re.compile(r'\$\{?HOME\}?[\\/]\.emrg[\\/](?P<name>[A-Za-z0-9][\w.-]*)'), None),
    ("shell `~/.emrg/<name>`",
     re.compile(r'(?<![\w$~])~/\.emrg/(?P<name>[A-Za-z0-9][\w.-]*)'), _SHELL_SUFFIXES),
    ("windows `%USERPROFILE%\\.emrg\\<name>`",
     re.compile(r'%USERPROFILE%[\\/]\.emrg[\\/](?P<name>[A-Za-z0-9][\w.-]*)'), None),
)

#: What counts as a log file name. A rotation suffix is admitted (`emrgd.log.1`)
#: because that is the shape the migration has to recognise; the detector is a
#: pattern over *names*, which is not the same thing as the exemption — the
#: exemption is `APP_LOG_FILES`, spelled out.
_LOG_NAME = re.compile(r"^.+\.(?:log|err)(?:\.\d+)?$")

_SCAN_SUFFIXES = {".py", ".js", ".sh", ".cmd", ".bat", ".ps1"}
#: Every root the walk visits, so the guard's cover can be read off one line. A
#: new directory of shipped source belongs here; anything generated (`dist/`) or
#: vendored (`node_modules/`) does not.
_SCAN_ROOTS = ("emrg", "scripts", "packaging", "bin")
_SCAN_SKIP_PARTS = {"test", "tests", "node_modules", "dist", "__pycache__"}

#: The harness's own capture, and the second exemption beside the migration list.
#: `packaging/smoke-test.sh` redirects the daemon's console output into
#: `emrgd-debug.log` **before** the daemon exists to create `logs/`, so that file
#: cannot move into it; nothing in the daemon ever opens it. Scoped to the pair
#: (the file that writes it, the name it writes) rather than to the name, so a
#: `emrgd-debug.log` built anywhere else still fails this guard.
_HARNESS_CAPTURE = (REPO_ROOT / "packaging" / "smoke-test.sh", "emrgd-debug.log")


def _root_log_hits(text: str, suffix: str | None = None) -> list[tuple[int, str, str]]:
    """Every root-log construction in `text`, for a file of `suffix`.

    `suffix` is the file's own (`.py`, `.sh`, …): a pattern may declare the
    suffixes it is meaningful in, and one pattern does (`_SHELL_SUFFIXES`).
    """
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pattern, suffixes in _ROOT_LOG_PATTERNS:
            if suffixes is not None and suffix not in suffixes:
                continue
            for match in pattern.finditer(line):
                if _LOG_NAME.match(match.group("name")):
                    hits.append((lineno, label, match.group("name")))
    return hits


def _source_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every file below `root` that the declared roots, suffixes and skip set admit.

    A function of `root`, so the scope can be driven on a tree a test builds. While it
    was hardwired to `REPO_ROOT`, the only scope any test could measure was this
    checkout's, and the declared roots could not be read back at all.
    """
    files: list[Path] = []
    for name in _SCAN_ROOTS:
        base = root / name
        for path in sorted(base.rglob("*")):
            if path.suffix not in _SCAN_SUFFIXES or not path.is_file():
                continue
            if _SCAN_SKIP_PARTS & set(path.relative_to(root).parts):
                continue
            files.append(path)
    return files


def _roots_contributing_nothing(root: Path) -> list[str]:
    """Declared roots carrying no file this scan reads, in declaration order.

    The enforcement asserts `scanned > 50`, which cannot see a **single** root leaving
    the walk: measured 2026-10-10 on this checkout, `emrg/` carries 224 of the 279
    files, so `scripts/` (43), `packaging/` (10), `bin/` (2) or even `emrg/` (224 -> 55)
    can each vanish and the threshold still passes. A root named in `_SCAN_ROOTS` is a
    claim about the scope, so it is read back rather than assumed.
    """
    seen = {name: 0 for name in _SCAN_ROOTS}
    for path in _source_files(root):
        head = path.relative_to(root).parts[0]
        if head in seen:
            seen[head] += 1
    return [name for name in _SCAN_ROOTS if not seen[name]]


def test_the_detector_fires_on_a_root_log_path_and_not_on_another_file(tmp_path):
    """The instrument's control: a scan that matches nothing proves nothing."""
    assert _root_log_hits('log_file = Path.home() / ".emrg" / "emrgd.log"', ".py") == [
        (1, 'python `Path.home() / ".emrg" / <name>`', "emrgd.log")
    ]
    assert _root_log_hits('log_file = config_dir() / "emrg-gui.log"', ".py") == [
        (1, "python `config_dir() / <name>`", "emrg-gui.log")
    ]
    assert _root_log_hits(
        'const x = path.join(os.homedir(), ".emrg", "emrgd-start.err");', ".js"
    ) == [(1, 'js `path.join(os.homedir(), ".emrg", <name>)`', "emrgd-start.err")]
    # the shell and Windows shapes, each with a control of its own — a pattern with
    # no control is a pattern nobody knows is looking at anything
    assert _root_log_hits('  tail -20 "$HOME/.emrg/emrgd.log" 2>/dev/null || true', ".sh") == [
        (1, "shell `$HOME/.emrg/<name>`", "emrgd.log")
    ]
    assert _root_log_hits("cat ~/.emrg/emrg-gui.log", ".sh") == [
        (1, "shell `~/.emrg/<name>`", "emrg-gui.log")
    ]
    assert _root_log_hits(r"type %USERPROFILE%\.emrg\emrgd-crash.log", ".cmd") == [
        (1, "windows `%USERPROFILE%\\.emrg\\<name>`", "emrgd-crash.log")
    ]
    # the `$HOME` shape is asked of a Python file too, because that is where a
    # shell command is *written* as often as in a `.sh`: the shape is unambiguous
    # and keeps the whole walk
    assert _root_log_hits('subprocess.run("cat $HOME/.emrg/emrgd.log", shell=True)', ".py") == [
        (1, "shell `$HOME/.emrg/<name>`", "emrgd.log")
    ]
    # ... and each stays silent on the *moved* spelling, which is the point of the
    # change: the segment after `.emrg/` is the directory, not a log name
    assert _root_log_hits('tail -20 "$HOME/.emrg/logs/emrgd.log"', ".sh") == []
    assert _root_log_hits('echo "logs go to ~/.emrg/logs/emrgd.log"', ".sh") == []
    assert _root_log_hits(r"echo %USERPROFILE%\.emrg\logs\emrgd.log", ".cmd") == []
    # the `~` shape's scope, both directions: in a shell file it is a path, and in
    # a comment it is a mention — the three lines it first fired on here were
    # comments citing the log as evidence
    assert _root_log_hits('cat ~/.emrg/emrg-gui.log', ".js") == []
    assert _root_log_hits("// 实测（`~/.emrg/emrg-gui.log`，176404 行）", ".js") == []
    # a non-log file under the config root is not this guard's business
    assert _root_log_hits('token = config_dir() / "emrgd.token"', ".py") == []


def test_no_source_builds_a_log_path_under_the_config_root():
    offenders: list[str] = []
    scanned = 0
    for path in _source_files():
        scanned += 1
        for lineno, label, name in _root_log_hits(
            path.read_text(encoding="utf-8", errors="replace"), path.suffix
        ):
            if path == LOGFILES_MODULE and name in APP_LOG_FILES:
                continue  # the migration list is the only exemption, and it is explicit
            if (path, name) == _HARNESS_CAPTURE:
                continue  # the smoke test's own capture, before logs/ exists
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} [{label}] {name!r}")
    assert scanned > 50, (
        f"the scan visited {scanned} file(s); a scan that reads nothing passes for the "
        "wrong reason (a moved directory would silently pass this guard)"
    )
    assert not offenders, (
        "these source lines build a log path under the config root, where nothing "
        "should still write one (rant 2026-10-09T14:20:18): the log directory is "
        "`emrg.config.logs_dir()`. The scan covers "
        + ", ".join(f"`{name}/`" for name in _SCAN_ROOTS)
        + " ("
        + ", ".join(sorted(_SCAN_SUFFIXES))
        + "), and exactly two exemptions are allowed: the migration in "
        "`emrg/logfiles.py`, for a name in `APP_LOG_FILES`, and the smoke test's own "
        "capture of the daemon's console output, which is written before `logs/` "
        "exists: "
        + "; ".join(offenders)
    )


def test_every_declared_scan_root_still_carries_a_file():
    """The declaration is read back, so a root that stopped being walked fails by name.

    `scanned > 50` above cannot see one root going: measured 2026-10-10, `emrg/` carries
    224 of the 279 files, so any single root can vanish and the threshold still passes.
    This measures the declaration against **the tree** — a declared root that no longer
    carries a file the walk admits. It cannot see an edit to `_SCAN_ROOTS` itself, which
    is a different question: the family's roots are pinned to each other in
    `tests/test_guard_scan_scope_pairing.py`, and these four differ from that family's
    deliberately (`bin/` is a root here, `tests/` is not).
    """
    gone = _roots_contributing_nothing(REPO_ROOT)
    assert gone == [], (
        f"declared scan root(s) {gone} carry no file this scan reads, so the guard has "
        f"silently shrunk by that much while still claiming to cover {list(_SCAN_ROOTS)} - "
        "either the directory left the tree or the walk stopped admitting its files"
    )


def test_a_declared_root_that_is_not_there_is_reported(tmp_path):
    """The other direction: a root that is absent has to be named, not silently skipped."""
    for name in _SCAN_ROOTS:
        (tmp_path / name).mkdir()
    (tmp_path / "bin" / "launch.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    assert _roots_contributing_nothing(tmp_path) == [
        name for name in _SCAN_ROOTS if name != "bin"
    ], "a declared root with no admittable file is a root the scan does not read"

    for name in _SCAN_ROOTS:
        (tmp_path / name / f"{name}.py").write_text("pass\n", encoding="utf-8")

    assert _roots_contributing_nothing(tmp_path) == []
