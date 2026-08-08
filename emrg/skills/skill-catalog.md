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

(New recommended skills get a section appended here — adding a skill only
touches this file, the system prompt never changes.)
