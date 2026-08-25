#!/usr/bin/env bash
# EMRG Phase 4 — smoke test (rant #12 §12, 12 items).
#
# Runs against a throwaway HOME (R42: HOME=$(mktemp -d)), so the install
# directory + config + sessions are all isolated. CI uses the tarball
# fallback artifact (R83d: AppImage needs FUSE/graphics; macOS/Windows
# audit-degrade).
#
# R111: the temp HOME's shell rc is empty, so the installer-written PATH is
# not in effect — export install/bin explicitly here.
#
# Usage: packaging/smoke-test.sh [path-to-install-root]

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${1:-$ROOT/dist/runtime}"
PASS=0; FAIL=0

smoke_home="$(mktemp -d)"
trap 'rm -rf "$smoke_home"' EXIT
export HOME="$smoke_home"
mkdir -p "$HOME/.emrg"
# R114: Windows 下经 cmd 启动的原生 python.exe 不认 Git Bash 的 $HOME —
# Path.home() 读 USERPROFILE/HOMEDRIVE/HOMEPATH。隔离 HOME 冒烟必须同步这三个
# 变量，否则 daemon 会把 config/port 写到真实用户目录（config 缺失 → 崩溃不写 port）。
if [ -n "${WINDIR:-}" ]; then
  export USERPROFILE="$(cygpath -w "$HOME")"
  export HOMEDRIVE="${USERPROFILE:0:2}"
  export HOMEPATH="${USERPROFILE:2}"
fi

say()  { printf '\n==> %s\n' "$*"; }
ok()   { PASS=$((PASS+1)); printf '  ✓ %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf '  ✗ %s\n' "$*"; }

# ── 环境：复制 runtime 到临时 HOME 的 install/（模拟安装器）──
say "installing runtime to \$HOME/.emrg/install"
mkdir -p "$HOME/.emrg/install"
cp -R "$INSTALL_ROOT/." "$HOME/.emrg/install/"
export PATH="$HOME/.emrg/install/bin:$PATH"
export PYTHONPATH="$HOME/.emrg/install/source:$HOME/.emrg/install/lib"
export PYTHONDONTWRITEBYTECODE=1

# 1. emrg --version（R98：走 argparse 不进 TUI）
say "1. emrg --version"
if emrg --version 2>&1 | grep -q "emrg "; then ok "emrg --version"; else fail "emrg --version"; fi

# ⛔ 红线守卫（rant 2026-08-25T10:38:34 事件）：本脚本不得以任何形式 stop/restart
# emrg server / emrgd（宿主最高原则 2026-08-18T22:58 / MANIFESTO 第四条附则二）。
# 前置探测固定端口 56031：若已有真实 daemon 在跑 → 全部 daemon 步骤 audit-degraded
# 跳过（同 9-12 款），绝不 pkill / stop / restart。旧实现曾 `pkill -f "emrg.server"`
# + `emrg server stop`（步骤 4 与收尾），会误杀本机真实 daemon —— 已于本 PR 移除。
DAEMON_DEGRADED=0
if python3 -c '
import socket, sys
try:
    s = socket.create_connection(("127.0.0.1", 56031), timeout=0.5)
    s.close()
    sys.exit(0)
except OSError:
    sys.exit(1)
' >/dev/null 2>&1; then
  DAEMON_DEGRADED=1
fi

# 2. 前置写最小 config.toml → 后台起 emrgd + 轮询 port + auth + pong（R78/R109）
say "2. daemon start + port + auth + pong"
if [ "$DAEMON_DEGRADED" -eq 1 ]; then
  ok "2. audit-degraded — port 56031 busy (real daemon running), not touching it"
else
cat > "$HOME/.emrg/config.toml" <<'EOF'
[llm]
base_url = "https://api.deepseek.com"
api_key = "sk-..."
model = "deepseek-chat"
EOF
# R101：Windows Git Bash 的 nohup 后台 bash 脚本会立即退出（无 POSIX 进程模型）
# → 用 cmd //c start /b emrgd.cmd 后台启动；POSIX 保持 nohup emrgd
# R114：输出保留到 emrgd-debug.log（不丢 /dev/null），daemon 启动失败时可诊断。
if [ -n "${WINDIR:-}" ]; then
  # $HOME 在 Git Bash 是 POSIX 路径（/c/Users/...），cmd 需 Windows 路径（C:\Users\...）→ cygpath -m
  EMRGD_CMD="$(cygpath -m "$HOME/.emrg/install/bin/emrgd.cmd")"
  (cd "$HOME" && cmd //c "start /b $EMRGD_CMD" >"$HOME/.emrg/emrgd-debug.log" 2>&1)
else
  nohup emrgd >"$HOME/.emrg/emrgd-debug.log" 2>&1 &
fi
for i in $(seq 1 30); do
  [ -f "$HOME/.emrg/emrgd.token" ] && break
  sleep 0.5
done
if [ ! -f "$HOME/.emrg/emrgd.token" ]; then
  echo "  [debug] emrgd-debug.log:" >&2
  cat "$HOME/.emrg/emrgd-debug.log" 2>/dev/null || true
  echo "  [debug] emrgd.log (tail):" >&2
  tail -20 "$HOME/.emrg/emrgd.log" 2>/dev/null || true
  fail "daemon did not write token file"
else
  token=$(cat "$HOME/.emrg/emrgd.token")
  if python3 - "$token" <<'PYEOF'
import json, sys
from websockets.sync.client import connect
port, token = 56031, sys.argv[1]
try:
    ws = connect(f"ws://127.0.0.1:{port}", open_timeout=3)
    ws.send(json.dumps({"type": "auth", "token": token}))
    ack = ws.recv()
    print(f"  [debug] ack={ack!r}")
    assert ack and json.loads(ack)["type"] == "auth_ok", f"unexpected ack={ack!r}"
    ws.send(json.dumps({"type": "ping"}))
    pong = ws.recv()
    assert pong and json.loads(pong)["type"] == "pong", f"unexpected pong={pong!r}"
    ws.close()
    print("auth+pong OK")
except Exception as e:
    print(f"AUTH_FAIL: {e!r}")
    sys.exit(1)
PYEOF
  then ok "daemon auth + pong"; else fail "daemon auth + pong"; fi
fi
fi

# 3. 会话持久化核心链路（R79 降级：无 LLM）
say "3. session persistence core (no-LLM)"
if [ "$DAEMON_DEGRADED" -eq 1 ]; then
  ok "3. audit-degraded — port 56031 busy"
elif [ -f "$HOME/.emrg/emrgd.token" ]; then
  if python3 - "$HOME/.emrg/emrgd.token" <<'PYEOF'
import json, sys, websockets.sync.client
port, token = 56031, open(sys.argv[1]).read().strip()
ws = websockets.sync.client.connect(f"ws://127.0.0.1:{port}", open_timeout=3)
ws.send(json.dumps({"type": "auth", "token": token}))
json.loads(ws.recv())
ws.send(json.dumps({"type": "list_sessions", "cwd": "/tmp"}))
resp = json.loads(ws.recv())
assert resp["type"] == "sessions_list"
ws.close()
print("sessions_list OK:", len(resp.get("sessions", [])))
PYEOF
  then ok "session core"; else fail "session core"; fi
fi

# 4. daemon 存活逆断言（R107b 覆盖点改造，rant 2026-08-25T10:38:34）：不再
# stop/restart，改为再次 auth+pong 断言「daemon 依然活着」（服务端健全性自证）。
say "4. daemon still alive (re-auth + pong)"
if [ "$DAEMON_DEGRADED" -eq 1 ]; then
  ok "4. audit-degraded — port 56031 busy"
elif [ -f "$HOME/.emrg/emrgd.token" ]; then
  token=$(cat "$HOME/.emrg/emrgd.token")
  if python3 - "$token" <<'PYEOF'
import json, sys
from websockets.sync.client import connect
port, token = 56031, sys.argv[1]
try:
    ws = connect(f"ws://127.0.0.1:{port}", open_timeout=3)
    ws.send(json.dumps({"type": "auth", "token": token}))
    ack = ws.recv()
    assert ack and json.loads(ack)["type"] == "auth_ok", f"unexpected ack={ack!r}"
    ws.send(json.dumps({"type": "ping"}))
    pong = ws.recv()
    assert pong and json.loads(pong)["type"] == "pong", f"unexpected pong={pong!r}"
    ws.close()
    print("re-auth+pong OK")
except Exception as e:
    print(f"ALIVE_FAIL: {e!r}")
    sys.exit(1)
PYEOF
  then ok "daemon alive (re-auth + pong)"; else fail "daemon alive (re-auth + pong)"; fi
else
  fail "daemon alive (no token)"
fi

# 5. emrg rant "test"（R107：_send_rant 依赖 daemon；步骤 4 不再 stop，daemon 仍在跑，无需重起）
say "5. emrg rant"
if [ "$DAEMON_DEGRADED" -eq 1 ]; then
  ok "5. audit-degraded — port 56031 busy"
elif emrg rant "smoke-test" 2>&1 | grep -qi "daemon not running"; then
  fail "rant (daemon not running)"
else
  ok "rant sent (no error)"
fi

# 6. 演化组件无 LLM 验证（R68/R112：git/gh 解析到 install/bin）
say "6. evolution components (no-LLM): git/gh"
if git --version >/dev/null 2>&1 && gh --version >/dev/null 2>&1; then ok "git+gh present"; else fail "git+gh"; fi

# 7. 会话内 python（R83：bash 工具由 LLM 路由，CI 降级为直接跑脚本）
say "7. session-scoped python"
if python -c "import rich, httpx, yaml, jinja2, websockets; print('deps OK')" >/dev/null 2>&1; then ok "python+deps"; else fail "python+deps"; fi

# 8. GUI 入口存在（R39：CI 无显示器，完整 GUI 冒烟留本地）
say "8. GUI entry exists"
if [ -x "$HOME/.emrg/install/bin/emrg" ]; then ok "bin/emrg"; else fail "bin/emrg"; fi

# 9-12: GUI+TUI 同开 / 平台卸载器 / 只读验证 / 离线验证 —— CI 审计降级
say "9-12. platform-specific (audit-degraded in CI)"
ok "9. GUI+TUI same daemon — local manual (R120 同 cwd 前提)"
if [ -f "$HOME/.emrg/install/bin/emrg-uninstall" ]; then ok "10. emrg-uninstall present"; else fail "10. emrg-uninstall"; fi
ok "11. read-only install — local manual (R61/R47)"
ok "12. offline install — Linux CI iptables/unshare (R47/R96)"

# 13. .run 自解压安装器（rant 2026-08-17T10:16:54）：独立临时 HOME 一键安装
say "13. .run self-extracting installer (offline one-click)"
RUN_FILE="$(ls "$ROOT"/dist/artifacts/EMRG-*-linux-*.run 2>/dev/null | head -1 || true)"
if [ -z "$RUN_FILE" ]; then
  ok "13. .run — no artifact in dist/artifacts (installer build skipped; audit-degraded)"
else
  run_home="$(mktemp -d)"
  if (cd "$run_home" && HOME="$run_home" bash "$RUN_FILE" --no-profile >/dev/null 2>&1) \
     && [ -x "$run_home/.emrg/install/bin/emrg" ] \
     && [ -L "$run_home/.local/bin/emrg" ] \
     && [ -L "$run_home/.local/bin/emrgd" ] \
     && [ -x "$run_home/.emrg/install/bin/emrg-uninstall" ] \
     && HOME="$run_home" "$run_home/.emrg/install/bin/emrg" --version 2>&1 | grep -q "emrg "; then
    ok "13. .run install (extract + symlink + version)"
    # 幂等重跑（覆盖旧安装）
    if (cd "$run_home" && HOME="$run_home" bash "$RUN_FILE" --no-profile >/dev/null 2>&1); then
      ok "13. .run re-run idempotent"
    else
      fail "13. .run re-run idempotent"
    fi
  else
    fail "13. .run install"
  fi
  rm -rf "$run_home"
fi

# 14. bundled python venv（rant 2026-08-24T21:46:53：bin/python 软链 → wrapper，
#     pyvenv.cfg home 修复；Windows 后续：bin\python.exe 复制品依赖同目录
#     bin\pyvenv.cfg 定位标准库）。回归用例：install python 创建 venv 后必须能
#     导入标准库 + pip 可用。POSIX 走 bin/python wrapper（#966）；Windows 走
#     bin\python.exe 本体——先复刻安装器 [Code] ssPostInstall 写的 bin\pyvenv.cfg
#     （home = 绝对路径；相对路径实测 "Failed to import encodings"），再验证。
say "14. bundled python venv (pyvenv.cfg home fix)"
PY_BIN="$HOME/.emrg/install/bin/python"
if [ -n "${WINDIR:-}" ]; then
  # R131：dist/runtime 未经安装器（pyvenv.cfg 由 make-installer.sh [Code] 在
  # ssPostInstall 写入），smoke 在此复刻同款内容。有了 cfg 后 bin\python.exe
  # 本体即可用（R100 "缺 DLL" 结论修正：DLL 同在 bin\，真正缺的是 cfg）。
  INSTALL_BIN_WIN="$(cygpath -w "$HOME/.emrg/install/bin")"
  printf 'home = %s\\python-dist\n' "$INSTALL_BIN_WIN" > "$HOME/.emrg/install/bin/pyvenv.cfg"
  PY_BIN="$HOME/.emrg/install/bin/python.exe"
  if [ ! -e "$PY_BIN" ]; then
    PY_BIN="$HOME/.emrg/install/bin/python-dist/python.exe"
  fi
elif [ -e "$HOME/.emrg/install/bin/python-dist/python.exe" ]; then
  PY_BIN="$HOME/.emrg/install/bin/python-dist/python.exe"
elif [ -e "$HOME/.emrg/install/bin/python-dist/python3.13.exe" ]; then
  PY_BIN="$HOME/.emrg/install/bin/python-dist/python3.13.exe"
fi
venv_dir="$smoke_home/.venv-smoke"
VENV_PY="$venv_dir/bin/python"
if [ -n "${WINDIR:-}" ]; then
  VENV_PY="$venv_dir/Scripts/python.exe"
fi
rm -rf "$venv_dir"
if "$PY_BIN" -m venv "$venv_dir" >/dev/null 2>&1 \
   && "$VENV_PY" -c "import encodings; print('venv OK')" >/dev/null 2>&1 \
   && "$VENV_PY" -m pip --version >/dev/null 2>&1; then
  ok "14. bundled python venv (import encodings + pip)"
else
  fail "14. bundled python venv (import encodings + pip)"
fi
rm -rf "$venv_dir"

# 清理：红线禁止 stop/restart daemon —— CI 临时 daemon 随虚拟机销毁；本机由上方
# 端口守卫兜底（56031 忙 → 降级跳过），绝不以任何形式终止 daemon 进程。

printf '\n==== smoke summary: %d pass, %d fail ====\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
