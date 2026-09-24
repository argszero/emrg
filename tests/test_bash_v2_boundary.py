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

The boundary half is per-backend and skips where its backend is absent: the
darwin tests need ``sandbox-exec``, and the linux tests (P3) need a ``bwrap``
that can really create a namespace — the container recipe beside them is the
environment this repository measured them in.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import emrg.sandbox.providers.linux as linux_provider

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
    """The whole point of the contract: no silent fallback to a bare run.

    The platform is a synthetic one with no rung: this test is about the seam's
    refusal, and it must keep asking that question as the chain table fills up
    (``win32`` was the example until the Windows rung landed in P4).
    """
    sentinel = tmp_path / "must-not-exist"
    with pytest.raises(SandboxUnavailableError) as excinfo:
        asyncio.run(
            run_command(
                f"touch {sentinel}",
                policy=SandboxPolicy(mode="workspace-write", workspace_root=str(tmp_path)),
                workdir=str(tmp_path),
                timeout=30.0,
                platform_name="freebsd",
            )
        )
    assert excinfo.value.code == "SANDBOX_UNAVAILABLE"
    assert not sentinel.exists(), "the command ran despite the refusal"


def test_the_tool_reports_the_refusal_instead_of_running_bare(tmp_path, monkeypatch):
    """Through the tool, the model sees a refusal rather than an unconfined result.

    Two patches, and the second is not decoration: emptying the chain is not
    enough on Linux, which short-circuits to an unconfined run **before** any
    provider is consulted (deviation D4).  This test is about the tool's handling
    of the seam's refusal, so it isolates exactly that — a chain-less platform
    with no deviation — instead of quietly exercising D4 on one runner and the
    refusal on another (the first CI run did the former, and failed).
    """
    import emrg.sandbox.providers as providers
    import emrg.tools.bash_tool_v2 as v2

    monkeypatch.setattr(providers, "PLATFORM_CHAINS", {})
    monkeypatch.setattr(v2, "unconfined_mode", lambda mode, platform_name=None: None)
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

    fatal = classify_runner_failure(
        65, "sandbox-exec: syntax error: expecting ')'\n", RUNNER_FAILURE_RULES
    )
    assert fatal == "sandbox-exec: syntax error: expecting ')'"
    assert classify_runner_failure(65, "Operation not permitted\n", RUNNER_FAILURE_RULES) is None
    assert classify_runner_failure(0, "sandbox-exec: nope\n", RUNNER_FAILURE_RULES) is None
    assert classify_runner_failure(None, "sandbox-exec: nope\n", RUNNER_FAILURE_RULES) is None


def test_the_darwin_rule_classifies_the_lines_sandbox_exec_really_prints():
    """The darwin rung's rule, pinned by recorded output rather than a plausible one.

    The sibling ``linux`` rung received this treatment in #1587, for issue #1543's
    reason: a rule that has only ever been fed a line it will obviously match is not
    measured. Here the line was ``"sandbox-exec: syntax error"`` — no line
    ``sandbox-exec`` prints (the real one continues ``: expecting ')'``), so the pin
    could pass while the rule stopped matching the runner's own output, which is the
    whole of what the rule is for.

    Recorded, not invented: ``/usr/bin/sandbox-exec`` on macOS 26.6.2 (build 25G83),
    ``-p <profile> /bin/echo hi``, each of the five profiles in the rule's own
    comment exiting 65 with the prefix on stderr. Pure predicates over strings, so
    this runs on every leg — nothing below needs macOS.
    """
    from emrg.tools.bash_tool_v2 import classify_runner_failure
    from emrg.sandbox.providers.darwin import RUNNER_FAILURE_RULES

    for line in (
        "sandbox-exec: unbound variable: bogus-op at <input string>, line 1, column 28",
        "sandbox-exec: Error reading string",
        "sandbox-exec: no version specified",
    ):
        assert classify_runner_failure(65, line + "\n", RUNNER_FAILURE_RULES) == line, line

    # The detail without the prefix is **not** a line this runner prints (all five
    # measured fatals carry it), and it is not classified — this is what makes the
    # prefix the load-bearing half of the rule rather than decoration.
    assert (
        classify_runner_failure(65, "syntax error: expecting ')'\n", RUNNER_FAILURE_RULES) is None
    )

    # And the row stays signature-only, exactly as the blueprint has it. The tempting
    # "tightening" is to gate on 65, because all five measured cases exit with it —
    # but that is one macOS version's behaviour, while the prefix is the launcher's own
    # contract; a gate would silently stop classifying a runner failure on a version
    # that exits differently, reporting it as the denial this rule exists to outrank.
    assert len(RUNNER_FAILURE_RULES) == 1
    rule = RUNNER_FAILURE_RULES[0]
    assert rule.fatal_signatures == ("sandbox-exec: ",)
    assert rule.allowed_exit_codes is None, "an exit status is not evidence here (see darwin.py)"


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


# ── what the confined child's environment gets ────────────────────────────
#
# The policies below take their workspace root from ``tmp_path`` rather than a
# literal ``/tmp``: ``SandboxPolicy`` asserts the root is absolute, and ``/tmp``
# is not an absolute path on Windows — the first version of these tests was green
# on the dev host and red on the Windows leg for exactly that reason.  A
# workspace root is a directory that exists, so the test supplies one.
#
# The mode's other half: granting a boundary that a package manager cannot work
# inside is not usable, and the fix must not be a wider boundary.  These tests
# pin the two properties that keep it honest — the relocated directory lies
# inside what the policy already grants, and a deployer's own declaration wins.


def test_the_caches_of_a_confined_run_are_relocated_into_a_granted_root(tmp_path, monkeypatch):
    """The relocation widens nothing: it points caches at a root the policy grants.

    Both the measured breaks (``uv`` fails, ``npm``'s default is unwritable) and
    the quiet one (``pip`` disables its cache) come from the tool's default cache
    living under ``$HOME``, which a confined run cannot write.  The answer is to
    move the cache, never the boundary — so this asserts containment against
    ``writable_roots``, the same derivation the Seatbelt profile is built from.

    The variables are cleared first, because the subject is the relocation and not
    the ambient environment: ``confined_env`` deliberately leaves alone a variable
    the deployer already set, and CI's own environment sets ``UV_CACHE_DIR``.  The
    equality below is with the module's list *as relocated*, never with the list as
    an environment-independent expectation — asserted the other way, this test
    measured GitHub's environment and failed on both CI legs.
    """
    from emrg.sandbox.roots import writable_roots
    from emrg.tools.bash_tool_v2 import _CACHE_ENV, confined_env

    for name in _CACHE_ENV:
        monkeypatch.delenv(name, raising=False)

    policy = SandboxPolicy(mode="workspace-write", workspace_root=str(tmp_path))
    granted = writable_roots(policy)
    env = confined_env(policy)

    assert set(env) == set(_CACHE_ENV), "every cache variable the module names, and only those"
    for name, value in env.items():
        assert any(Path(value).is_relative_to(root) for root in granted), (
            f"{name}={value} is outside every root the policy grants ({granted})"
        )


def test_a_read_only_run_relocates_nothing(tmp_path):
    """No writable root, no cache to point anywhere: the mode is the whole answer."""
    from emrg.tools.bash_tool_v2 import confined_env

    assert confined_env(SandboxPolicy(mode="read-only", workspace_root=str(tmp_path))) == {}


def test_the_deployer_declared_cache_wins(tmp_path, monkeypatch):
    """A warm cache the deployer put somewhere stays reachable.

    The variable is set in the child's environment only when the environment does
    not already name one, so this is "the deployer's declaration wins", not "the
    sandbox knows better".  Both variables are cleared first, so the one it sets is
    the only declaration this measures.
    """
    from emrg.tools.bash_tool_v2 import _CACHE_ENV, confined_env

    for name in _CACHE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UV_CACHE_DIR", "/the/deployers/cache")
    policy = SandboxPolicy(mode="workspace-write", workspace_root=str(tmp_path))
    env = confined_env(policy)
    assert "UV_CACHE_DIR" not in env
    assert "PIP_CACHE_DIR" in env, "the others are still relocated"


def test_the_unconfined_path_relocates_nothing(tmp_path, monkeypatch):
    """``danger-full-access`` runs bare, so its caches belong where they always were."""
    import emrg.tools.bash_tool_v2 as v2

    seen: dict = {}

    async def fake_spawn(*argv, **kwargs):
        seen.update(kwargs)
        raise AssertionError("stop here: the environment is the subject")

    monkeypatch.setattr(v2.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    policy = SandboxPolicy(mode="danger-full-access", workspace_root=str(tmp_path))
    try:
        asyncio.run(v2.run_command("echo hi", policy=policy, workdir=str(tmp_path), timeout=5.0))
    except AssertionError:
        pass
    assert "UV_CACHE_DIR" not in seen["env"], "nothing was confined, so nothing was relocated"


@needs_seatbelt
def test_a_confined_command_really_sees_the_relocated_cache(boundary, monkeypatch):
    """The end-to-end half: the variable is in the child's environment, not just planned.

    Without it the measured ``uv`` failure stands — ``uv run`` exits with
    "Failed to initialize cache" — so this is the difference between a boundary
    that is correct and one that is usable.

    The deployer's own ``UV_CACHE_DIR`` is cleared first, because a declared cache
    is deliberately left alone and would be the variable this test then reads: with
    one exported from the shell it asserted about a path outside every grant and
    failed for a reason that is not a defect.  The relocation is the subject, so the
    environment must not be able to decide it.
    """
    from emrg.sandbox.roots import writable_roots

    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    result = boundary.run('echo "$UV_CACHE_DIR" && mkdir -p "$UV_CACHE_DIR" && echo cache-writable')
    assert result.exit_code == 0, result.stderr
    relocated = result.stdout.splitlines()[0]
    assert any(Path(relocated).is_relative_to(root) for root in writable_roots(boundary.policy("workspace-write")))
    assert "cache-writable" in result.stdout


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
def test_linux_refuses_instead_of_running_bare_when_its_runner_is_missing(tmp_path, monkeypatch):
    """P3's end-to-end half, and the assertion that D4 is really gone.

    Deviation D4 used to answer this platform *before* any provider was
    consulted: ``unconfined_mode("workspace-write", platform_name="linux")``
    returned ``danger-full-access`` and the command ran bare.  Now the platform
    has a rung, so the seam is consulted, the spawn fails on a runner that is
    not there, and the run is refused — the same refusal darwin gives.

    It runs on every platform (that is the point): the runner is removed from
    ``PATH`` by patching the program name, so nothing here needs Linux or
    bubblewrap to be installed.
    """
    import emrg.sandbox.providers.linux as linux_provider

    monkeypatch.setattr(linux_provider, "BWRAP_BIN", "emrg-no-such-runner-anywhere")
    sentinel = tmp_path / "must-not-exist"
    with pytest.raises(SandboxUnavailableError) as excinfo:
        asyncio.run(
            run_command(
                f"touch {sentinel}",
                policy=SandboxPolicy(mode="workspace-write", workspace_root=str(tmp_path)),
                workdir=str(tmp_path),
                timeout=30.0,
                platform_name="linux",
            )
        )
    assert excinfo.value.code == "SANDBOX_UNAVAILABLE"
    assert not sentinel.exists(), "a linux host with no runner must fail closed, never run bare"


# ── the boundary itself (linux only) ──────────────────────────────────────
#
# Measured in this repository's own container recipe (2026-09-22), which is the
# only environment available here that can create a user namespace:
#
#   docker run --rm -v "$PWD:/src:ro" \
#     --security-opt seccomp=unconfined --security-opt apparmor=unconfined --cap-add SYS_ADMIN \
#     debian:bookworm-slim bash -c 'apt-get update -qq && apt-get install -y -qq python3 python3-pip git bubblewrap
#       cp -a /src /work && cd /work && pip install --break-system-packages -e . pytest
#       python3 -m pytest tests/test_bash_v2_boundary.py -q'
#
# All three security options are needed: with only ``seccomp=unconfined`` the
# AppArmor profile refuses ``mount --make-rslave /`` and ``bwrap`` dies with
# ``Failed to make / slave: Permission denied`` **before** it reaches the profile
# — a red suite that says nothing about this code (measured).


def _bwrap_problem() -> str | None:
    """Why this host cannot run the linux boundary tests, or ``None`` if it can.

    Presence is not usability: ``bwrap`` that exists but cannot create a
    namespace (a locked-down container) would make these tests fail for a reason
    that is not a defect, so usability is *measured* with the same profile shape
    the provider builds.
    """
    program = shutil.which(linux_provider.BWRAP_BIN)
    if program is None:
        return "bubblewrap (bwrap) is not installed"
    try:
        probe = subprocess.run(
            [program, "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "--", "true"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"bwrap could not be probed: {exc}"
    if probe.returncode != 0:
        detail = probe.stderr.decode("utf-8", "replace").strip().splitlines()
        return f"bwrap cannot create a namespace here: {detail[0] if detail else 'no stderr'}"
    return None


_BWRAP_PROBLEM = _bwrap_problem()

needs_bwrap = pytest.mark.skipif(
    _BWRAP_PROBLEM is not None,
    reason=(
        f"the real linux boundary needs a usable bwrap: {_BWRAP_PROBLEM} "
        "(the container recipe in this section's comment is the measured environment)"
    ),
)


@needs_bwrap
def test_bwrap_lets_a_command_write_inside_the_workspace(boundary):
    """The ``--bind`` mount, proven where the temp grant cannot reach."""
    result = boundary.run("echo hi > inside.txt && cat inside.txt")
    assert (result.exit_code, result.stderr) == (0, "")
    assert result.stdout == "hi"
    assert (boundary.workspace / "inside.txt").read_text().strip() == "hi"
    assert result.sandbox == {"mode": "workspace-write", "denied": False, "enforcement": "full"}


@needs_bwrap
def test_bwrap_refuses_an_interpreter_write_outside_the_workspace(boundary):
    """The same interpreter hole the old scan could not close, refused by the kernel."""
    target = boundary.outside / "escaped.txt"
    script = f"open({str(target)!r}, 'w').write('escaped')"
    result = boundary.run(f"{sys.executable} -c {_shell_quote(script)}")
    assert result.exit_code not in (0, None), "an escaping write must not report success"
    assert not target.exists(), "the sandbox did not hold"
    assert result.sandbox["denied"] is True
    assert "read-only file system" in result.stderr.lower()
    assert sandbox_denial_marker("workspace-write") in render_result(result)


@needs_bwrap
def test_bwrap_reads_outside_the_workspace_and_refuses_a_write_inside_it_read_only(boundary):
    """``--ro-bind / /`` is the whole read-only tier: readable everywhere, writable nowhere."""
    read = boundary.run("cat /etc/hosts")
    assert (read.exit_code, read.stderr) == (0, "")

    target = boundary.workspace / "forbidden.txt"
    refused = boundary.run(f"echo nope > {target}", mode="read-only")
    assert refused.exit_code not in (0, None)
    assert not target.exists()
    assert refused.sandbox == {"mode": "read-only", "denied": True, "enforcement": "full"}


@needs_bwrap
def test_bwrap_gives_the_sandbox_a_private_tmp(boundary):
    """The linux profile's difference from Seatbelt, measured: ``/tmp`` is a fresh tmpfs.

    A profile can only allow or deny, so the darwin backend grants the host's
    ``/tmp``; a mount namespace can do better, and the blueprint's bwrap profile
    does.  What makes this assertion worth having is the *outside* half: the file
    the confined command writes to ``/tmp`` must not exist on the host
    afterwards — a grant would leave it there.
    """
    probe = Path(tempfile.gettempdir()) / f"emrg-v2-bwrap-tmp-{os.getpid()}"
    assert not probe.exists()
    result = boundary.run(f"echo private > {probe} && cat {probe}")
    assert (result.exit_code, result.stderr) == (0, "")
    assert result.stdout == "private"
    assert not probe.exists(), "the sandbox's /tmp leaked into the host's"


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


@needs_seatbelt
def test_a_missing_workdir_is_not_reported_as_a_sandbox_problem(tmp_path):
    """A bad cwd is the caller's error, and the classification must not launder it.

    The runner is present and the profile is fine; what cannot be entered is the
    cwd the caller asked for, and POSIX reports the failure with the *cwd* as its
    filename (measured: ``FileNotFoundError: ... '/…/gone'``).  Reporting this as
    "no sandbox backend is usable" would send the operator to the sandbox for a
    typing error, so the original error must survive — which is what
    ``is_runner_spawn_failure``'s independently-usable-workdir clause buys.
    """
    missing = tmp_path / "no-such-dir"
    with pytest.raises(OSError) as excinfo:
        asyncio.run(
            run_command(
                "echo hi",
                policy=SandboxPolicy(mode="workspace-write", workspace_root=str(tmp_path)),
                workdir=str(missing),
                timeout=30.0,
            )
        )
    assert not isinstance(excinfo.value, SandboxUnavailableError)
    assert "sandbox mode" not in str(excinfo.value)


def _shell_quote(text: str) -> str:
    """Single-quote a string for ``bash`` (the inner shell)."""
    return "'" + text.replace("'", "'\\''") + "'"
