"""The frontmatter block is delimited by **lines**, and one home says so.

``emrg/skills/loader.py``, ``emrg/skills/registry.py`` and
``emrg/skills/installer.py`` all have to answer "where does the frontmatter end",
and each answered it with its own ``text.split("---", 2)`` — a **substring**
split, so a ``---`` inside a value ended the block early. ``emrg/memory.py`` has
always read the delimiters as lines (its own comments say so), so the rule had
several homes and only one of them was the convention.

Measured on master, a skill whose description mentions ``---``:

    description: "Use --- to separate sections"    ->  loaded as "Use"
    body                                           ->  "to separate sections\\n---\\n# Body"

and since progressive disclosure shows the model *only* the description, the
skill is advertised as doing something it does not. ``installer.py`` had a second
half of the same defect: it handed ``_parse_frontmatter`` the **whole file**, and
that parser reads every unindented ``key: value`` line it is given — so a body
line could satisfy the "has frontmatter" check and the file was installed as
``{"ok": True}`` while ``load_skills`` rejected it.

These tests pin the boundary itself, and pin all three readers to it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from emrg.skills import installer, registry
from emrg.skills.loader import (
    _parse_frontmatter,
    _parse_skill_file,
    frontmatter_block,
    load_skills,
)


def _run(coro):
    return asyncio.run(coro)


# ── the rule: a delimiter is a line, not a substring ─────────────────────


def test_a_delimiter_inside_a_value_does_not_end_the_block():
    """The defect: `---` in a description used to close the block early."""
    text = (
        "---\n"
        "name: tripledash\n"
        "description: Use --- to separate sections\n"
        "---\n"
        "# Body\n"
    )
    pair = frontmatter_block(text)
    assert pair is not None, "the delimiters are lines, so this is a well-formed block"
    block, body = pair
    fm = _parse_frontmatter(block)
    assert fm["description"] == "Use --- to separate sections"
    assert body == "# Body"


def test_a_value_containing_the_delimiter_survives_the_file_parser(tmp_path):
    """The same read through the reader the loader actually uses."""
    skill = tmp_path / "tripledash.md"
    skill.write_text(
        "---\n"
        "name: tripledash\n"
        "description: Use --- to separate sections\n"
        "---\n"
        "# Body\n\nInstructions here.\n"
    )
    parsed = _parse_skill_file(skill, "user")
    assert parsed is not None
    assert parsed.description == "Use --- to separate sections"
    assert parsed.body == "# Body\n\nInstructions here."
    assert "to separate sections" not in parsed.body, "the description must not leak into the body"


@pytest.mark.parametrize(
    "opening",
    ["----", "---x", "-–-"],
)
def test_only_a_line_that_is_exactly_the_delimiter_opens_a_block(opening):
    """The delimiter is three dashes and nothing else; near-misses are content."""
    assert frontmatter_block(f"{opening}\nname: x\n---\nbody") is None


def test_a_trailing_space_on_the_delimiter_is_tolerated():
    """Whitespace around the delimiter is the line's, not the block's."""
    pair = frontmatter_block("---\nname: x\n--- \nbody")
    assert pair is not None, "whitespace around a delimiter is the line's, not the block's"
    block, body = pair
    assert _parse_frontmatter(block) == {"name": "x"}
    assert body == "body"


# ── the boundary, from both sides ────────────────────────────────────────


@pytest.mark.parametrize(
    "text,why",
    [
        ("no frontmatter at all", "no opening delimiter"),
        ("# Heading\n\n---\nname: x\n---\n", "a delimiter later in the body is not an opening"),
        ("---\nname: x\n", "an opening with no closing delimiter"),
        ("---\nname: x\n# Body\n", "and the same with body text after it"),
        ("", "empty text"),
    ],
)
def test_shapes_without_a_block_answer_none(text, why):
    assert frontmatter_block(text) is None, why


def test_an_empty_block_is_still_a_block_and_an_empty_body():
    """Both delimiters present with nothing between them is well formed."""
    assert frontmatter_block("---\n---\n") == ("", "")
    assert frontmatter_block("---\n---\nbody") == ("", "body")


def test_the_block_ends_at_the_first_closing_delimiter_line():
    """A later `---` line belongs to the body — it is markdown's horizontal rule."""
    pair = frontmatter_block("---\nname: x\n---\n# T\n\n---\n\nafter\n")
    assert pair is not None, "the block exists; the question is where it ends"
    block, body = pair
    assert block == "name: x"
    assert body == "# T\n\n---\n\nafter"


# ── one home: every reader reads this rule ───────────────────────────────


def test_the_catalog_parser_reads_the_same_rule(tmp_path, monkeypatch):
    """``registry`` parses a catalog whose entry description contains `---`."""
    catalog = (
        "---\n"
        "name: skill-catalog\n"
        "description: catalog\n"
        "skills:\n"
        "  - name: a-skill\n"
        "    description: uses --- as a separator\n"
        "    repo: o/r\n"
        "    install: self-publishing\n"
        "    dest: ~/.emrg/skills/\n"
        "    check: github_release\n"
        "---\n"
        "# body\n"
    )
    entries = registry.parse_skills_frontmatter(catalog)
    assert len(entries) == 1
    assert entries[0]["name"] == "a-skill"
    assert entries[0]["description"] == "uses --- as a separator"


def test_the_shipped_catalog_still_parses():
    """The real baseline is well formed under the line rule (no regression)."""
    entries = registry.parse_skills_frontmatter(registry.BASELINE_CATALOG_MD)
    assert [e["name"] for e in entries] == ["browser-harness"]


def test_the_installer_parses_the_block_not_the_whole_file(tmp_path, monkeypatch):
    """A body line must not satisfy the published-file check.

    Measured on master: this text was accepted and written, and ``load_skills``
    then rejected the same file for having no frontmatter — ``{"ok": True}`` for a
    skill that can never load.
    """
    published = "---\nname: from-the-body\n---\n# Body\ndescription: written by the body\n"

    class _Runner:
        async def __call__(self, cmd, **kwargs):
            return installer.CmdResult(returncode=0, stdout=published)

    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".emrg")
    entry = {"install": "self-publishing", "dest": "~/.emrg/skills/"}
    result = _run(installer._publish_skill(entry, _Runner()))

    # The check must be about the *block*, and the block has no description.
    assert "error" in result, result
    assert "missing name/description" in result["error"]
    assert not (tmp_path / ".emrg" / "skills" / "from-the-body.md").exists()


def test_the_installer_accepts_a_real_block_and_the_loader_loads_it(tmp_path, monkeypatch):
    """The positive side: an accepted file is one `load_skills` will load.

    The pair is the point — an install that reports ok for a file the loader then
    skips is the silent half of the same defect.
    """
    published = "---\nname: real-skill\ndescription: does the thing\n---\n# Body\n"

    class _Runner:
        async def __call__(self, cmd, **kwargs):
            return installer.CmdResult(returncode=0, stdout=published)

    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".emrg")
    entry = {"install": "self-publishing", "dest": "~/.emrg/skills/"}
    result = _run(installer._publish_skill(entry, _Runner()))
    assert result.get("ok") is True, result

    loaded = {s.name: s for s in load_skills(project_dir=tmp_path)}
    # The published file lands under the (redirected) config dir, which is also the
    # project dir this read is scoped to. The user dir is the host's real one, so
    # the assertion is about the installed skill rather than about the whole list.
    assert "real-skill" in loaded, sorted(loaded)
    assert loaded["real-skill"].description == "does the thing"
