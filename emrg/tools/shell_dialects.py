"""Which shell dialect each platform mounts — one roster, every reader.

Blueprint: ``.emrg/designs/bash-tool-v2-design.md`` §14.2 layer 6. The blueprint
does not branch on the platform at runtime; it *composes* by it
(``packages/bundle/base/cordis.patch.yml`` lists both shell stacks and every row
decides for itself where it mounts). The reason is recorded in its own Agent
Note: *the model-visible contract is the dialect itself* — ``bash`` and ``pwsh``
are peers, not one executor with a mode, so "mounted as one dialect, running
another" is not a state the composition can reach.

EMRG's composition site is the daemon's registry (``build_shell_tool``), and this
module is the roster it reads. It is a module rather than a few literals inside
the daemon because the roster has more than one reader — the argument injection,
the TUI's argument formatter, any guard that must not forget a dialect — and a
name list two readers edit separately is a list one of them will miss. That is
not a hypothetical: the phase this repairs shipped a Windows host a ``bash``
tool that spawned an executable Windows does not have, and the accident was
reachable precisely because "which dialect runs here" was a constant inside one
file rather than a fact of the composition (design §14.6, "A3 从未变成阶段").

**This module imports nothing.** It is the dependency-free half of the roster,
for the same reason the blueprint splits ``pwsh-local/resolve.ts`` out of the
package that consumes it: a non-package consumer — the TUI — must be able to read
the roster without dragging the executors in behind it. The executable decision
(which class to build) lives with the composition, in
``emrg/server/daemon.py::build_shell_tool``.

The Electron renderer is TypeScript and cannot import this. Its copy is marked
at its own definition (``emrg/gui/renderer/src/lib/resultPanel.ts``,
``SHELL_TOOL_NAMES``) and pinned by that side's tests — the honest statement is
that the cross-language boundary is where the single definition stops.
"""

from __future__ import annotations

import sys

#: The dialect POSIX mounts.
SHELL_TOOL_NAME_POSIX = "bash"

#: The dialect Windows mounts. Windows has no ``bash`` tool row at all — not a
#: fallback, not a second option. Git for Windows ships a ``bash.exe`` under
#: ``…\Git\bin`` while the installer puts ``…\Git\cmd`` on ``PATH``, so "bash is
#: on PATH on Windows" is not a fact anyone can rely on; the blueprint's answer
#: is that Windows simply mounts a different shell, and this is that answer.
SHELL_TOOL_NAME_WINDOWS = "pwsh"

#: Every dialect the daemon can mount. Readers that must cover all of them read
#: this set instead of repeating the two literals — the shape that produced the
#: Windows accident (a list with a dialect missing from it).
SHELL_TOOL_NAMES = frozenset({SHELL_TOOL_NAME_POSIX, SHELL_TOOL_NAME_WINDOWS})


def shell_tool_name(platform_name: str | None = None) -> str:
    """The name of the shell tool this platform mounts.

    Pure: the platform is a parameter, so a test can ask the question about
    Windows without being on Windows (the host's own answer is one call with no
    argument).

    :param platform_name: the platform to decide for; defaults to the host's.
    :returns: ``pwsh`` on Windows, ``bash`` everywhere else.
    """
    platform_name = platform_name or sys.platform
    return SHELL_TOOL_NAME_WINDOWS if platform_name == "win32" else SHELL_TOOL_NAME_POSIX
