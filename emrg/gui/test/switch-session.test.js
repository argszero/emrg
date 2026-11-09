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

/**
 * 把 `//` 与块注释从一段源码里剥掉（issue #2771）。
 *
 * 守卫问的是「这段处理块里还有没有**代码**读这个名字」，而子串匹配回答的是「这一段正文里还
 * 有没有这串字符」。两者差在注释上：只点名这个名字、一行代码都没读它的注释会被读成代码
 * ⇒ 「写一句解释为什么删掉那条拒绝」这种正当改动变红，而红字指向的代码没坏。注释不是代码，
 * 所以先剥再匹配。
 *
 * 两处细节是被测出来的，不是挑好看的：
 * - **先剥块注释**：块注释里的 `//` 不是行注释的开头。
 * - **行注释的 `//` 不能紧跟在 `:` 后面**：`"http://…"` 里的 `//` 是字符串的一部分，从这里
 *   一直剥到行尾会把**同一行后面的代码**一起吃掉 —— 那会让真读字段的代码读成没读。假绿比
 *   假红贵，所以这一步有自己的判别臂（见文件末尾那条测）。
 */
function stripComments(src) {
  const withoutBlocks = src.replace(/\/\*[\s\S]*?\*\//g, "");
  return withoutBlocks.replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

/**
 * 这个字段名本身，按**整词**匹配。
 *
 * 子串匹配分不清 `ownStream` 与以它开头的更长的名字：`ownStreamRequestId` 是一次请求的 id，
 * 读它与读流标记是两件事（issue #2771）。整词匹配把这两件事分开，而一条真拒绝不可能存在
 * 却不出现这个名字的整词 —— 匹配变窄的代价因此为零。
 */
const OWN_STREAM_FIELD = /\bownStream\b/;

/**
 * 一条源码是否**在代码里**读了那个字段：先剥注释，再按整词匹配。
 *
 * 谓词有名字是有理由的：数测试的仪器按行首形状认「这一行是不是在定义一条测试」，
 * `OWN_STREAM_FIELD.test(...)` 单独起一行会被它读成一条定义（issue #2771 实测）。
 * 一个名字把「读一个字段」写成一句话，顺带让它不再长得像一条测试。
 */
const readsOwnStream = (text) => OWN_STREAM_FIELD.test(stripComments(text));

test("emrg:switchSession 的处理块不再读 ownStream（issue #2637）", () => {
  const region = handlerRegion(source, "switchSession");
  assert.ok(region.length > 500, `the switch region must be the real handler, got ${region.length} chars`);
  assert.ok(
    !readsOwnStream(region),
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

// ── issue #2771：上面那条断言问的对象是「代码」，所以它得在注释与更长的名字上都读对 ──

test("只点名 ownStream 的注释读作「没有代码读它」，真读它的一行仍然红", () => {
  // 两向各一句，都在**字符串**上，不动 main.js：改真文件只能证明这一次，改不得下一次。
  const commented = [
    "async (_e, { sessionId } = {}) => {",
    "  // the refusal removed in issue #2637 read connManager.get(prevSid).ownStream before",
    "  // opening any connection: these lines name that field and read nothing from it.",
    "  const prevSid = currentSessionId;",
    "  return {};",
    "}",
  ].join("\n");
  assert.ok(
    !readsOwnStream(commented),
    "a comment explaining why the refusal was removed must not be read as the refusal itself",
  );

  const refused = [
    "async (_e, { sessionId } = {}) => {",
    "  if (connManager.get(currentSessionId).ownStream) {",
    '    throw new Error("session busy");',
    "  }",
    "}",
  ].join("\n");
  assert.ok(
    readsOwnStream(refused),
    "a handler that really refuses on the field must still be caught — this is the arm the guard exists for",
  );

  // 块注释同样是注释：`/* … */` 里的点名不算读。
  const blockCommented = "  /* this used to read conn.ownStream — issue #2637 */\n  return {};";
  assert.ok(!readsOwnStream(blockCommented), "a block comment is not code either");
});

test("以 ownStream 开头的更长的名字不是读这个字段", () => {
  // `ownStreamRequestId` 是一次请求的 id，`_releaseOwnStream()` 是释放锁；两者都与「本处理块
  // 是否因流标记拒绝」无关，而子串匹配会把前者读成后者（issue #2771 实测 `includes` 为 true）。
  const longer = [
    "  conn._releaseOwnStream();",
    "  this.ownStreamRequestId = rid;",
    "  if (frame.request_id === this.ownStreamRequestId) this._releaseOwnStream();",
  ].join("\n");
  assert.ok(
    !readsOwnStream(longer),
    "names that merely begin with the field are a different subject, and the guard must not redden them",
  );
  // 对照：这一类名字确实存在于本程序里，所以上一条不是在与一个假想的形状搏斗。
  assert.ok(
    source.includes("_releaseOwnStream"),
    "control: the release helper this test excludes does exist in main.js",
  );
});

test("字符串里的 // 不是注释起点（剥除不能吃掉同一行后面的代码）", () => {
  // 这一条是剥除本身的判别臂，方向是**假绿**：若行注释的 `//` 不排除前面是 `:` 的那种，
  // 一次 URL 字面量之后的所有代码都会被剥掉，于是同行的真拒绝读成「没读」。
  const withUrl = '  const u = "http://example.test/x"; if (conn.ownStream) throw new Error("busy");';
  assert.ok(
    readsOwnStream(withUrl),
    "a `//` inside a string literal must not swallow the code that follows it on the same line",
  );
  // 对照：同一行里真的是注释时，行尾的 `//` 之后不再算代码。
  const withComment = '  const u = "http://example.test/x"; return {}; // ownStream lives elsewhere';
  assert.ok(
    !readsOwnStream(withComment),
    "a trailing line comment is still a comment",
  );
});
