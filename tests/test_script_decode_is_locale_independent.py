"""Host scripts must not decode a child process's output with the locale codec.

The class, and why it keeps coming back
---------------------------------------
`subprocess.run(..., text=True)` with no `encoding=` decodes the child's bytes
with the *locale* codec. That is invisible on a UTF-8 host - which is every
developer machine and every CI runner in this repo - and fatal on the hosts this
repo actually ships to, where the locale is often cp936/GBK or cp1252:

* the child's output is UTF-8, because that is what `gh`, `git` and `node` emit;
* the locale codec cannot decode arbitrary UTF-8 bytes, so `UnicodeDecodeError`
  is raised *instead of* the result, on data that is perfectly correct.

Four instances of this class have been fixed one at a time, each after a human
found it: `bump-version.py` and `check_nonlocal.py` (#1119/#1121, *output*
encoding), the scripts' `print` literals, and `check-node-test-count.py`
(issue #1132, decode + a bare `npm`). Fixing them one at a time is exactly why
the fifth and sixth survived: they are only found if someone re-runs the same
scan by hand.

This module is that scan, kept. It is a *class* guard: it names every call site
at once and keeps naming new ones, so the next instance is caught by CI rather
than by a reviewer noticing.

Two halves, deliberately different in kind
------------------------------------------
* **static** — every `subprocess` call under `scripts/` that asks for text
  (`text=True` / `universal_newlines=True`) must also pin `encoding=`. This half
  covers call sites no test drives, which is where the surviving instances lived:
  `sync-master-from-api.py`'s API fallback only runs when the primary path 403s,
  and `reader_fix_latency.py` is a manual reporting tool with no test at all.
* **behavioural** — a real child emits a byte that is invalid in both UTF-8 and
  the hostile locale codecs, and the two call shapes are compared: the pinned
  shape returns text, the unpinned shape raises. Without this half the static
  rule could be satisfied by a refactor that still mis-decodes somewhere else.

Scope boundary (stated, not implied)
------------------------------------
`emrg/` product code is **not** covered, and that is a decision rather than an
omission: the daemon's subprocess wrapper (`emrg/server/git_utils.py`) already
pins `encoding="utf-8"`, while `emrg/tools/bash_tool.py` deliberately *tries the
locale codec first and falls back to UTF-8* because on Windows it must read both
GBK console output and UTF-8 git output. That is a different, reasoned policy for
a different audience - commands a user runs interactively - so this guard reads
`scripts/` only. If that policy ever changes, extend this guard in the same
commit.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

_SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "call", "check_call"}
_TEXT_KWARGS = {"text", "universal_newlines"}

# A byte invalid in UTF-8 *and* in the codecs the affected hosts use (cp936/GBK,
# cp1252, ascii), so the probe does not depend on which hostile locale the runner
# happens to have. Pinned by test_the_probe_byte_is_invalid_in_every_relevant_codec.
_PROBE_BYTE = 0x81


def _text_mode_calls(source: str, label: str = "<string>") -> list[tuple[int, str, set[str]]]:
    """(lineno, func_name, kwargs) for every text-mode subprocess call in `source`.

    Keywords forwarded through `**kwargs` cannot be read, so such a call reports
    the empty name `"**"` in place of the func name - the caller can then refuse
    to treat it as clean instead of assuming the options are visible.
    """
    found: list[tuple[int, str, set[str]]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", "")
        )
        if name not in _SUBPROCESS_FUNCS:
            continue
        kwargs = {k.arg for k in node.keywords if k.arg}
        if any(k.arg is None for k in node.keywords):
            # options are forwarded; text= and encoding= may both be in there
            found.append((node.lineno, "**", kwargs))
        elif kwargs & _TEXT_KWARGS:
            found.append((node.lineno, name, kwargs))
    return found


def test_every_script_text_mode_subprocess_pins_its_encoding() -> None:
    """Every text-mode subprocess call under `scripts/` pins `encoding=`."""
    scripts = sorted(SCRIPTS.glob("*.py"))
    assert scripts, f"no scripts found under {SCRIPTS}"

    violations: list[str] = []
    for script in scripts:
        for lineno, func, kwargs in _text_mode_calls(
            script.read_text(encoding="utf-8"), script.name
        ):
            if func == "**":
                violations.append(
                    f"{script.name}:{lineno} forwards **kwargs into a subprocess "
                    "call - the encoding is not visible here, check the caller"
                )
            elif "encoding" not in kwargs:
                violations.append(
                    f"{script.name}:{lineno} subprocess.{func}("
                    f"{', '.join(sorted(kwargs))}) has no encoding="
                )

    assert not violations, (
        "a text-mode subprocess without encoding= decodes with the locale codec, "
        "so it raises UnicodeDecodeError on a non-UTF-8 host (cp936/GBK, cp1252) "
        "for output that is correctly UTF-8 - the class behind #1119/#1121 and "
        'issue #1132. Add `encoding="utf-8", errors="replace"`:\n  '
        + "\n  ".join(violations)
    )


def test_the_scan_catches_an_unpinned_site() -> None:
    """Positive control: the scan must actually flag the shape it forbids.

    Otherwise a refactor that stops matching (say the rule starts requiring a
    keyword nothing uses) turns the guard above into a test that always passes.
    """
    bad = "import subprocess\nr = subprocess.run(['gh', 'api', 'x'], capture_output=True, text=True)\n"
    pinned = (
        "import subprocess\n"
        "r = subprocess.run(['gh', 'api', 'x'], capture_output=True, text=True,\n"
        "                   encoding='utf-8', errors='replace')\n"
    )
    forwarded = "import subprocess\nr = subprocess.run(['gh'], **opts)\n"

    assert [f for _, f, _ in _text_mode_calls(bad)] == ["run"], (
        "the scan no longer sees a plain unpinned call"
    )
    flagged = [(f, k) for _, f, k in _text_mode_calls(bad) if "encoding" not in k]
    assert flagged, "an unpinned call must be reported, not just seen"

    seen_pinned = [k for _, _, k in _text_mode_calls(pinned)]
    assert seen_pinned and all("encoding" in k for k in seen_pinned), (
        "a pinned call must be seen and must not be reported"
    )

    assert [f for _, f, _ in _text_mode_calls(forwarded)] == ["**"], (
        "a call forwarding **kwargs must be reported as unreadable, never ignored"
    )


def test_the_probe_byte_is_invalid_in_every_relevant_codec() -> None:
    """The discriminator holds on any host, so the behavioural test cannot self-pass.

    If `_PROBE_BYTE` were decodable in one of these codecs, the test below would
    pass for a reason unrelated to the fix on that host - the "green where I ran
    it" failure this whole class of bug is made of.
    """
    for codec in ("utf-8", "ascii", "cp936", "gbk", "cp1252"):
        with pytest.raises(UnicodeDecodeError):
            bytes([_PROBE_BYTE]).decode(codec)


_CHILD = (
    "import subprocess, sys\n"
    "child = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(bytes([%d]))']\n"
    % _PROBE_BYTE
) + (
    "for pinned in (True, False):\n"
    "    kw = {'text': True}\n"
    "    if pinned:\n"
    "        kw.update(encoding='utf-8', errors='replace')\n"
    "    label = 'PINNED' if pinned else 'UNPINNED'\n"
    "    try:\n"
    "        out = subprocess.run(child, capture_output=True, **kw).stdout\n"
    # Report the *length* only: the decoded text contains U+FFFD, and printing it
    # here would make the child's own stdout raise under the hostile locale -
    # i.e. the probe would fail for a reason unrelated to what it measures.
    "        print(label, 'OK', len(out))\n"
    "    except UnicodeDecodeError as e:\n"
    "        print(label, 'RAISED', type(e).__name__)\n"
)


@pytest.mark.parametrize("locale_codec", ["C", "en_US.ISO-8859-1"])
def test_pinned_decoding_survives_a_hostile_locale(locale_codec: str) -> None:
    """Behavioural half: pinning returns text exactly where the unpinned shape raises.

    Both shapes run in the same child under an explicitly non-UTF-8 locale, so
    the difference is real on any host - including a UTF-8 developer machine,
    where an unpinned decode would succeed and this test would prove nothing.
    """
    env = dict(os.environ, PYTHONUTF8="0", LC_ALL=locale_codec, LANG=locale_codec)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PINNED OK" in proc.stdout, (
        "pinned decoding must return text; got:\n" + proc.stdout + proc.stderr
    )
    assert "UNPINNED RAISED" in proc.stdout, (
        "the unpinned shape must be shown to fail here, else this test would "
        "pass on a UTF-8 host and prove nothing:\n" + proc.stdout + proc.stderr
    )


def _load_script(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader, f"cannot load scripts/{name}.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gh_api_helper_pins_encoding(monkeypatch) -> None:
    """`reader_fix_latency._gh` really passes encoding= to its subprocess.

    The static half names the site; this one drives the function, so a future
    edit that moves the decoding somewhere the AST rule cannot see must still
    keep the behaviour the rule exists to protect.
    """
    module = _load_script("reader_fix_latency")
    seen: dict[str, object] = {}

    class _Proc:
        returncode = 0
        stdout = '{"number": 7}'
        stderr = ""

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen.update(kwargs)
        return _Proc()

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    assert module._gh("repos/o/r/issues?state=closed") == {"number": 7}
    assert seen.get("encoding") == "utf-8", (
        f"`gh api` JSON must not be decoded with the locale codec; got {seen!r}"
    )
    assert seen.get("text") is True
