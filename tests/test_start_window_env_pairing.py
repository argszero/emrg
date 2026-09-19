"""One variable, two entry points: read the pairing where both sides exist.

Why this file exists
--------------------
Issue #1276 item 5 gave the host a lever for a slow cold start — `EMRG_START_TIMEOUT`
— and the point of the lever is that it is *one* variable reaching *both* entry
points: the CLI/TUI (`emrg/client/daemon_manager.py`, #1402) and the GUI
(`emrg/gui/daemon_client.js`, #1404). Each side is pinned on its own: this repo's
tests read `dm._START_WINDOW_ENV`, the GUI's read the literal `EMRG_START_TIMEOUT`.
What nothing pinned, until here, is that the two sides are the *same name* — and the
gap is not hypothetical. Measured 2026-09-19 (`cyc20260919-060712`) by renaming the
Python constant to `EMRG_START_WINDOW_TIMEOUT` and touching nothing else: the Python
suite stayed green (38 passed, because those assertions read the constant and so
*follow* a rename) and the GUI's stayed green (77 passed, because its literal had not
moved). A host's `EMRG_START_TIMEOUT=30` would have stopped reaching the TUI with no
red anywhere — the lever #1402 exists to provide, unhooked in silence.

How the two sides are read
--------------------------
The Python side is imported and read as a value. The GUI side is a file in another
language, so its declaration is *parsed* — and a parser that is the instrument is
only as good as its controls, which is what `_JS_SAMPLE_*` below are for. The
extractor is deliberately name-agnostic: it returns whatever name the file declares,
which is what makes the equality assertion a comparison rather than a re-statement of
what it is looking for.

Named limit
-----------
This pins the *name*, not that either side honours it. That the value reaches the
wait is covered per side (`tests/test_daemon_start_diagnostics.py` drives the loop
end to end; `emrg/gui/test/daemon_client.test.js` feeds both spawn sites). And the
GUI half is only comparable once it exists: before that, this file reports the
comparison as unmeasurable rather than as a pass.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.client import daemon_manager as dm  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
GUI_CLIENT = REPO_ROOT / "emrg" / "gui" / "daemon_client.js"

#: The declaration the GUI side would carry. Matched as `const <ANY> = "name";` so the
#: extractor reports the name the file *has*, never a name it was told to expect.
_JS_DECLARATION = re.compile(r'^const\s+\w*START_WINDOW\w*\s*=\s*"([^"]*)"\s*;', re.MULTILINE)

_JS_SAMPLE_DECLARED = 'const START_WINDOW_ENV = "EMRG_START_TIMEOUT";\n'
_JS_SAMPLE_DECLARED_DIFFERENTLY = 'const START_WINDOW_ENV = "EMRG_START_WINDOW_TIMEOUT";\n'
_JS_SAMPLE_SILENT = "const SPAWN_WAIT_MS = 5_000;\n"


def _js_env_name(text: str) -> str | None:
    """The start-window variable name `text` declares, or `None` if it declares none."""
    found = _JS_DECLARATION.search(text)
    return found.group(1) if found else None


def test_the_extractor_reads_the_name_the_file_has_not_the_one_it_wants():
    """The instrument's control, in both directions that matter.

    A parser that returned nothing unless it saw the expected name would make the
    assertion below a tautology, and one that returned the expected name whatever it
    saw would make it a rubber stamp. So it must report a *differing* name as that
    name (that is the case a rename produces), and nothing at all when no declaration
    is present (`None`, not the default).
    """
    assert _js_env_name(_JS_SAMPLE_DECLARED) == "EMRG_START_TIMEOUT"
    assert _js_env_name(_JS_SAMPLE_DECLARED_DIFFERENTLY) == "EMRG_START_WINDOW_TIMEOUT"
    assert _js_env_name(_JS_SAMPLE_SILENT) is None


def test_the_python_side_names_the_documented_variable():
    """The TUI's spelling is a contract with the host's shell, so it is pinned literally.

    Every other assertion about this feature reads `dm._START_WINDOW_ENV`, which is the
    right instrument for "does the window change" and the wrong one for "is it still
    called that": it follows a rename. `DEVELOPMENT.md` documents `EMRG_START_TIMEOUT`
    for the host, so the string is asserted here rather than left to whoever edits the
    constant next — a rename is a deliberate act, and this is the red that says so.
    """
    assert dm._START_WINDOW_ENV == "EMRG_START_TIMEOUT"


def test_the_two_entry_points_read_the_same_variable():
    """The pairing itself: whatever the GUI declares must be the name the TUI reads."""
    text = GUI_CLIENT.read_text(encoding="utf-8")
    declared = _js_env_name(text)
    if declared is None:
        # Not a pass: with no declaration there is no pairing to read, and saying that
        # out loud is the measurement (issue #1276's GUI half is PR #1404).
        assert dm._START_WINDOW_ENV not in text, (
            f"{GUI_CLIENT.name} names {dm._START_WINDOW_ENV} but declares no constant "
            "for it — the pairing cannot be read from this file"
        )
        pytest.skip(f"{GUI_CLIENT.name} does not read the start-window variable yet")
    assert declared == dm._START_WINDOW_ENV, (
        "the GUI and the TUI must read the same start-window variable: the host sets "
        f"one value, and renaming either side unhooks it silently (GUI declares "
        f"{declared!r}, TUI reads {dm._START_WINDOW_ENV!r})"
    )
