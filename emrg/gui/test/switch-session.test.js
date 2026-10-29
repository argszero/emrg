"use strict";
/**
 * switch-session.test.js — 切会话永不因「当前会话仍在流式」被拒（issue #2637）。
 *
 * 守卫的对象是 `main.js` 里 `emrg:switchSession` 的处理块：它以前在打开任何连接之前，
 * 用 `connManager.get(currentSessionId).ownStream` 拒绝切换。那条拒绝来自「切会话会关掉
 * 旧连接」的年代；切走早已不再关闭旧连接（打开目标会话的连接，旧连接原样保留），所以它
 * 只拒绝、不保护 —— 一个在会话 A 上生成的回合，与把窗口移到 B 无关。
 *
 * 断言两块，一正一反：
 * - 该处理块**不再**读 `ownStream`；
 * - 该处理块**仍然**带另外三条拒绝（非法 session_id / 非法 agent path / 20 条上限），
 *   所以「区域取空 ⇒ 断言通过」这种空过法在这里读作失败。
 *
 * 另一半（`ConnManager.close()` 对在忙连接先 cancel 再 close）不在这里断言：它是行为，
 * 不是源码形状，`conn-manager.test.js` 的「P6 close: 在忙连接（ownStream）先 cancel 再
 * close」用真实帧把它测住了。一处规则一处断言。
 */
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const MAIN_JS = path.join(__dirname, "..", "main.js");
const source = fs.readFileSync(MAIN_JS, "utf8");

/**
 * 从 `ipcMain.handle("emrg:<name>"` 起，到**下一个** `ipcMain.handle(` 为止的处理块。
 *
 * 上界用下一个处理块而不是「文件末尾」：`split(marker)[0]` 会一路铺到 EOF，于是区域外的
 * 任何一处命中都被读成区域内命中，反之区域内的改动也可能被更大的区域掩盖。
 */
function handlerRegion(src, name) {
  const marker = `ipcMain.handle("emrg:${name}"`;
  const start = src.indexOf(marker);
  assert.notStrictEqual(start, -1, `main.js must register ${marker}`);
  const rest = src.slice(start + marker.length);
  const next = rest.indexOf("ipcMain.handle(");
  return next === -1 ? rest : rest.slice(0, next);
}

test("emrg:switchSession 的处理块不再读 ownStream（issue #2637）", () => {
  const region = handlerRegion(source, "switchSession");
  assert.ok(region.length > 500, `the switch region must be the real handler, got ${region.length} chars`);
  assert.ok(
    !region.includes("ownStream"),
    "the switch handler must not refuse on ownStream any more: switching leaves the previous " +
      "connection open, so a running turn is unaffected by moving the window to another session",
  );
});

test("switchSession 的其余三条拒绝仍在（区域是真的处理块，不是空片）", () => {
  const region = handlerRegion(source, "switchSession");
  assert.ok(region.includes('throw new Error("invalid session_id")'), "the session_id refusal must stay");
  assert.ok(region.includes('throw new Error("invalid agent path")'), "the agent-path refusal must stay");
  assert.ok(region.includes("too many open sessions"), "the open-session cap refusal must stay");
});

test("switchSession 不关闭它离开的那个连接（去掉拒绝的那一步所依赖的事实）", () => {
  // 上一条断言（不再因 ownStream 拒绝）只有在「切走不关闭旧连接」成立时才是安全的：
  // 旧连接若被关，`ConnManager.close()` 会先给在忙的连接发 cancel（见 conn-manager.test.js），
  // 那个正在生成的回合就真的被切换掐断了。所以这里把那条事实也钉住。
  // 允许的 close 只有一处：会话不存在时关掉**目标**那条失败连接（catch 里的防御），
  // 所以断言的对象是「离开的那个 sid」——`close(prevSid` 一律不许出现。
  const region = handlerRegion(source, "switchSession");
  assert.ok(!/close\(\s*prevSid/.test(region), "switching must not close the connection it leaves");
  assert.ok(
    region.includes("clearGroups()"),
    "the previous session's group cache is cleared (a cache, not the connection) — if this call " +
      "moved, re-read which of the two the switch path still does",
  );
});

test("区域取法本身会切分（换一个处理块取到的是另一块）", () => {
  // 仪器的控制：同一份源码取两个不同处理块，必须得到不同文本，且各自带自己的标记。
  const switchRegion = handlerRegion(source, "switchSession");
  const closeRegion = handlerRegion(source, "closeSession");
  assert.notStrictEqual(switchRegion, closeRegion);
  assert.ok(closeRegion.includes("closeSession("), "the closeSession region must carry its own body");
  assert.ok(!switchRegion.includes("closeSession("), "the switch region must not run into the next handler");
});
