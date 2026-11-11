---
name: skill-catalog
description: "Catalog of optional installable skills (browser-harness, etc.). Read this file when a task needs a capability you don't have — it lists what is installable, how to install, and how updates are checked."
skills:
  - name: browser-harness
    description: "Direct browser control via CDP: automation, scraping, testing, site work."
    repo: browser-use/browser-harness
    install: self-publishing
    dest: ~/.emrg/skills/
    check: github_release
  - name: skill-creator
    description: "Create, revise and benchmark skills: authoring workflow, evals, description tuning."
    repo: anthropics/skills
    source: skills/skill-creator
    install: vendor-copy
    dest: ~/.emrg/skills/
    check: github_commit
---

# Installable Skills

This catalog lists optional skills that are NOT installed by default. When
a task needs one (e.g. browser interaction), install it on demand:

## browser-harness

- **What it does**: Direct browser control via CDP — automation, scraping, testing, site work.
- **How to install**: `/skills install browser-harness`
- **Source repo**: browser-use/browser-harness
- **Install method**: self-publishing (CLI's own `skill` command emits the skill files)
- **Install destination**: `~/.emrg/skills/`
- **Update check**: GitHub release tag (api.github.com)

## skill-creator

- **What it does**: Create new skills, revise existing ones, and measure a skill's
  performance with evals — a skill-authoring workflow with its own scripts and references.
- **How to install**: `/skills install skill-creator`
- **Source repo**: anthropics/skills, directory `skills/skill-creator/`
- **Install method**: vendor-copy (the repository directory is copied whole as a bundle —
  `skill-creator/SKILL.md` plus its scripts and references; no CLI is involved)
- **Install destination**: `~/.emrg/skills/`
- **Update check**: the last commit touching `skills/skill-creator` (api.github.com)

`self-publishing` entries are refreshed against a release tag; `vendor-copy`
entries against a commit, and only when that commit moves.

(New recommended skills get a section appended here — adding a skill only
touches this file, the system prompt never changes.)
