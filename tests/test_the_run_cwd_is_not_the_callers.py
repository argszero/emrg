"""The run's working directory belongs to the kernel — and every carrier says so.

Measured on the live daemon, 2026-10-03 (cycle ``cyc20261003-115810``):

* a shell call carrying ``workdir=<a git worktree>`` printed
  ``C:\\Users\\Administrator\\.emrg\\evolution\\emrg`` — the session's cwd, not the
  worktree it named;
* ``glob`` with ``workdir=<a directory holding one marker file>`` answered "No
  files matched pattern 'zz-wdprobe-marker.txt' in
  C:\\Users\\Administrator\\.emrg\\evolution\\emrg" — the session's cwd again.

The kernel is right about this, and ``tests/test_shell_tool_mount.py`` pins why it
must be: ``workdir`` is also the sandbox's *authorization root*, so a model that
could name it could name the root it was trusted in (D1 — ``workdir=/Users/<host>``
plus a write to ``.zshrc`` was allowed).  What was wrong was the other half.  The
carriers told the caller to choose it anyway: the system prompt read "Set
``timeout`` (default: 30s) and ``workdir`` to control execution" and "Use
``workdir`` to search in a specific directory", the ``pwsh`` description read "so
pass ``workdir`` instead of using ``cd``", and the three ``workdir`` parameter
descriptions said only that another value "does not widen" the boundary — as if
narrowing were still the caller's to do.  A caller that follows an instruction the
kernel discards cannot tell where its command ran without printing the directory,
and the two measurements above are exactly that.

So this file binds the halves together, in both directions: the kernel must still
decide the directory, and no carrier may offer the choice it would discard.
Change either half alone and one of these goes red.

Two limits, stated rather than left to the reader.  These are text readings: a
carrier that describes the choice *without naming* ``workdir`` ("run it wherever
the call asks") is outside what a mention-shaped check can see.  And the scan
covers the **model-facing** carriers — the tool surface, i.e. the registered
descriptions and the prompt that offers them — not ``DEVELOPMENT.md``, whose
``workdir`` sentence was corrected in the same change but is read by the host
rather than by the model.
"""

import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server.daemon import EmrgServer, _get_jinja_env
from emrg.tools.bash_tool_v2 import BashToolV2
from emrg.tools.glob_tool import GlobTool
from emrg.tools.pwsh_tool_v2 import PwshToolV2
from emrg.tools.shell_dialects import SHELL_TOOL_NAMES

#: The instruction as it stood before this change, verbatim.  Kept as the
#: measured form of the defect rather than as a paraphrase, so a revert of the
#: wording is red here even if a rewrite would have satisfied the check below.
RETIRED = (
    "and `workdir` to control execution",
    "pass `workdir` instead of using `cd`",
    "use `workdir` to search in a specific directory",
    "working directory for the pattern (default: project root)",
    "does not widen it",
)

#: What a sentence about the run's directory has to say: the daemon supplies that
#: value, and one supplied by the call is not the one used.
DECIDES = (
    "is ignored",
    "are ignored",
    "is discarded",
    "are discarded",
    "supplies",
)


def _sentences(text: str) -> list[str]:
    """Split a carrier into sentence-shaped pieces.

    Locality is the point: "``workdir`` is ignored" in one sentence must not
    excuse "pass ``workdir``" in the next, so the check reads pieces rather than
    whole descriptions — a long tool description is exactly where the two would
    otherwise sit side by side and pass.
    """
    return [piece for piece in re.split(r"(?<=\.)\s+|\n", text) if piece.strip()]


def _instantiate() -> EmrgServer:
    """Build a server the way the daemon does, then keep its logs out of the host."""
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def _tool_carriers() -> list[tuple[str, str]]:
    """Every description the model is offered for a tool that takes a ``workdir``."""
    carriers: list[tuple[str, str]] = []
    for tool in (BashToolV2(), PwshToolV2(), GlobTool()):
        definition = tool.definition()
        carriers.append((f"{definition.name} description", definition.description))
        for key, spec in definition.parameters.get("properties", {}).items():
            carriers.append((f"{definition.name}.{key}", spec.get("description") or ""))
    return carriers


def _template_carriers() -> list[tuple[str, str]]:
    """The tool-usage lines of the prompt template, dialect named explicitly.

    ``_get_jinja_env`` is what ``_build_system_prompt`` renders through, so this
    is the same template the daemon uses, read one level earlier so a bare render
    can be taken without the memory index a real session also carries.
    """
    rendered = _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent", shell_tool="pwsh"
    )
    return [(f"system.j2:{n}", line) for n, line in enumerate(rendered.splitlines(), 1)]


def _carriers() -> list[tuple[str, str]]:
    return _tool_carriers() + _template_carriers()


# ── the kernel's half of the bind ─────────────────────────────────────────


def test_the_kernel_still_decides_the_run_directory(tmp_path):
    """If this stops being true, the carriers' "it is ignored" is the lie instead.

    Driven from ``SHELL_TOOL_NAMES`` plus ``glob``, so a dialect added to the
    roster is covered here without editing this test, and a call that names its
    own directory is answered with the session's.
    """
    session = SimpleNamespace(cwd=tmp_path)
    for name in sorted(SHELL_TOOL_NAMES) + ["glob"]:
        args = {"workdir": "/somewhere/else"}
        EmrgServer._inject_tool_arguments(name, args, session, TaskRequest(sandbox=None))
        assert args["workdir"] == str(tmp_path), name
        assert args["workdir"] != "/somewhere/else", name


def test_the_kernel_keeps_the_boundary_with_the_directory(tmp_path):
    """The reason the directory is not the caller's: it *is* the authorization root.

    The ``workdir`` the injection sets and the ``workspace`` the executor reads as
    its boundary are one value, which is why a caller-chosen ``workdir`` could not
    be honoured without handing the caller its own sandbox root.
    """
    session = SimpleNamespace(cwd=tmp_path)
    args = {"command": "ls", "workdir": "/somewhere/else", "workspace": "/"}
    EmrgServer._inject_tool_arguments(
        next(iter(sorted(SHELL_TOOL_NAMES))), args, session,
        TaskRequest(sandbox="workspace-write"),
    )
    assert args["workspace"] == str(tmp_path)
    assert args["workdir"] == str(tmp_path)


# ── the carriers' half ────────────────────────────────────────────────────


def test_no_carrier_mentions_the_directory_without_saying_who_decides_it():
    """A sentence naming ``workdir`` must say the daemon supplies it.

    The general form of the defect: any new line — in a tool description or in the
    prompt — that offers ``workdir`` as a way to say where to run is red here,
    without this test having to enumerate the way it is worded.
    """
    offenders = []
    for where, text in _carriers():
        for piece in _sentences(text):
            lowered = piece.lower()
            if "workdir" in lowered and not any(m in lowered for m in DECIDES):
                offenders.append(f"{where}: {piece.strip()!r}")
    assert not offenders, (
        "the daemon supplies the run's working directory and discards the call's, "
        "so a carrier may not mention `workdir` without saying so; found:\n  "
        + "\n  ".join(offenders)
    )


def test_every_workdir_parameter_says_the_value_is_discarded():
    """The parameter itself: not "cannot widen it" — the value is not used at all.

    "Does not widen" reads as permission to narrow, which is the reading the
    measured call acted on.  The parameter stays in the schema because the tools
    are also usable on their own, so its description has to say what it does
    under the daemon: nothing.
    """
    for tool in (BashToolV2(), PwshToolV2(), GlobTool()):
        definition = tool.definition()
        spec = definition.parameters["properties"]["workdir"]
        text = spec["description"].lower()
        assert "discard" in text or "ignored" in text, (
            f"{definition.name}'s `workdir` still does not say a value passed here "
            f"is unused: {spec['description']!r}"
        )
        assert not any(r in text for r in RETIRED), definition.name


def test_the_retired_instructions_are_gone():
    """The measured wording, verbatim: it may not come back in any of these forms."""
    survivors = []
    for where, text in _carriers():
        for retired in RETIRED:
            if retired in text.lower():
                survivors.append(f"{where}: {retired!r}")
    assert not survivors, "a discarded choice is offered again:\n  " + "\n  ".join(survivors)


# ── the carriers reach the artifact a session gets ────────────────────────


def test_the_corrected_bullets_reach_the_prompt_a_session_actually_gets():
    """Reading the template answers "is it in ``system.j2``?" — one level short.

    ``_build_system_prompt`` is the single render site of ``system.j2`` and the
    first line of every tool loop, so the lines fixed above must be in *that*
    artifact.  The retired-phrase scan is not repeated here on purpose: a real
    session's prompt also carries the memory index, which may quote the phrasing
    it is recording — a record about the defect is not the defect.
    """
    rendered = _instantiate()._build_system_prompt()
    for where, line in _template_carriers():
        if "workdir" not in line.lower():
            continue
        assert line.strip() in rendered, (
            f"{where} is in system.j2 but not in the prompt a session receives: {line.strip()!r}"
        )
