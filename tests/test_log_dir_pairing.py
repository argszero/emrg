"""One log directory, two languages: read the pairing where both sides exist.

Why this file exists
--------------------
Rant 2026-10-09T14:20:18 moved EMRG's application-level logs into
``~/.emrg/logs/`` and asked for **one derivation** of that directory. The Python
side has it (`emrg.config.logs_dir`); the GUI's main process is JavaScript and
cannot import Python, so it carries its own constant — the same shape
`EMRGD_PORT` already has in three places. What nothing pinned, until here, is
that the two sides name the **same path**: a change to one side alone leaves the
GUI reading a file the daemon no longer writes, and every test on each side
stays green, because each side is asserted against its own spelling.

How the two sides are read
--------------------------
The Python side is imported and read as a *value* relative to `Path.home()`. The
JS side is a file in another language, so its declaration is *parsed* — and a
parser that is the instrument is only as good as its controls, which is what the
`_JS_SAMPLE_*` texts below are for. The extractor returns whatever
`path.join(...)` arguments the file declares, so the comparison is a comparison
and not a re-statement of what it is looking for.

Named limit
-----------
This pins the directory the two sides *name*, not that either writes there.
That the files really land under it is covered behaviourally on the Python side
(`tests/test_app_logs_live_under_logs_dir.py`) and on the GUI side by
`emrg/gui/test/daemon_client.test.js`, whose `logFile()` fixture now builds the
path from the same directory the code reads.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.client import daemon_manager as dm  # noqa: E402
from emrg.config import config_dir, logs_dir  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
GUI_CLIENT = REPO_ROOT / "emrg" / "gui" / "daemon_client.js"
GUI_MAIN = REPO_ROOT / "emrg" / "gui" / "main.js"

#: `os.homedir()` is the JS spelling of `Path.home()`; it is kept as a token here
#: rather than resolved, because resolving it in the test process would compare
#: the JS file against *this* interpreter's home instead of against its rule.
HOMEDIR_TOKEN = "os.homedir()"

_JS_JOIN = re.compile(
    r"^\s*const\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:\(\)\s*=>\s*)?path\.join\((?P<args>[^\n]*)\)\s*;",
    re.MULTILINE,
)
_JS_STRING = re.compile(r'"([^"]*)"')


def _join_args(text: str, constant: str) -> list[str] | None:
    """The `path.join(...)` arguments of `constant`'s declaration, or `None`.

    Asked for by *name* and answered with what the file has, so a declaration
    that was renamed away reports as absent rather than as the expected value.
    """
    for match in _JS_JOIN.finditer(text):
        if match.group("name") != constant:
            continue
        args = match.group("args")
        parts = [token.strip() for token in args.split(",")]
        return [
            part if part == HOMEDIR_TOKEN else (m.group(1) if (m := _JS_STRING.fullmatch(part)) else part)
            for part in parts
        ]
    return None


def _expected_parts() -> list[str]:
    """What the JS declaration must spell to reach the same directory."""
    relative = logs_dir().relative_to(Path.home())
    return [HOMEDIR_TOKEN, *relative.parts]


# ── the extractor's controls ────────────────────────────────────────────────


def test_the_extractor_reads_what_the_file_has_not_what_it_wants():
    """Both directions that matter: a different value is reported as itself.

    An extractor that returned the expected list whenever it saw the expected
    name would make the pairing assertion a rubber stamp; one that returned
    `None` unless the name matched would make it a tautology. So the same shape
    with a different directory must come back as that directory.
    """
    same = 'const EMRG_LOGS_DIR = () => path.join(os.homedir(), ".emrg", "logs");\n'
    other = 'const EMRG_LOGS_DIR = () => path.join(os.homedir(), ".emrg");\n'
    silent = "const EMRGD_PORT = 56031;\n"

    assert _join_args(same, "EMRG_LOGS_DIR") == [HOMEDIR_TOKEN, ".emrg", "logs"]
    assert _join_args(other, "EMRG_LOGS_DIR") == [HOMEDIR_TOKEN, ".emrg"]
    assert _join_args(silent, "EMRG_LOGS_DIR") is None


# ── the pairing itself ──────────────────────────────────────────────────────


def test_the_python_side_derives_the_directory_from_the_config_dir():
    """The one derivation: `logs` is a subdirectory of the config dir, nothing else."""
    assert logs_dir() == config_dir() / "logs"


def test_the_gui_declares_the_same_directory():
    declared = _join_args(GUI_CLIENT.read_text(encoding="utf-8"), "EMRG_LOGS_DIR")
    assert declared is not None, (
        f"{GUI_CLIENT.name} declares no EMRG_LOGS_DIR — the JS side cannot be "
        "read, so the pairing is unmeasurable rather than absent"
    )
    assert declared == _expected_parts(), (
        "the GUI and the daemon must name the same log directory: the daemon "
        f"writes {logs_dir()}, and the GUI declares {declared!r}"
    )


def test_the_gui_client_takes_both_its_paths_from_that_one_constant():
    """`EMRGD_LOG` and `EMRGD_START_ERR` must not re-spell the root themselves."""
    text = GUI_CLIENT.read_text(encoding="utf-8")
    for constant, filename in (("EMRGD_LOG", "emrgd.log"), ("EMRGD_START_ERR", "emrgd-start.err")):
        assert _join_args(text, constant) == ["EMRG_LOGS_DIR()", filename], (
            f"{constant} must be built from EMRG_LOGS_DIR(), not from a second "
            f"spelling of the config root — that is what makes the directory "
            f"changeable in one place"
        )


def test_the_gui_main_process_writes_its_log_into_the_same_directory():
    """`createLogger`'s `logDir` is the third site, and it moved with the other two."""
    declared = _join_args(GUI_MAIN.read_text(encoding="utf-8"), "logDir")
    assert declared == _expected_parts(), (
        f"emrg-gui.log must land in {logs_dir()}, and main.js declares {declared!r}"
    )


def test_the_python_writers_take_the_one_derivation():
    """Both client-side paths, read as values — a second spelling here is the defect."""
    assert dm._log_path() == logs_dir() / "emrgd.log"
    assert dm._start_stderr_path() == logs_dir() / "emrgd-start.err"
