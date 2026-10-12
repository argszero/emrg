// config_mode.js — the permission rule every writer of ~/.emrg/config.toml applies.
//
// The file holds `api_key` in plaintext, so it is private: mode 0o600. Two writers already
// state that as code — `emrg/config.py::CONFIG_FILE_MODE` (applied by `_make_config_private`,
// PR #2143) and `install.sh`'s `chmod 600 "$cfg"` (PR #2147). This module is the GUI's copy of
// the same rule, and it exists because the GUI cannot state it through `fs.writeFileSync`'s
// `mode` option: Node applies that option **only when it creates the file**, so a file an
// earlier install left at 0644 keeps 0644 through every rewrite (measured 2026-10-12, issue
// #2148). `main.js::writeConfig()` is the Save path (`emrg:saveSettings`), i.e. the only path
// the host's packaged GUI has for an existing file — the daemon calls `load_config()`, never
// `ensure_config()`, so nothing else tightens it there.
"use strict";

const fs = require("node:fs");

/** The mode the config file must have — the Python writer's `CONFIG_FILE_MODE`. */
const CONFIG_FILE_MODE = 0o600;

/** The bits that mean someone other than the owner can reach the file. */
const OTHERS_BITS = 0o077;

/**
 * Tighten `filePath` to CONFIG_FILE_MODE unless it is already that or narrower.
 *
 * The predicate is the whole test, and it is the one `emrg/config.py::_make_config_private`
 * uses: a file already at the rule carries none of OTHERS_BITS, so nothing is written for it,
 * and a mode narrower than the rule (0400, or a set-id bit its owner meant) is left alone —
 * this function's job is to close a file to other users, never to open one already shut
 * further. An equality branch was measured redundant on the Python side and is not repeated
 * here.
 *
 * Failures are swallowed the way that writer swallows them: on a filesystem that does not
 * carry POSIX modes (a FAT volume, a Windows share) the call either raises or does nothing, and
 * neither is a reason to fail a save whose *contents* are what the caller came for.
 *
 * @param {string} filePath the file to tighten; it must already exist.
 */
function makeConfigPrivate(filePath) {
  try {
    const current = fs.statSync(filePath).mode & 0o777;
    if (current & OTHERS_BITS) fs.chmodSync(filePath, CONFIG_FILE_MODE);
  } catch {
    // Best effort — see the docstring.
  }
}

module.exports = { CONFIG_FILE_MODE, makeConfigPrivate };
