"use strict";
/**
 * nav-policy.test.js — 主窗口外部导航策略单测（rant 2026-09-01T20:33:44）。
 */
const { test } = require("node:test");
const assert = require("node:assert");
const { externalNavPolicy } = require("../nav-policy");

test("file:// URLs are allowed (loadFile / reload / SPA self-navigation)", () => {
  assert.strictEqual(externalNavPolicy("file:///Users/x/emrg/renderer/dist/index.html"), "allow");
  assert.strictEqual(externalNavPolicy("file:///"), "allow");
});

test("http(s) external URLs are opened in the system browser", () => {
  assert.strictEqual(externalNavPolicy("https://github.com/argszero/emrg"), "open-external");
  assert.strictEqual(externalNavPolicy("http://example.com/a?b=c#d"), "open-external");
});

test("non-file schemes (mailto, javascript, data) are treated as external / not internal", () => {
  assert.strictEqual(externalNavPolicy("mailto:x@y.com"), "open-external");
  assert.strictEqual(externalNavPolicy("javascript:alert(1)"), "open-external");
  assert.strictEqual(externalNavPolicy("data:text/html,hi"), "open-external");
});

test("empty / non-string URLs are denied", () => {
  assert.strictEqual(externalNavPolicy(""), "deny");
  assert.strictEqual(externalNavPolicy(undefined), "deny");
  assert.strictEqual(externalNavPolicy(null), "deny");
});
