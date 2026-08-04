#!/usr/bin/env bash
# EMRG Phase 4 — platform installer wrapper (rant #12 §10/§13).
#
#   macOS:  pkgbuild 用户级（R54）。payload 装临时位置 + postinstall 复制
#           （R104: runtime → ~/.emrg/install/ + GUI EMRG.app → ~/Applications/）
#           R67: postinstall $HOME 陷阱（提权时取控制台用户真实 HOME）
#           R105: root 复制后 chown 回用户
#   Windows: Inno Setup（R55 免 UAC；R97 {%USERPROFILE} 替代 {userhome}——旧版 iscc 不识）
#            GUI win-unpacked → install/emrg-gui/（R97）
#   Linux:  AppImage（自解压归 GUI main.js §5）+ tar.gz 兜底（R83d 冒烟用）。
#           R116: 脚本负责收集 electron-builder 的 AppImage（emrg/gui/dist/*.AppImage）
#           到 dist/artifacts/（PR #391 —— 之前只产 tar.gz，Release 缺 AppImage）。
#
# Artifact naming (R103): EMRG-<ver>-macos-arm64.pkg / EMRG-<ver>-windows-x64.exe /
# EMRG-<ver>-linux-x86_64.AppImage + .tar.gz
#
# Usage: bash packaging/make-installer.sh [darwin|linux|windows]
#   darwin: needs pkgbuild (macOS). GUI from emrg/gui/dist/mac*/EMRG.app
#   windows: needs iscc (Inno Setup 6) + GUI win-unpacked from emrg/gui/dist/win-unpacked
#   linux: tarball of runtime (AppImage built separately by electron-builder)

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
    # GUI 产物（electron-builder dir 输出，arch 目录 mac-arm64 或 mac-x64）
    GUI_APP="$(ls -d "$ROOT"/emrg/gui/dist/mac*/EMRG.app 2>/dev/null | head -1 || true)"
    if [ -z "$GUI_APP" ]; then
      echo "!! EMRG.app not found (run: cd emrg/gui && npm run dist first)" >&2
      exit 1
    fi
    PKG_ROOT="$(mktemp -d)"
    mkdir -p "$PKG_ROOT/payload/runtime" "$PKG_ROOT/payload/runtime/emrg-gui"
    cp -R "$RUNTIME/." "$PKG_ROOT/payload/runtime/"
    cp -R "$GUI_APP" "$PKG_ROOT/payload/runtime/emrg-gui/EMRG.app"
    # 生成"卸载 EMRG.app"（R30/R31/R102：bash 包装调 emrg-uninstall + 删主 GUI + 提示拖废纸篓）
    UNINSTALL_APP="$PKG_ROOT/payload/runtime/emrg-gui/卸载 EMRG.app"
    mkdir -p "$UNINSTALL_APP/Contents/MacOS"
    cat > "$UNINSTALL_APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>uninstall</string>
  <key>CFBundleIdentifier</key><string>com.argszero.emrg.uninstall</string>
  <key>CFBundleName</key><string>卸载 EMRG</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
EOF
    cat > "$UNINSTALL_APP/Contents/MacOS/uninstall" <<'EOF'
#!/bin/bash
# 卸载 EMRG（R30/R31/R102）——bash 包装，双击运行
# 调统一卸载脚本 emrg-uninstall（R58）→ 删主 GUI → 提示拖本 app 进废纸篓
set +e
USER="$(stat -f "%Su" /dev/console 2>/dev/null || echo "$USER")"
HOME_DIR="$(dscl . -read "/Users/$USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
[ -z "$HOME_DIR" ] && HOME_DIR="$HOME"
UNINST="$HOME_DIR/.emrg/install/bin/emrg-uninstall"
if [ -x "$UNINST" ]; then
  # R15：卸载脚本需自设 PYTHONPATH
  export PYTHONPATH="$HOME_DIR/.emrg/install/source:$HOME_DIR/.emrg/install/lib"
  "$UNINST" 2>&1
fi
# R102：删主 GUI（未运行时可直接删）
rm -rf "$HOME_DIR/Applications/EMRG.app" 2>/dev/null
osascript -e 'display dialog "EMRG 已卸载。请将「卸载 EMRG」图标拖入废纸篓以完成删除。" buttons {"好"} default button 1' 2>/dev/null || \
  echo "EMRG 已卸载。请将「卸载 EMRG」图标拖入废纸篓。"
exit 0
EOF
    chmod +x "$UNINSTALL_APP/Contents/MacOS/uninstall"
    mkdir -p "$PKG_ROOT/scripts"
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
# ③ 卸载 EMRG.app（R30：pkg 无原生卸载器，放置卸载 app）
if [ -d "/tmp/emrg-payload/runtime/emrg-gui/卸载 EMRG.app" ]; then
  cp -R "/tmp/emrg-payload/runtime/emrg-gui/卸载 EMRG.app" "$HOME_DIR/Applications/"
  chown -R "$USER":staff "$HOME_DIR/Applications/卸载 EMRG.app" 2>/dev/null || true
fi
exit 0
EOF
    chmod +x "$PKG_ROOT/scripts/postinstall"
    rm -rf /tmp/emrg-payload
    cp -R "$PKG_ROOT/payload/runtime" /tmp/emrg-payload
    pkgbuild --root "$PKG_ROOT/payload" --scripts "$PKG_ROOT/scripts" \
      --identifier "com.argszero.emrg" --version "$VERSION" \
      "$DIST/artifacts/EMRG-$VERSION-macos-$(uname -m).pkg"
    rm -rf "$PKG_ROOT" /tmp/emrg-payload
    ;;

  linux)
    echo "==> Linux tar.gz + AppImage (AppImage built by electron-builder, collected R116)"
    tar -czf "$DIST/artifacts/EMRG-$VERSION-linux-$(uname -m).tar.gz" -C "$(dirname "$RUNTIME")" runtime
    # R116: 收集 electron-builder 产出的 AppImage（emrg/gui/dist/*.AppImage）到
    # dist/artifacts/ —— 之前只生成 tar.gz，release 缺 linux AppImage（rant #13 Step 5）。
    # electron-builder 命名：<productName>-<version>-<arch>.AppImage（x86_64 / arm64）。
    APPIMAGE="$(ls "$ROOT"/emrg/gui/dist/*.AppImage 2>/dev/null | head -1 || true)"
    if [ -n "$APPIMAGE" ]; then
      cp "$APPIMAGE" "$DIST/artifacts/EMRG-$VERSION-linux-$(uname -m).AppImage"
      echo "==> AppImage collected: $(basename "$APPIMAGE")"
    else
      echo "!! AppImage not found in emrg/gui/dist — Linux release 缺 AppImage（有 tar.gz 兜底）" >&2
    fi
    ;;

  windows|win32|mingw*|msys*)
    echo "==> Windows Inno Setup (R55/R87/R97)"
    ISCC="${ISCC:-iscc}"
    if ! command -v "$ISCC" >/dev/null 2>&1; then
      echo "!! iscc (Inno Setup 6) not found — set ISCC path or install" >&2
      exit 1
    fi
    WIN_UNPACKED="$ROOT/emrg/gui/dist/win-unpacked"
    if [ ! -d "$WIN_UNPACKED" ]; then
      echo "!! win-unpacked not found (run: cd emrg/gui && npm run dist first)" >&2
      exit 1
    fi
    # 组装 Inno payload：runtime + GUI → staging（Inno 安装到 %USERPROFILE%\.emrg\install）
    STAGE="$(mktemp -d)"
    # R98：iscc 是 Windows 原生程序，读不懂 Git Bash POSIX 路径（/d/a/emrg/...）
    # → cygpath -m 转 Windows 路径（正斜杠，iscc 可读；cygpath 仅 Git Bash 有，兜底保持原样）
    if command -v cygpath >/dev/null 2>&1; then
      ROOT_WIN="$(cygpath -m "$ROOT")"
      DIST_WIN="$(cygpath -m "$DIST")"
      STAGE_WIN="$(cygpath -m "$STAGE")"
    else
      ROOT_WIN="$ROOT"; DIST_WIN="$DIST"; STAGE_WIN="$STAGE"
    fi
    mkdir -p "$STAGE/payload"
    cp -R "$RUNTIME/." "$STAGE/payload/"
    mkdir -p "$STAGE/payload/emrg-gui"
    cp -R "$WIN_UNPACKED" "$STAGE/payload/emrg-gui/EMRG"
    cat > "$STAGE/emrg.iss" <<EOF
; EMRG Inno Setup script — user-level install (R55: PrivilegesRequired=lowest)
; R97: {userhome} 需 Inno 6.1+（runner 5.x 编译失败）→ 用 {%USERPROFILE}（全版本支持，与 Registry 段一致）
#define MyAppName "EMRG"
#define MyAppVersion "$VERSION"
#define MyAppId "com.argszero.emrg"
[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={%USERPROFILE}\\.emrg\\install
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DisableProgramGroupPage=yes
OutputDir=$DIST_WIN/artifacts
OutputBaseFilename=EMRG-$VERSION-windows-x64
SetupIconFile=$ROOT_WIN/packaging/assets/icon.ico
UninstallDisplayIcon={app}\\bin\\emrg.cmd
Compression=lzma2
SolidCompression=yes
[Files]
Source: "$STAGE_WIN/payload\\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
[Icons]
Name: "{userprograms}\\EMRG"; Filename: "{app}\\emrg-gui\\EMRG\\EMRG.exe"; IconFilename: "{app}\\emrg-gui\\EMRG\\EMRG.exe"
[UninstallRun]
Filename: "{app}\\bin\\python.exe"; Parameters: "{app}\\bin\\emrg-uninstall"; Flags: runhidden
[Registry]
; R27: 用户级 PATH（确定格式 %USERPROFILE%\\.emrg\\install\\bin，卸载时精确移除）
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; \
  ValueName: "Path"; ValueData: "%USERPROFILE%\\.emrg\\install\\bin;{olddata}"; \
  Check: NeedsPath
[Code]
function NeedsPath: Boolean;
begin
  Result := Pos(LowerCase('%USERPROFILE%\\.emrg\\install\\bin'), LowerCase(GetEnv('Path'))) = 0;
end;
EOF
    # Windows 路径转义（iscc 需要 Windows 路径，但在 bash/msys 下用当前路径）
    echo "    iscc version: $("$ISCC" /? 2>&1 | head -1)"
    "$ISCC" "$STAGE/emrg.iss" >/dev/null
    rm -rf "$STAGE"
    ;;

  *)
    echo "!! unknown platform $PLATFORM" >&2; exit 1 ;;
esac

echo "==> artifacts:"
ls -lh "$DIST/artifacts/"
