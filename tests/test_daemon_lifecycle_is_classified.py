"""The daemon's life is classified, not only stated (issue #1324).

Never stopping or restarting the emrg server is the project's highest-priority
rule (host 2026-08-18T22:58, `MANIFESTO.md` 第四条附则二). Until `_stops_or_restarts_the_daemon`
it was enforced in **one** place and stated in **two** — and classified nowhere a
shell command passes through, which is the shape the host actually hit: on
2026-08-21T09:41 a session in another project ran a "measurement" whose
subprocess signalled the daemon, the TUI answered `server connection lost`, and
the host asked *"你怎么验证的，怎么把 emrg server重启了？"*.

Every test here asks the **pure predicate** (`_check_sandbox`) or the classifier
directly. Nothing in this file drives `BashTool.execute` with a lifecycle
command: a test that really reached the daemon would be the incident, not a
proof — and a test's safety must not depend on the code it is testing. The
argv-side reader of the same act (`tests/conftest.py::_spawns_a_daemon_stop_or_restart`)
has its own corpus in `tests/test_hermeticity_guard.py`, which asks for the
refusal *before* anything is signalled or spawned.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from emrg.tools.bash_tool import (
    _check_sandbox,
    _nested_command_texts,
    _stops_or_restarts_the_daemon,
    _tokenize_command,
)


def _argv_side_reader():
    """`tests/conftest.py`'s classifier, from the copy pytest already loaded.

    Read out of `sys.modules` by file identity rather than `import conftest`:
    whether the tests directory is on `sys.path` depends on pytest's import mode,
    while the rootdir conftest is loaded under *some* name in every run — and a
    second copy of that module (executed through `importlib`) would install the
    red-line guards twice.
    """
    wanted = (Path(__file__).with_name("conftest.py")).resolve()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and Path(path).resolve() == wanted:
            return module._spawns_a_daemon_stop_or_restart
    raise AssertionError("tests/conftest.py is not loaded in this run")


#: Rows that stop or restart the daemon — the act, in the spellings a command
#: line can carry it. Each is refused at both checked tiers.
THE_ACT = [
    # the CLI's own two verbs, and the one that also takes the clients with it
    "emrg server stop",
    "emrg server restart",
    "emrg stop",
    "emrg stop --skip-gui",
    # a path to the program, and the interpreter spelling this repo itself uses
    "/usr/local/bin/emrg server restart",
    "python -m emrg server stop",
    "python -m emrg.server server stop",
    # a prefix hands the program over
    "uv run emrg server stop",
    "env emrg server stop",
    "nohup emrg server restart",
    "timeout 5 emrg server stop",
    # a flag between the program and its verb
    "emrg --verbose server stop",
    # a signaller whose operand names emrg, in all three spellings
    "pkill -f emrg.server",
    'pkill -f "python -m emrg"',
    "killall emrgd",
    'kill $(pgrep -f "python -m emrg")',
    "nice -n 5 pkill -f emrg.server",
    # a shell re-parsing the text, and a substitution naming the program
    "sh -c 'emrg server stop'",
    'bash -c "emrg server restart"',
    "eval 'emrg server stop'",
    'sh -c "$(which emrg) server stop"',
    # a chain, and the same chain across a newline
    "echo done; emrg server stop",
    "echo done\nemrg server stop",
    "pkill -f emrgd && ls",
]

#: Rows that merely *mention* the rule, or signal something that is not the
#: daemon. Each stays allowed — the #1513 lesson is that over-blocking a mention
#: is a defect too (`echo sh "patch /etc/hosts"` had to be fixed, not kept).
NOT_THE_ACT = [
    "git log --grep emrg",
    "git -C /Users/x/.emrg/evolution/emrg log --grep restart",
    "emrg --help",
    "emrg --help | grep stop",
    "emrg dev",
    "emrg update",
    "ls emrg",
    "cat emrg/tools/bash_tool.py",
    "grep -rn stop emrg/",
    # a *quoted* mention is one argument, not a command line
    "echo 'emrg server stop'",
    # a signaller whose operand names somebody else — a stated limit, pinned
    "pkill -f firefox",
    "kill 12345",
    "killall python",
    # a string handed over by a non-shell program — the other stated limit
    'env -S "emrg server stop"',
]


@pytest.mark.parametrize("cmd", THE_ACT)
def test_the_act_is_refused_at_both_checked_tiers(cmd):
    for mode in ("read-only", "workspace-write"):
        allowed, reason, _enforcement = _check_sandbox(cmd, mode, "/tmp")
        assert allowed is False, f"{cmd!r} was allowed at {mode}"
        assert "emrg" in (reason or "")


@pytest.mark.parametrize("cmd", NOT_THE_ACT)
def test_a_mention_is_not_the_act(cmd):
    for mode in ("read-only", "workspace-write"):
        allowed, reason, _enforcement = _check_sandbox(cmd, mode, "/tmp")
        assert allowed is True, f"{cmd!r} was refused at {mode}: {reason}"
    assert _stops_or_restarts_the_daemon(cmd) is None


def test_full_access_is_untouched():
    """The tier that checks nothing still checks nothing — the host's rule there."""
    allowed, _reason, enforcement = _check_sandbox("emrg server stop", "danger-full-access")
    assert allowed is True
    assert enforcement == "full"


def test_the_refusal_names_the_rule_and_a_way_out():
    allowed, reason, _enforcement = _check_sandbox("emrg server restart", "workspace-write")
    assert allowed is False
    assert "red line" in reason
    assert "#1324" in reason
    assert "emrg resume" in reason


def test_the_classifier_names_the_spelling_it_saw():
    """The message quotes the act, not a truncation of it."""
    assert _stops_or_restarts_the_daemon("emrg --verbose server stop") == (
        "emrg --verbose server stop"
    )
    assert _stops_or_restarts_the_daemon("emrg stop --skip-gui") == "emrg stop"
    assert _stops_or_restarts_the_daemon("pkill -f emrg.server") == "pkill emrg.server"


def test_the_verb_must_follow_the_program():
    """A verb *before* the `emrg` word is somebody else's argument."""
    assert _stops_or_restarts_the_daemon("git commit -m stop emrg") is None
    assert _stops_or_restarts_the_daemon("echo stop emrg") is None


def test_a_data_only_shell_word_is_read_by_the_payload_walk_not_by_this_rule():
    """`echo sh "emrg server stop"` prints a string, so the act is not in it.

    Measured rather than asserted from memory, because the answer differs between
    two trees: `_nested_command_texts` reads a shell *word*'s payload wherever
    the word stands on this tree, and the position gate of issue #1513 (PR #1515)
    narrows that. This rule inherits whichever reading the walk holds — it adds
    no over-block of its own, which is the claim worth pinning.
    """
    cmd = 'echo sh "emrg server stop"'
    read = _nested_command_texts(_tokenize_command(cmd))
    if not read:
        assert _stops_or_restarts_the_daemon(cmd) is None
    else:
        assert read == ["emrg server stop"]
        assert _stops_or_restarts_the_daemon(cmd) == "emrg server stop"


# ── the two readers of one act must not drift ───────────────────────────────
#
# `tests/conftest.py` classifies the act on an **argv** (a spawned child), and
# the product classifies it on a **command line** (the bash tool). They are
# different inputs to one rule, and the defect this guards against is the one
# this project has paid for repeatedly: two readers of one rule that must change
# together, where the second silently stops firing. The assertion is deliberately
# one-way — argv side refuses ⇒ the command-line side refuses — because the
# command-line side reaches further (unresolved wrappers, a signaller's operand).

_ARGV_ROWS = [
    ["emrg", "server", "stop"],
    ["/usr/local/bin/emrg", "server", "restart"],
    ["env", "emrg", "stop"],
    ["nohup", "emrg", "stop"],
    ["timeout", "5", "emrg", "server", "restart"],
    ["uv", "run", "emrg", "server", "stop"],
    ["python", "-m", "emrg", "server", "stop"],
    ["nice", "-n", "5", "pkill", "-f", "emrg.server"],
    ["sh", "-c", "emrg server stop"],
    ["sh", "-c", "(emrg server restart)"],
    ["sh", "-c", "$(which emrg) server stop"],
    ["sh", "-c", "'emrg' server stop"],
    ["sh", "-c", "`emrg server stop`"],
]


@pytest.mark.parametrize("argv", _ARGV_ROWS)
def test_the_two_readers_of_the_act_agree(argv):
    refuses = _argv_side_reader()

    if not refuses(argv, shell_parsed=True):
        pytest.skip("the argv side does not classify this row")

    line = " ".join(shlex.quote(part) for part in argv)
    assert _stops_or_restarts_the_daemon(line) is not None, (
        f"the argv side refuses {argv!r} but the command line {line!r} is allowed"
    )


def test_the_env_string_handover_is_the_argv_side_only():
    """A measured asymmetry, pinned so it is a limit rather than a surprise.

    `env -S "emrg server stop"` hands one string to be split into an argv. The
    argv side reads it (it is looking at an argv); the command-line side does
    not, because reading every quoted argument as a command is the over-block
    #1513 removed from this file. Stating the difference here keeps the two
    readers' scopes honest without widening either.
    """
    assert _argv_side_reader()(
        ["sh", "-c", 'env -S "emrg server stop"'], shell_parsed=True
    )
    assert _stops_or_restarts_the_daemon('env -S "emrg server stop"') is None
