"""Agent.md must arrive whole — the daemon keeps only its first 8000 chars.

Rant 2026-09-14T07:20:11 ("Agent.md 内容不应该包含演化的历史。Agent 应该只是定义
原则") reported the drift; the measurement here is what let it happen unnoticed.

Measured on master `e644cf6` (2026-09-14, this cycle):
`EmrgServer._collect_project_context` reads each project-context file
(`CLAUDE.md`, `AGENTS.md`, `Agent.md`, `MANIFESTO.md`) out of the session cwd and
keeps at most `PROJECT_CONTEXT_MAX_CHARS` of it, appending a truncation notice —
so an over-long file still parses, and what it loses is the **tail**. `Agent.md`
had grown to 64466 chars: the model received the first 8000 (per-rule evidence, PR
numbers, cycle ids) and lost the other 87%, conventions included. Nothing in the
tree measured the file, so nothing could object to it growing there.

The same mechanism, measured 2026-09-14 on this repo's `MANIFESTO.md` (10434 chars):
the cut fell **mid-line** — the injected head ended inside a `**bold**` span — and
the notice named neither the file nor a way to reach the rest, so its last six
sections (第十三条 through 第十七条) were dropped without a pointer. The cut now
lands on the last complete line inside the cap and the notice names the file and
the read tool; both properties are pinned below.

The cap is imported from the daemon rather than restated: a second spelling could
disagree with the code that does the cutting — the same defect the stored-count
rule pins for documented counts in `tests/test_doc_counts.py`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.server.daemon import PROJECT_CONTEXT_MAX_CHARS, EmrgServer
from emrg.session import Session

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_server() -> EmrgServer:
    """A minimal server; its project log is redirected off the real `~/.emrg`."""
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def test_the_cap_is_the_stated_one() -> None:
    """Pinned so moving the cap stays a decision rather than an accident."""
    assert PROJECT_CONTEXT_MAX_CHARS == 8000


def test_this_repos_agent_md_reaches_the_prompt_whole() -> None:
    """The repo's own brief has to fit, because the cut takes the tail.

    A file over the cap is not rejected — it is quietly shortened from the end,
    which is where a brief's later conventions are.
    """
    text = (REPO_ROOT / "Agent.md").read_text(encoding="utf-8")
    assert len(text) <= PROJECT_CONTEXT_MAX_CHARS, (
        f"Agent.md is {len(text)} chars, and the daemon keeps at most "
        f"{PROJECT_CONTEXT_MAX_CHARS} of each project-context file — so its last "
        f"{len(text) - PROJECT_CONTEXT_MAX_CHARS} chars never reach the model. "
        "Keep the file a brief (principles and conventions); move measurements, "
        "PR numbers and cycle ids to the memory records and the scripts' own "
        "docstrings (rant 2026-09-14T07:20:11)."
    )


def test_one_char_over_the_cap_is_cut(tmp_path: Path) -> None:
    """The negative half: one char more and the tail is gone.

    Both halves are exercised against the same method, so the guard above is
    known to discriminate rather than merely to be silent on today's tree.

    A one-line file with no newline inside the cap keeps the hard cut: the
    line-boundary rule needs a boundary to find.
    """
    server = _make_server()
    body = "x" * (PROJECT_CONTEXT_MAX_CHARS + 1)
    (tmp_path / "Agent.md").write_text(body, encoding="utf-8")
    session = Session.create_with_id("agent-md-cap-over", tmp_path)

    content = server._collect_project_context(session)[0]["content"]
    assert content.startswith(body[:PROJECT_CONTEXT_MAX_CHARS])
    assert len(content) > PROJECT_CONTEXT_MAX_CHARS  # the notice is added, not swallowed
    assert "truncated 1 chars" in content
    # The notice says which file lost text and how to read the rest. Without the
    # path hint the truncation is a dead end: measured 2026-09-14 on MANIFESTO.md,
    # whose last six sections were dropped by a notice naming neither
    # (2026-09-14, defect family: a message that does not name what it dropped).
    assert "Agent.md" in content, "the notice must name the file it cut"
    assert "read tool" in content, "the notice must say the tail is still readable"


def test_the_cut_lands_on_a_line_boundary(tmp_path: Path) -> None:
    """An over-long file is cut at its last complete line inside the cap.

    The old hard cut ended mid-sentence: on this repo's `MANIFESTO.md` the injected
    text ended inside a `**bold**` span, so the prompt carried an unterminated
    emphasis marker and a half sentence. The measurable rule: the injected head
    contains no fragment of the line the cut fell in.
    """
    server = _make_server()
    whole_line = project_line = "a" * 7000
    next_line_marker = "SECOND-LINE-MARKER"
    body = project_line + "\n" + (next_line_marker * 500)
    assert len(body) > PROJECT_CONTEXT_MAX_CHARS
    (tmp_path / "Agent.md").write_text(body, encoding="utf-8")
    session = Session.create_with_id("agent-md-line-cut", tmp_path)

    content = server._collect_project_context(session)[0]["content"]
    head = content.split("\n\n... [truncated", 1)[0]
    assert head == whole_line, f"head is not the complete first line: {head[-40:]!r}"
    assert next_line_marker not in head, "a fragment of the cut line reached the prompt"
    assert "truncated" in content


def test_a_file_at_the_cap_is_not_cut(tmp_path: Path) -> None:
    """The positive half, driven through the real method: at the cap, nothing is dropped."""
    server = _make_server()
    body = "x" * PROJECT_CONTEXT_MAX_CHARS
    (tmp_path / "Agent.md").write_text(body, encoding="utf-8")
    session = Session.create_with_id("agent-md-cap-at", tmp_path)

    content = server._collect_project_context(session)[0]["content"]
    assert content == body
    assert "truncated" not in content
