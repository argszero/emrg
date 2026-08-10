"use strict";
/**
 * build-config.test.js — 打包配置守卫（rant 2026-08-10T11:03:51）。
 *
 * 打包版 GUI Markdown 不渲染的根因：electron-builder `files` 白名单漏掉
 * `vendor/**` → marked/DOMPurify/highlight 三个第三方脚本没进安装包 →
 * renderMarkdown 静默降级纯文本（源码模式正常、打包版中招）。
 *
 * 本测试直接钉死三类回归：
 *   1. files 白名单必须包含 "vendor/**"（有人删掉即红）
 *   2. vendor/ 目录必须存在全部 3 个运行时脚本（build-vendor.js 产物）
 *   3. renderMarkdown 的 `!window.marked` 降级路径必须带 console.warn
 *      （静默降级 → 可诊断，问题可快速定位）
 */
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const GUI_ROOT = path.join(__dirname, "..");
const PKG = JSON.parse(fs.readFileSync(path.join(GUI_ROOT, "package.json"), "utf-8"));
const VENDOR_DIR = path.join(GUI_ROOT, "vendor");

const REQUIRED_VENDOR_FILES = ["marked.min.js", "dompurify.min.js", "highlight.custom.js"];

test("electron-builder files whitelist bundles vendor/**", () => {
  const files = (PKG.build && PKG.build.files) || [];
  assert.ok(
    files.includes("vendor/**"),
    `build.files must include "vendor/**" — got: ${JSON.stringify(files)}`
  );
});

test("vendor directory ships all runtime scripts", () => {
  for (const f of REQUIRED_VENDOR_FILES) {
    const p = path.join(VENDOR_DIR, f);
    assert.ok(fs.existsSync(p), `missing vendored script: ${f}`);
    assert.ok(fs.statSync(p).size > 0, `vendored script is empty: ${f}`);
  }
});

test("renderMarkdown fallback logs when marked is missing", () => {
  const md = fs.readFileSync(path.join(GUI_ROOT, "renderer", "js", "markdown.js"), "utf-8");
  assert.match(
    md,
    /if \(!window\.marked\) \{[\s\S]*console\.warn\("markdown vendor missing \(marked not loaded\)"\)/,
    "silent markdown degradation must be diagnostic (console.warn when marked is missing)"
  );
});
