"use strict";
/**
 * build-config.test.js — 打包配置守卫（rant 2026-08-10T11:03:51 + 2026-08-10T20:37:08）。
 *
 * 打包版 GUI 故障的根因（两类同根）：electron-builder `files` 白名单漏掉
 * 运行时模块 → 打包后 require 失败 / 功能缺失：
 *   - #612：漏 `vendor/**` → marked/DOMPurify/highlight 没进包 → Markdown 静默降级
 *   - rant 20:37:08：漏 `conn-manager.js` / `gui-state.js` → main.js require 直接抛
 *     "Cannot find module './conn-manager'"（v0.2.21 Windows 打包版启动崩溃）
 *
 * 本测试钉死四类回归：
 *   1. files 白名单必须包含 "vendor/**"（有人删掉即红）
 *   2. vendor/ 目录必须存在全部 3 个运行时脚本（build-vendor.js 产物）
 *   3. renderMarkdown 的 `!window.marked` 降级路径必须带 console.warn
 *   4. **main.js / preload.js 的每个本地 require("./x") 必须被 files 白名单覆盖**
 *      （新增本地模块而忘记加白名单 → 即红。conn-manager.js/gui-state.js 即此漏洞的实例）
 */
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const GUI_ROOT = path.join(__dirname, "..");
const PKG = JSON.parse(fs.readFileSync(path.join(GUI_ROOT, "package.json"), "utf-8"));
const VENDOR_DIR = path.join(GUI_ROOT, "vendor");

const REQUIRED_VENDOR_FILES = ["marked.min.js", "dompurify.min.js", "highlight.custom.js"];

/** 判断文件名是否被 electron-builder files 白名单覆盖（支持 "x.js" 与 "dir/**" 形态） */
function whitelistCovers(whitelist, relPath) {
  for (const w of whitelist) {
    if (w.endsWith("/**")) {
      if (relPath.startsWith(w.slice(0, -3))) return true; // "renderer/**" covers "renderer/js/x.js"
    } else if (relPath === w || relPath.startsWith(w + "/")) {
      return true;
    }
  }
  return false;
}

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

test("every main.js/preload.js local require is covered by files whitelist", () => {
  const files = (PKG.build && PKG.build.files) || [];
  const localRequires = [];
  for (const entry of ["main.js", "preload.js"]) {
    const src = fs.readFileSync(path.join(GUI_ROOT, entry), "utf-8");
    // 收集 require("./xxx") 与 require("./xxx.js") 形态
    const re = /require\(\s*"(\.[^"]+)"\s*\)/g;
    let m;
    while ((m = re.exec(src)) !== null) {
      let rel = m[1].replace(/^\.\//, "");
      if (!rel.endsWith(".js") && fs.existsSync(path.join(GUI_ROOT, rel + ".js"))) {
        rel += ".js";
      }
      localRequires.push({ entry, rel });
    }
  }
  assert.ok(localRequires.length > 0, "no local requires found — scan is broken");
  const uncovered = localRequires.filter(({ rel }) => !whitelistCovers(files, rel));
  assert.deepStrictEqual(
    uncovered.map((u) => `${u.entry} → ${u.rel}`),
    [],
    `local modules required by main.js/preload.js but MISSING from build.files whitelist: ` +
      `add each to package.json build.files (e.g. "conn-manager.js", "gui-state.js"). ` +
      `These files exist in the repo but would NOT be packed into app.asar → packaged GUI ` +
      `crashes with "Cannot find module './<name>'"`
  );
});

