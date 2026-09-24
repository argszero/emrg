"""The upgrade session's tier is a deployment decision, not a Python literal.

Rant ``2026-09-23T21:46:12`` (auto-upgrade deadlock), whose own text is the
reason this file exists. The measured incident: the upgrade session is the one
consumer in the product that must write **outside its own cwd** — it writes the
install tree, the upgrade backup and a copy under ``~/Applications`` — and it
declared ``sandbox="workspace-write"`` as a hard-coded literal. Those three
trees are *siblings* of the session's work dir, not children of it, so every
granted root excluded all three and every attempt failed identically: 23
attempts over 132 minutes, one full LLM session each, zero bytes written, and
nothing visible host-side except a version number that never moved.

Two things are pinned here, and they are separate claims:

1. **The literal cannot come back** — a tier may only arrive from configuration
   (``tasks.yml`` → ``scheduler._resolve_sandbox``) or from the protocol field.
   A mode name written into server code is a deployment decision impersonating a
   constant, and the next consumer that copies it inherits the same silent
   failure. The scan is AST-based rather than a grep for ``sandbox="`` because a
   grep cannot tell a keyword argument from a comment, a string in a log line,
   or a different call's argument.

2. **"No declaration" keeps its meaning** — ``resolve_policy(mode=None, ...)``
   resolves to :data:`DEFAULT_MODE` (``danger-full-access``), and the in-process
   write/edit tools fence only on a *declared* tier. Fixing (1) by teaching the
   upgrade session to declare a *different* literal would break this, so the
   silence is pinned instead of the specific deletion: it is the state the fix
   relies on, and reversing it would re-break the upgrade silently.

The safety rule this file obeys (host 2026-09-17T11:38:16): a real write is only
ever attempted under a directory the test itself creates. The upgrade's actual
targets live under ``$HOME``, so they are passed **only** to pure predicates
(``check_workspace_write``, ``writable_roots``), which resolve paths and open
nothing — a test whose safety depends on the fence working would become a real
``$HOME`` write the moment that fence were the thing under test.
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path

from emrg.sandbox.policy import (
    DANGER_FULL_ACCESS,
    DEFAULT_MODE,
    SANDBOX_MODES,
    resolve_policy,
)
from emrg.sandbox.roots import writable_roots
from emrg.tools.bash_tool import check_workspace_write
from emrg.tools.edit_tool import EditTool
from emrg.tools.write_tool import WriteTool

SERVER_DIR = Path(__file__).resolve().parents[1] / "emrg" / "server"


def _run(coro):
    return asyncio.run(coro)


def declared_tiers(source: str, *, filename: str = "<source>") -> list[tuple[int, str]]:
    """Every ``sandbox=<mode literal>`` in *source*, as ``(lineno, mode)``.

    Only a **string literal** whose value is a known mode counts: ``_resolve_sandbox``
    legitimately returns bare mode strings, and the daemon legitimately forwards
    ``sandbox=data.get("sandbox")`` from the protocol. Neither is a declaration
    written into code, which is the defect this looks for.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "sandbox":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value in SANDBOX_MODES:
                found.append((value.lineno, value.value))
    return found


def _upgrade_task_request_keywords() -> set[str]:
    """The keyword names of the ``TaskRequest(...)`` built by ``_run_upgrade_session``.

    Read off the AST rather than by calling the method: the method creates a
    session and runs a tool loop, and the auto-upgrade chain is a permanent red
    line (MANIFESTO 第四条附则三). Nothing is executed here, so there is nothing to
    isolate.
    """
    tree = ast.parse((SERVER_DIR / "daemon.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # `AsyncFunctionDef` does not subclass `FunctionDef`, and the method is
        # `async def` — matching only the sync class finds nothing and would raise
        # the "not found" error below on a file that is perfectly fine.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_run_upgrade_session":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "TaskRequest":
                    return {kw.arg for kw in inner.keywords}
            raise AssertionError(
                "_run_upgrade_session no longer builds a TaskRequest — this guard is stale"
            )
    raise AssertionError("_run_upgrade_session not found in emrg/server/daemon.py")


# --------------------------------------------------------------------------
# Guard 1 — no server module declares a tier as a literal
# --------------------------------------------------------------------------


def test_the_literal_scanner_detects_a_literal() -> None:
    """The scan below proves nothing unless it can fail.

    A positive control on synthetic source: the same shape the old code had,
    including a comment mentioning a mode name that must NOT be counted.
    """
    source = (
        "req = TaskRequest(\n"
        "    session_id=s,\n"
        '    # tier is "workspace-write" per the old comment — not a declaration\n'
        '    sandbox="workspace-write",\n'
        ")\n"
    )
    assert declared_tiers(source) == [(4, "workspace-write")]


def test_the_literal_scanner_ignores_a_forwarded_value() -> None:
    """The negative control: forwarding a field is not declaring a tier."""
    source = 'req = TaskRequest(session_id=s, sandbox=data.get("sandbox"))\n'
    assert declared_tiers(source) == []


def test_no_server_module_declares_a_sandbox_tier_literal() -> None:
    """A tier comes from config or the protocol field — never from a literal.

    This is the regression guard for the deadlock: with the literal restored,
    the upgrade session is confined to roots that exclude everything it exists
    to write.
    """
    offenders: list[str] = []
    files = sorted(SERVER_DIR.glob("*.py"))
    assert files, f"no server modules found under {SERVER_DIR} — this guard would pass vacuously"
    for path in files:
        for lineno, mode in declared_tiers(
            path.read_text(encoding="utf-8"), filename=str(path)
        ):
            offenders.append(f"{path.name}:{lineno} declares sandbox={mode!r}")
    assert offenders == [], (
        "the sandbox tier must be resolved from configuration (tasks.yml → "
        "scheduler._resolve_sandbox) or taken from the protocol field, not "
        "written into server code:\n  " + "\n  ".join(offenders)
    )


def test_the_upgrade_session_declares_no_tier() -> None:
    """The call site that failed: its request carries no ``sandbox`` argument."""
    assert "sandbox" not in _upgrade_task_request_keywords(), (
        "the upgrade session must declare no tier — it is the one consumer that "
        "writes outside its own cwd (rant 2026-09-23T21:46:12)"
    )


# --------------------------------------------------------------------------
# Guard 2 — the three real targets are outside every granted root
# --------------------------------------------------------------------------


def _upgrade_targets() -> dict[str, str]:
    """The three trees the upgrade session exists to write.

    Spelled from ``$HOME`` because that is where they really are. They are used
    as **inputs to pure predicates only** — see the module docstring.
    """
    home = os.path.expanduser("~")
    return {
        "install tree": os.path.join(home, ".emrg", "install", "version.txt"),
        "upgrade backup": os.path.join(home, ".emrg", "upgrade-backup", "0.0.0", "f"),
        "app copy": os.path.join(home, "Applications", "EMRG.app", "Contents", "f"),
    }


def test_every_upgrade_target_is_outside_the_workspace_write_roots() -> None:
    """The measurement that explains the deadlock, as an assertion.

    ``workspace-write`` grants the policy's workspace root plus the platform
    temp areas (``emrg/sandbox/roots.py``). The upgrade's three targets are in
    none of them, so a ``workspace-write`` upgrade session could not write a
    single byte of its own job — which is what the 23 silent attempts were.
    """
    workspace = os.path.join(os.path.expanduser("~"), ".emrg", "upgrade-work", "emrg")
    granted = [Path(p) for p in writable_roots(
        resolve_policy(mode="workspace-write", workspace_root=workspace)
    )]
    for name, target in _upgrade_targets().items():
        resolved = Path(target).resolve()
        assert not any(resolved.is_relative_to(root) for root in granted), (
            f"{name} ({target}) is inside a granted root — this guard's premise changed"
        )


def test_the_workspace_write_fence_refuses_every_upgrade_target() -> None:
    """The predicate that actually refused them, called the way the tool calls it.

    Pure: a path in, a reason-or-None out. No write is attempted, so a broken
    fence cannot turn this test into a real ``$HOME`` write.
    """
    for name, target in _upgrade_targets().items():
        assert check_workspace_write(target, None), (
            f"{name} ({target}) is no longer refused under workspace-write — "
            "either the tier's reach grew or the target moved"
        )


# --------------------------------------------------------------------------
# Guard 3 — silence keeps meaning "unconfined"
# --------------------------------------------------------------------------


def _in_workspace_write(tmp_path: Path) -> Path:
    """A workspace under the directory pytest owns for this test.

    A dedicated subdirectory rather than ``tmp_path`` itself, so "inside the
    workspace" and "outside it" are two different places.
    """
    workspace = tmp_path / "work"
    workspace.mkdir()
    return workspace


def test_resolve_policy_without_a_mode_is_the_documented_default() -> None:
    """``mode=None`` is not an error and not fail-closed — it is the default tier.

    The upgrade session now relies on exactly this. Reversing it (treating
    silence as ``read-only``) would re-break the upgrade while leaving guard 1
    green, which is why the semantics are pinned separately.
    """
    policy = resolve_policy(mode=None, workspace_root=str(Path.cwd()))
    assert policy.mode == DEFAULT_MODE
    assert DEFAULT_MODE == DANGER_FULL_ACCESS


def test_no_declaration_means_the_write_tool_does_not_fence(tmp_path: Path) -> None:
    """Files under a directory this test created; nothing outside temp is touched.

    Both cases carry a ``workspace``, because the daemon injects one
    unconditionally (``_inject_tool_arguments``) — so the *only* variable is the
    tier. Without that the second case would be fail-open for two reasons at
    once and the pair would prove nothing: measured 2026-09-24, a version of
    this test that omitted ``workspace`` from the un-declared case still passed
    when both tools were mutated to fence silence.
    """
    workspace = _in_workspace_write(tmp_path)

    # Declared tier: the read-only fence guards the session's own workspace.
    refused = _run(WriteTool().execute({
        "file_path": str(workspace / "guarded.txt"),
        "content": "x\n",
        "sandbox": "read-only",
        "workspace": str(workspace),
    }))
    assert refused.error, "the read-only tier stopped fencing its workspace"
    assert not (workspace / "guarded.txt").exists()

    # No tier declared, workspace present: nothing is fenced.
    allowed = _run(WriteTool().execute({
        "file_path": str(workspace / "free.txt"),
        "content": "x\n",
        "workspace": str(workspace),
    }))
    assert not allowed.error, allowed.content
    assert (workspace / "free.txt").read_text() == "x\n"


def test_no_declaration_means_the_edit_tool_does_not_fence(tmp_path: Path) -> None:
    """Same pair, edit side — the two tools must not drift apart."""
    workspace = _in_workspace_write(tmp_path)
    guarded = workspace / "guarded.txt"
    guarded.write_text("old\n")

    refused = _run(EditTool().execute({
        "file_path": str(guarded),
        "old_string": "old",
        "new_string": "new",
        "sandbox": "read-only",
        "workspace": str(workspace),
    }))
    assert refused.error, "the read-only tier stopped fencing its workspace"
    assert guarded.read_text() == "old\n"

    free = workspace / "free.txt"
    free.write_text("old\n")
    allowed = _run(EditTool().execute({
        "file_path": str(free),
        "old_string": "old",
        "new_string": "new",
        "workspace": str(workspace),
    }))
    assert not allowed.error, allowed.content
    assert free.read_text() == "new\n"
