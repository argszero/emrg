"""The bash-tool-v2 confinement boundary, measured at the process boundary.

Rant ``2026-09-21T18:50:03`` ("bash tool v2"), design
``/Users/argszero/.emrg/designs/bash-tool-v2-design.md`` §3, §4.

Two halves, and they are deliberately not the same instrument:

* the **fail-closed** half spawns nothing, so it runs everywhere — it is the part
  ``ubuntu`` and ``windows-2025`` CI actually exercise, and it asserts the one
  thing that must never regress: a request for confinement that cannot be
  honoured does not become an unconfined run;
* the **boundary** half spawns the real Seatbelt runner, so it is darwin-only and
  skips elsewhere.  Its subject is the interpreter hole the old static scan could
  not close: a ``python3 -c`` write is refused by the kernel, not by a parser.

The outside location is a sibling of the pytest temp dir *inside the per-user
temp root* — outside ``/tmp`` and outside ``tempfile.gettempdir()``, so it lies
outside every root a ``workspace-write`` policy grants.  That is what stops a
test from passing on the temp grant while believing it proved the workspace
grant.
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from emrg.sandbox.contract import SandboxUnavailableError, sandbox_denial_marker
from emrg.sandbox.policy import SandboxPolicy
from emrg.tools.bash_tool_v2 import (
    BashToolV2,
    _decode_output,
    is_runner_spawn_failure,
    render_result,
    run_command,
)

SEATBELT_AVAILABLE = sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec")

#: The real boundary: macOS Seatbelt, present on the dev host and on no CI runner.
needs_seatbelt = pytest.mark.skipif(
    not SEATBELT_AVAILABLE,
    reason="the real boundary needs macOS sandbox-exec; the fail-closed half covers the rest",
)

#: v2's inner shell is ``bash`` by contract (blueprint §3.6), so any test that
#: actually spawns is POSIX-only.  Windows CI is covered by the fail-closed half,
#: which is exactly the half that decides whether a Windows host runs v2 at all.
needs_a_shell = pytest.mark.skipif(
    sys.platform == "win32",
    reason="v2's inner shell is bash; Windows is covered by the fail-closed half",
)


class Boundary:
    """A workspace and an outside directory, both outside every granted root."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.workspace = base / "ws"
        self.outside = base / "outside"
        self.workspace.mkdir()
        self.outside.mkdir()

    def policy(self, mode: str) -> SandboxPolicy:
        return SandboxPolicy(mode=mode, workspace_root=str(self.workspace))

    def run(self, command: str, mode: str = "workspace-write", timeout: float = 30.0):
        return asyncio.run(
            run_command(command, policy=self.policy(mode), workdir=str(self.workspace), timeout=timeout)
        )

    def execute(self, command: str, mode: str = "workspace-write"):
        return asyncio.run(
            BashToolV2().execute(
                {
                    "command": command,
                    "intent": "probe",
                    "sandbox": mode,
                    "workspace": str(self.workspace),
                    "workdir": str(self.workspace),
                }
            )
        )


@pytest.fixture
def boundary():
    """A scratch pair whose parent is outside /tmp and outside gettempdir().

    ``tempfile.gettempdir()`` is a granted root; its *parent* is not, which is
    what gives the test a genuine outside.  If the host refuses that directory the
    test cannot be honest about the boundary, so it skips rather than passes.
    """
    parent = Path(os.path.realpath(tempfile.gettempdir())).parent
    base = parent / f"emrg-v2-boundary-{os.getpid()}"
    try:
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True)
    except OSError as exc:  # pragma: no cover - host-specific
        pytest.skip(f"cannot create a scratch tree outside the temp root: {exc}")
    try:
        yield Boundary(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ── fail closed: the half that runs on every platform ─────────────────────


def test_the_command_does_not_run_when_no_backend_can_confine_it(tmp_path):
    """The whole point of the contract: no silent fallback to a bare run."""
    sentinel = tmp_path / "must-not-exist"
    with pytest.raises(SandboxUnavailableError) as excinfo:
        asyncio.run(
            run_command(
                f"touch {sentinel}",
                policy=SandboxPolicy(mode="workspace-write", workspace_root=str(tmp_path)),
                workdir=str(tmp_path),
                timeout=30.0,
                platform_name="win32",
            )
        )
    assert excinfo.value.code == "SANDBOX_UNAVAILABLE"
    assert not sentinel.exists(), "the command ran despite the refusal"


def test_the_tool_reports_the_refusal_instead_of_running_bare(tmp_path, monkeypatch):
    """Through the tool, the model sees a refusal rather than an unconfined result."""
    import emrg.sandbox.providers as providers

    monkeypatch.setattr(providers, "PLATFORM_CHAINS", {})
    sentinel = tmp_path / "must-not-exist"
    result = asyncio.run(
        BashToolV2().execute(
            {
                "command": f"touch {sentinel}",
                "intent": "probe",
                "sandbox": "workspace-write",
                "workspace": str(tmp_path),
                "workdir": str(tmp_path),
            }
        )
    )
    assert result.error is True
    assert result.content.startswith("⛔ ")
    assert not sentinel.exists()


def test_a_missing_runner_program_is_a_refusal_and_a_bad_cwd_is_not(tmp_path):
    """Only an error naming ``argv[0]`` exactly, with a usable cwd, is runner evidence.

    A bad cwd must never be laundered into "the sandbox is unavailable": the two
    failures have different fixes, and reporting the wrong one sends the operator
    to the wrong place.
    """
    runner = "/usr/bin/sandbox-exec"
    missing_runner = FileNotFoundError(2, "No such file or directory", runner)
    assert is_runner_spawn_failure(missing_runner, runner, str(tmp_path)) is True
    # Same error, but the caller's own cwd cannot be entered: not runner evidence.
    assert is_runner_spawn_failure(missing_runner, runner, str(tmp_path / "gone")) is False
    # The rejection names something else entirely.
    other = FileNotFoundError(2, "No such file or directory", "/bin/bash")
    assert is_runner_spawn_failure(other, runner, str(tmp_path)) is False
    # EACCES counts, an unrelated errno does not.
    assert is_runner_spawn_failure(PermissionError(13, "denied", runner), runner, str(tmp_path)) is True
    assert is_runner_spawn_failure(OSError(12, "no memory", runner), runner, str(tmp_path)) is False
    # A Python-level error carries no executable evidence.
    assert is_runner_spawn_failure(ValueError("nope"), runner, str(tmp_path)) is False


# ── the result surface ────────────────────────────────────────────────────


def test_an_unconfined_run_claims_no_enforcement():
    """A ``{mode, denied}`` shape with no ``enforcement`` key: no promise was made."""
    from emrg.tools.bash_tool_v2 import ShellRunResult

    text = render_result(ShellRunResult(stdout="hi", exit_code=0, sandbox={"mode": "danger-full-access", "denied": False}))
    assert text == "hi"


def test_a_denial_is_announced_in_the_vocabulary_the_model_reads():
    from emrg.tools.bash_tool_v2 import ShellRunResult

    text = render_result(
        ShellRunResult(
            stderr="bash: x: Operation not permitted",
            exit_code=1,
            sandbox={"mode": "read-only", "denied": True, "enforcement": "full"},
        )
    )
    assert sandbox_denial_marker("read-only") in text
    assert text.rstrip().endswith("[exit code: 1]")


def test_a_runner_failure_is_never_reported_as_a_denial():
    """Runner failure outranks denial: the command did not run, so it was not refused.

    The two are different events with different fixes — a broken profile is not
    the policy saying no.
    """
    from emrg.tools.bash_tool_v2 import classify_runner_failure
    from emrg.sandbox.providers.darwin import RUNNER_FAILURE_RULES

    fatal = classify_runner_failure(65, "sandbox-exec: syntax error\n", RUNNER_FAILURE_RULES)
    assert fatal == "sandbox-exec: syntax error"
    assert classify_runner_failure(65, "Operation not permitted\n", RUNNER_FAILURE_RULES) is None
    assert classify_runner_failure(0, "sandbox-exec: nope\n", RUNNER_FAILURE_RULES) is None
    assert classify_runner_failure(None, "sandbox-exec: nope\n", RUNNER_FAILURE_RULES) is None


def test_the_package_does_not_import_the_frozen_tool():
    """R1: the parallel period's whole safety property is that neither reaches the other.

    A shared helper would make every later edit to the frozen file a v2 bug, which
    is the failure mode the new package exists to remove.  Scanned over *import
    statements*, not substrings: the module's docstrings legitimately name the old
    file while explaining why they no longer share code with it.
    """
    import re

    root = Path(__file__).resolve().parents[1] / "emrg"
    frozen = {"emrg.tools.bash_tool", "emrg.tools.bash_tool_v2"}
    offenders = []
    for source in [root / "tools" / "bash_tool_v2.py", *(root / "sandbox").rglob("*.py")]:
        for line in source.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*(?:from|import)\s+(emrg[\w.]*)", line)
            if match and match.group(1) in frozen:
                offenders.append(f"{source.name}: {line.strip()}")
    assert offenders == []


# ── the boundary itself (darwin only) ─────────────────────────────────────


@needs_seatbelt
def test_a_command_runs_inside_the_workspace_and_may_write_there(boundary):
    """The workspace grant, proven where the temp grant cannot reach."""
    result = boundary.run("echo hi > inside.txt && cat inside.txt")
    assert (result.exit_code, result.stderr) == (0, "")
    assert result.stdout == "hi"
    assert (boundary.workspace / "inside.txt").read_text().strip() == "hi"
    assert result.sandbox == {"mode": "workspace-write", "denied": False, "enforcement": "full"}


@needs_seatbelt
def test_an_interpreter_write_outside_the_workspace_is_refused(boundary):
    """The hole the old scan could not close: the refusal is the kernel's.

    ``bash_tool.py:2880`` documented "what it does not reach is an interpreter".
    Here the write is attempted by ``python3`` — a language the static scan cannot
    read — and Seatbelt refuses it at the process boundary, whatever the language.
    """
    target = boundary.outside / "escaped.txt"
    script = f"open({str(target)!r}, 'w').write('escaped')"
    result = boundary.run(f"{sys.executable} -c {_shell_quote(script)}")
    assert result.exit_code not in (0, None), "an escaping write must not report success"
    assert not target.exists(), "the sandbox did not hold"
    assert result.sandbox["denied"] is True
    assert "operation not permitted" in result.stderr.lower()
    assert sandbox_denial_marker("workspace-write") in render_result(result)


@needs_seatbelt
def test_a_mutation_by_a_subprocess_is_refused_too(boundary):
    """The boundary is the process group's, not the outer shell's."""
    target = boundary.outside / "nested.txt"
    result = boundary.run(f"sh -c 'echo nested > {_shell_quote(str(target))}'")
    assert result.exit_code not in (0, None)
    assert not target.exists()


@needs_seatbelt
def test_reading_outside_the_workspace_is_allowed(boundary):
    """read-only means "no writes", not "no filesystem" — the profile allows default."""
    result = boundary.run("cat /etc/hosts")
    assert result.exit_code == 0
    assert result.stderr == ""


@needs_seatbelt
def test_read_only_refuses_a_write_inside_the_workspace(boundary):
    target = boundary.workspace / "forbidden.txt"
    result = boundary.run(f"echo nope > {target}", mode="read-only")
    assert result.exit_code not in (0, None)
    assert not target.exists()
    assert result.sandbox == {"mode": "read-only", "denied": True, "enforcement": "full"}


@needs_seatbelt
def test_the_command_reaches_the_shell_as_one_element(boundary):
    """No second parse and no re-quoting: the source survives verbatim.

    Every character here defeats at least one re-quoting scheme: a space inside
    quotes, an embedded double quote, an escaped space, a dollar that must expand
    as the shell would, a semicolon that must not split the command, and a quoted
    heredoc.
    """
    source = "printf '%s|' \"a b\" 'c\"d' e\\ f $HOME ';' \"$(echo no)\" && cat <<'EOF_Q'\nheredoc\nEOF_Q"
    result = boundary.run(source)
    assert result.stderr == ""
    assert result.exit_code == 0
    assert result.stdout == 'a b|c"d|e f|' + os.path.expanduser("~") + "|;|no|" + "heredoc"


@needs_seatbelt
def test_a_timeout_still_reports_the_output_the_command_managed_to_write(boundary):
    """A cancelled read loses buffered bytes; this shape drains after the kill."""
    result = boundary.run("echo before-timeout; sleep 30", timeout=1.0)
    assert result.timed_out is True
    assert "before-timeout" in result.stdout


@needs_seatbelt
def test_the_tool_returns_the_rendered_result_for_a_normal_run(boundary):
    result = boundary.execute("echo done")
    assert result.error is False
    assert result.content == "done"


@needs_seatbelt
def test_the_tool_returns_a_nonzero_exit_as_output_not_as_an_error(boundary):
    """The model decides how to react to a failure — the run itself succeeded."""
    result = boundary.execute("echo oops >&2; exit 3")
    assert result.error is False
    assert "[stderr]\noops" in result.content
    assert result.content.rstrip().endswith("[exit code: 3]")


@needs_seatbelt
def test_the_tool_refuses_a_denied_write_with_the_marker_and_the_exit_code(boundary):
    result = boundary.execute(f"echo nope > {boundary.outside / 'tool-escape.txt'}")
    assert not (boundary.outside / "tool-escape.txt").exists()
    assert result.error is False, "a policy denial is the model's to handle, not a tool error"
    assert sandbox_denial_marker("workspace-write") in result.content


@needs_a_shell
def test_a_bare_command_with_no_tier_is_not_confined():
    """A call carrying no tier is a host session — the mode it already had."""
    result = asyncio.run(
        run_command(
            "echo unconfined",
            policy=SandboxPolicy(mode="danger-full-access", workspace_root=os.getcwd()),
            workdir=os.getcwd(),
            timeout=30.0,
            platform_name="darwin",
        )
    )
    assert result.exit_code == 0
    assert result.stdout == "unconfined"
    assert result.sandbox == {"mode": "danger-full-access", "denied": False}


@needs_a_shell
def test_linux_reports_an_unconfined_run_rather_than_a_boundary_it_lacks():
    """Host-authorised deviation D4: the Linux chain has no artifact yet."""
    result = asyncio.run(
        run_command(
            "echo linux",
            policy=SandboxPolicy(mode="workspace-write", workspace_root=os.getcwd()),
            workdir=os.getcwd(),
            timeout=30.0,
            platform_name="linux",
        )
    )
    assert result.exit_code == 0
    assert "enforcement" not in result.sandbox


def test_the_decoder_never_raises_on_bytes_it_cannot_decode():
    """POSIX child output is UTF-8; undecodable bytes are replaced, never fatal."""
    assert _decode_output("café".encode(), os_name="posix") == "café"
    assert _decode_output(b"\xff\xfe", os_name="posix") == "\ufffd\ufffd"
    assert _decode_output(b"", os_name="posix") == ""


def test_the_decoder_prefers_the_locale_codec_strictly_on_windows(monkeypatch):
    """The measured case behind the console-codec exemption (rant 2026-08-08T09:35:30).

    ``café`` in cp1252 is ``b'caf\\xe9'``, which is *not* valid UTF-8 — so this
    fails if the locale codec is skipped, and it names which one won.
    """
    import emrg.tools.bash_tool_v2 as v2

    monkeypatch.setattr(v2.locale, "getpreferredencoding", lambda *_: "cp1252")
    assert v2._decode_output("café".encode("cp1252"), os_name="nt") == "café"


def test_the_decoder_falls_back_to_utf8_when_the_locale_codec_declines(monkeypatch):
    """A UTF-8-emitting child on a non-UTF-8 console host must not lose its text."""
    import emrg.tools.bash_tool_v2 as v2

    monkeypatch.setattr(v2.locale, "getpreferredencoding", lambda *_: "ascii")
    assert v2._decode_output("café".encode(), os_name="nt") == "café"


def test_the_decoder_survives_a_host_that_reports_no_usable_codec(monkeypatch):
    """An unknown codec name is a ``LookupError``, not a crash."""
    import emrg.tools.bash_tool_v2 as v2

    monkeypatch.setattr(v2.locale, "getpreferredencoding", lambda *_: "definitely-not-a-codec")
    assert v2._decode_output(b"hello", os_name="nt") == "hello"


def _shell_quote(text: str) -> str:
    """Single-quote a string for ``bash`` (the inner shell)."""
    return "'" + text.replace("'", "'\\''") + "'"
