"use strict";
/**
 * image-hash.test.js — 图片落盘用的摘要必须是**打包运行时**支持的（issue #2363）。
 *
 * 错在哪：`main.js` 的 `emrg:saveImage` 曾用 `crypto.createHash("blake2b512")` 给图片文件
 * 名算 8 位去重前缀。Electron 自带的 Node 编译在 BoringSSL 上，**没有 blake2b**，所以打包版
 * 每次粘贴/拖拽/选择图片都抛 `Digest method not supported` —— 这个功能从 #1109 引入起
 * 一次都没成功过。
 *
 * 为什么测试没拦住：开发机/CI 的 `node` 有 blake2b512（本机 v26.5.0 实测 `true`），于是
 * 所有 JS 测试全绿，而唯一会抛的是 Electron 主进程里的那个 IPC handler，没有测试走到它。
 *
 * 本测试钉两件事：
 *   1. **CI 腿（处处可跑）**：`main.js` 里每个 `createHash("<literal>")` 的字面量都必须在本
 *      文件记录的 Electron 摘要白名单内 —— 用 blake2b512 的老树在这里即红。
 *      白名单是**实测**来的，不是猜的（见 ELECTRON_DIGESTS 上方注释与 `--print` 复现命令）。
 *   2. **真机腿（附近有 Electron 才跑）**：直接问那个运行时 `crypto.getHashes()`，断言
 *      `main.js` 用到的摘要一个都不缺。白名单因此不会悄悄漂移成「测试和现实各说一套」。
 *      CI 用 `npm ci --omit=dev` 装依赖，没有 Electron 二进制，所以这腿在 CI 里是**跳过**，
 *      CI 的真门禁是第 1 条。
 *
 * 非空洞：第 1 条先断言「提取到了至少一个摘要字面量」。把算法名挪进变量
 * （`createHash(ALGO)`）会让提取结果为空 —— 那时它必须**红**，不能因为「没东西可查」而绿：
 * 一个什么都没量的守卫，对任何树都会回答「没问题」。
 */
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const GUI_ROOT = path.join(__dirname, "..");
const MAIN_JS = path.join(GUI_ROOT, "main.js");

// Electron 自带的 Node（BoringSSL）`crypto.getHashes()` 的全集。复现：
//   ELECTRON_RUN_AS_NODE=1 /Applications/EMRG.app/Contents/MacOS/EMRG \
//     -e "console.log(require('crypto').getHashes().join(','))"
// 实测 2026-10-18，Electron 31.7.7 / Node 20.18.0（package.json 钉 `electron ^31.3.0`）。
// 注意这份名单里**没有任何 blake2b / sha3 / shake** —— BoringSSL 只提供下面这些。
const ELECTRON_DIGESTS = new Set([
  "md4",
  "md5",
  "ripemd160",
  "sha1",
  "sha224",
  "sha256",
  "sha384",
  "sha512",
  "sha512-256",
]);

/** `main.js` 里每个 `createHash("<literal>")` 的摘要名，按出现顺序。 */
function digestsUsed(source) {
  const out = [];
  const re = /createHash\(\s*["']([^"']+)["']\s*\)/g;
  let m;
  while ((m = re.exec(source)) !== null) out.push(m[1]);
  return out;
}

/** 找一台能当 Node 用的 Electron 二进制；找不到返回 null（不是失败）。 */
function findElectron() {
  const fromEnv = process.env.EMRG_ELECTRON_BIN;
  if (fromEnv && fs.existsSync(fromEnv)) return fromEnv;
  const dist = path.join(GUI_ROOT, "node_modules", "electron", "dist");
  const candidates = [
    path.join(dist, "electron"), // linux
    path.join(dist, "electron.exe"), // windows
    path.join(dist, "Electron.app", "Contents", "MacOS", "Electron"), // darwin
    "/Applications/EMRG.app/Contents/MacOS/EMRG", // 打包版（本机已安装）
    path.join(process.env.HOME || "", "Applications", "EMRG.app", "Contents", "MacOS", "EMRG"),
  ];
  for (const c of candidates) {
    try {
      if (fs.existsSync(c)) return c;
    } catch {
      // 一个够不着的候选不是失败：下一个
    }
  }
  return null;
}

test("every digest in main.js is one the packaged Electron runtime reports", () => {
  const used = digestsUsed(fs.readFileSync(MAIN_JS, "utf-8"));
  assert.ok(
    used.length > 0,
    "提取不到任何 createHash(\"<literal>\") —— 算法名挪进了变量，本测试就量不到东西了；" +
      "请把它写成字面量，或同步更新 image-hash.test.js 的提取规则",
  );
  const unknown = used.filter((d) => !ELECTRON_DIGESTS.has(d));
  assert.deepStrictEqual(
    unknown,
    [],
    `main.js 用了 Electron 不支持的摘要 ${JSON.stringify(unknown)}：` +
      `Electron 的 BoringSSL 只报 ${[...ELECTRON_DIGESTS].join("/")}；` +
      "开发机的 node 有别的摘要不算数（issue #2363）",
  );
});

test("the local Electron binary reports every digest main.js uses", (t) => {
  const bin = findElectron();
  if (!bin) {
    t.skip("no Electron binary here (CI installs `npm ci --omit=dev`); the allowlist leg above is the gate");
    return;
  }
  const out = execFileSync(
    bin,
    ["-e", "console.log(require('crypto').getHashes().join(','))"],
    {
      env: { ...process.env, ELECTRON_RUN_AS_NODE: "1" },
      encoding: "utf-8",
      timeout: 60_000,
    },
  );
  const available = new Set(out.trim().split(",").filter(Boolean));
  const used = digestsUsed(fs.readFileSync(MAIN_JS, "utf-8"));
  assert.ok(used.length > 0, "提取不到任何摘要字面量 —— 见上一条的说明");
  const missing = used.filter((d) => !available.has(d));
  assert.deepStrictEqual(
    missing,
    [],
    `${bin} 没有 ${JSON.stringify(missing)}（它报的是 ${[...available].join("/")}）`,
  );
});
