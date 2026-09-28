"""The daemon's tool reachability must not depend on what launched it.

Rant 2026-09-27T19:45:54 (project ``emrg``), defect B — measured on this host.
The host restarted the daemon from the GUI, and the new daemon's parent was
``EMRG.app/Contents/MacOS/EMRG`` (ppid 1): a GUI started from the Dock, which
inherits launchd's minimal environment. Its ``PATH`` was

    /Users/argszero/.emrg/install/bin:/usr/bin:/bin:/usr/sbin:/sbin

— missing ``~/.local/bin`` (uv) and ``/opt/homebrew/bin`` (gh). The launcher
only *prepends* its own ``bin/`` (``bin/emrgd``), and the GUI spawn passes no
``env`` (``emrg/gui/daemon_client.js``), so the child inherits exactly what the
GUI has. Every scheduled task's ``uv`` / ``gh`` / homebrew tool then fails with
``command not found`` until each agent rediscovers the directories on its own —
and nothing recorded the effective ``PATH``, so the host could not see it.

The defect is the *difference*, not the missing directory: a terminal-started
daemon and a Dock-started one behaved differently. (The irony the rant records:
``emrg/gui/main.js`` creates the ``~/.local/bin/emrg`` symlink itself, while the
daemon it starts cannot see that directory.)

The fix is one implementation, in the process every launch path ends in —
``bin/emrgd`` / ``bin/emrgd.cmd`` exec ``python -m emrg.server``, and
``emrg server`` reaches the same package — so a terminal start, a GUI start and
the CLI are normalized by the same code, and Windows needs no second copy of
the logic in its ``.cmd`` launcher. The alternative the rant also allows (the
GUI passing an ``env`` on spawn) was not taken because it repairs one of the
three launch paths and can drift from the others.

Two properties are deliberate:

* **Append, never prepend.** The inherited ``PATH`` is the host's explicit
  choice and keeps priority; a directory is added only when it exists and is
  not already there, so a healthy environment is left byte-identical.
* **One INFO line at startup, whether or not anything was added.** The absence
  of that record is part of what made this take a day to find.

Only user-tool directories are named here — never a shell rc file. Sourcing
``.zshrc``/``.bashrc`` would drag arbitrary side effects into a long-lived
daemon, which the rant rules out.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import MutableMapping

logger = logging.getLogger(__name__)

#: Directories holding user-installed tools, in the order they are added.
#: ``~/.local/bin`` is where this product's installer puts ``emrg``/``emrgd``
#: and where ``uv`` installs itself; the two absolute prefixes are the ones a
#: Homebrew install uses (Apple silicon first, then Intel/Linux).
POSIX_TOOL_DIRS: tuple[str, ...] = (
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)

#: The same convention on Windows: ``~/.local/bin`` is still the user-script
#: directory this project's own tooling names (``emrg/gui/main.js``). The
#: bundled ``git``/``gh`` directories need no entry — ``bin/emrgd.cmd`` already
#: prepends them, and this module runs after it, in the same child process.
WINDOWS_TOOL_DIRS: tuple[str, ...] = ("~/.local/bin",)


def _is_windows(platform: str | None) -> bool:
    """Whether ``platform`` (default: the running one) is Windows."""
    name = sys.platform if platform is None else platform
    return name.startswith("win") or name == "nt"


def path_separator(platform: str | None = None) -> str:
    """The ``PATH`` separator of ``platform`` (default: the running one)."""
    return ";" if _is_windows(platform) else ":"


def tool_dirs(
    platform: str | None = None,
    home: Path | str | None = None,
) -> list[Path]:
    """The user-tool directories this platform's daemon must be able to reach.

    ``home`` exists so a test can name a directory it owns instead of the
    host's; ``~`` expands against ``Path.home()`` otherwise.
    """
    names = WINDOWS_TOOL_DIRS if _is_windows(platform) else POSIX_TOOL_DIRS
    base = Path(home) if home is not None else Path.home()
    out: list[Path] = []
    for name in names:
        out.append(base / name[2:] if name.startswith("~/") else Path(name))
    return out


def _same_dir(candidate: str, existing: str, windows: bool) -> bool:
    """Whether two ``PATH`` entries name the same directory.

    A trailing separator is not a different directory, and Windows compares
    case-insensitively — both spellings arrive in practice (an installer may
    write ``C:\\X\\`` where the registry holds ``c:\\x``).
    """
    left = candidate.rstrip("/\\") or candidate
    right = existing.rstrip("/\\") or existing
    return left.lower() == right.lower() if windows else left == right


def augment_path(
    env: MutableMapping[str, str],
    *,
    platform: str | None = None,
    home: Path | str | None = None,
    isdir=os.path.isdir,
) -> list[Path]:
    """Append every standard tool dir that exists and ``env['PATH']`` lacks.

    Returns what was appended, in order (empty when the environment already
    reaches all of them — the case of a daemon started from a terminal).
    ``isdir`` is injectable so a test can decide which prefixes exist without
    depending on what the machine happens to have installed.
    """
    sep = path_separator(platform)
    windows = _is_windows(platform)
    current = env.get("PATH") or ""
    present = [entry for entry in current.split(sep) if entry]

    added: list[Path] = []
    for directory in tool_dirs(platform, home):
        text = str(directory)
        if not isdir(text):
            continue
        if any(_same_dir(text, entry, windows) for entry in present):
            continue
        added.append(directory)

    if added:
        tail = [str(directory) for directory in added]
        env["PATH"] = sep.join([current, *tail]) if current else sep.join(tail)
    return added


def ensure_tool_dirs(log: logging.Logger | None = None) -> list[Path]:
    """Normalize the daemon's ``PATH`` and record the result. Runs at startup.

    Returns what was added. The INFO line is the daemon's only statement of the
    environment its tools will run in — logged on both branches, because
    "nothing was added" is as much an answer as a list of additions.
    """
    added = augment_path(os.environ)
    effective = os.environ.get("PATH", "")
    sink = log if log is not None else logger
    if added:
        sink.info(
            "tool path: added %s — effective PATH: %s",
            ", ".join(str(directory) for directory in added),
            effective,
        )
    else:
        sink.info("tool path: effective PATH (unchanged): %s", effective)
    return added
