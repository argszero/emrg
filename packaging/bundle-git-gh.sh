#!/usr/bin/env bash
# EMRG Phase 4 — bundle git + gh (rant #12 §6/§13).
#
# gh: official single-file binary for all platforms (Go, zero deps).
# git:
#   - Windows: Git for Windows portable (official single-file self-extracting exe).
#   - macOS/Linux: NO official portable binary (R59) → CI builds from source:
#       macOS: ./configure && make (system libs)
#       Linux: static build (musl/openssl)
#
# Output layout (matches bin/emrg launcher expectations):
#   dist/runtime/bin/gh          (all platforms)
#   dist/runtime/git/            (Windows: cmd/ + bin/ + mingw64/ + libexec/)
#   dist/runtime/bin/git         (macOS/Linux: single binary)
#
# On CI this is called per-platform after build-runtime.sh.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$ROOT/dist/runtime"
PLATFORM="${1:-$(uname -s | tr '[:upper:]' '[:lower:]')}"

echo "==> bundling git/gh for $PLATFORM"

# ── gh ──────────────────────────────────────────────────────────
if [ -x "$RUNTIME/bin/gh" ]; then
  echo "    gh already present"
else
  GH_VERSION="${GH_VERSION:-2.58.0}"
  case "$PLATFORM" in
    darwin|macos)
      GH_OS="macOS"; GH_ARCH="$(uname -m)" ;;   # amd64 / arm64
    linux)
      GH_OS="linux"; GH_ARCH="$(uname -m)" ;;
    windows|mingw*|msys*|win32)
      GH_OS="windows"; GH_ARCH="amd64" ;;
    *) echo "!! unknown platform $PLATFORM — gh bundling skipped"; exit 1 ;;
  esac
  GH_URL="https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_${GH_OS}_${GH_ARCH}.tar.gz"
  echo "    downloading gh from $GH_URL"
  TMP="$(mktemp -d)"
  curl -sL "$GH_URL" -o "$TMP/gh.tgz"
  tar -xzf "$TMP/gh.tgz" -C "$TMP"
  find "$TMP" -name gh -type f | head -1 | xargs -I{} cp {} "$RUNTIME/bin/gh"
  chmod +x "$RUNTIME/bin/gh"
  rm -rf "$TMP"
fi

# ── git ─────────────────────────────────────────────────────────
if [ -x "$RUNTIME/bin/git" ] || [ -x "$RUNTIME/git/cmd/git.exe" ]; then
  echo "    git already present"
  exit 0
fi

case "$PLATFORM" in
  windows|mingw*|msys*|win32)
    # Git for Windows portable（官方单文件自解压，R60）
    GIT_VERSION="${GIT_VERSION:-2.46.0}"
    GIT_URL="https://github.com/git-for-windows/git/releases/download/v${GIT_VERSION}.windows.1/PortableGit-${GIT_VERSION}-64-bit.7z.exe"
    echo "    downloading Git for Windows portable"
    TMP="$(mktemp -d)"
    curl -sL "$GIT_URL" -o "$TMP/git.exe"
    # 7z 自解压（静默解压到 git/）
    mkdir -p "$RUNTIME/git"
    "$TMP/git.exe" -o"$RUNTIME/git" -y 2>/dev/null || 7z x "$TMP/git.exe" -o"$RUNTIME/git" -y
    rm -rf "$TMP"
    ;;
  darwin|macos|linux)
    # R59：无官方便携二进制 → CI 源码编译。本地开发跳过（用系统 git）。
    if [ "${CI:-}" != "true" ]; then
      echo "!! git source build is CI-only (R59). Local dev uses system git." >&2
      echo "   Set CI=true to force a source build." >&2
      exit 0
    fi
    echo "==> building git from source (R59, ~3-5min)"
    GIT_VERSION="${GIT_VERSION:-2.46.0}"
    TMP="$(mktemp -d)"
    curl -sL "https://github.com/git/git/archive/refs/tags/v${GIT_VERSION}.tar.gz" -o "$TMP/git.tgz"
    tar -xzf "$TMP/git.tgz" -C "$TMP"
    cd "$TMP/git-${GIT_VERSION}"
    make configure >/dev/null 2>&1 || ./configure --prefix="$TMP/prefix" >/dev/null
    make -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)" prefix="$TMP/prefix" install >/dev/null 2>&1
    cp "$TMP/prefix/bin/git" "$RUNTIME/bin/git"
    chmod +x "$RUNTIME/bin/git"
    # macOS: git 依赖系统库，产物跨版本可移植；Linux: 需静态（musl）——CI 用静态基础镜像
    rm -rf "$TMP"
    echo "    git built: $("$RUNTIME/bin/git" --version 2>/dev/null || echo 'check failed')"
    ;;
esac

echo "==> bundle-git-gh done"
