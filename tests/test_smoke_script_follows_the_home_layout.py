"""The release smoke test reads and writes the daemon's files where the daemon keeps them.

Why this file exists
--------------------
The home-root split (#3130) moved everything EMRG owns under ``~/.emrg/global/`` and
left the ``.emrg`` root holding ``global/`` plus an agent's own state. The release
smoke test followed the split for the *install tree* (``global/install``) but not for
the three runtime carriers it also touches: it wrote the config to the pre-split root
and then polled ``$HOME/.emrg/emrgd.token``.

Measured, tag ``v0.3.70``, run ``44870840332``: **all four** platform legs failed at
``Smoke test (R42 temp HOME)`` with

    ==> 2. daemon start + port + auth + pong
      [debug] emrgd-debug.log:
      [debug] emrgd.log (tail):
      ✗ daemon did not write token file

and an empty debug log — the signature of a daemon that started correctly and wrote its
token where the script was not looking, not of one that crashed.

What the guard reads
--------------------
The three paths are taken as *values* from the package — :func:`emrg.config.config_path`,
:func:`emrg.connect.get_server_path`, :func:`emrg.config.logs_dir` — reduced to their
relation to :func:`emrg.home_layout.global_dir`, and then looked for in the script as
``"$HOME/.emrg/global/<rel>``. A layout change on the Python side therefore reddens here
in a pull request, instead of in a tagged release that has to be re-cut.

Both sides of that reduction come from the same call, so a suite that redirects the home
root (this repo's autouse fixture points every path at a temporary home) still compares
two things measured the same way.

What it does not read
---------------------
That the smoke test *passes* against a real runtime; only CI has a built one, and a
daemon in a throwaway HOME on a machine whose port 56031 is busy is skipped as
``audit-degraded``. This pins the paths, not the behaviour.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emrg.config import config_path, logs_dir  # noqa: E402
from emrg.connect import get_server_path  # noqa: E402
from emrg.home_layout import GLOBAL_DIRNAME, global_dir  # noqa: E402

SMOKE = ROOT / "packaging" / "smoke-test.sh"

#: How the script spells the home-level root, which is what the residue rule reads.
_HOME_ROOT = "$HOME/.emrg"

#: ``$HOME/.emrg/<the rest of the path>`` — the segment after the root is the verdict.
_ROOT_PATH = re.compile(r"\$HOME/\.emrg/([^\"'\s]*)")


def _script() -> str:
    return SMOKE.read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """The script's commands, without comment lines.

    A comment may name a path it is explaining (the script's cleanup note cites the
    daemon's log file), and a rule that reads prose reddens on the explanation rather
    than on the defect.
    """
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def _spelling_in_the_script(path: Path) -> str:
    """The path as the script must spell it: ``$HOME/<.emrg>/global/<rest>``."""
    root = global_dir()
    assert root.name == GLOBAL_DIRNAME, f"the global root is {root!r}, not a {GLOBAL_DIRNAME!r} directory"
    assert root.parent.name == ".emrg", f"the global root's parent is {root.parent.name!r}, not '.emrg'"
    tail = path.relative_to(root).as_posix()
    return f'"$HOME/.emrg/{GLOBAL_DIRNAME}/{tail}'


def test_the_smoke_test_names_the_paths_the_daemon_uses() -> None:
    text = _script()
    for path in (config_path(), Path(get_server_path()), logs_dir()):
        spelling = _spelling_in_the_script(path)
        assert spelling in text, (
            f"packaging/smoke-test.sh never names {spelling!r}: it is reading a path the "
            "daemon does not write (the v0.3.70 smoke failure)"
        )


def test_the_smoke_test_names_no_pre_split_runtime_path() -> None:
    offenders: list[tuple[str, str]] = []
    for line in _code_lines(_script()):
        for pre_split in (".emrg/config.toml", ".emrg/emrgd.token", ".emrg/logs/"):
            if pre_split in line:
                offenders.append((pre_split, line.strip()))
    assert offenders == [], (
        "these lines read the pre-split home root, which no daemon writes since the "
        f"split: {offenders}"
    )


def test_every_home_root_path_in_the_smoke_test_is_under_global() -> None:
    """The residue rule: any new carrier in the script inherits the split's verdict."""
    global_name = GLOBAL_DIRNAME
    seen: list[str] = []
    offenders: list[tuple[str, str]] = []
    for line in _code_lines(_script()):
        for segment in _ROOT_PATH.findall(line):
            seen.append(segment)
            # the global root itself is created by name (`mkdir -p "$HOME/.emrg/global"`),
            # so the bare name is legitimate and only a *different* child is a defect
            if segment != global_name and not segment.startswith(f"{global_name}/"):
                offenders.append((segment, line.strip()))
    # a pattern that matched nothing would pass every assertion below it
    assert seen, f"no {_HOME_ROOT}/<path> in the script at all — the rule is reading nothing"
    assert offenders == [], f"paths under the home root that are not under {global_name}/: {offenders}"
