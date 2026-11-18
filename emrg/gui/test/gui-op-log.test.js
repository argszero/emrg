"use strict";
/**
 * gui-op-log.test.js — 每一种会话操作都在日志里留一行，而且那行说着它动的是哪条会话
 * （rant 2026-11-18T18:41:06，清单 10）。
 *
 * 为什么断言源码形状而不是行为：这条要求的产出**就是**那行日志，而日志的读者是事后翻
 * 文件的人。切会话一直有一行，其余五种（发送 / 附图 / 关闭 / 重命名 / 删除）此前一行
 * 都没有，于是「这条会话经历过什么」在日志里根本拼不出来。
 *
 * 断言的是**那次调用**，不是那串字符（源码守卫的常见死法：标记写进了注释，一行代码都
 * 没执行，断言照样绿）：每个标记都必须出现在 `logger.info(` 的模板串里，并且同一行带着
 * `sid=`。只写进注释、或者写了却不带会话 id 的日志，在这里都读作没有。
 */
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const MAIN_JS = path.join(__dirname, "..", "main.js");
const source = fs.readFileSync(MAIN_JS, "utf8");

/**
 * 注释不是代码：先剥块注释，再剥行注释。
 *
 * 行注释的 `//` 不能紧跟在 `:` 后面 —— `"http://…"` 里的 `//` 是字符串的一部分，从那里
 * 一路剥到行尾会把同一行后面的代码一起吃掉，于是真读标记的代码被读成没读。这一步自己
 * 有判别臂（本文件最后一条）。
 */
function stripComments(src) {
  const withoutBlocks = src.replace(/\/\*[\s\S]*?\*\//g, "");
  return withoutBlocks.replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

const CODE = stripComments(source);

/** 那一行日志：标记在 `logger.info(` 的模板串里，且同一行带着调用方要的字段。 */
function logLine(marker, field) {
  const line = CODE.split("\n").find((l) => l.includes("logger.info(") && l.includes(`[gui:${marker}]`));
  assert.ok(line, `main.js must log [gui:${marker}] through logger.info (not only in a comment)`);
  assert.ok(line.includes(field), `the [gui:${marker}] line must carry ${field}: ${line.trim()}`);
  return line;
}

/**
 * 六种操作一个不少。`switch` 也在里面：它本来就有那一行，而这条 rant 要的是「六种
 * 操作都能在日志里追到」，把已有的那一种漏在断言外，六行里断哪一行都不会红。
 */
test("six session operations each leave one log line naming its session", () => {
  for (const marker of ["send", "saveImage", "saveFile", "close", "rename", "delete", "switch"]) {
    logLine(marker, "sid=");
  }
});

/**
 * 关闭那一行必须写在**真正关闭的那个函数**里。
 *
 * `emrg:closeSession` 这个处理块只做校验然后转调 `closeSession(sid)`，日志在函数体里；
 * 只按标记全文件搜一遍，任何一处同名的话都能让它绿，包括一处早就不再被走到的旧代码。
 */
test("the close line sits inside the function that actually closes the session", () => {
  const start = CODE.indexOf("async function closeSession(sid)");
  assert.notStrictEqual(start, -1, "main.js must define closeSession(sid)");
  const rest = CODE.slice(start + "async function closeSession(sid)".length);
  const next = rest.indexOf("\n  async function ");
  const region = next === -1 ? rest : rest.slice(0, next);
  assert.ok(region.includes("[gui:close]"), "the close log must sit inside closeSession(sid)");
  assert.ok(region.includes("sid=${sid}"), "that line must name the session it closed");
});

/**
 * 发送那一行还得记下这一次带了什么：一条发送可能只有文字，也可能拖着几张图，而「发了
 * 但模型没看见图」与「根本没附图」在只有会话 id 的日志里长得一模一样。
 */
test("the send line records how much went with it", () => {
  const line = logLine("send", "sid=");
  assert.ok(line.includes("chars="), "the send line must carry chars=");
  assert.ok(line.includes("images="), "the send line must carry images=");
});

/** 判别臂：剥注释这一步真的在剥 —— 否则上面几条量的是注释里的字样。 */
test("a marker written only in a comment is not a log line", () => {
  const commented = "// logger.info(`[gui:send] sid=x`)\n";
  assert.strictEqual(stripComments(commented).includes("[gui:send]"), false);
  const real = "logger.info(`[gui:send] sid=${x}`); // trailing note\n";
  assert.strictEqual(stripComments(real).includes("[gui:send]"), true);
});
