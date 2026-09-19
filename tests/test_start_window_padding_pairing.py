"""The two start-window entry points strip the same padding (issue #1410).

`EMRG_START_TIMEOUT` is read by two resolvers in two languages —
`emrg/client/daemon_manager.py::_start_window_seconds` and
`emrg/gui/daemon_client.js::_startWindowMs` — and both used to normalise the value with
their host language's default (`str.strip()` / `String.prototype.trim()`). Those defaults
are not the same set. Measured 2026-09-19 by enumerating both, in the engine that
implements them: Python strips **29** code points, JavaScript trims **25**, and the
symmetric difference is `U+001C`–`U+001F` and `U+0085` one way, `U+FEFF` the other.

Driven through the two real resolvers on a shared head, 9 of 12 padded values disagreed,
in both directions — `\ufeff30` was a 30 s window to the GUI and a refused typo (4.5 s) to
the client; `\x1c30` was 30 s to the client and 5 s to the GUI. Neither direction is
"safe": on the GUI side the host's window silently shrinks to 5 s, on the client side it
silently shrinks to 4.5 s. So the fix is the **union** of the two sets, declared
explicitly and independently in both files, and this file is what makes "the same set"
checkable rather than asserted — issue #1410, whose own text is the claim that the two
sides disagreed.

The declarations were compared textually *and* each one's coverage is enumerated against
its own language's default: a class written by hand can be short (`\x1c` is easy to forget)
and prose about "the union" cannot tell you whether the text is right. The JS half of that
enumeration lives in `emrg/gui/test/daemon_client.test.js` — `trim()` only exists there.
Issue #1410's fix also carries rows in `tests/data/start_window_shapes.json`, the list both
suites read through the real resolvers, so the two directions are checked on real values
and not only on the declared text.
"""
from __future__ import annotations

import inspect
import logging
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emrg.client import daemon_manager as dm  # noqa: E402


class _Warnings(logging.Handler):
    """What the resolver logged, taken from its own logger rather than `caplog`."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


CLIENT_SOURCE = ROOT / "emrg" / "client" / "daemon_manager.py"
GUI_SOURCE = ROOT / "emrg" / "gui" / "daemon_client.js"

_PY_DECL = re.compile(r'^_START_WINDOW_PADDING = r"([^"]*)"$', re.M)
_JS_DECL = re.compile(r"^const START_WINDOW_PADDING = String\.raw`([^`]*)`;$", re.M)

#: The code points JavaScript trims and Python does not. Enumerated in the engines, not
#: copied from the ECMAScript spec: `Array.from` of U+FEFF — a string's own `.trim()` is
#: the only authority on what `.trim()` strips.
JS_ONLY = {0xFEFF}


def _declared_text() -> tuple[str, str]:
    python = _PY_DECL.search(CLIENT_SOURCE.read_text(encoding="utf-8"))
    gui = _JS_DECL.search(GUI_SOURCE.read_text(encoding="utf-8"))
    assert python, "emrg/client/daemon_manager.py no longer declares _START_WINDOW_PADDING"
    assert gui, "emrg/gui/daemon_client.js no longer declares START_WINDOW_PADDING"
    return python.group(1), gui.group(1)


def _members(text: str) -> set[int]:
    """Every code point the class matches, asked of `re` rather than assumed."""
    member = re.compile(f"[{text}]")
    return {cp for cp in range(0x110000) if member.fullmatch(chr(cp))}


def test_the_two_declarations_are_the_same_text():
    """The whole claim: one set, two spellings of it. Compared as text, because a class
    that differs in one escape is exactly the defect this file is about — `\x1c` looks
    like `\x1d` to a reader and neither notices."""
    python_text, gui_text = _declared_text()
    assert python_text == gui_text, (
        "the two padding declarations drifted apart:\n"
        f"  python: {python_text!r}\n  javascript: {gui_text!r}"
    )
    assert python_text, "an empty class would strip nothing and silently unset every value"


def test_the_class_covers_every_code_point_python_strips():
    """Enumerated, not asserted from the docstring: the union's job is to lose nothing the
    client accepts today, and `str.strip()` is the authority on what that is."""
    python_text, _ = _declared_text()
    members = _members(python_text)
    default = {cp for cp in range(0x110000) if chr(cp).strip() == ""}
    missed = sorted(hex(cp) for cp in default - members)
    assert not missed, f"the class drops code points str.strip() removes: {missed}"
    # and the class is exactly the union: wider than Python's default by the JS-only set
    assert members - default == JS_ONLY, sorted(hex(cp) for cp in members - default)


def test_a_language_default_is_not_what_the_resolver_uses():
    """The declared class has to be the thing the resolver runs. `.strip()` in that
    function would pass every fixture row that is spelled with ASCII spaces and fail
    only on the six code points this issue is about — a variant that looks correct."""
    source = inspect.getsource(dm._start_window_seconds)
    assert "_START_WINDOW_TRIM" in source, "the resolver does not use the shared class"
    assert ".strip()" not in source, "the resolver still normalises with Python's default"


@pytest.mark.parametrize(
    "padding", [chr(cp) for cp in sorted(JS_ONLY | {0x1C, 0x1D, 0x1E, 0x1F, 0x85})]
)
def test_a_value_padded_with_a_differing_code_point_is_accepted(padding, monkeypatch):
    """The six code points the two languages disagreed about, through the real resolver.

    `\ufeff30` is the GUI-only one — the client refused it and fell back to 4.5 s while the
    GUI waited 30 s. It has to become a 30 s window, not a fallback: taking the intersection
    instead of the union would 'agree' by refusing it on both sides, which is agreement
    about a typo rather than about the variable."""
    warnings = _Warnings()
    dm.logger.addHandler(warnings)
    try:
        monkeypatch.setenv(dm._START_WINDOW_ENV, f"{padding}30{padding}")
        assert dm._start_window_seconds() == pytest.approx(30.0), repr(padding)
        assert warnings.messages == [], f"{padding!r} is padding; it must not warn"
    finally:
        dm.logger.removeHandler(warnings)


def test_a_code_point_in_neither_default_is_still_refused(monkeypatch):
    """The control for the test above. Widening the set must not widen it to *any*
    character: `U+200B` (zero-width space) is whitespace to neither language, and a class
    built by "strip whatever is not a digit" would accept it — the padding would then
    quietly convert a typo into a window instead of reporting it."""
    warnings = _Warnings()
    dm.logger.addHandler(warnings)
    try:
        monkeypatch.setenv(dm._START_WINDOW_ENV, "\u200b30")
        assert dm._start_window_seconds() == dm._START_WINDOW_DEFAULT_SECONDS
        assert warnings.messages, "a refused value must still warn"
    finally:
        dm.logger.removeHandler(warnings)
