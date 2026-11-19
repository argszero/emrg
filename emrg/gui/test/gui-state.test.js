// gui-state.test.js — P4 slice 1（rant 2026-08-10T15:07:19）
// gui_state.json 持久化模块：路径、清洗（上限 20 + 失效条目跳过 + lastActive 倒序）、原子写。
const { test, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  guiStatePath,
  sanitizeOpenSessions,
  addOpened,
  reorderOpenSessions,
  saveGuiState,
  DEFAULT_CAP,
} = require("../gui-state.js");

// Every temp home this file creates, so the run can hand them all back. Without the
// `after()` hook below, `node --test` left three directories per run behind in
// `os.tmpdir()` — measured 2026-10-08 on this host: 36 stale `emrg-gui-state-*`
// directories / 96 KB, i.e. 12 runs, and a long-lived runner accumulates them without
// bound (a peer measured 672 directories / 1.8 MB). A test that creates host state owes
// the removal of it, exactly as it owes the assertions.
const tmpHomes = [];

function tmpHome() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "emrg-gui-state-"));
  tmpHomes.push(dir);
  return dir;
}

after(() => {
  for (const dir of tmpHomes) {
    fs.rmSync(dir, { recursive: true, force: true });
    // A cleanup that silently failed would be the same litter with a tidier story.
    assert.ok(!fs.existsSync(dir), `temp home was not removed: ${dir}`);
  }
});

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

test("gui-state: sanitize 保持顺序，只按 lastActive 裁掉最久无活动的（issue #3022）", () => {
  const list = Array.from({ length: 25 }, (_, i) => ({
    sid: `s${i}`,
    projectPath: `/p/${i}`,
    lastActive: `2026-08-10T${String(i).padStart(2, "0")}:00:00`,
  }));
  const out = sanitizeOpenSessions(list);
  assert.strictEqual(out.length, DEFAULT_CAP, "capped at 20");
  // 顺序是**入参的顺序**，不是 lastActive 的顺序：最久无活动的 s0..s4 被裁掉，
  // 留下的 20 条仍按交进来的次序排列。
  assert.deepStrictEqual(
    out.map((s) => s.sid),
    list.slice(5).map((s) => s.sid),
    "the 20 kept entries keep the order they were handed in",
  );
});

test("gui-state: 缺失 lastActive 只影响「谁被裁掉」，不影响顺序", () => {
  const out = sanitizeOpenSessions([
    { sid: "no-ts", projectPath: "/x" },
    { sid: "new", projectPath: "/y", lastActive: "2026-08-10T12:00:00" },
  ]);
  assert.deepStrictEqual(out.map((s) => s.sid), ["no-ts", "new"], "order is the order it was handed in");
  // 满 21 条时，没有时间戳的那条就是最久无活动的一条，被裁掉的是它。
  const full = Array.from({ length: 21 }, (_, i) => ({
    sid: `k${i}`,
    projectPath: `/k/${i}`,
    lastActive: i === 7 ? undefined : `2026-08-10T${String(i).padStart(2, "0")}:00:00`,
  }));
  const trimmed = sanitizeOpenSessions(full);
  assert.strictEqual(trimmed.length, DEFAULT_CAP);
  assert.ok(!trimmed.some((s) => s.sid === "k7"), "the entry with no stamp is the one the cap drops");
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

test("gui-state: 新打开的一条排最上面，已打开的原地不动（issue #3022）", () => {
  const a = { sid: "a", projectPath: "/a", openedAt: "o1", lastActive: "t1" };
  const b = { sid: "b", projectPath: "/b", openedAt: "o2", lastActive: "t2" };
  let list = addOpened([], a).entries;
  list = addOpened(list, b).entries;
  // A 又来了一条消息：活动变了，位置不变。
  list = addOpened(list, { ...a, lastActive: "t9" }).entries;
  assert.deepStrictEqual(list.map((s) => s.sid), ["b", "a"], "activity never moves a row; open order does");
});

test("gui-state: 满 20 条时开第 21 条，挤掉最久无活动的那条，其余位置逐条不变（issue #3022）", () => {
  const full = Array.from({ length: DEFAULT_CAP }, (_, i) => ({
    sid: `s${i}`,
    projectPath: `/p/${i}`,
    openedAt: `o${i}`,
    lastActive: `2026-08-10T${String(i).padStart(2, "0")}:00:00`,
  }));
  const fresh = { sid: "new", projectPath: "/new", openedAt: "now", lastActive: "2026-08-11T00:00:00" };
  const { entries, evicted } = addOpened(full, fresh, DEFAULT_CAP);
  assert.deepStrictEqual(evicted, ["s0"], "the longest-inactive entry is the one evicted");
  assert.strictEqual(entries.length, DEFAULT_CAP);
  assert.strictEqual(entries[0].sid, "new", "the session just opened goes to the top");
  assert.deepStrictEqual(
    entries.slice(1).map((s) => s.sid),
    full.slice(1).map((s) => s.sid),
    "every other row keeps its place, compared row by row",
  );
});

test("gui-state: 拖出来的顺序原样落地，没点名的一律按原相对顺序跟在后面（issue #3022）", () => {
  const list = ["c", "b", "a"].map((sid) => ({ sid, projectPath: `/${sid}` }));
  assert.deepStrictEqual(reorderOpenSessions(list, ["a", "c", "b"]).map((s) => s.sid), ["a", "c", "b"]);
  assert.deepStrictEqual(
    reorderOpenSessions(list, ["a"]).map((s) => s.sid),
    ["a", "c", "b"],
    "an unnamed entry keeps its own order after the named ones",
  );
  assert.deepStrictEqual(
    reorderOpenSessions(list, ["a", "nope"]).map((s) => s.sid),
    ["a", "c", "b"],
    "an unknown sid is skipped, not left as a hole",
  );
});

test("gui-state: 顺序与打开时刻写盘再读回来一模一样（issue #3022）", () => {
  const h = tmpHome();
  const state = {
    openSessions: sanitizeOpenSessions([
      { sid: "a", projectPath: "/a", openedAt: "o1", lastActive: "t1" },
      { sid: "c", projectPath: "/c", openedAt: "o3", lastActive: "t3" },
      { sid: "b", projectPath: "/b", openedAt: "o2", lastActive: "t2" },
    ]),
    activeSid: "a",
  };
  saveGuiState(h, state);
  const back = JSON.parse(fs.readFileSync(guiStatePath(h), "utf8"));
  assert.deepStrictEqual(back.openSessions.map((s) => s.sid), ["a", "c", "b"]);
  assert.deepStrictEqual(back.openSessions.map((s) => s.openedAt), ["o1", "o3", "o2"]);
});
