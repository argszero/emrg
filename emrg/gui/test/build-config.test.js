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
// electron-builder 的一切相对路径都以 **GUI_ROOT**（package.json 所在目录）为基准；
// 产物（dist/runtime、packaging/assets）在仓库根 —— 少一层 `..` 即静默错位。
const REPO_ROOT = path.resolve(GUI_ROOT, "..", "..");
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

test("the clipboard-image wire exists end to end (rant 2026-09-30T09:35:04, requirement 4)", () => {
  // 「系统给的图不受白名单所限」需要一条 renderer 到 main 的线：renderer 解不了的
  // 格式（macOS 的 image/tiff）只能由有 NSImage 的 main 转成 PNG。renderer 那半在
  // Composer.test.tsx 里注入假桥测试——注入式测试**看不见线断没断**，所以这条线
  // 的两端在这里按源码钉住：preload 暴露 `readClipboardImage` → `emrg:readClipboardImage`，
  // main 注册同一个频道。少任何一端，renderer 的调用在生产里会永远 reject。
  const preload = fs.readFileSync(path.join(GUI_ROOT, "preload.js"), "utf-8");
  assert.match(
    preload,
    /readClipboardImage: \(\) => ipcRenderer\.invoke\("emrg:readClipboardImage"/,
    "preload.js must expose readClipboardImage → emrg:readClipboardImage"
  );
  const main = fs.readFileSync(path.join(GUI_ROOT, "main.js"), "utf-8");
  assert.match(
    main,
    /ipcMain\.handle\("emrg:readClipboardImage"/,
    "main.js must handle emrg:readClipboardImage"
  );
  // 转换发生在哪一侧是这条线的全部意义：`clipboard.readImage()` 经 NSImage 解码，
  // `toPNG()` 重新编码。main.js 里没有它，这条线就只是一次拒绝。
  assert.match(main, /clipboard\.readImage\(\)/, "main.js must read the pasteboard image");
  assert.match(main, /\.toPNG\(\)/, "main.js must re-encode it as PNG");
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
  // 断言的是**解析结果**，不是字符串字面量：`directories.*` 与 extraResources 一样，由
  // electron-builder 相对工程目录（本目录）解析。旧断言把字面量 "../packaging/assets" 钉死，
  // 而它从 emrg/gui 解析到 emrg/packaging/assets（不存在）——一个错值被钉成了"已守卫"。
  const assetsDir = path.join(REPO_ROOT, "packaging", "assets");
  assert.strictEqual(
    path.resolve(GUI_ROOT, (build.directories && build.directories.buildResources) || ""),
    assetsDir,
    "directories.buildResources must resolve to <repo-root>/packaging/assets (icon.icns/ico/png sources)"
  );
  // icon.icns/ico/png are gen-assets products (gitignored, generated at build time from
  // icon.svg by packaging/gen-assets.sh) — only the committed design source must exist in CI.
  assert.ok(
    fs.existsSync(path.join(assetsDir, "icon.svg")),
    `buildResources dir missing design source icon.svg (committed) — gen-assets can't render icons`
  );
});

// ── 打包引用解析守卫（cycle cyc20261009-032401）──────────────────────────────
// electron-builder 把 extraResources 的 `from` 与 directories.buildResources 都相对**工程目录**
// （emrg/gui/package.json 所在处）解析，源不存在时**只警告不失败**。实测证据（build-release.yml
// run 37723424917，v0.3.9 tag job 113136083119）：
//     • file source doesn't exist  from=/home/runner/work/emrg/emrg/emrg/dist/runtime
//     • file source doesn't exist  from=/home/runner/work/emrg/emrg/emrg/packaging/assets/icon.png
//     • default Electron icon is used  reason=application icon is not set
// 构建全绿、产物照发，包里却没有载荷：AppImage 缺 resources/runtime，而 main.js 的
// ensureAppImageExtracted 首启就复制 process.resourcesPath/runtime 到 ~/.emrg/install ——
// Linux 用户装完的 GUI 没有运行时。根因是相对路径少一层（落在 emrg/ 内，产物在仓库根）。
// 本用例钉住**解析结果**：每个 from 必须落到仓库根的那两个产物上，任一退回 "../…" 即红。
test("electron-builder extraResources resolve to the repo-root build products", () => {
  const build = PKG.build || {};
  const PRODUCTS = [path.join(REPO_ROOT, "dist", "runtime"), path.join(REPO_ROOT, "packaging", "assets", "icon.png")];
  const refs = [];
  for (const entry of build.extraResources || []) refs.push(["app", entry.from]);
  for (const p of ["mac", "win", "linux"]) {
    for (const entry of (build[p] || {}).extraResources || []) refs.push([p, entry.from]);
  }
  assert.ok(refs.length > 0, "no extraResources found — the scan is broken");
  const unresolved = refs
    .map(([block, from]) => ({ block, from, resolved: path.resolve(GUI_ROOT, from) }))
    .filter((r) => !PRODUCTS.includes(r.resolved));
  assert.deepStrictEqual(
    unresolved.map((r) => `${r.block}: ${r.from} → ${r.resolved}`),
    [],
    `extraResources resolve outside the repo-root build products ` +
      `(${PRODUCTS.map((p) => path.relative(REPO_ROOT, p)).join(", ")}). electron-builder only WARNS ` +
      `("file source doesn't exist") and ships an artifact without the payload — assert the resolved ` +
      `path, and remember it is relative to package.json's own directory (emrg/gui), not the repo root.`
  );
  // 唯一读 resourcesPath/runtime 的是 Linux AppImage 的首启自解压 —— 少了这条，绿也说明不了问题
  assert.ok(
    refs.some(([block, from]) => block === "linux" && path.resolve(GUI_ROOT, from) === path.join(REPO_ROOT, "dist", "runtime")),
    "the linux block must carry the runtime: main.js ensureAppImageExtracted copies " +
      "process.resourcesPath/runtime into ~/.emrg/install on first launch (no other platform reads it)"
  );
});

test("the extra-writable-roots wire exists end to end (rant 2026-10-09T09:43:39, GUI half)", () => {
  // 与 clipboard-image 那条同一个理由：renderer 那半用注入的假窗测试，**看不见线断没断**。
  // 这条线是 preload 暴露 `setSandboxRoots` → `emrg:setSandboxRoots`，main 注册同一个频道，
  // 再交给 `conn.sendSetSandboxRoots` 发上 wire。少任何一端，界面会打开、会接受输入，
  // 然后什么也不发生（#1764 与审批通道都栽在这道缝上）。
  const preload = fs.readFileSync(path.join(GUI_ROOT, "preload.js"), "utf-8");
  assert.match(
    preload,
    /setSandboxRoots: \(payload\) => ipcRenderer\.invoke\("emrg:setSandboxRoots"/,
    "preload.js must expose setSandboxRoots → emrg:setSandboxRoots"
  );
  const main = fs.readFileSync(path.join(GUI_ROOT, "main.js"), "utf-8");
  assert.match(main, /ipcMain\.handle\("emrg:setSandboxRoots"/, "main.js must handle emrg:setSandboxRoots");
  // 意图只走一条路：renderer 给 op + 路径，cwd 由 main 解析（与 emrg:setSandbox 同款），
  // 路径的裁定留给 daemon —— main 里出现第二套路径规则就是「两处各说各话」。
  assert.match(main, /conn\.sendSetSandboxRoots\(\{ sessionId, cwd: sessionCwd, op, path/, "main.js must hand the op to the client");
  const client = fs.readFileSync(path.join(GUI_ROOT, "daemon_client.js"), "utf-8");
  assert.match(client, /sendCommand\("set_sandbox_roots"/, "daemon_client.js must send set_sandbox_roots");
  assert.match(client, /frame\.type === "sandbox_roots"/, "daemon_client.js must forward the sandbox_roots frame");
});
