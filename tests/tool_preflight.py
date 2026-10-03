"""One home for the question `shutil.which` does not answer: will this program run?

`shutil.which(name)` answers **is this name on PATH**. The suite kept reading that as
**is this tool available**, and on a host where the name resolves to something that
cannot start, the two come apart - measured 2026-10-03 on the evolution host, where the
`npm`, `node` and `npx` asdf shims point at a removed interpreter:

    $ which npm        -> /Users/.../.asdf/shims/npm        (a path, so `which` is happy)
    $ npm --version    -> rc=126, "No such file or directory: .../libexec/bin/asdf"

Every gate written on `which(...) is None` therefore does not fire there, and the test
runs and fails for a reason that is not a defect in what it tests - while the docstring
beside it promises a skip when the tool is missing.

**The rule is one line: presence is measured by starting the program.** `starts` does
exactly that and nothing else, so a caller can state the failure it wants in its own
words and this module never has to guess one.

Why a module rather than a helper in each file: the same predicate was being written a
second time wherever it was needed (the git-repair fixture and the node-count probe),
and a rule with two homes is a rule free to drift apart. The one deliberate exception is
`tests/test_check_node_test_count.py::_runner_that_cannot_start`, which runs `npm`
through the tool's *own* resolver (`mod._run`) because that resolution is the code under
test - and it is listed, with that reason, in the guard beside this module.

The cache is keyed by the executable path, not by the tool name: a test that repoints
PATH at its own shim has a different path, so it gets its own reading rather than a
stale one, and `starts` is called once per distinct program rather than once per call
site.
"""

from __future__ import annotations

import subprocess

#: `(executable, args)` -> whether it starts. Keyed by path so a repointed PATH is a
#: different entry, and cached because the git fixture is autouse.
_RUNS: dict[tuple[str, tuple[str, ...]], bool] = {}

#: How long a program gets to answer `--version`. A program that hangs is not a program
#: that starts, and an unbounded probe turns a broken shim into a hung suite.
_TIMEOUT_SECONDS = 60.0


def starts(exe: str | None, args: tuple[str, ...] = ("--version",)) -> bool:
    """Whether `exe` is a program that starts, as opposed to a name that resolves.

    `False` for a name that is not there at all, for a file that exists and cannot be
    executed, and for one that runs and exits non-zero - in each case the caller cannot
    use it, which is the only question a precondition is asking.

    `args` exists because `--version` is not universal: `codesign` rejects it. The
    default is the one every tool this suite probes answers.
    """
    if not exe:
        return False
    key = (exe, tuple(args))
    if key not in _RUNS:
        try:
            proc = subprocess.run(
                [exe, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_TIMEOUT_SECONDS,
            )
            _RUNS[key] = proc.returncode == 0
        except (OSError, subprocess.SubprocessError, ValueError):
            _RUNS[key] = False
    return _RUNS[key]
