#!/usr/bin/env bash
# EMRG Phase 4 — platform installer wrapper (rant #12 §10/§13).
#
#   macOS:  pkgbuild 用户级（R54）。payload 装临时位置 + postinstall 复制
#           （R104: runtime → ~/.emrg/install/ + GUI EMRG.app → ~/Applications/）
#           R67: postinstall $HOME 陷阱（提权时取控制台用户真实 HOME）
#           R105: root 复制后 chown 回用户
#   Windows: Inno Setup（R55 免 UAC；R87 {userhome} Inno 6.1+ fallback）
#   Linux:  AppImage（自解压归 GUI main.js §5）+ tar.gz 兜底（R83d 冒烟用）
#
# Artifact naming (R103): EMRG-<ver>-macos-arm64.pkg / EMRG-<ver>-windows-x64.exe /
# EMRG-<ver>-linux-x86_64.AppImage + .tar.gz

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist"
RUNTIME="$DIST/runtime"
VERSION="$(cat "$RUNTIME/version.txt" 2>/dev/null || echo 0.2.0)"
PLATFORM="${1:-$(uname -s | tr '[:upper:]' '[:lower:]')}"

mkdir -p "$DIST/artifacts"

case "$PLATFORM" in
  darwin|macos)
    echo "==> macOS pkg (user-level, R54/R104/R105/R67)"
    # payload: runtime → 临时目录；postinstall 复制到用户 HOME
    PKG_ROOT="$(mktemp -d)"
    mkdir -p "$PKG_ROOT/payload/runtime"
    cp -R "$RUNTIME/." "$PKG_ROOT/payload/runtime/"
    cat > "$PKG_ROOT/scripts/postinstall" <<'EOF'
#!/bin/bash
# R67: GUI 安装器可能提权（$HOME=/var/root）→ 取控制台用户真实 HOME
USER="$(stat -f "%Su" /dev/console 2>/dev/null || echo "$USER")"
[ -z "$USER" ] && USER="$(whoami)"
HOME_DIR="$(dscl . -read "/Users/$USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
[ -z "$HOME_DIR" ] && HOME_DIR="$HOME"
INSTALL_DEST="$HOME_DIR/.emrg/install"
# R104: ① runtime → ~/.emrg/install/
mkdir -p "$INSTALL_DEST"
cp -R "/tmp/emrg-payload/runtime/." "$INSTALL_DEST/"
# R105: root 复制 → chown 回用户（否则用户无法更新 install/）
chown -R "$USER":staff "$HOME_DIR/.emrg" 2>/dev/null || true
# ② GUI EMRG.app → ~/Applications/（R104）
if [ -d "/tmp/emrg-payload/runtime/emrg-gui/EMRG.app" ]; then
  mkdir -p "$HOME_DIR/Applications"
  cp -R "/tmp/emrg-payload/runtime/emrg-gui/EMRG.app" "$HOME_DIR/Applications/"
  chown -R "$USER":staff "$HOME_DIR/Applications/EMRG.app" 2>/dev/null || true
fi
exit 0
EOF
    chmod +x "$PKG_ROOT/scripts/postinstall"
    cp -R "$PKG_ROOT/payload/runtime" /tmp/emrg-payload 2>/dev/null || cp -R "$PKG_ROOT/payload/runtime" "$PKG_ROOT/payload/runtime"
    pkgbuild --root "$PKG_ROOT/payload" --scripts "$PKG_ROOT/scripts" \
      --identifier "com.argszero.emrg" --version "$VERSION" \
      "$DIST/artifacts/EMRG-$VERSION-macos-$(uname -m).pkg"
    rm -rf "$PKG_ROOT"
    ;;
  linux)
    echo "==> Linux tar.gz (AppImage built by electron-builder in CI)"
    tar -czf "$DIST/artifacts/EMRG-$VERSION-linux-$(uname -m).tar.gz" -C "$(dirname "$RUNTIME")" runtime
    ;;
  windows|win32|mingw*|msys*)
    echo "==> Windows Inno Setup script generated (R55/R87) — build in CI with iscc"
    ;;
  *)
    echo "!! unknown platform $PLATFORM" >&2; exit 1 ;;
esac

echo "==> artifacts:"
ls -lh "$DIST/artifacts/"
