#!/usr/bin/env bash
# EMRG Phase 4 — platform installer wrapper (rant #12 §10/§13).
#
#   macOS:  用户级 pkg（R54/R126）。pkgbuild --install-location '/.emrg/install'
#           + distribution currentUserHome 域 → pkg 引擎直接装 ~/.emrg/install/
#           （R104: GUI EMRG.app → ~/Applications/，postinstall 复制）
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
VERSION="$(cat "$RUNTIME/version.txt" 2>/dev/null || echo 0.2.50)"
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
    # R126: payload 不再套 runtime/ 层 —— 直接放 runtime 内容。
    # pkgbuild --install-location '/.emrg/install' + distribution currentUserHome 域
    # → pkg 引擎直接装到 ~/.emrg/install/（rant 2026-08-05T18:45:35）
    mkdir -p "$PKG_ROOT/payload/emrg-gui"
    cp -R "$RUNTIME/." "$PKG_ROOT/payload/"
    cp -R "$GUI_APP" "$PKG_ROOT/payload/emrg-gui/EMRG.app"
    # macOS 公证要求 pkg 内所有 Mach-O 二进制都有 Developer ID 签名 + 时间戳 +
    # hardened runtime（第 10 次构建教训：Python runtime 的 .so/dylib 未签名 →
    # notarytool Invalid statusCode 4000，12 个文件报三类错：
    #   "not signed with a valid Developer ID certificate"
    #   "does not include a secure timestamp"
    #   "does not have the hardened runtime enabled"）。
    # 对 payload 内除 EMRG.app（electron-builder 已签）外的所有 Mach-O 签名。
    # --options runtime = hardened runtime（缺它会触发第三类错误）；实测 OK。
    if [ "$(uname -s)" = "Darwin" ] && command -v codesign >/dev/null; then
      SIGN_ID="$(security find-identity -v -p codesigning 2>/dev/null | grep 'Developer ID Application' | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
      if [ -n "$SIGN_ID" ]; then
        echo "==> codesign runtime binaries（公证要求：Developer ID + timestamp + hardened runtime）"
        # 收集 payload 内所有 Mach-O（.so / 无扩展名可执行 / Python 解释器）
        # 排除 EMRG.app（electron-builder 已签）与"卸载 EMRG.app"（纯 bash 脚本无 Mach-O）
        find "$PKG_ROOT/payload" -path '*EMRG.app' -prune -o -path '*卸载 EMRG.app' -prune -o \
          -type f \( -name '*.so' -o -name '*.dylib' -o -name 'python*' -o -name 'emrgd' -o -name 'emrg' -o -name 'emrg-uninstall' \) \
          -exec file {} + 2>/dev/null | grep 'Mach-O' | grep -v 'for architecture' | cut -d: -f1 | sort -u | while read -r BIN; do
          codesign --force --timestamp --options runtime --sign "$SIGN_ID" "$BIN" 2>/dev/null && echo "    ✓ $(basename "$BIN")" || echo "    ✗ 跳过 $(basename "$BIN")"
        done
      else
        echo "!! 未找到 Developer ID Application 身份，跳过 runtime codesign（公证可能失败）" >&2
      fi
    fi
    # 生成"卸载 EMRG.app"（R30/R31/R102：bash 包装调 emrg-uninstall + 删主 GUI + 提示拖废纸篓）
    UNINSTALL_APP="$PKG_ROOT/payload/emrg-gui/卸载 EMRG.app"
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
# R126: pkg 引擎已把 payload 直接装到用户 home（currentUserHome 域），
# postinstall 不再从 /tmp 复制（旧实现依赖构建机路径 /tmp/emrg-payload，
# 用户机不存在 → 空安装；pkgbuild 未指定 --install-location → 默认装系统宗卷）。
# R67: GUI 安装器可能提权（$HOME=/var/root）→ 取控制台用户真实 HOME
USER="$(stat -f "%Su" /dev/console 2>/dev/null || echo "$USER")"
[ -z "$USER" ] && USER="$(whoami)"
HOME_DIR="$(dscl . -read "/Users/$USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
[ -z "$HOME_DIR" ] && HOME_DIR="$HOME"
# $2 = 安装器传入的真实安装位置（currentUserHome 域下 = <home>/.emrg/install）
INSTALL_DEST="${2:-$HOME_DIR/.emrg/install}"
# R105: root 复制 → chown 回用户（否则用户无法更新 install/）
chown -R "$USER":staff "$HOME_DIR/.emrg" 2>/dev/null || true
# ② GUI EMRG.app → ~/Applications/（R104）
if [ -d "$INSTALL_DEST/emrg-gui/EMRG.app" ]; then
  mkdir -p "$HOME_DIR/Applications"
  cp -R "$INSTALL_DEST/emrg-gui/EMRG.app" "$HOME_DIR/Applications/"
  chown -R "$USER":staff "$HOME_DIR/Applications/EMRG.app" 2>/dev/null || true
fi
# ③ 卸载 EMRG.app（R30：pkg 无原生卸载器，放置卸载 app）
if [ -d "$INSTALL_DEST/emrg-gui/卸载 EMRG.app" ]; then
  cp -R "$INSTALL_DEST/emrg-gui/卸载 EMRG.app" "$HOME_DIR/Applications/"
  chown -R "$USER":staff "$HOME_DIR/Applications/卸载 EMRG.app" 2>/dev/null || true
fi
# ④ PATH anchor（R127/R19：安装后 emrg 命令可用 —— rant 2026-08-05T18:45:35 验收项）。
# anchor 标记与 bin/emrg-uninstall clean_environment() 的 PATH_ANCHOR_START/END 完全一致，
# 卸载时按同标记清理（Windows 用 HKCU PATH，Linux AppImage 用 ~/.local/bin 软链，macOS 用 rc）。
# 幂等：任一 rc 已含 anchor 则跳过；写所有存在的 rc（zsh 默认 + bash 兼容），无 rc 兜底建 ~/.zshrc。
ANCHOR_START="# >>> EMRG PATH >>>"
ANCHOR_END="# <<< EMRG PATH <<<"
if ! grep -qsF "$ANCHOR_START" "$HOME_DIR/.zshrc" "$HOME_DIR/.bash_profile" "$HOME_DIR/.bashrc" "$HOME_DIR/.profile" 2>/dev/null; then
  for RC in "$HOME_DIR/.zshrc" "$HOME_DIR/.bash_profile" "$HOME_DIR/.bashrc" "$HOME_DIR/.profile"; do
    [ -f "$RC" ] || continue
    printf '\n%s\nexport PATH="$HOME/.emrg/install/bin:$PATH"\n%s\n' "$ANCHOR_START" "$ANCHOR_END" >> "$RC"
    chown "$USER":staff "$RC" 2>/dev/null || true
  done
  if [ ! -f "$HOME_DIR/.zshrc" ]; then
    printf '\n%s\nexport PATH="$HOME/.emrg/install/bin:$PATH"\n%s\n' "$ANCHOR_START" "$ANCHOR_END" >> "$HOME_DIR/.zshrc"
    chown "$USER":staff "$HOME_DIR/.zshrc" 2>/dev/null || true
  fi
fi
exit 0
EOF
    chmod +x "$PKG_ROOT/scripts/postinstall"
    # 组件包：--install-location '/.emrg/install'（currentUserHome 域下相对 home 锚点，
    # 解析为 ~/.emrg/install/，不再落到系统宗卷 /）
    pkgbuild --root "$PKG_ROOT/payload" --scripts "$PKG_ROOT/scripts" \
      --identifier "com.argszero.emrg" --version "$VERSION" \
      --install-location '/.emrg/install' \
      "$PKG_ROOT/EMRG-component.pkg"
    # distribution：仅当前用户 home 域（enable_currentUserHome=true, localSystem=false）
    # → 安装器显示"仅当前用户"，装到 ~/.emrg/install/，不要求系统卷权限
    cat > "$PKG_ROOT/distribution.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<installer-gui-script minSpecVersion="1">
  <domains enable_currentUserHome="true" enable_localSystem="false"/>
  <options customize="never" require-scripts="true"/>
  <choices-outline><line choice="default"/></choices-outline>
  <choice id="default" visible="false"><pkg-ref id="com.argszero.emrg"/></choice>
  <pkg-ref id="com.argszero.emrg" version="$VERSION" onConclusion="none">EMRG-component.pkg</pkg-ref>
</installer-gui-script>
EOF
    productbuild --distribution "$PKG_ROOT/distribution.xml" --package-path "$PKG_ROOT" \
      "$DIST/artifacts/EMRG-$VERSION-macos-$(uname -m).pkg"
    rm -rf "$PKG_ROOT"
    ;;

  linux)
    echo "==> Linux tar.gz + AppImage + .run (AppImage built by electron-builder, collected R116)"
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
    # .run 自解压离线安装器（rant 2026-08-17T10:16:54）：Linux 无头服务器
    # SSH/无桌面/普通用户场景一键安装。payload = 同一 runtime/ 目录，零网络。
    bash "$ROOT/packaging/make-run-installer.sh"
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
; R118: ChangesEnvironment=yes — 写 HKCU\Environment\Path 后广播 WM_SETTINGCHANGE，
; 否则 explorer 不刷新环境变量缓存，新开 cmd 也看不到更新后的 PATH（rant 2026-08-05T14:28:02）
ChangesEnvironment=yes
DisableProgramGroupPage=yes
; R125: CloseApplications=no — Inno Restart Manager (CloseApplications=yes default) 会误报
; 任何占用 install 目录文件的非 EMRG 进程（sh/vim/explorer/Defender）弹 "unable to automatically
; close all applications" 选择框，且 Try again 反复失败；EMRG 进程关闭由 R130 stop_all.py 精确负责
CloseApplications=no
OutputDir=$DIST_WIN/artifacts
OutputBaseFilename=EMRG-$VERSION-windows-x64
SetupIconFile=$ROOT_WIN/packaging/assets/icon.ico
UninstallDisplayIcon={app}\\bin\\emrg.cmd
Compression=lzma2
SolidCompression=yes
[Files]
Source: "$STAGE_WIN/payload\\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; R130: dontcopy — 供 [Code] PrepareToInstall 在覆盖文件前 ExtractTemporaryFile 取出
; bin\stop_all.py 并用 runtime python 执行（升级安装前优雅关闭 GUI/TUI/daemon/bundled
; git，rant 2026-08-17T10:32:27：停止逻辑全部收敛到 Python，stop-emrg.cmd 已删除）。
; stop_all.py 是纯标准库单文件（emrg/_stop_all.py 的副本），不依赖 emrg 包可导入。
; 正常安装时该文件仍由上方通配符装入 {app}\bin\stop_all.py。
Source: "$STAGE_WIN/payload\\bin\\stop_all.py"; DestDir: "{tmp}"; Flags: dontcopy
[Icons]
Name: "{userprograms}\\EMRG"; Filename: "{app}\\emrg-gui\\EMRG\\EMRG.exe"; IconFilename: "{app}\\emrg-gui\\EMRG\\EMRG.exe"
[UninstallRun]
Filename: "{app}\\bin\\python.exe"; Parameters: "{app}\\bin\\emrg-uninstall"; Flags: runhidden
[UninstallDelete]
; R121: emrg-uninstall 脚本退出后（python.exe 已退出，无文件锁），强制删除
; {app}（install/）— 兜底卸载彻底（rant 2026-08-05T15:35:17）
Type: filesandordirs; Name: "{app}"
[Code]
{ R120: HWND_BROADCAST 为 iscc 预定义常量（Compiler.ScriptFunc.pas RegisterConst），
  显式定义会报 Duplicate identifier 'HWND_BROADCAST'（v0.2.2 CI 二次失败）。
  WM_SETTINGCHANGE / SMTO_ABORTIFHUNG 未预置，需保留 const 定义。 }
const
  WM_SETTINGCHANGE = 26;        { \$001A }
  SMTO_ABORTIFHUNG = 2;         { \$0002 }

{ R122: WPARAM/LPARAM 类型在 Inno Setup 6.7.2+ 才加入 iscc 预置
  （issrc commit 27bce18660, 2025-12-27）；runner windows-2025 为 6.7.1 →
  Unknown type 'WPARAM'（v0.2.2 CI 三次失败）。改用 DWORD（6.7.1 已注册
  = LongWord）。iscc 生成 32 位安装器，WPARAM/LPARAM 在 32 位下即 4 字节，
  与 DWORD 完全兼容。 }
function SendMessageTimeout(hWnd: HWND; Msg: UINT; wParam: DWORD; lParam: DWORD;
  fuFlags: UINT; uTimeout: UINT; var lpdwResult: DWORD): BOOL;
  external 'SendMessageTimeoutW@user32.dll stdcall';

{ R119: PATH 段精确匹配（大小写不敏感，段边界防 C:\Users 与 c:\users 误判） }
function PathHasSegment(const PathEnv, Segment: string): Boolean;
begin
  Result := Pos(';' + UpperCase(Segment) + ';', ';' + UpperCase(PathEnv) + ';') > 0;
end;

{ R119: 从 PATH 中移除指定段，保留其余段与相对顺序 }
function PathRemoveSegment(const PathEnv, Segment: string): string;
var
  i, StartPos: Integer;
  Part: string;
begin
  Result := '';
  StartPos := 1;
  for i := 1 to Length(PathEnv) + 1 do
  begin
    if (i > Length(PathEnv)) or (PathEnv[i] = ';') then
    begin
      Part := Copy(PathEnv, StartPos, i - StartPos);
      if (Part <> '') and (CompareText(Part, Segment) <> 0) then
      begin
        if Result <> '' then
          Result := Result + ';';
        Result := Result + Part;
      end;
      StartPos := i + 1;
    end;
  end;
end;

procedure BroadcastEnvironmentChange;
var
  Dummy: DWORD;
begin
  SendMessageTimeout(HWND_BROADCAST, WM_SETTINGCHANGE, 0, 0,
    SMTO_ABORTIFHUNG, 5000, Dummy);
end;

// R123: 以下注释用 // 行注释（Inno Pascal 块注释 { } 不支持嵌套，
// 内含 {app}/{olddata} 的 } 会提前终止注释块 → Syntax error，v0.2.2 CI 四次失败）。
// R119: 安装后把 {app}\bin 加入 HKCU 用户 PATH。
//   旧实现（R27 [Registry]{olddata} + NeedsPath）缺陷：
//   1. NeedsPath 用未展开的字面 %USERPROFILE%\... 与已展开的 GetEnv('Path')
//      比较 → 永不匹配 → 重复安装 PATH 段累积；
//   2. ValueData 写死 %USERPROFILE%\...\bin → 用户自定义安装目录时 PATH 指向错误；
//   3. {olddata} 依赖值已存在，HKCU Path 缺失（Server/精简镜像）时行为不确定。
//   R119 改 [Code] 显式读写：展开 {app}\bin 真实路径 + 段边界去重 + 值缺失时创建。
procedure AddBinDirToPath;
var
  BinPath, OldPath, NewPath: string;
begin
  BinPath := ExpandConstant('{app}\bin');
  if RegQueryStringValue(HKCU, 'Environment', 'Path', OldPath) then
  begin
    if PathHasSegment(OldPath, BinPath) then
      Exit;
    NewPath := BinPath + ';' + OldPath;
  end
  else
    NewPath := BinPath;
  RegWriteExpandStringValue(HKCU, 'Environment', 'Path', NewPath);
  BroadcastEnvironmentChange;
end;

// R119: 卸载后从 HKCU 用户 PATH 移除 {app}\bin
procedure RemoveBinDirFromPath;
var
  BinPath, PathEnv: string;
begin
  BinPath := ExpandConstant('{app}\bin');
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', PathEnv) then
    Exit;
  if not PathHasSegment(PathEnv, BinPath) then
    Exit;
  PathEnv := PathRemoveSegment(PathEnv, BinPath);
  RegWriteExpandStringValue(HKCU, 'Environment', 'Path', PathEnv);
  BroadcastEnvironmentChange;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    AddBinDirToPath;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveBinDirFromPath;
end;

// R130: 升级安装前优雅关闭运行中的 EMRG 进程（rant 2026-08-10T08:50:44 起，
// 2026-08-17T10:32:27 收敛：stop-emrg.cmd 删除，全部停止逻辑进 emrg/_stop_all.py）——
// Inno CloseApplications 看不到无窗口的 pythonw daemon（emrgd.cmd → pythonw.exe
// -m emrg.server 常驻锁文件），覆盖 ~/.emrg\install 时卡在"停止已有进程"。
// PrepareToInstall 在安装开始前用 runtime 的 python 运行 bin\stop_all.py：
// ws 协议关闭 daemon → emrgd.pid 兜底 → taskkill /F、taskkill EMRG.exe 优雅→/F、
// CIM 命令行过滤 TUI、install\git\ 前缀连坐强杀 bundled git、verify 残留检查；
// 有残留 exit 1（脚本打印残留清单）→ 中止安装。干净安装（无旧 install）直接跳过。
// {app} 是旧版安装目录——不能依赖旧版 emrg 命令（可能无 stop 子命令），所以用
// 单文件脚本 + runtime python（{app}\bin\python-dist\python.exe，R90 布局，
// 与 DLL 同目录；回退 python3.13.exe，与 emrg.cmd 探测链一致）。
// 返回非空字符串 = 中止安装并显示该消息（宁可中止也不卡死）。
// 重定向必须经 {cmd}（cmd.exe）——CreateProcess 不解释 > 重定向，直接 Exec
// python.exe 会把 ">" "log" 2>&1 当脚本参数静默吞掉（R125 日志展示失效）。
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  PythonExe: string;
  StopScript: string;
  LogFile: string;
  LogText: AnsiString;
begin
  Result := '';
  // 干净安装：{app} 尚不存在或旧版无 python-dist 布局 → 无进程需停止
  PythonExe := ExpandConstant('{app}\bin\python-dist\python.exe');
  if not FileExists(PythonExe) then
    PythonExe := ExpandConstant('{app}\bin\python-dist\python3.13.exe');
  if not FileExists(PythonExe) then
    Exit;
  ExtractTemporaryFile('stop_all.py');
  StopScript := ExpandConstant('{tmp}\stop_all.py');
  // R125: rant 2026-08-13T09:24:37 — 输出重定向到日志，失败时直接展示日志内容
  // （列出杀不掉的进程），宿主不再需要手动跑诊断。
  // cmd 引号嵌套：外层 /c "..."，内层脚本路径用双引号包裹，重定向在外、
  // 仍在内层引号外（SW_HIDE 隐藏窗口后 stdout/stderr 经 > log 2>&1 落盘）。
  // -I（isolated mode，v0.2.50 引入）已移除（rants 2026-08-18T21:13:03 / 21:14:16）：
  // python-build-standalone 定位 stdlib 依赖目录结构 / ._pth 机制，-I 忽略之 →
  // v0.2.50 Windows 安装 "Failed to import encodings" 启动即崩（连 stop_all.log 都没有）。
  // -I 防的"自锁"假设已被证伪：stop_all 的 excluded-chain 已排除自身 + 祖先进程链
  // （从不杀自己），#847 self-held 归属已处理自身锁，且 stop_all 纯标准库从不加载
  // websockets。恢复 v0.2.49 的 "{PythonExe}" "{StopScript}"，无需任何替代 flag。
  LogFile := ExpandConstant('{tmp}\stop_all.log');
  if Exec(ExpandConstant('{cmd}'), '/c ""' + PythonExe + '" "' + StopScript + '" > "' + LogFile + '" 2>&1"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if ResultCode <> 0 then
    begin
      LogText := '';
      // LoadStringFromFile 的 Inno Pascal Script 签名是
      // function LoadStringFromFile(const FileName: String; var S: AnsiString): Boolean;
      // （6.7.1 → 7.x 全版本一致，见 issrc Shared.ScriptFunc.pas）——不存在单参数
      // 字符串返回形式！v0.2.30 Build Release 31661378619 因此编译失败
      // （iscc "Invalid number of parameters"，Test CI 不编译 .iss 未拦住）。
      // 正确用法：out-param 写入 LogText，返回 Boolean 表示成功。
      // 注意：本注释位于未加引号 heredoc 内，禁用反引号与命令替换语法
      // （iscc compile gate 实证捕获），以免破坏 .iss 渲染。
      if FileExists(LogFile) then
        LoadStringFromFile(LogFile, LogText);
      if Length(LogText) > 2000 then
        LogText := Copy(LogText, 1, 2000);
      if LogText <> '' then
        Result := 'EMRG could not stop all running processes (emrg stop exit code ' + IntToStr(ResultCode) + '). Details from the stop script:' + #13#10 + #13#10 + LogText + #13#10 + #13#10 + 'Please close EMRG (GUI/TUI) and retry the install, or restart the computer and retry (a helper process such as the bundled Git may still hold a file lock).'
      else
        Result := 'EMRG could not stop all running processes (emrg stop exit code ' + IntToStr(ResultCode) + '). Please close EMRG (GUI/TUI) and retry the install, or restart the computer and retry (a helper process such as the bundled Git may still hold a file lock).';
    end;
  end
  else
    Result := 'EMRG could not run the process-stop script (stop_all.py). Please close EMRG (GUI/TUI) and retry the install, or restart the computer and retry.';
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
