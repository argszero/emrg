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
// rant 09:17:45：Monaco Editor 本地 vendor（loader + 入口 js/css 必须入库）
const REQUIRED_MONACO_FILES = [
  "monaco/vs/loader.js",
  "monaco/vs/editor/editor.main.js",
  "monaco/vs/editor/editor.main.css",
];

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

test("vendor ships Monaco editor (loader + editor.main js/css)", () => {
  for (const f of REQUIRED_MONACO_FILES) {
    const p = path.join(VENDOR_DIR, f);
    assert.ok(fs.existsSync(p), `missing monaco file: ${f} — run \`node scripts/build-vendor.js\``);
    assert.ok(fs.statSync(p).size > 0, `monaco file is empty: ${f}`);
  }
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


test("preload exposes workspace-panel APIs (listFiles/readFile)", () => {
  // 右栏工作区面板 P1（rant 2026-08-11T12:20:35 P1.2）：preload 桥必须暴露两个新 API，
  // 否则 renderer 无法触达 daemon 的 list_files/read_file 命令。
  const preload = fs.readFileSync(path.join(GUI_ROOT, "preload.js"), "utf-8");
  for (const [api, channel] of [
    ["listFiles", "emrg:listFiles"],
    ["readFile", "emrg:readFile"],
  ]) {
    assert.match(
      preload,
      new RegExp(`${api}: \\((payload)?\\) => ipcRenderer\\.invoke\\("${channel}"`),
      `preload.js must expose ${api} → ${channel}`
    );
  }
  const main = fs.readFileSync(path.join(GUI_ROOT, "main.js"), "utf-8");
  for (const channel of ["emrg:listFiles", "emrg:readFile"]) {
    assert.match(main, new RegExp(`ipcMain\\.handle\\("${channel}"`), `main.js must handle ${channel}`);
  }
});


// ── rant 2026-08-18T12:45:47 (v0.2.47 Build Release) ──
// #836 把 buildResources 放在 electron-builder config 根级 → schema 校验失败
// （"configuration has an unknown property 'buildResources'"）→ 4/4 build jobs 全红。
// 正确位置是 directories.buildResources。Test CI 不跑 electron-builder，只有打包会炸 ——
// 本守卫在 PR 阶段钉死该 schema 约束，防止再次放行。
test("electron-builder config: buildResources lives under directories (schema guard, rant 12:45:47)", () => {
  const build = PKG.build || {};
  assert.ok(
    !Object.prototype.hasOwnProperty.call(build, "buildResources"),
    `buildResources must NOT be at config root — electron-builder rejects unknown property ` +
      `(v0.2.47 Build Release 4/4 failure). Place it under directories.buildResources. ` +
      `Actual root keys: ${JSON.stringify(Object.keys(build))}`
  );
  assert.strictEqual(
    build.directories && build.directories.buildResources,
    "../packaging/assets",
    "directories.buildResources must point at ../packaging/assets (icon.icns/ico/png sources)"
  );
  // icon.icns/ico/png are gen-assets products (gitignored, generated at build time from
  // icon.svg by packaging/gen-assets.sh) — only the committed design source must exist in CI.
  const assetsDir = path.join(GUI_ROOT, "..", "..", "packaging", "assets");
  assert.ok(
    fs.existsSync(path.join(assetsDir, "icon.svg")),
    `buildResources dir missing design source icon.svg (committed) — gen-assets can't render icons`
  );
});
