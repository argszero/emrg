// gui-state.js — P4 of the GUI multi-session rant (2026-08-10T15:07:19)
//
// gui_state.json persistence: { openSessions: [{sid, projectName, projectPath,
// openedAt, lastActive}], activeSid }.
//
// This slice (P4 slice 1) covers the WRITE path (main.js keeps open sessions
// alive across switches, persists them, and exposes close/get IPC). The READ
// path (restore on boot + sidebar UI) lands in P4 slice 2.
//
// Design notes (from the rant):
// - Open sessions = cross-agent tabs (aligned with the TUI multi-open model).
// - Cap 20: never persist more than the cap.
// - Write is atomic (.tmp + rename) so a crash mid-write never corrupts state.
//
// **The array order is the sidebar order (issue #3022).** A session's position is fixed
// when it is opened and moved only by the user dragging its row; activity never reorders
// anything. So every function here preserves the order it is handed, and the one thing
// `lastActive` decides is which entry the cap drops — the longest inactive. A sort by
// `lastActive` here would move a row under the reader's eyes the moment that session
// received a message, which is the defect this rule exists to prevent.
"use strict";

const fs = require("fs");
const path = require("path");

const DEFAULT_CAP = 20;

function guiStatePath(homeDir) {
  return path.join(homeDir, ".emrg", "global", "gui_state.json");
}

function isOpenEntry(s) {
  return !!s && typeof s.sid === "string" && !!s.sid && typeof s.projectPath === "string" && !!s.projectPath;
}

// Which entry the cap drops first: the one inactive longest. A missing stamp is the
// oldest there is, and the index breaks ties, so the answer never depends on how stable
// the sort happens to be.
function oldestFirst(entries) {
  return entries
    .map((entry, index) => ({ entry, index }))
    .sort((a, b) => {
      const cmp = String(a.entry.lastActive || "").localeCompare(String(b.entry.lastActive || ""));
      return cmp !== 0 ? cmp : a.index - b.index;
    });
}

// Keep at most `cap`, dropping the longest-inactive entries. `keepSid` is never dropped:
// it is the session that was just opened, and evicting it would leave the caller holding
// a row for a session that is not in the list.
function trimToCap(entries, cap = DEFAULT_CAP, keepSid = null) {
  if (entries.length <= cap) return { kept: entries, evicted: [] };
  const droppable = entries.filter((s) => s.sid !== keepSid);
  const drop = new Set(
    oldestFirst(droppable)
      .slice(0, entries.length - cap)
      .map((x) => x.entry.sid)
  );
  return {
    kept: entries.filter((s) => !drop.has(s.sid)),
    evicted: entries.filter((s) => drop.has(s.sid)).map((s) => s.sid),
  };
}

// 清洗 openSessions：跳过失效条目（缺 sid/projectPath），**保持数组顺序**，只按
// 「最久没有活动」裁到 cap（默认 20）。写盘前与恢复（slice 2）共用同一规则。
function sanitizeOpenSessions(list, cap = DEFAULT_CAP) {
  return trimToCap((list || []).filter(isOpenEntry), cap).kept;
}

// A session opened for the first time goes to the TOP of the list. One already open keeps
// both its position and its `openedAt` — only `lastActive` moves, and only the cap reads
// it. The evicted sids are returned so the caller can drop their connections as well,
// rather than keeping a live connection behind a row that no longer exists.
function addOpened(entries, entry, cap = DEFAULT_CAP) {
  const valid = (entries || []).filter(isOpenEntry);
  if (valid.some((s) => s.sid === entry.sid)) return { entries: valid, evicted: [] };
  const { kept, evicted } = trimToCap([entry, ...valid], cap, entry.sid);
  return { entries: kept, evicted };
}

// Apply a drag: the named sids take exactly the order given, and any entry the caller did
// not name keeps its relative position after them. An unknown sid is skipped rather than
// inserted as a hole.
function reorderOpenSessions(entries, sids) {
  const remaining = new Set((entries || []).filter(isOpenEntry).map((s) => s.sid));
  const bySid = new Map((entries || []).filter(isOpenEntry).map((s) => [s.sid, s]));
  const next = [];
  for (const sid of sids || []) {
    if (!remaining.has(sid)) continue;
    next.push(bySid.get(sid));
    remaining.delete(sid);
  }
  for (const entry of entries || []) {
    if (!remaining.has(entry.sid)) continue;
    next.push(entry);
    remaining.delete(entry.sid);
  }
  return next;
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

module.exports = {
  guiStatePath,
  sanitizeOpenSessions,
  addOpened,
  reorderOpenSessions,
  trimToCap,
  saveGuiState,
  DEFAULT_CAP,
};
