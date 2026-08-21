// gui-state.js — P4 of the GUI multi-session rant (2026-08-10T15:07:19)
//
// gui_state.json persistence: { openSessions: [{sid, projectName, projectPath,
// lastActive}], activeSid }.
//
// This slice (P4 slice 1) covers the WRITE path (main.js keeps open sessions
// alive across switches, persists them, and exposes close/get IPC). The READ
// path (restore on boot + sidebar UI) lands in P4 slice 2.
//
// Design notes (from the rant):
// - Open sessions = cross-project tabs (aligned with the TUI multi-open model).
// - Cap 20: never persist more than the cap; restore (slice 2) takes the most
//   recent 20 by lastActive.
// - Write is atomic (.tmp + rename) so a crash mid-write never corrupts state.
"use strict";

const fs = require("fs");
const path = require("path");

const DEFAULT_CAP = 20;

function guiStatePath(homeDir) {
  return path.join(homeDir, ".emrg", "gui_state.json");
}

// 清洗 openSessions：跳过失效条目（缺 sid/projectPath），按 lastActive 倒序，
// 截断到 cap（默认 20）。写盘前与恢复（slice 2）共用同一规则。
function sanitizeOpenSessions(list, cap = DEFAULT_CAP) {
  const valid = (list || []).filter(
    (s) => s && typeof s.sid === "string" && s.sid && typeof s.projectPath === "string" && s.projectPath
  );
  valid.sort((a, b) => String(b.lastActive || "").localeCompare(String(a.lastActive || "")));
  return valid.slice(0, cap);
}

// 原子写：tmp + rename（镜像 install-info.json 原子写 #569 模式）。
// 目录缺失 → 创建。写失败 → 抛（调用方捕获并记录，不阻断主流程）。
function saveGuiState(homeDir, state) {
  const p = guiStatePath(homeDir);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const tmp = p + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2) + "\n", "utf8");
  fs.renameSync(tmp, p);
}

module.exports = { guiStatePath, sanitizeOpenSessions, saveGuiState, DEFAULT_CAP };
