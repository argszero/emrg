"""The four skill roots, the order they are read in, and the descriptions inside them.

Issue #2654. The loader read two roots — the instance's ``~/.emrg/skills`` and the agent's
``<agent>/.emrg/skills`` — so a skill installed into either provider directory
(``~/.claude/skills``, ``~/.agents/skills``), which is where the ecosystem actually publishes
them, was not loaded at all and had to be copied by hand. Where the same name existed in two
places there was also no rule: the one collision the module knew about was agent-beats-user,
and every other overlap was decided by whichever directory happened to be read last.

Two properties hold the whole file together.

**Every test states its own roots.** `load_skills_with_shadows(..., roots=[...])` is the
parameter that exists for this, and the reason is measured rather than stylistic: the
development host carries 28 skills in ``~/.claude/skills`` and 27 of the same names in
``~/.agents/skills``, so a suite that read the real directories would answer one way here and
another way on a CI runner with none. A test that inherited them would be measuring the
machine, not this rule.

**The last root read wins.** Precedence is *position*, not a comparison of labels, so the
tests below assert which root's copy is in effect and which one is reported as shadowed —
the pair the prompt and the ``/skills`` screen both render.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emrg.skills import loader as loader_mod
from emrg.skills.loader import (
    build_skills_context,
    default_skill_roots,
    load_skills_with_shadows,
)

SKILL_TEXT = "---\nname: {name}\ndescription: {description}\n---\n\nbody\n"


def _skill(name: str, description: str = "a skill") -> str:
    return SKILL_TEXT.format(name=name, description=description)


def _write_md(root: Path, name: str, description: str) -> Path:
    """One flat ``<name>.md`` skill in ``root``, the layout an installer publishes."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    path.write_text(_skill(name, description), encoding="utf-8")
    return path


def _write_bundle(root: Path, bundle: str, name: str, description: str) -> Path:
    """One ``<bundle>/SKILL.md`` skill, the layout that can carry files beside it."""
    directory = root / bundle
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(_skill(name, description), encoding="utf-8")
    return path


@pytest.fixture
def roots(tmp_path):
    """Three instance roots and one agent directory, all inside ``tmp_path``.

    Nothing here resolves a real home: the three roots are handed to the loader as its
    ``roots`` argument, and the agent root comes from ``_agent_root`` finding the ``.git``
    this fixture creates. The ``.emrg`` name on the instance root is deliberate — it is the
    layout a host has, and the loader's own default derivation is exercised separately.
    """
    base = tmp_path / "tree"
    agent = tmp_path / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / ".git").mkdir(exist_ok=True)
    instance = {
        "claude": base / "claude",
        "agents": base / "agents",
        "user": base / "emrg",
    }
    return {
        "dirs": instance,
        "agent": agent,
        "agent_skills": agent / ".emrg" / "skills",
        "roots": [
            ("claude", instance["claude"]),
            ("agents", instance["agents"]),
            ("user", instance["user"]),
        ],
    }


def _load(tree):
    return load_skills_with_shadows(agent_dir=tree["agent"], roots=tree["roots"])


def test_all_three_default_roots_declaring_one_name_resolve_to_the_instance_copy(roots):
    """The staircase's top step: three copies, one in effect, two named as displaced.

    `~/.emrg/skills` is read last of the three, so its copy is the one offered — and the
    other two are not silently gone: each is reported with the root it came from and the
    file it lives in, which is what lets a host see that a provider copy exists and was
    overridden rather than wonder why editing it changed nothing.
    """
    name = "shared"
    files = {
        label: _write_md(directory, name, f"from {label}")
        for label, directory in roots["dirs"].items()
    }

    skills, shadows = _load(roots)

    assert [s.name for s in skills] == [name], skills
    assert skills[0].description == "from user", skills[0].description
    assert skills[0].source == "user"
    assert skills[0].path == files["user"]

    assert len(shadows) == 2, shadows
    by_dropped = {shadow.dropped.source: shadow for shadow in shadows}
    assert set(by_dropped) == {"claude", "agents"}, by_dropped
    for label, shadow in by_dropped.items():
        assert shadow.name == name
        assert shadow.kept.source == "user"
        assert shadow.kept.path == files["user"]
        assert shadow.dropped.path == files[label], (
            f"the displaced {label} declaration is not named by its own file"
        )


def test_the_agents_directory_wins_over_the_claude_directory(roots):
    """The first two steps on their own: `~/.agents/skills` is read after `~/.claude/skills`."""
    name = "shared"
    claude_file = _write_md(roots["dirs"]["claude"], name, "from claude")
    agents_file = _write_md(roots["dirs"]["agents"], name, "from agents")

    skills, shadows = _load(roots)

    assert [s.name for s in skills] == [name], skills
    assert skills[0].source == "agents"
    assert skills[0].path == agents_file
    assert skills[0].description == "from agents"
    assert [(s.kept.path, s.dropped.path) for s in shadows] == [(agents_file, claude_file)]


@pytest.mark.parametrize("label", ["claude", "agents", "user"])
def test_a_name_in_one_root_loads_and_names_that_root(roots, label):
    """No collision: the skill loads, and its entry says which of the three it came from.

    The label is the point of the row. A single ``user`` label for all three would pass a
    test that only asked whether the skill loaded, which is exactly the reading the three
    roots were added to make possible.
    """
    path = _write_md(roots["dirs"][label], "solo", f"from {label}")

    skills, shadows = _load(roots)

    assert [s.name for s in skills] == ["solo"], skills
    assert skills[0].source == label
    assert skills[0].path == path
    assert skills[0].description == f"from {label}"
    assert shadows == [], shadows


def test_a_root_that_does_not_exist_is_skipped_and_the_others_still_load(roots):
    """Two of the three roots absent, the third present: one skill, no error, no shadow.

    A missing directory is the normal state of a host that has never used one of the
    provider layouts, so the answer is the other roots' skills rather than an exception or
    an empty list.
    """
    assert not roots["dirs"]["claude"].exists()
    assert not roots["dirs"]["agents"].exists()
    _write_md(roots["dirs"]["user"], "only-here", "the survivor")

    skills, shadows = _load(roots)

    assert [s.name for s in skills] == ["only-here"], skills
    assert shadows == []


def test_a_bundle_beats_a_flat_file_in_the_same_root(roots):
    """Both layouts of one name inside one root: the bundle is the copy in effect.

    The bundle is the form that can carry the files a skill references, so the flat file
    beside it is a leftover copy of the same skill. Read within one root, which is why this
    is a property of `_skill_files_in` rather than of the root order.
    """
    unrelated = _write_md(roots["dirs"]["user"], "same", "the flat copy")
    bundle = _write_bundle(roots["dirs"]["user"], "same", "same", "the bundle copy")

    skills, shadows = _load(roots)

    assert [s.name for s in skills] == ["same"], skills
    assert skills[0].path == bundle, "the flat file beside a bundle won §5 of issue #2654"
    assert skills[0].path != unrelated
    assert skills[0].description == "the bundle copy"
    assert skills[0].source == "user"


def test_the_agent_copy_beats_a_default_root_and_the_shadow_names_which_one(roots):
    """The agent tier is read last of all, and says which default root's copy it displaced.

    This is the precedence the module already documented, kept — what is new is that the
    loser is named *by its root*, so a reader can tell a displaced provider copy from a
    displaced instance copy.
    """
    name = "deploy"
    user_file = _write_md(roots["dirs"]["user"], name, "the instance copy")
    roots["agent_skills"].mkdir(parents=True)
    agent_file = roots["agent_skills"] / f"{name}.md"
    agent_file.write_text(_skill(name, "the agent copy"), encoding="utf-8")

    skills, shadows = _load(roots)

    assert [s.name for s in skills] == [name], skills
    assert skills[0].source == "agent"
    assert skills[0].path == agent_file
    assert skills[0].description == "the agent copy"
    assert len(shadows) == 1, shadows
    shadow = shadows[0]
    assert shadow.kept.source == "agent" and shadow.kept.path == agent_file
    assert shadow.dropped.source == "user" and shadow.dropped.path == user_file


BLOCK_SCALAR_CASES = [
    ("folded-strip", ">-", "first line second line"),
    ("folded", ">", "first line second line"),
    ("literal-strip", "|-", "first line\nsecond line"),
    ("literal", "|", "first line\nsecond line"),
]


@pytest.mark.parametrize("name, indicator, expected", BLOCK_SCALAR_CASES)
def test_a_block_scalar_description_reads_as_its_text(roots, name, indicator, expected):
    """`>`, `>-`, `|` and `|-` describe the skill; the indicator never becomes the text.

    The provider directories' skills write their description this way, and the earlier
    reader stored the indicator verbatim and skipped every indented line — so such a skill
    was described by the two characters ``>-`` and its real text existed nowhere in the
    prompt. Both halves are asserted: the text is there, and the indicator is not.
    """
    root = roots["dirs"]["claude"]
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {indicator}\n"
        "  first line\n"
        "  second line\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )

    skills, _ = _load(roots)

    assert [s.name for s in skills] == [name], skills
    description = skills[0].description
    assert description == expected, f"{indicator} read as {description!r}"
    assert indicator not in description, "the indicator itself became the description"
    assert "first line" in description and "second line" in description, (
        "a line of the block scalar was dropped"
    )


def test_a_colon_in_a_plain_description_is_kept(roots):
    """The control for the block-scalar branch: an ordinary value is not read as one."""
    root = roots["dirs"]["user"]
    root.mkdir(parents=True, exist_ok=True)
    (root / "plain.md").write_text(
        '---\nname: plain\ndescription: "One thing: and another"\n---\n\nbody\n',
        encoding="utf-8",
    )

    skills, _ = _load(roots)

    assert [s.name for s in skills] == ["plain"], skills
    assert skills[0].description == "One thing: and another"


def test_a_nested_description_key_still_loses_to_the_top_level_one(roots):
    """The nested-YAML rule the block-scalar reader had to keep, not trade away.

    A skill whose frontmatter carries a nested metadata list must keep its top-level
    name/description: an indented ``description:`` is nested YAML, not this file's value.
    """
    root = roots["dirs"]["user"]
    root.mkdir(parents=True, exist_ok=True)
    (root / "nested.md").write_text(
        "---\n"
        "name: nested\n"
        "metadata:\n"
        "  description: the nested one\n"
        "description: the top-level one\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )

    skills, _ = _load(roots)

    assert [s.name for s in skills] == ["nested"], skills
    assert skills[0].description == "the top-level one"


def test_the_rendered_entry_names_the_root_the_skill_came_from(roots):
    """The prompt carrier: three distinct labels, not one `user` for all three roots."""
    for label, directory in roots["dirs"].items():
        _write_md(directory, f"only-{label}", f"from {label}")

    skills, shadows = _load(roots)
    rendered = build_skills_context(skills, shadows)

    for label in roots["dirs"]:
        assert f"**only-{label}** ({label}, " in rendered, (
            f"the {label} root's skill is not labelled with its own root: {rendered}"
        )


def test_the_default_roots_are_the_three_beside_the_instance_root(tmp_path, monkeypatch):
    """The real chain, and the one fact that keeps a suite off the host's own directories.

    `config_dir()` is ``~/.emrg``, so its parent is the home the three roots sit in — one
    derivation means a host (or a test) that redirects the instance root redirects all
    three. Asserted on a redirected instance root: the roots follow it, and none of them
    points at the real home.
    """
    instance_root = tmp_path / "home" / ".emrg"
    instance_root.mkdir(parents=True)
    monkeypatch.setattr(loader_mod, "config_dir", lambda: instance_root)

    actual = default_skill_roots()

    assert [label for label, _ in actual] == ["claude", "agents", "user"], actual
    assert [path for _, path in actual] == [
        tmp_path / "home" / ".claude" / "skills",
        tmp_path / "home" / ".agents" / "skills",
        instance_root / "skills",
    ], actual
    for _, path in actual:
        assert str(tmp_path) in str(path), f"{path} is not under the redirected home"
