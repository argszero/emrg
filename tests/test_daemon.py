"""Unit tests for daemon internals — prompt building and project discovery.

These test the methods that were broken by squash-merge conflict markers
in cycles #6-#8. Having test coverage here ensures that critical
evolution infrastructure stays operational.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.protocol import InstanceIdentity
from emrg.server.daemon import BackgroundThread, EmrgServer
from emrg.session import Session


# ── _get_auto_evolve_projects ──────────────────────────────────


def test_get_auto_evolve_no_file():
    """Returns empty list when projects.yml doesn't exist."""
    bt = BackgroundThread(InstanceIdentity())
    bt._projects_log = Path("/nonexistent/path/emrg_projects_test.yml")
    assert bt._get_auto_evolve_projects() == []


def test_get_auto_evolve_empty_list():
    """Returns empty list for an empty YAML list."""
    bt = BackgroundThread(InstanceIdentity())
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write("[]\n")
        tmp = f.name
    try:
        bt._projects_log = Path(tmp)
        assert bt._get_auto_evolve_projects() == []
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_get_auto_evolve_no_auto_evolve_entries():
    """Only returns entries with auto_evolve: true."""
    bt = BackgroundThread(InstanceIdentity())
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(
            "- name: a\n  path: /tmp/a\n  auto_evolve: false\n"
            "- name: b\n  path: /tmp/b\n  auto_evolve: false\n"
        )
        tmp = f.name
    try:
        bt._projects_log = Path(tmp)
        assert bt._get_auto_evolve_projects() == []
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_get_auto_evolve_mixed_entries():
    """Filters for auto_evolve=True entries."""
    bt = BackgroundThread(InstanceIdentity())
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(
            "- name: manual\n  path: /tmp/m\n  auto_evolve: false\n"
            "- name: auto1\n  path: /tmp/a1\n  repo: owner/a1\n  auto_evolve: true\n"
            "- name: auto2\n  path: /tmp/a2\n  auto_evolve: true\n"
        )
        tmp = f.name
    try:
        bt._projects_log = Path(tmp)
        result = bt._get_auto_evolve_projects()
        assert len(result) == 2
        assert result[0]["name"] == "auto1"
        assert result[1]["name"] == "auto2"
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_get_auto_evolve_invalid_yaml():
    """Returns empty list for garbage YAML (doesn't crash)."""
    bt = BackgroundThread(InstanceIdentity())
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(": not valid yaml {[[\n")
        tmp = f.name
    try:
        bt._projects_log = Path(tmp)
        assert bt._get_auto_evolve_projects() == []
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_get_auto_evolve_non_list():
    """Returns empty list when YAML root is not a list."""
    bt = BackgroundThread(InstanceIdentity())
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write("key: value\n")
        tmp = f.name
    try:
        bt._projects_log = Path(tmp)
        assert bt._get_auto_evolve_projects() == []
    finally:
        Path(tmp).unlink(missing_ok=True)


# ── _build_evolution_prompt ────────────────────────────────────


def test_build_prompt_emrg_self():
    """Builds prompt for emrg self-evolution (no project)."""
    bt = BackgroundThread(InstanceIdentity(instance_id="test-id", host_name="testhost"))
    prompt = bt._build_evolution_prompt(seq=1, project=None)

    # Core template variables must be present
    assert "演化周期 #1" in prompt
    assert "test-id" in prompt
    assert "testhost" in prompt
    assert "argszero/emrg" in prompt
    assert "emrg-evolution" in prompt
    # Post-#41/#42 variables
    assert "repo_url" not in prompt  # substituted, not literal
    assert "local_source" not in prompt  # substituted, not literal
    assert "https://github.com/argszero/emrg.git" in prompt
    # Conflict markers must NOT be present
    assert "<<<<<<<" not in prompt
    assert ">>>>>>>" not in prompt


def test_build_prompt_with_project():
    """Builds prompt for a custom project — derives owner/repo via git remote."""
    from unittest.mock import patch

    bt = BackgroundThread(
        InstanceIdentity(instance_id="test-id", host_name="testhost")
    )
    project = {
        "name": "myproject",
        "path": "/home/user/src/myproject",
        "auto_evolve": True,
    }
    with patch("emrg.server.daemon._detect_git_remote", return_value="user/myproject"):
        prompt = bt._build_evolution_prompt(seq=2, project=project)

    # Project-specific values should flow through
    assert "owner/repo" not in prompt.lower().replace("/", " ")  # not the literal placeholder
    assert "/home/user/src/myproject" in prompt  # source_dir from project.path
    # session_id should be project-specific (PR #54)
    assert "emrg-evolution-myproject" in prompt
    # repo_url derived from git remote detection
    assert "https://github.com/user/myproject.git" in prompt
    assert "argszero/emrg" not in prompt  # not the default owner/repo
    assert "<<<<<<<" not in prompt
    assert ">>>>>>>" not in prompt


def test_build_prompt_project_no_repo_field():
    """Falls back to defaults when git remote cannot be detected."""
    bt = BackgroundThread(InstanceIdentity(instance_id="i", host_name="h"))
    project = {"name": "mine", "path": "/tmp/mine", "auto_evolve": True}
    prompt = bt._build_evolution_prompt(seq=3, project=project)

    # Falls back to default owner/repo when git remote detection fails
    assert "argszero/emrg" in prompt
    assert "/tmp/mine" in prompt
    assert "<<<<<<<" not in prompt


def test_build_prompt_increments_seq():
    """seq number is per-cycle and should appear in the prompt."""
    bt = BackgroundThread(InstanceIdentity(instance_id="i", host_name="h"))
    p1 = bt._build_evolution_prompt(seq=5, project=None)
    p2 = bt._build_evolution_prompt(seq=99, project=None)

    assert "演化周期 #5" in p1
    assert "演化周期 #99" in p2


def test_build_prompt_all_variables_substituted():
    """No raw template placeholders ({var}) should remain in output."""
    import re

    bt = BackgroundThread(InstanceIdentity(instance_id="test-id", host_name="testhost"))

    # Self-evolution (no project)
    p1 = bt._build_evolution_prompt(seq=1, project=None)
    braces = re.findall(r"\{[a-z_]+\}", p1)
    assert not braces, f"Unsubstituted placeholders in self prompt: {braces}"

    # Project-based evolution
    project = {
        "name": "myproj",
        "path": "/home/user/src/myproj",
        "repo": "user/myproj",
        "auto_evolve": True,
    }
    p2 = bt._build_evolution_prompt(seq=2, project=project)
    braces = re.findall(r"\{[a-z_]+\}", p2)
    assert not braces, f"Unsubstituted placeholders in project prompt: {braces}"

    # Project without repo field
    project_no_repo = {"name": "x", "path": "/tmp/x", "auto_evolve": True}
    p3 = bt._build_evolution_prompt(seq=3, project=project_no_repo)
    braces = re.findall(r"\{[a-z_]+\}", p3)
    assert not braces, f"Unsubstituted placeholders in no-repo prompt: {braces}"


# ── _build_project_context_section ─────────────────────────────


def _make_server() -> EmrgServer:
    """Create a minimal EmrgServer for testing."""
    return EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))


def test_context_section_no_files(tmp_path):
    """No context files found → returns empty string."""
    server = _make_server()
    session = Session.create_with_id("ctx-test", tmp_path)
    result = server._build_project_context_section(session)
    assert result == ""


def test_context_section_single_file(tmp_path):
    """When CLAUDE.md exists, it's included in the context section."""
    server = _make_server()
    (tmp_path / "CLAUDE.md").write_text("# Project Rules\n- Use tabs\n")
    session = Session.create_with_id("ctx-test", tmp_path)
    result = server._build_project_context_section(session)
    assert "## Project Context" in result
    assert "### CLAUDE.md" in result
    assert "- Use tabs" in result
    assert "# Project Rules" in result


def test_context_section_multiple_files(tmp_path):
    """All matching context files are included."""
    server = _make_server()
    (tmp_path / "CLAUDE.md").write_text("claude content")
    (tmp_path / "AGENTS.md").write_text("agents content")
    session = Session.create_with_id("ctx-test", tmp_path)
    result = server._build_project_context_section(session)
    assert "### CLAUDE.md" in result
    assert "### AGENTS.md" in result
    assert "claude content" in result
    assert "agents content" in result


def test_context_section_truncation(tmp_path):
    """Files over 8000 chars are truncated with a notice."""
    server = _make_server()
    big = "x" * 9000
    (tmp_path / "CLAUDE.md").write_text(big)
    session = Session.create_with_id("ctx-test", tmp_path)
    result = server._build_project_context_section(session)
    assert "truncated" in result
    assert "1000 chars" in result  # 9000 - 8000 = 1000


def test_context_section_manifesto(tmp_path):
    """MANIFESTO.md is also read as a context file."""
    server = _make_server()
    (tmp_path / "MANIFESTO.md").write_text("# Design\nKeep it simple.\n")
    session = Session.create_with_id("ctx-test", tmp_path)
    result = server._build_project_context_section(session)
    assert "### MANIFESTO.md" in result
    assert "Keep it simple" in result
