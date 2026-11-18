# Skills

A skill is a Markdown file an agent reads when it bears on the work in front of it. This
page is the contract an author satisfies to add one.

The loader is `emrg/skills/loader.py`. Where this page and that file disagree, the file is
right and this page is the bug.

## Where a skill is read from

Four roots are read, **lowest precedence first**:

| Read | Root | Label the prompt gives it |
|---|---|---|
| first | `~/.claude/skills` | `claude` |
| second | `~/.agents/skills` | `agents` |
| third | `~/.emrg/skills` | `user` |
| last | `<agent>/.emrg/skills` | `agent` |

The order is precedence, not breadth: **a later root's declaration of a name replaces an
earlier one's.** The three instance-wide roots come first and the agent's own directory is
read last whatever the list says — so an agent's copy beats the host's instance copy, and
the instance copy beats both provider directories. The agent's root is deliberately absent
from `default_skill_roots()`, because it depends on the session; `load_skills_with_shadows`
adds it rather than a caller.

A root the host does not have is skipped, and the others still load.

### `<agent>` is the session's repository, not the daemon's directory

`<agent>` is the nearest ancestor of the **session's** working directory that carries a
`.git`. A session is usually opened somewhere inside a project while the project's skills
sit at its top, so the root is resolved rather than assumed to be the directory the session
happens to be in. A directory with no repository above it is its own root, which is what a
scratch directory wants.

This is the rule most easily got backwards: taking the daemon's working directory instead
would give every session on the host one agent's skills.

## The two layouts

Both are read in every root, and one root may carry either:

```
~/.emrg/skills/
├── release-checklist.md            # flat — the whole skill in one file
└── browser-harness/                # bundle — the spec's layout
    ├── SKILL.md                    #   the skill itself
    └── reference.md                #   files it references, read on demand
```

The flat form is what this project's installer publishes (`installer.install_skill` writes
`<name>.md`). The bundle is the layout the skill spec documents, and the only one that can
carry the files a skill references beside it.

**When one root carries both for the same name, the bundle wins.** The flat file beside a
bundle is a leftover copy of the same skill, not a second one.

## The file

```markdown
---
name: release-checklist
description: Use when cutting a release — the ordered steps, and the check that decides each one.
---
# Release checklist

1. …
```

The file must **open** with `---` and carry a second `---` after the frontmatter; a file
that does not is skipped rather than loaded with defaults.

- **`description` is required.** A file without one is skipped, so a skill that "does not
  appear" is most often a frontmatter mistake.
- **`name` is optional**, and falls back to the filename stem. It is what the skill is
  listed under, and it is what collides with another declaration.
- Frontmatter keys other than `name` and `description` are read and ignored by the loader,
  so a skill may carry metadata of its own — the installable-skill catalog does.

The parser is hand-written rather than a YAML dependency, and covers plain and quoted
values, a plain value folded across the lines indented past it, and the block scalars
`>` `>-` `|` `|-` with their `+` forms. The block forms are read because the skills
published into the provider directories write `description:` that way.

## What reaches the agent

Only **name and description** are injected into the system prompt, together with the root
label and the file's path. The body is not: the agent reads the file when the description
says it bears on the work.

That is the whole reason the description matters. It is not a summary of the skill, it is
the trigger the agent matches against — so write it as *when to use this*, not as *what
this contains*.

## Nothing is cached

Every load re-reads the files. A skill written, edited or deleted on disk reaches the next
prompt without restarting the daemon.

## A name that loses is recorded, not dropped

Two declarations of one name resolve to one skill, and the loss is reported rather than
silent: `SkillShadow`, paired by `load_skills_with_shadows` once every root has been read.
The system prompt lists the declaration in effect and names the one that lost beside it,
and the host's `/skills` listing names both.

The record exists because the loss is otherwise invisible — a log line goes to a stream the
deployment discards, which is how a report ends up saying "clean" about a tree it could not
read.

Across roots the winner is the precedence rule above. **Within one root the tie-break is
the filename sort order**, which nothing promises — so two files declaring one name in one
root is a mistake to fix by renaming, not a situation to rely on.

## Checking a claim on this page

Each rule above is pinned by a test that answers that rule and no other:

| Rule | Test |
|---|---|
| The four roots, their order, and which root a name came from | `tests/test_skills_read_order.py` |
| Frontmatter parsing, and `name` falling back to the stem | `tests/test_skills_loader.py` |
| Both layouts, the bundle's tie-break, the agent root, and no caching | `tests/test_skills_read_live.py` |
| A displaced name is named, by the prompt and by `/skills` | `tests/test_skills_listing_names_shadows.py` |

The installable-skill catalog is itself a skill file, `~/.emrg/skills/skill-catalog.md`,
which is how it reaches the prompt; its `skills:` frontmatter list is machine metadata for
`/skills available`, `/skills install` and `/skills update`, read by
`emrg/skills/registry.py` and `emrg/skills/installer.py` and covered by
`tests/test_skills_registry.py`. The catalog is not a root of its own — it lists the skills
that can be installed **into** the roots above.
