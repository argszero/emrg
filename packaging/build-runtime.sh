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
# R69：standalone 位于 `uv python dir`（R45 实测目录名 cpython-3.13.9-<platform>-<arch>-none/）；
# `uv python find` 在 venv 存在时返回 venv 路径，不可用于定位。
PY_ROOT="$(uv python dir)/cpython-3.13.9-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)-none"
if [ ! -x "$PY_ROOT/bin/python3.13" ]; then
  # 动态 glob 兜底（架构名差异）
  PY_ROOT="$(ls -d "$(uv python dir)"/cpython-3.13.9-* | head -1)"
fi
# R45：整目录复制（只复制 bin/python3.13 会缺 lib/libpython3.13.dylib）
cp -R "$PY_ROOT/." "$DIST/bin/python-dist/"

# ── 2. bin/python 软链（R82：相对软链；python-dist 与软链同在 bin/ 下）──
(
  cd "$DIST/bin"
  ln -sfn python-dist/bin/python3.13 python
  ln -sfn python-dist/bin/python3.13 python3
  cp "$ROOT/bin/emrg" emrg
  cp "$ROOT/bin/emrgd" emrgd
  cp "$ROOT/bin/emrg.cmd" emrg.cmd 2>/dev/null || true
  cp "$ROOT/bin/emrgd.cmd" emrgd.cmd 2>/dev/null || true
  cp "$ROOT/bin/emrg-uninstall" emrg-uninstall
  chmod +x emrg emrgd emrg-uninstall
)

# ── 3. source/（只读，排除 gui node_modules R14）──
echo "==> copying source"
(cd "$ROOT" && tar --exclude='emrg/gui/node_modules' --exclude='emrg/gui/dist' \
  --exclude='.git' --exclude='dist' --exclude='.venv' \
  -cf - emrg bin | (cd "$DIST/source" && tar -xf -))
touch "$DIST/source/py.typed"

# ── 4. lib/（pip --target 全量含传递依赖，R3/R43）──
echo "==> installing deps to lib/"
"$DIST/bin/python" -m pip install --quiet --target "$DIST/lib" \
  rich httpx pyyaml jinja2 websockets

# ── 5. assets/LICENSE/version ──
cp "$ROOT/LICENSE" "$DIST/assets/LICENSE" 2>/dev/null || echo "Apache-2.0" > "$DIST/assets/LICENSE"
"$DIST/bin/python" -c "import emrg,sys; sys.path.insert(0,'$DIST/source'); import emrg as e; print(e.__version__)" > "$DIST/version.txt" 2>/dev/null \
  || echo "0.2.0" > "$DIST/version.txt"

echo "==> runtime size: $(du -sh "$DIST" | cut -f1)"
echo "==> runtime built OK"
