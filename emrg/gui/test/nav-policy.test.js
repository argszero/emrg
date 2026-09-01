"use strict";
/**
 * nav-policy.test.js — 主窗口外部导航策略单测（rant 2026-09-01T20:33:44）。
 * issue #1100 加固（2026-09-02）：只放行应用自身 renderer dist 前缀内的 file:// URL，
 * 其余 file:// 一律 deny——任意本地 HTML 不得加载进特权主窗口。
 */
const { test } = require("node:test");
const assert = require("node:assert");
const { externalNavPolicy } = require("../nav-policy");

const DIST_PREFIX = "file:///Users/x/emrg/renderer/dist/";

test("renderer dist file:// URLs are allowed (loadFile / reload / SPA self-navigation)", () => {
  assert.strictEqual(externalNavPolicy("file:///Users/x/emrg/renderer/dist/index.html", DIST_PREFIX), "allow");
  assert.strictEqual(externalNavPolicy("file:///Users/x/emrg/renderer/dist/assets/app.js", DIST_PREFIX), "allow");
  assert.strictEqual(externalNavPolicy("file:///Users/x/emrg/renderer/dist/index.html?x=1#/conv", DIST_PREFIX), "allow");
});

test("file:// URLs outside the renderer dist prefix are denied (issue #1100)", () => {
  // 任意本地文件：不得加载进特权主窗口
  assert.strictEqual(externalNavPolicy("file:///etc/passwd", DIST_PREFIX), "deny");
  assert.strictEqual(externalNavPolicy("file:///Users/x/evil.html", DIST_PREFIX), "deny");
  assert.strictEqual(externalNavPolicy("file:///Users/x/emrg/renderer/index.html", DIST_PREFIX), "deny");
  assert.strictEqual(externalNavPolicy("file:///", DIST_PREFIX), "deny");
});

test("prefix boundary: dist-evil / traversal / encoded variants must not match the dist prefix", () => {
  // 前缀以 / 结尾：dist-evil 不算 dist/ 内
  assert.strictEqual(externalNavPolicy("file:///Users/x/emrg/renderer/dist-evil/index.html", DIST_PREFIX), "deny");
  assert.strictEqual(externalNavPolicy("file:///Users/x/emrg/renderer/dist/../index.html", DIST_PREFIX), "deny");
  assert.strictEqual(externalNavPolicy("file:///Users/x/emrg/renderer/dist/%2e%2e/index.html", DIST_PREFIX), "deny");
});

test("file:// URLs are denied when no dist prefix is provided (fail-closed)", () => {
  assert.strictEqual(externalNavPolicy("file:///Users/x/emrg/renderer/dist/index.html"), "deny");
  assert.strictEqual(externalNavPolicy("file:///etc/passwd"), "deny");
});

test("http(s) external URLs are opened in the system browser", () => {
  assert.strictEqual(externalNavPolicy("https://github.com/argszero/emrg", DIST_PREFIX), "open-external");
  assert.strictEqual(externalNavPolicy("http://example.com/a?b=c#d", DIST_PREFIX), "open-external");
});

test("non-file schemes (mailto, javascript, data) are treated as external / not internal", () => {
  assert.strictEqual(externalNavPolicy("mailto:x@y.com", DIST_PREFIX), "open-external");
  assert.strictEqual(externalNavPolicy("javascript:alert(1)", DIST_PREFIX), "open-external");
  assert.strictEqual(externalNavPolicy("data:text/html,hi", DIST_PREFIX), "open-external");
});

test("empty / non-string URLs are denied", () => {
  assert.strictEqual(externalNavPolicy("", DIST_PREFIX), "deny");
  assert.strictEqual(externalNavPolicy(undefined, DIST_PREFIX), "deny");
  assert.strictEqual(externalNavPolicy(null, DIST_PREFIX), "deny");
});
