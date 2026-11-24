"use strict";
/**
 * frame-route.test.js — 转发给渲染层的帧按「帧自己说的会话」落桶
 * （rant 2026-11-24T14:29:19，客户端那一半）。
 *
 * 守卫的对象是 `frameSessionId`：`main.js` 的事件桥用它给信封填 `sid`，渲染层再用
 * 这个 `sid` 分桶。以前这里填的是「这条连接是为哪个会话打开的」，所以一条从别的会话
 * 送到这条连接上的帧，和这个会话自己的帧在信封上完全一样。
 *
 * 两面都断言，因为只有一面能被「总是返回 sid」和「总是返回 data.session_id」这两种
 * 错法各自满足：
 * - 帧自报了会话 → 用帧的，哪怕连接是另一个会话；
 * - 帧没自报（或报了空串/非字符串）→ 用连接的。
 */
const { test } = require("node:test");
const assert = require("node:assert");
const { frameSessionId } = require("../frame-route");

test("帧自报的 session_id 胜过连接的 sid", () => {
  assert.strictEqual(frameSessionId({ session_id: "B" }, "A"), "B");
});

test("帧没自报时沿用连接的 sid（状态帧、全局帧）", () => {
  assert.strictEqual(frameSessionId({ type: "status" }, "A"), "A");
  assert.strictEqual(frameSessionId(undefined, "A"), "A");
  assert.strictEqual(frameSessionId(null, "A"), "A");
});

test("空串与非字符串的 session_id 等于没自报", () => {
  // 空串没有名字；数字或对象落到 Map 的键上会成为一个谁也对不上的桶。
  assert.strictEqual(frameSessionId({ session_id: "" }, "A"), "A");
  assert.strictEqual(frameSessionId({ session_id: 7 }, "A"), "A");
  assert.strictEqual(frameSessionId({ session_id: { id: "B" } }, "A"), "A");
});

test("两边都没有会话时是 null，而不是字符串 \"undefined\"", () => {
  assert.strictEqual(frameSessionId({ type: "status" }, null), null);
  assert.strictEqual(frameSessionId({ type: "status" }, undefined), null);
});
