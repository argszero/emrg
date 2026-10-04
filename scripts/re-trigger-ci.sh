#!/usr/bin/env bash
# Re-trigger the Test workflow without an empty commit (PR #527).
#
# GitHub Actions outages can drop push events entirely (runs never created,
# check-runs empty) — empty-commit re-triggers depend on the same broken
# push-event pipeline and also pollute git history. workflow_dispatch is
# API-triggered and bypasses the push-event pipeline entirely.
#
# Usage: scripts/re-trigger-ci.sh [branch]     (default: current branch)
#   branch: a branch name or tag; use 'master' to verify master after merges.
set -euo pipefail

# The default ref must be a **branch name**, and a detached HEAD has none. Measured
# 2026-10-05: `git rev-parse --abbrev-ref HEAD` answers the literal string `HEAD` when
# detached - a value that names no ref - so the old line handed `gh workflow run --ref
# HEAD` something that cannot resolve, and the reader met gh's own error instead of this
# script naming the cause. `git symbolic-ref` is the reading that refuses (`fatal: ref
# HEAD is not a symbolic ref`), and the refusal below says which ref is missing and what
# to pass instead. A cycle's checkout is often detached, so this is the common case, not
# an exotic one.
branch="${1:-}"
if [ -z "${branch}" ]; then
  branch="$(git symbolic-ref --short HEAD 2>/dev/null)" || branch=""
fi
if [ -z "${branch}" ]; then
  echo "error: no ref to dispatch - pass a branch name, or check out a branch first." >&2
  echo "       (a detached HEAD has no branch name: 'git rev-parse --abbrev-ref HEAD'" >&2
  echo "        would answer the literal 'HEAD', which gh cannot resolve)" >&2
  echo "usage: scripts/re-trigger-ci.sh [branch]" >&2
  exit 2
fi
echo "Dispatching Test workflow on branch: ${branch}"
gh workflow run test.yml --ref "${branch}"
echo "Dispatched. Watch: gh run list --workflow=test.yml"
