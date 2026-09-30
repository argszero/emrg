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
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import MutableMapping

logger = logging.getLogger(__name__)

#: Directories holding user-installed tools, in the order they are added.
#: ``~/.local/bin`` is where this product's installer puts ``emrg``/``emrgd``
#: and where ``uv`` installs itself; the two absolute prefixes are the ones a
#: Homebrew install uses (Apple silicon first, then Intel/Linux).
#:
#: The rest are **version-manager** directories, and they are here because the
#: first three are where an *installer* writes (issue #1783, measured
#: 2026-09-30). A host whose toolchain comes from a version manager keeps it
#: behind a shim directory of the manager's own, so a list of installer
#: directories alone left ``node``/``npm``/``python3``/``cargo``/``java``
#: unreachable for a daemon started from the GUI — 76 executables in this host's
#: ``~/.asdf/shims``, while a terminal start saw all of them. That is the same
#: defect this module exists to close (#1673), one level down: the parent fix
#: named the two directories *its* rant needed and closed.
#:
#: Shims are the stable path and that is why they are named rather than an
#: install root: ``asdf``, ``mise`` and ``pyenv`` all expose ``<root>/shims``,
#: which no version upgrade moves.
POSIX_TOOL_DIRS: tuple[str, ...] = (
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.asdf/shims",
    "~/.mise/shims",
    "~/.local/share/mise/shims",
    "~/.pyenv/shims",
    "~/.volta/bin",
    "~/.bun/bin",
    "~/.cargo/bin",
    "~/Library/pnpm",
    "~/.local/share/pnpm",
)

#: Directories a static list **cannot** name, kept here so the gap is stated
#: where the list is rather than rediscovered: ``nvm`` keeps its binaries at
#: ``~/.nvm/versions/node/<version>/bin``. The path is version-keyed, so no
#: entry above can stand for it and a host whose ``node`` comes only from
#: ``nvm`` is still not reached. Named rather than implied, because a list that
#: looks complete is how this defect survived its own fix once already.
VERSION_KEYED_TOOL_DIRS: tuple[str, ...] = ("~/.nvm/versions/node/<version>/bin",)

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


def path_flavour(platform: str | None = None) -> type[PurePath]:
    """The path flavour of ``platform`` (default: the running one).

    Named rather than left to ``Path``, because the two can disagree and the
    disagreement is silent: a caller that says ``platform="linux"`` on a Windows
    host is asking what Linux looks like, but ``Path("/opt/homebrew/bin")``
    answers with ``\\opt\\homebrew\\bin`` — a spelling that exists on neither
    platform, and one that a Windows ``isdir`` therefore never finds. Measured
    on CI 2026-09-28: the three behaviour tests passed on macOS and failed on
    `test-windows` for exactly this, because their expected spellings came from
    the named platform and their actual ones from the interpreter.
    """
    return PureWindowsPath if _is_windows(platform) else PurePosixPath


def tool_dirs(
    platform: str | None = None,
    home: Path | str | None = None,
) -> list[PurePath]:
    """The user-tool directories this platform's daemon must be able to reach.

    Both halves come from ``platform`` — the list, the separator and the path
    flavour — so a named platform really is that platform, whatever host the
    caller runs on. With the default (the running platform) this is
    ``Path.home()`` and the host's own spelling, which is all production sees.

    ``home`` exists so a test can name a directory it owns instead of the
    host's, and it is read as a string so the caller may pass it in the *named*
    platform's flavour (a ``PurePosixPath`` for a simulated Linux) rather than
    in the host's.
    """
    names = WINDOWS_TOOL_DIRS if _is_windows(platform) else POSIX_TOOL_DIRS
    flavour = path_flavour(platform)
    base = flavour(str(home) if home is not None else str(Path.home()))
    out: list[PurePath] = []
    for name in names:
        out.append(base / name[2:] if name.startswith("~/") else flavour(name))
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
) -> list[PurePath]:
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

    added: list[PurePath] = []
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


def ensure_tool_dirs(log: logging.Logger | None = None) -> list[PurePath]:
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
