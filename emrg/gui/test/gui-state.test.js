// gui-state.test.js — P4 slice 1（rant 2026-08-10T15:07:19）
// gui_state.json 持久化模块：路径、清洗（上限 20 + 失效条目跳过 + lastActive 倒序）、原子写。
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { guiStatePath, sanitizeOpenSessions, saveGuiState, DEFAULT_CAP } = require("../gui-state.js");

function tmpHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "emrg-gui-state-"));
}

test("gui-state: 路径固定在 <home>/.emrg/gui_state.json", () => {
  const h = tmpHome();
  assert.strictEqual(guiStatePath(h), path.join(h, ".emrg", "gui_state.json"));
});

test("gui-state: sanitize 跳过失效条目（缺 sid/projectPath）", () => {
  const list = [
    { sid: "s1", projectPath: "/a", lastActive: "2026-08-10T10:00:00" },
    { projectPath: "/b" }, // 缺 sid
    { sid: "s2" }, // 缺 projectPath
    { sid: "", projectPath: "/c" }, // 空 sid
    null,
    "junk",
    { sid: "s3", projectPath: "/d", lastActive: "2026-08-10T09:00:00" },
  ];
  const out = sanitizeOpenSessions(list);
  assert.deepStrictEqual(out.map((s) => s.sid), ["s1", "s3"], "invalid entries dropped, order preserved");
});

test("gui-state: sanitize 按 lastActive 倒序 + 上限 20", () => {
  const list = Array.from({ length: 25 }, (_, i) => ({
    sid: `s${i}`,
    projectPath: `/p/${i}`,
    lastActive: `2026-08-10T${String(i).padStart(2, "0")}:00:00`,
  }));
  const out = sanitizeOpenSessions(list);
  assert.strictEqual(out.length, DEFAULT_CAP, "capped at 20");
  assert.strictEqual(out[0].sid, "s24", "most recent first (lastActive desc)");
  assert.strictEqual(out[19].sid, "s5", "oldest kept is the 20th");
});

test("gui-state: sanitize 缺失 lastActive 视作最旧（字符串比较稳定）", () => {
  const out = sanitizeOpenSessions([
    { sid: "no-ts", projectPath: "/x" },
    { sid: "new", projectPath: "/y", lastActive: "2026-08-10T12:00:00" },
  ]);
  assert.strictEqual(out[0].sid, "new");
  assert.strictEqual(out[1].sid, "no-ts");
});

test("gui-state: saveGuiState 原子写 + 目录自动创建", () => {
  const h = tmpHome();
  const state = { openSessions: [{ sid: "s1", projectName: "a", projectPath: "/a", lastActive: "t" }], activeSid: "s1" };
  saveGuiState(h, state);
  const p = guiStatePath(h);
  assert.ok(fs.existsSync(p), "file written");
  assert.deepStrictEqual(JSON.parse(fs.readFileSync(p, "utf8")), state, "content round-trips");
  assert.ok(!fs.existsSync(p + ".tmp"), "no tmp residue after rename");
});

test("gui-state: saveGuiState 覆盖旧内容（原子替换）", () => {
  const h = tmpHome();
  saveGuiState(h, { openSessions: [], activeSid: null });
  saveGuiState(h, { openSessions: [{ sid: "x", projectName: "p", projectPath: "/p", lastActive: "t" }], activeSid: "x" });
  const parsed = JSON.parse(fs.readFileSync(guiStatePath(h), "utf8"));
  assert.strictEqual(parsed.openSessions.length, 1);
  assert.strictEqual(parsed.activeSid, "x");
});

test("gui-state: 写盘侧上限——persist 前先 sanitize（主流程调用方模式）", () => {
  const entries = Array.from({ length: 30 }, (_, i) => ({
    sid: `s${i}`,
    projectName: `p${i}`,
    projectPath: `/p/${i}`,
    lastActive: `2026-08-10T${String(59 - i).padStart(2, "0")}:00:00`, // s0 最新（59 > 58 > …）
  }));
  const out = sanitizeOpenSessions(entries);
  assert.strictEqual(out.length, DEFAULT_CAP, "never persists more than 20");
  assert.strictEqual(out[0].sid, "s0", "most recent first");
  assert.strictEqual(out[19].sid, "s19", "20th kept");
});
