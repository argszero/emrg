"""``python -m emrg.sandbox.win32.runner`` — the Windows confinement runner.

Ported from the blueprint's ``runner.ts`` (``sandbox-windows-acl``, dsh
0.1.6-alpha.2).  This is the argv-prefix wrapper the sandbox seam spawns *in
place of* the caller's command: it creates the ``WRITE_RESTRICTED`` token with
the workspace write-SID allowlist, spawns the wrapped argv under it with the
caller's stdio inherited (bytes flow straight through), mirrors the child's
exit code, and revokes its temp grant on exit (workspace ACEs stay standing as
the reuse cache).

Stable argv contract (the seam builds it; a native-exe replacement would keep
the same contract)::

    python -m emrg.sandbox.win32.runner --workspace <dir> --temp <dir>
        --mode <read-only|workspace-write>
        [--write-sid <S-1-4-…> --temp-write-sid <S-1-4-…>] -- <argv...>

``--write-sid`` + ``--temp-write-sid`` are the seam's grant contract: the
**caller** has already materialized distinct workspace and private-temp ACEs and
owns their revocation, so the runner neither grants nor revokes them
(``manage_dacls: False``).  Both values are checked against their owning paths.
Without the pair (standalone use), ``workspace-write`` treats ``--temp`` as a
**root**, creates a random private child directory, derives its own temp SID,
and removes that directory after the child exits.  In both flows the runner
rewrites ``TMP``/``TEMP`` in its **own** environment before spawning, so the
child inherits the private directory; ``read-only`` leaves the ambient temp
entries untouched (writes there are denied anyway).  It also announces the
granted workspace as git's ``safe.directory`` — the token's restriction changes
the child's *identity*, which is what makes git refuse a repository the
deployer's own elevated process created (``git_safety_env``).

Failure contract: every runner-side failure (bad args, missing directories,
token/grant/spawn errors) prints ``windows-acl-run: <detail>`` to stderr and
exits 127 — the signature the seam's runner-failure rule matches.  The child is
**never** spawned unrestricted.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass

from emrg.sandbox.win32.ffi import Win32Bindings, set_environment_variable, win32
from emrg.sandbox.win32.sandbox import AclSandbox
from emrg.sandbox.win32.sid import assert_temp_root_outside_workspace, temp_write_sid, workspace_write_sid
from emrg.sandbox.win32.spawn import exit_status_for_mirroring

#: The signature every runner-side failure prints, and the seam matches.
RUNNER_SIGNATURE = "windows-acl-run"

#: The runner's documented failure exit, distinct from Landlock's 125.
RUNNER_FAILURE_EXIT = 127

#: How ``workspace-write`` names the temp directories it creates for itself.
OWNED_TEMP_PREFIX = "emrg-"


class RunnerFailure(Exception):
    """A runner-side failure: reported with the signature, exits 127."""


def fail(detail: str) -> None:
    """Print the runner-failure signature line and unwind.

    :param detail: what went wrong, for the seam's ``Runner failure:`` line.
    :raises RunnerFailure: always.
    """
    sys.stderr.write(f"{RUNNER_SIGNATURE}: {detail}\n")
    sys.stderr.flush()
    raise RunnerFailure(detail)


@dataclass(frozen=True)
class ParsedArgs:
    """The runner's argv, parsed."""

    workspace: str
    temp: str
    mode: str
    write_sid: str | None
    temp_write_sid: str | None
    command: str
    args: list[str]


def parse_args(raw: list[str]) -> ParsedArgs:
    """Parse the runner's own argv.

    :param raw: the arguments after the module name.
    :returns: the parsed invocation.
    :raises RunnerFailure: when an argument is missing, unknown or malformed.
    """
    workspace: str | None = None
    temp: str | None = None
    mode: str | None = None
    write_sid: str | None = None
    temp_write_sid_value: str | None = None
    index = 0
    while index < len(raw):
        token = raw[index]
        if token == "--":
            index += 1
            break
        index += 1
        if index >= len(raw):
            fail(f"missing value after {token}")
        value = raw[index]
        index += 1
        if token == "--workspace":
            workspace = value
        elif token == "--temp":
            temp = value
        elif token == "--mode":
            mode = value
        elif token == "--write-sid":
            write_sid = value
        elif token == "--temp-write-sid":
            temp_write_sid_value = value
        else:
            fail(f"unknown argument: {token}")
    if workspace is None:
        fail("missing --workspace")
    if temp is None:
        fail("missing --temp")
    if mode not in ("read-only", "workspace-write"):
        fail(f"unknown mode: {mode}")
    argv = raw[index:]
    if not argv:
        fail("missing command after --")
    return ParsedArgs(
        workspace=workspace,
        temp=temp,
        mode=mode,
        write_sid=write_sid,
        temp_write_sid=temp_write_sid_value,
        command=argv[0],
        args=list(argv[1:]),
    )


def require_directory(label: str, path: str) -> None:
    """Fail loudly when a directory argument does not name a directory.

    Both directories are validated in **both** modes: a provider bug that passes
    a bogus root must fail at the runner boundary, never mid-child.

    :param label: the argument's name, for the failure line.
    :param path: the path it named.
    :raises RunnerFailure: when the path is missing or not a directory.
    """
    if not os.path.isdir(path):
        fail(f"{label} is not an existing directory: {path}")


#: The variables git reads as extra global config entries (git ≥ 2.31).  They are
#: the only way to hand git a config *entry* without writing a config file, which
#: matters here: the private temp is the run's only writable directory and
#: ``read-only`` grants none at all.
GIT_CONFIG_COUNT = "GIT_CONFIG_COUNT"

#: The git key that names a directory git may work in however it is owned.
GIT_SAFE_DIRECTORY = "safe.directory"


def git_safety_env(workspace: str, environ: Mapping[str, str]) -> dict[str, str]:
    """The ``GIT_CONFIG_*`` entries that announce the granted workspace to git.

    Why confinement alone is not enough: the ``WRITE_RESTRICTED`` token is also a
    change of **identity**.  It carries the logon SID and Everyone, and the
    capability SIDs — but not the group membership that made objects created by
    this host's elevated daemon look like the caller's own, and the default owner
    of such an object is ``BUILTIN\\Administrators``.  git refuses to work in a
    repository whose owner is not the caller, and it refuses in a dialect no
    denial signature covers.  Measured on Windows Server 2022 at both confined
    tiers, with a repository at the granted workspace root:

        $ git status --porcelain
        fatal: detected dubious ownership in repository at '…'
        [exit 128]

    The same spawn outside the boundary exits 0, because an unrestricted
    Administrator's token *is* an Administrators member — which is exactly the
    trap: the failure appears only under the boundary, so a confined tier looks
    unusable and the operator's way out is ``danger-full-access``.

    The answer is to announce the root the policy granted, never to disable the
    check: this value is the same directory the workspace ACE already names, so
    git is told nothing the run was not already allowed to write.  Nested
    repositories keep git's refusal, because git matches this value against the
    repository path **exactly** (measured: a repository at ``<workspace>/sub/repo``
    is still refused when the entry names ``<workspace>``); widening it to ``*``
    would cover them, and would also cover every repository the child can reach
    outside the grant, which is not this run's to declare safe.

    An environment that already carries a count keeps it — the entry is appended
    at the first free index — and a count that is not an integer leaves the
    environment untouched rather than guessed at.

    :param workspace: the canonical workspace root this runner was granted.
    :param environ: the environment the entries are computed against.
    :returns: the variables to set, or ``{}`` when nothing can be added safely.
    """
    index = 0
    raw = environ.get(GIT_CONFIG_COUNT)
    if raw is not None:
        try:
            index = int(raw)
        except ValueError:
            return {}
        if index < 0:
            return {}
    while f"GIT_CONFIG_KEY_{index}" in environ or f"GIT_CONFIG_VALUE_{index}" in environ:
        index += 1
    return {
        GIT_CONFIG_COUNT: str(index + 1),
        f"GIT_CONFIG_KEY_{index}": GIT_SAFE_DIRECTORY,
        f"GIT_CONFIG_VALUE_{index}": workspace,
    }


def _build_sandbox(parsed: ParsedArgs) -> tuple[AclSandbox, str | None]:
    """Turn the parsed argv into a ready-to-init sandbox.

    :param parsed: the parsed invocation.
    :returns: the sandbox and the temp directory it owns (``None`` when the
        seam owns it, or when there is no temp grant).
    :raises RunnerFailure: when the arguments contradict each other.
    """
    seam_managed = parsed.write_sid is not None or parsed.temp_write_sid is not None
    if parsed.mode == "read-only" and seam_managed:
        fail("read-only does not accept --write-sid or --temp-write-sid")
    if parsed.mode == "workspace-write" and (parsed.write_sid is None) != (parsed.temp_write_sid is None):
        fail("workspace-write requires --write-sid and --temp-write-sid together")
    assert_temp_root_outside_workspace(parsed.workspace, parsed.temp)

    write_sid: str | None = None
    private_temp: str | None = None
    private_temp_sid: str | None = None
    owned_temp: str | None = None
    if parsed.mode == "workspace-write":
        write_sid = workspace_write_sid(parsed.workspace)
        if seam_managed:
            if parsed.write_sid != write_sid:
                fail("--write-sid does not match --workspace")
            private_temp = parsed.temp
            private_temp_sid = temp_write_sid(private_temp)
            if parsed.temp_write_sid != private_temp_sid:
                fail("--temp-write-sid does not match --temp")
        else:
            owned_temp = tempfile.mkdtemp(prefix=OWNED_TEMP_PREFIX, dir=parsed.temp)
            private_temp = owned_temp
            private_temp_sid = temp_write_sid(private_temp)
    sandbox = AclSandbox(
        writable_dirs=[parsed.workspace] if parsed.mode == "workspace-write" else [],
        temp_dir=private_temp,
        mode=parsed.mode,
        write_sid=write_sid,
        temp_write_sid=private_temp_sid,
        manage_dacls=not seam_managed,
    )
    return sandbox, owned_temp


def run(parsed: ParsedArgs, api: Win32Bindings) -> int:
    """Run the wrapped command under the sandbox and return its exit code.

    :param parsed: the parsed invocation.
    :param api: the binding table.
    :returns: the child's exit code.
    :raises RunnerFailure: when the args contradict each other.
    :raises Win32Error: when a Win32 call failed.
    """
    require_directory("--workspace", parsed.workspace)
    require_directory("--temp", parsed.temp)
    sandbox, owned_temp = _build_sandbox(parsed)
    # Ignore this process's own Ctrl+C: the confined child shares the console
    # and keeps handling its own, while the runner must survive to revoke its
    # grants and mirror the child's exit code.
    if int(api.kernel32.SetConsoleCtrlHandler(None, True)) == 0:
        fail(f"SetConsoleCtrlHandler failed (Win32 {api.get_last_error()})")
    initialized = False
    try:
        sandbox.init(api)
        initialized = True
        if sandbox.temp_dir is not None:
            set_environment_variable(api, "TMP", sandbox.temp_dir)
            set_environment_variable(api, "TEMP", sandbox.temp_dir)
        # The token's identity change travels with the child, and the granted
        # workspace is the one directory this run may declare safe (see
        # ``git_safety_env``): without it every git command in a workspace the
        # deployer's own elevated process created is refused.
        for name, value in git_safety_env(parsed.workspace, os.environ).items():
            set_environment_variable(api, name, value)
        child = sandbox.spawn([parsed.command, *parsed.args])
        try:
            return child.wait()
        finally:
            child.close()
    finally:
        # Cleanup failures must not mask the child's exit code: report and go on.
        if initialized:
            try:
                sandbox.dispose()
            except BaseException as exc:  # noqa: BLE001 - reported, exit code wins
                sys.stderr.write(f"{RUNNER_SIGNATURE}: cleanup: {exc}\n")
        if owned_temp is not None:
            shutil.rmtree(owned_temp, ignore_errors=True)


def main(raw: list[str] | None = None) -> int:
    """The runner entry point.

    :param raw: the arguments after the module name; defaults to ``sys.argv[1:]``.
    :returns: the process exit code.
    """
    arguments = sys.argv[1:] if raw is None else list(raw)
    try:
        parsed = parse_args(arguments)
        return run(parsed, win32())
    except RunnerFailure:
        return RUNNER_FAILURE_EXIT
    except BaseException as exc:  # noqa: BLE001 - every failure is the same contract
        sys.stderr.write(f"{RUNNER_SIGNATURE}: {exc}\n")
        return RUNNER_FAILURE_EXIT


if __name__ == "__main__":  # pragma: no cover - process entry
    sys.exit(exit_status_for_mirroring(main()))
