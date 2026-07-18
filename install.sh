#!/usr/bin/env bash
# Install/update EMRG with uv editable mode.
#
# Usage:
#   ./install.sh          # clone (if needed) + install/update
#   ./install.sh update   # same as above
#
# Prerequisites: git, python >=3.11, uv
set -euo pipefail

REPO_URL="https://github.com/argszero/emrg.git"
REPO_DIR="$HOME/scm/github.com/argszero/emrg"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log()  { echo -e "${GREEN}→${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }

# ── Prerequisites ──────────────────────────────────────────

check_prereqs() {
    local missing=0

    if ! command -v git &>/dev/null; then
        err "git not found — install it first: brew install git"
        missing=1
    fi

    if ! command -v python3 &>/dev/null; then
        err "python3 not found"
        missing=1
    fi

    if ! command -v uv &>/dev/null; then
        err "uv not found — install it first: curl -LsSf https://astral.sh/uv/install.sh | sh"
        missing=1
    fi

    if [[ $missing -ne 0 ]]; then
        exit 1
    fi

    log "prerequisites: git=$(git --version | awk '{print $NF}') python=$(python3 --version | awk '{print $NF}') uv=$(uv --version | awk '{print $NF}')"
}

# ── Clone ──────────────────────────────────────────────────

ensure_repo() {
    if [[ -d "$REPO_DIR/.git" ]]; then
        log "repo found: $REPO_DIR"
    else
        log "cloning $REPO_URL → $REPO_DIR ..."
        mkdir -p "$(dirname "$REPO_DIR")"
        git clone "$REPO_URL" "$REPO_DIR"
    fi
}

# ── Stop daemon ────────────────────────────────────────────

stop_daemon() {
    if command -v emrg &>/dev/null; then
        log "stopping daemon (if running) ..."
        emrg server stop 2>/dev/null || true
    fi
}

# ── Git pull ───────────────────────────────────────────────

git_pull() {
    log "git pull ..."
    local output rc
    cd "$REPO_DIR"
    set +e
    output=$(git pull 2>&1)
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        err "git pull failed:"
        err "$output"
        exit 1
    fi
    echo "$output"
}

# ── Install ────────────────────────────────────────────────

uv_install() {
    cd "$REPO_DIR"

    # 1. Global tool install (for CLI access anywhere)
    log "uv tool install --reinstall -e . ..."
    uv tool install --reinstall -e .

    # 2. Local venv install (for when working inside the repo)
    if [[ -f "$REPO_DIR/.venv/bin/activate" ]]; then
        log "uv pip install -e . (local venv) ..."
        uv pip install -e . --python "$REPO_DIR/.venv/bin/python"
    fi

    # Verify
    local emrg_bin
    emrg_bin="$(command -v emrg 2>/dev/null || true)"
    if [[ -n "$emrg_bin" ]]; then
        log "emrg → $emrg_bin — done"
    else
        warn "emrg CLI not on PATH — ensure \$HOME/.local/bin is in your PATH"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
}

# ── Main ───────────────────────────────────────────────────

main() {
    check_prereqs
    ensure_repo
    stop_daemon
    git_pull
    uv_install
}

main
