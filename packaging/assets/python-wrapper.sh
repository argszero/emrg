#!/usr/bin/env bash
# EMRG bundled Python wrapper (POSIX) — rant 2026-08-24T21:46:53.
#
# Installed at ~/.emrg/install/bin/python (and python3) in place of the former
# symlink `python -> python-dist/bin/python3.13` (R82).
#
# Why a wrapper instead of a symlink:
#   A symlink makes sys.executable resolve to <install>/bin/python (the symlink
#   dir), while the stdlib actually lives under <install>/bin/python-dist/lib/.
#   `python -m venv` writes pyvenv.cfg `home = <install>/bin` (derived from
#   dirname(sys._base_executable)) and the venv base-executable recompute does
#   not follow the symlink, so the venv python cannot locate the stdlib:
#       Could not find platform independent libraries <prefix>
#       Fatal Python error: Failed to import encodings module
#   → every emrg task that creates a venv in its workspace fails to pip install
#   research deps (numpy/scipy/ILP).
#
# exec'ing the real binary makes sys.executable = <install>/bin/python-dist/bin/
# python3.13, so venv derives the correct home (<install>/bin/python-dist/bin)
# and the created venv works out of the box. exec preserves argv, exit codes and
# signals, so the wrapper is behavior-identical for all other callers.
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  TARGET="$(readlink "$SOURCE")"
  case "$TARGET" in
    /*) SOURCE="$TARGET" ;;
    *) SOURCE="$(dirname "$SOURCE")/$TARGET" ;;
  esac
done
DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
exec "$DIR/python-dist/bin/python3.13" "$@"
