#!/usr/bin/env bash
# EMRG — Linux .run self-extracting offline installer builder
# (rant 2026-08-17T10:16:54: Linux headless server offline one-click install)
#
# Produces dist/artifacts/EMRG-<ver>-linux-<arch>.run
#
# Structure (classic self-extracting archive):
#   #!/usr/bin/env bash   ← installer header script (this file's heredoc)
#   __EMRG_PAYLOAD_MARKER__  ← single marker line
#   <runtime.tar.gz payload> ← tarball of the runtime/ directory
#
# The header script is pure bash + standard coreutils (grep/tail/tar/mkdir/ln)
# — zero external dependencies, zero network at install time (the payload is
# the fully offline runtime: bundled CPython + source + lib deps + launchers).
#
# Idempotent/upgrade: re-running overwrites the previous install (same
# semantics as the AppImage self-extract), symlinks are refreshed.
#
# Usage: bash packaging/make-run-installer.sh
#   (needs dist/runtime/ built by build-runtime.sh first)
#   ARCH=x86_64|aarch64 env override (default: uname -m)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist"
RUNTIME="$DIST/runtime"
VERSION="$(cat "$RUNTIME/version.txt" 2>/dev/null || echo 0.2.71)"
ARCH="${ARCH:-$(uname -m)}"   # x86_64 / aarch64
OUT="$DIST/artifacts/EMRG-$VERSION-linux-$ARCH.run"

if [ ! -d "$RUNTIME" ]; then
  echo "error: runtime not built — run bash packaging/build-runtime.sh first" >&2
  exit 1
fi
mkdir -p "$DIST/artifacts"

PAYLOAD="$(mktemp)"
HEADER="$(mktemp)"
trap 'rm -f "$PAYLOAD" "$HEADER"' EXIT

# ── payload: same layout as the tar.gz artifact (runtime/ dir) ──
echo "==> packing runtime payload"
tar -czf "$PAYLOAD" -C "$(dirname "$RUNTIME")" runtime

# ── installer header (VERSION injected via placeholder) ──
cat > "$HEADER" <<'HEADER_EOF'
#!/usr/bin/env bash
# EMRG __EMRG_VERSION__ — Linux self-extracting offline installer
# One command, fully offline (bundled Python 3.13 + deps + launchers).
# Works on headless servers (SSH, no desktop, no uv/gh, normal user).
set -euo pipefail

VERSION="__EMRG_VERSION__"
# Default from HOME unconditionally — never inherit an ambient PREFIX env var
# (common on build servers / containers: make install PREFIX=..., SDKs, CI
# images). --prefix= is the only override.
PREFIX="$HOME/.emrg/install"
WRITE_PROFILE=1
START=0

usage() {
  cat <<'EOF'
Usage: ./EMRG-<ver>-linux-<arch>.run [options]

One-command offline install of EMRG (bundled Python + deps, no network).

Options:
  --prefix=<dir>   Install directory (default: ~/.emrg/install)
  --no-profile     Do not append ~/.local/bin to the shell profile PATH
  --start          Start the background daemon (emrgd) after install
  -h, --help       Show this help

After install:
  emrg  → ~/.local/bin/emrg   (TUI; ~/.local/bin added to PATH in ~/.bashrc
  emrgd → ~/.local/bin/emrgd   when missing — skip with --no-profile)
  uninstall → ~/.emrg/install/bin/emrg-uninstall
EOF
}

for arg in "$@"; do
  case "$arg" in
    --prefix=*) PREFIX="${arg#--prefix=}" ;;
    --no-profile) WRITE_PROFILE=0 ;;
    --start) START=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "error: unknown option: $arg" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# ── self-extract payload (marker line + everything after) ──
# -a: the .run is "binary" (gzip payload) — grep must treat it as text to
# report line numbers (GNU/BSD grep otherwise prints "Binary file ... matches").
MARKER_LINE="$(grep -an '^__EMRG_PAYLOAD_MARKER__$' "$0" | tail -1 | cut -d: -f1 || true)"
if [ -z "${MARKER_LINE:-}" ]; then
  echo "error: corrupt installer (payload marker not found)" >&2
  exit 1
fi

echo "==> EMRG $VERSION — installing to $PREFIX (offline)"
mkdir -p "$PREFIX"
tail -n +$((MARKER_LINE + 1)) "$0" | tar -xzf - -C "$PREFIX" --strip-components=1
chmod +x "$PREFIX/bin/emrg" "$PREFIX/bin/emrgd" "$PREFIX/bin/emrg-uninstall"

# ── symlinks (idempotent: -f refresh) ──
mkdir -p "$HOME/.local/bin"
ln -sfn "$PREFIX/bin/emrg" "$HOME/.local/bin/emrg"
ln -sfn "$PREFIX/bin/emrgd" "$HOME/.local/bin/emrgd"

# ── PATH handling (idempotent dedup; skip with --no-profile) ──
if [ "$WRITE_PROFILE" = "1" ]; then
  RC="$HOME/.bashrc"
  [ -f "$RC" ] || RC="$HOME/.profile"
  if ! printf '%s' "$PATH" | tr ':' '\n' | grep -Fqx "$HOME/.local/bin"; then
    if [ ! -f "$RC" ] || ! grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$RC" 2>/dev/null; then
      {
        echo ''
        echo '# EMRG launcher path'
        echo 'export PATH="$HOME/.local/bin:$PATH"'
      } >> "$RC"
    fi
    echo "==> added $HOME/.local/bin to PATH ($RC) — new shell or: export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
fi

# ── optional daemon start ──
if [ "$START" = "1" ]; then
  echo "==> starting emrgd (daemon)"
  (nohup "$HOME/.local/bin/emrgd" >/dev/null 2>&1 &)
fi

echo "==> EMRG $VERSION installed"
echo "    install  : $PREFIX"
echo "    emrg     : $HOME/.local/bin/emrg   (TUI — run: emrg)"
echo "    emrgd    : $HOME/.local/bin/emrgd  (daemon)"
echo "    uninstall: $PREFIX/bin/emrg-uninstall"
# MUST exit before the payload marker — otherwise bash would keep executing
# the payload bytes as shell commands (classic self-extracting pitfall).
exit 0
HEADER_EOF

# portable in-place sed (GNU: -i; BSD/macOS: -i.bak)
sed -i.bak "s/__EMRG_VERSION__/$VERSION/g" "$HEADER"
rm -f "$HEADER.bak"

# ── assemble .run ──
echo "==> assembling $OUT"
cat "$HEADER" > "$OUT"
printf '__EMRG_PAYLOAD_MARKER__\n' >> "$OUT"
cat "$PAYLOAD" >> "$OUT"
chmod +x "$OUT"

# ── build-time sanity: header must not contain a 2nd marker; payload readable ──
MARKERS="$(grep -ac '^__EMRG_PAYLOAD_MARKER__$' "$OUT" || true)"
if [ "$MARKERS" != "1" ]; then
  echo "error: payload marker count != 1 ($MARKERS) — .run corrupted" >&2
  exit 1
fi
LINE="$(grep -an '^__EMRG_PAYLOAD_MARKER__$' "$OUT" | cut -d: -f1)"
tail -n +$((LINE + 1)) "$OUT" | tar -tzf - >/dev/null
echo "==> $(basename "$OUT") — $(du -h "$OUT" | cut -f1) (payload OK)"
