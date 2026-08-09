#!/usr/bin/env bash
# EMRG Phase 4 — build runtime directory (rant #12 §3/§13).
#
# Builds dist/runtime/ containing:
#   bin/python-dist/   standalone CPython 3.13.9 整目录（含 lib/libpython3.13.dylib）
#   bin/python         软链 → ../python-dist/bin/python3.13（R82 相对软链）
#   bin/python3        软链（同规则）
#   bin/emrg bin/emrgd bin/emrg.cmd bin/emrgd.cmd bin/emrg-uninstall  启动/卸载脚本
#   source/            emrg 源码（只读，排除 gui node_modules）
#   lib/               pip --target 依赖（全量含传递依赖，R3 禁 --no-deps）
#   assets/ LICENSE version.txt
#
# 100% offline at install time (R47): the installer is pure file copy.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist/runtime"
PY_VER="3.13.9"

echo "==> building runtime at $DIST"
rm -rf "$DIST"
mkdir -p "$DIST/bin/python-dist" "$DIST/bin" "$DIST/source" "$DIST/lib" "$DIST/assets"

# ── 1. standalone CPython（uv python install 3.13.9，锁 patch 版 R69）──
echo "==> installing standalone python $PY_VER"
uv python install "$PY_VER" >/dev/null
# R69/R89：standalone 位于 `uv python dir`（R45 实测目录名 cpython-3.13.9-<platform>-<arch>-none/）；
# `uv python find` 在 venv 存在时返回 venv 路径，不可用于定位。
# R89 修复：uname -s 在 Git Bash 返回 MINGW64_NT-*（≠ windows）前缀拼接不可靠；
# uv python dir 在 Windows 返回原生路径（C:\...），Git Bash 中反斜杠 glob 无法匹配
# → 一律 glob 定位 + cygpath 转 POSIX（Windows 仅）+ `|| true` 防 pipefail（set -euo）。
PY_DIR="$(uv python dir)"
if command -v cygpath >/dev/null 2>&1; then
  PY_DIR="$(cygpath -u "$PY_DIR")"
fi
PY_ROOT="$(ls -d "$PY_DIR"/cpython-3.13.9-* 2>/dev/null | head -1 || true)"
if [ -z "$PY_ROOT" ] || [ ! -d "$PY_ROOT" ]; then
  echo "error: cannot locate standalone python under $PY_DIR" >&2
  exit 1
fi
# R45：整目录复制（只复制 bin/python3.13 会缺 lib/libpython3.13.dylib）
cp -R "$PY_ROOT/." "$DIST/bin/python-dist/"

# ── 2. bin/python 软链/复制（R82：相对软链；python-dist 与软链同在 bin/ 下）──
(
  cd "$DIST/bin"
  # Windows 检测：uname -s 在 Git Bash 返回 MINGW64_NT-*（≠ windows），不可靠；
  # 改为文件系统探测——Windows standalone python 布局为 python.exe/python3.13.exe
  # 在根目录（无 bin/），POSIX 布局为 bin/python3.13。探测到 .exe 即走复制分支。
  # `|| true`：POSIX 平台 4 个 .exe 全不存在时 ls 非零，pipefail 下需吞掉。
  PYEXE="$(ls python-dist/python3.13.exe python-dist/python.exe \
               python-dist/bin/python3.13.exe python-dist/bin/python.exe 2>/dev/null | head -1 || true)"
  if [ -n "$PYEXE" ]; then
    # Windows：Git Bash 的 ln -s 需管理员权限（软链创建失败）→ 用复制替代
    cp "$PYEXE" python.exe
    cp "$PYEXE" python3.exe
    # R100：python-build-standalone 的 DLL（python313.dll/vcruntime140*.dll 等）
    # 在 python-dist/ 根目录——bin/python.exe 复制品缺 DLL 不可用（Windows loader
    # 找不到 → 启动失败）。把根目录 *.dll 一并复制到 bin/，使 PATH 里的
    # python 命令（session-scoped 脚本）可用。
    cp python-dist/*.dll . 2>/dev/null || true
  else
    ln -sfn python-dist/bin/python3.13 python
    ln -sfn python-dist/bin/python3.13 python3
  fi
  cp "$ROOT/bin/emrg" emrg
  cp "$ROOT/bin/emrgd" emrgd
  cp "$ROOT/bin/emrg.cmd" emrg.cmd 2>/dev/null || true
  cp "$ROOT/bin/emrgd.cmd" emrgd.cmd 2>/dev/null || true
  cp "$ROOT/bin/emrg-uninstall" emrg-uninstall
  chmod +x emrg emrgd emrg-uninstall
)

# ── 3. source/（只读，排除 gui node_modules R14；⚠️ 只含 emrg/ 源码，不含 bin/ ——
#       启动脚本已在 bin/ 顶层，source 里再放一份是冗余（pkg 双份））──
echo "==> copying source"
(cd "$ROOT" && tar --exclude='emrg/gui/node_modules' --exclude='emrg/gui/dist' \
  --exclude='.git' --exclude='dist' --exclude='.venv' \
  --exclude='emrg/__pycache__' --exclude='emrg/**/__pycache__' --exclude='emrg/**/*.pyc' \
  -cf - emrg | (cd "$DIST/source" && tar -xf -))
touch "$DIST/source/py.typed"
# LICENSE（若存在，否则占位）
if [ -f "$ROOT/LICENSE" ]; then cp "$ROOT/LICENSE" "$DIST/source/LICENSE"; fi

# ── 4. lib/（pip --target 全量含传递依赖，R3/R43）──
echo "==> installing deps to lib/"
# Windows：bin/python.exe 是从 python-dist 复制的复制品，python313.dll 等 DLL
# 仍在 python-dist/ 根目录（Windows loader 找不到 → 启动失败 exit 127）。
# → Windows 直接用 python-dist 根目录的 exe（与 DLL 同目录）；POSIX 用 bin/python 软链。
PY_BIN="$DIST/bin/python"
if [ -e "$DIST/bin/python-dist/python.exe" ]; then
  PY_BIN="$DIST/bin/python-dist/python.exe"
elif [ -e "$DIST/bin/python-dist/python3.13.exe" ]; then
  PY_BIN="$DIST/bin/python-dist/python3.13.exe"
fi
"$PY_BIN" -m pip install --quiet --target "$DIST/lib" \
  rich httpx pyyaml jinja2 websockets

# ── 5. assets/LICENSE/version ──
cp "$ROOT/LICENSE" "$DIST/assets/LICENSE" 2>/dev/null || echo "Apache-2.0" > "$DIST/assets/LICENSE"
# Installable-skills catalog baseline (rant 2026-08-08T10:14:29): ship it in
# the runtime so installed machines have it even before daemon first run.
cp "$ROOT/packaging/assets/skill-catalog.md" "$DIST/assets/skill-catalog.md" 2>/dev/null || true
"$PY_BIN" -c "import emrg,sys; sys.path.insert(0,'$DIST/source'); import emrg as e; print(e.__version__)" > "$DIST/version.txt" 2>/dev/null \
  || echo "0.2.15" > "$DIST/version.txt"

echo "==> runtime size: $(du -sh "$DIST" | cut -f1)"
echo "==> runtime built OK"
