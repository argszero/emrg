"use strict";
/**
 * theme-guard.test.js — renderer 主题完整性静态守卫。
 *
 * 背景（rant 2026-08-27T20:33:31 → #1047）：React shell 把 Catppuccin 深色硬编码
 * （#1e1e2e/#cdd6f4）而非接 tokens.css 的 var(--*) 主题变量 → 浅色主题失效，发布
 * v0.2.84 前被宿主发现。同类事故链：v0.2.81（init 丢线）、v0.2.82/83（GUI 构建截断）
 * ——均为发布后宿主才发现，本守卫把主题完整性变成 PR CI 即红的静态检查。
 *
 * 双维度：
 *   1. 所有渲染器 CSS（shell.css + css/*.css，tokens.css 除外）中无回退的
 *      `var(--x)` 必须能在 tokens.css 找到定义 —— 未定义变量使声明在计算期失效
 *      （background→transparent、border-radius→0、font-size→inherit），是静默 bug。
 *      实测发现并修复过 4 个此类缺失（--fs-small/--bg-1/--bg-hover/--radius-sm）。
 *   2. Catppuccin 深色调色板 hex 不得出现在 tokens.css 之外（注释剔除后）——
 *      #1047 回归的精确形态：主题色硬编码绕过变量层。
 *
 * 均为静态正则（boot-contract/build-config 同款模式）：廉价、无运行时依赖。
 */

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const RENDERER = path.join(__dirname, "..", "renderer");

function read(rel) {
  return fs.readFileSync(path.join(RENDERER, rel), "utf8");
}

/** 剔除 /* ... *\/ 与 // 注释，返回可扫描的 CSS 文本 */
function stripComments(css) {
  return css
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/[^\n]*/g, "");
}

function rendererCssFiles() {
  const cssDir = path.join(RENDERER, "css");
  const srcDir = path.join(RENDERER, "src");
  const files = [];
  for (const f of fs.readdirSync(cssDir)) if (f.endsWith(".css")) files.push(`css/${f}`);
  // 递归扫描 src/（Node 20+ readdirSync recursive，CI Node 22 支持）：组件级 scoped CSS
  // （src/components/*.css 等）同样必须过守卫，否则静默漏检（cyc20260827-232221 实测）
  for (const f of fs.readdirSync(srcDir, { recursive: true })) {
    if (typeof f === "string" && f.endsWith(".css")) files.push(`src/${f}`);
  }
  return files.filter((f) => !f.endsWith("tokens.css"));
}

test("theme: every bare var(--x) in renderer CSS must be defined in tokens.css", () => {
  // 未定义变量的 var(--x)（无内联回退）使整条声明在 computed-value 期失效——
  // 静默 UI bug（背景透明 / 圆角归零 / 字号继承）。tokens.css 是唯一变量源。
  const tokens = read("css/tokens.css");
  const defined = new Set([...tokens.matchAll(/--([a-z0-9-]+):/g)].map((m) => m[1]));

  const missing = [];
  for (const rel of rendererCssFiles()) {
    const text = stripComments(read(rel));
    for (const m of text.matchAll(/var\(--([a-z0-9-]+)([^)]*)\)/g)) {
      const [, name, rest] = m;
      const hasFallback = rest.includes(","); // var(--x, fallback) 无需定义
      if (!defined.has(name) && !hasFallback) {
        const line = text.slice(0, m.index).split("\n").length;
        missing.push(`${rel}:${line} var(--${name})`);
      }
    }
  }
  assert.deepStrictEqual(
    missing,
    [],
    `bare var(--x) without a tokens.css definition (silent invalid declaration):\n${missing.join("\n")}`
  );
});

test("theme: no Catppuccin dark-palette hexes outside tokens.css (comment-stripped)", () => {
  // #1047 回归（rant 2026-08-27T20:33:31）：shell.css 硬编码 Catppuccin 深色
  // （#1e1e2e base / #cdd6f4 text / #89b4fa blue / #fab387 peach / #a6adc8 subtext
  // / #11111b crust / #313244 #45475a #585b70 surfaces）→ 浅色主题失效。
  // 这些主题色只能存在于 tokens.css 的变量定义中；tokens.css 之外（含注释）出现
  // 即视为绕过主题层的回归。
  const CATPPUCCIN_DARK = [
    "1e1e2e", "cdd6f4", "89b4fa", "fab387", "a6adc8",
    "11111b", "313244", "45475a", "585b70",
  ];
  const hits = [];
  for (const rel of rendererCssFiles()) {
    const text = stripComments(read(rel)).toLowerCase();
    for (const hex of CATPPUCCIN_DARK) {
      if (text.includes(hex)) {
        const line = read(rel).toLowerCase().split("\n").findIndex((l) => l.includes(hex)) + 1;
        hits.push(`${rel}:${line} #${hex}`);
      }
    }
  }
  assert.deepStrictEqual(
    hits,
    [],
    `Catppuccin dark-palette hex hardcoded outside tokens.css (light-theme regression, #1047):\n${hits.join("\n")}`
  );
});
