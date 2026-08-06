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

branch="${1:-$(git rev-parse --abbrev-ref HEAD)}"
echo "Dispatching Test workflow on branch: ${branch}"
gh workflow run test.yml --ref "${branch}"
echo "Dispatched. Watch: gh run list --workflow=test.yml"
