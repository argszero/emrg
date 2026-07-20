#!/usr/bin/env bash
# Install/update/uninstall EMRG with uv editable mode.
#
# One-liner install:
#   curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
# One-liner uninstall (keep source & data):
#   curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- uninstall
# One-liner purge (remove everything):
#   curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- purge
#
# Local usage:
#   ./install.sh            # clone (if needed) + install/update
#   ./install.sh update     # same as above
#   ./install.sh uninstall  # remove emrg CLI, stop daemon, (keep source & data)
#   ./install.sh purge      # uninstall + remove source repo & ~/.emrg data
#
# Prerequisites: git, python >=3.10, uv, gh (recommended)
set -euo pipefail

REPO_URL="https://github.com/argszero/emrg.git"
REPO_DIR="${EMRG_SOURCE:-$HOME/.emrg/source}"
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
        err "git not found — install it: brew install git (macOS) / apt install git (Linux)"
        missing=1
    fi

    if ! command -v python3 &>/dev/null; then
        err "python3 not found — install Python 3.10+"
        missing=1
    else
        local py_ver
        py_ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        if [[ "$(python3 -c 'import sys; print(sys.version_info >= (3,10))')" != "True" ]]; then
            err "python3 $py_ver is too old — Python 3.10+ required"
            missing=1
        fi
    fi

    if ! command -v uv &>/dev/null; then
        err "uv not found — install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
        missing=1
    fi

    if ! command -v gh &>/dev/null; then
        warn "gh CLI not found — install it: brew install gh (macOS) / apt install gh (Linux)"
        warn "  After install, run: gh auth login"
    else
        if ! gh auth status &>/dev/null 2>&1; then
            warn "gh is installed but not authenticated — run: gh auth login"
        fi
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

    # Enable auto-evolution for the emrg source project
    if [[ -n "$emrg_bin" ]] && [[ -d "$REPO_DIR/.git" ]]; then
        log "enabling auto-evolution for emrg source project ..."
        cd "$REPO_DIR"
        "$emrg_bin" --init-auto-evolve || warn "init-auto-evolve failed; run 'emrg --init-auto-evolve' in $REPO_DIR manually"
    fi
}

# ── Uninstall ──────────────────────────────────────────────

do_uninstall() {
    log "uninstalling emrg CLI ..."
    uv tool uninstall emrg 2>/dev/null || warn "emrg was not installed as a uv tool"
    stop_daemon
    log "emrg uninstalled (source repo and data kept)"
    echo "  To reinstall: ./install.sh"
    echo "  To purge everything: ./install.sh purge"
}

do_purge() {
    log "purging emrg ..."
    uv tool uninstall emrg 2>/dev/null || true
    stop_daemon
    if [[ -d "$REPO_DIR" ]]; then
        log "removing source repo: $REPO_DIR"
        rm -rf "$REPO_DIR"
    fi
    if [[ -d "$HOME/.emrg" ]]; then
        log "removing data directory: $HOME/.emrg"
        rm -rf "$HOME/.emrg"
    fi
    log "emrg fully purged"
    echo "  To reinstall: curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash"
}

# ── Main ───────────────────────────────────────────────────

main() {
    case "${1:-}" in
        uninstall) do_uninstall ;;
        purge) do_purge ;;
        *) 
            check_prereqs
            ensure_repo
            stop_daemon
            git_pull
            uv_install
            ;;
    esac
}

main "${1:-}"
