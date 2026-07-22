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
# Dependencies (git, python >=3.11, uv, gh) are auto-installed if missing.
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

install_prereqs() {
    local os
    os="$(uname -s)"

    # ── macOS: ensure Homebrew ──────────────────────────────
    if [[ "$os" == "Darwin" ]] && ! command -v brew &>/dev/null; then
        log "installing Homebrew ..."
        if ! /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
            err "Homebrew install failed — install it manually: https://brew.sh"
            exit 1
        fi
        # Add brew to PATH for this session (Apple Silicon vs Intel)
        if [[ -f /opt/homebrew/bin/brew ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -f /usr/local/bin/brew ]]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
    fi

    # ── git ─────────────────────────────────────────────────
    if ! command -v git &>/dev/null; then
        log "installing git ..."
        case "$os" in
            Darwin) brew install git || { err "brew install git failed"; exit 1; } ;;
            Linux)  sudo apt-get update -qq && sudo apt-get install -y git || { err "apt install git failed"; exit 1; } ;;
        esac
    fi

    # ── python3 ─────────────────────────────────────────────
    if ! command -v python3 &>/dev/null; then
        log "installing python3 ..."
        case "$os" in
            Darwin) brew install python@3.12 || { err "brew install python failed"; exit 1; } ;;
            Linux)  sudo apt-get update -qq && sudo apt-get install -y python3 || { err "apt install python3 failed"; exit 1; } ;;
        esac
    fi
    # Version check (warn, don't auto-upgrade existing installs)
    if command -v python3 &>/dev/null; then
        if [[ "$(python3 -c 'import sys; print(sys.version_info >= (3,11))')" != "True" ]]; then
            local py_ver
            py_ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
            err "python3 $py_ver is too old — Python 3.11+ required"
            exit 1
        fi
    fi

    # ── uv ──────────────────────────────────────────────────
    if ! command -v uv &>/dev/null; then
        log "installing uv ..."
        curl -LsSf https://astral.sh/uv/install.sh | sh || { err "uv install failed"; exit 1; }
        export PATH="$HOME/.local/bin:$PATH"
    fi

    # ── gh (recommended) ────────────────────────────────────
    if ! command -v gh &>/dev/null; then
        log "installing gh CLI ..."
        case "$os" in
            Darwin)
                brew install gh || { err "brew install gh failed"; exit 1; }
                ;;
            Linux)
                # Install gh CLI to user directory (~/.local/bin), no sudo needed.
                # Same approach as uv install above.
                mkdir -p "$HOME/.local/bin"
                local gh_ver arch
                arch="amd64"
                [[ "$(uname -m)" == "aarch64" ]] && arch="arm64"
                gh_ver=$(curl -s https://api.github.com/repos/cli/cli/releases/latest \
                    | grep '"tag_name"' | cut -d'"' -f4)
                curl -sSL "https://github.com/cli/cli/releases/download/${gh_ver}/gh_${gh_ver#v}_linux_${arch}.tar.gz" \
                    | tar xz -C /tmp \
                    || { err "gh download failed"; exit 1; }
                find /tmp/gh_* -name gh -type f -exec cp {} "$HOME/.local/bin/gh" \; 2>/dev/null
                chmod +x "$HOME/.local/bin/gh"
                rm -rf /tmp/gh_*/
                export PATH="$HOME/.local/bin:$PATH"
                ;;
        esac
    fi

    if command -v gh &>/dev/null && ! gh auth status &>/dev/null 2>&1; then
        warn "gh is installed but not authenticated — run: gh auth login"
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

# ── Config template ────────────────────────────────────────

generate_config_template() {
    local cfg="$HOME/.emrg/config.toml"
    if [[ -f "$cfg" ]]; then
        log "config already exists: $cfg"
        return
    fi
    log "generating config template: $cfg"
    mkdir -p "$HOME/.emrg"
    cat > "$cfg" <<'EMRGCONF'
[llm]
# OpenAI-compatible API endpoint
base_url = "https://api.deepseek.com"
# Replace with your API key
api_key = "sk-..."
# Change to your preferred model
model = "deepseek-chat"
max_tokens = 8192
temperature = 0.7
context_window = 131072
auto_compact_threshold = 0.0
EMRGCONF
    log "config template created — edit $cfg to set your api_key and model"
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
            install_prereqs
            ensure_repo
            stop_daemon
            git_pull
            uv_install
            generate_config_template
            ;;
    esac
}

main "${1:-}"
