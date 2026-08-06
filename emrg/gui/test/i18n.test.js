"use strict";
/**
 * i18n.test.js — 轻量国际化单元测试（rant 21:19 Stage 1）。
 * 覆盖：detectLocale（zh 前缀/其他/无 navigator 兜底）、t()（跟随系统/手动覆盖/插值/未知 key 回退）、
 *       setLocale 持久化、apply() 应用 data-i18n 静态文案。
 * 方法：vm.createContext 加载 i18n.js（与 renderer.smoke 同模式）。
 */

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const RENDERER_JS = path.join(__dirname, "..", "renderer", "js");

/** 构造 i18n 沙箱；默认 en-US 系统 */
function makeSandbox(overrides = {}) {
  const store = new Map();
  const win = {
    console,
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    navigator: { language: "en-US" },
    document: { querySelectorAll: () => [], getElementById: () => null },
    ...overrides,
  };
  win.window = win;
  const ctx = vm.createContext(win);
  const code = fs.readFileSync(path.join(RENDERER_JS, "i18n.js"), "utf8");
  vm.runInContext(code, ctx, { filename: "renderer/js/i18n.js" });
  return { ctx, win, store };
}

/** 运行表达式并取回值（跨 Realm 逐个字段断言） */
function evalIn(ctx, expr) {
  return vm.runInContext(expr, ctx);
}

test("detectLocale：zh 前缀 → zh，其他 → en，navigator 异常 → zh 兜底", () => {
  const { ctx } = makeSandbox({ navigator: { language: "zh-CN" } });
  assert.strictEqual(evalIn(ctx, "I18N.detectLocale()"), "zh");
  const { ctx: ctx2 } = makeSandbox({ navigator: { language: "en-US" } });
  assert.strictEqual(evalIn(ctx2, "I18N.detectLocale()"), "en");
  // navigator 存在但不可用（null）→ 抛错 → zh 兜底（对应无 navigator 环境）
  const { ctx: ctx3 } = makeSandbox({ navigator: null });
  assert.strictEqual(evalIn(ctx3, "I18N.detectLocale()"), "zh");
});

test("t：en 系统默认返回英文文案", () => {
  const { ctx } = makeSandbox();
  assert.strictEqual(evalIn(ctx, 'I18N.t("sidebar.newChat")'), "＋ New chat");
  assert.strictEqual(evalIn(ctx, 'I18N.t("copy.disconnected")'), "Connection lost — reconnecting…");
});

test("t：zh 系统默认返回中文文案", () => {
  const { ctx } = makeSandbox({ navigator: { language: "zh-CN" } });
  assert.strictEqual(evalIn(ctx, 'I18N.t("sidebar.newChat")'), "＋ 新对话");
  assert.strictEqual(evalIn(ctx, 'I18N.t("copy.disconnected")'), "连接中断了，正在重新连接…");
});

test("setLocale：手动覆盖 en/zh，localStorage 持久化；空值恢复跟随系统", () => {
  const { ctx, store } = makeSandbox({ navigator: { language: "zh-CN" } });
  // zh 系统默认中文
  assert.strictEqual(evalIn(ctx, 'I18N.t("sidebar.newChat")'), "＋ 新对话");
  // 手动切英文 → 立即生效
  evalIn(ctx, 'I18N.setLocale("en")');
  assert.strictEqual(evalIn(ctx, 'I18N.t("sidebar.newChat")'), "＋ New chat");
  assert.strictEqual(store.get("emrg.locale"), "en", "localStorage 应持久化 en");
  // 切回中文
  evalIn(ctx, 'I18N.setLocale("zh")');
  assert.strictEqual(evalIn(ctx, 'I18N.t("sidebar.newChat")'), "＋ 新对话");
  // 恢复跟随系统 → 回到 zh（系统是 zh）
  evalIn(ctx, 'I18N.setLocale("")');
  assert.strictEqual(evalIn(ctx, 'I18N.t("sidebar.newChat")'), "＋ 新对话");
});

test("t：{var} 插值", () => {
  const { ctx } = makeSandbox();
  assert.strictEqual(
    evalIn(ctx, 'I18N.t("copy.aboutEvolution", { n: 42 })'),
    "EMRG has self-evolved 42 times — thanks for every bit of feedback"
  );
  const { ctx: zh } = makeSandbox({ navigator: { language: "zh-CN" } });
  assert.strictEqual(
    evalIn(zh, 'I18N.t("copy.aboutEvolution", { n: 7 })'),
    "EMRG 已自我成长 7 次，感谢你的每一次反馈"
  );
});

test("t：未知 key 回退 zh 词典，仍缺失则原样返回", () => {
  const { ctx } = makeSandbox();
  // zh 有而 en 没有的 key（此处所有 key 双语齐全 → 用 zh 兜底语义验证）
  assert.strictEqual(evalIn(ctx, 'I18N.t("definitely.missing.key")'), "definitely.missing.key");
});

test("apply：data-i18n / data-i18n-placeholder / data-i18n-title 应用当前语言", () => {
  const nodes = {
    text: { attrs: { "data-i18n": "sidebar.newChat" }, textContent: "", placeholder: "", title: "", getAttribute(k) { return this.attrs[k] ?? null; } },
    ph: { attrs: { "data-i18n-placeholder": "composer.placeholder" }, textContent: "", placeholder: "", title: "", getAttribute(k) { return this.attrs[k] ?? null; } },
    ti: { attrs: { "data-i18n-title": "composer.send" }, textContent: "", placeholder: "", title: "", getAttribute(k) { return this.attrs[k] ?? null; } },
  };
  const document = {
    querySelectorAll(sel) {
      if (sel === "[data-i18n]") return [nodes.text];
      if (sel === "[data-i18n-placeholder]") return [nodes.ph];
      if (sel === "[data-i18n-title]") return [nodes.ti];
      return [];
    },
    getElementById: () => null,
  };
  const { ctx } = makeSandbox({ document, navigator: { language: "en-US" } });
  evalIn(ctx, "I18N.apply()");
  assert.strictEqual(nodes.text.textContent, "＋ New chat");
  assert.strictEqual(nodes.ph.placeholder, "Message EMRG… (Enter to send / Shift+Enter for new line)");
  assert.strictEqual(nodes.ti.title, "Send");
});

test("词典完整性：zh 与 en 键集合一致", () => {
  const { ctx } = makeSandbox();
  const zhKeys = evalIn(ctx, "Object.keys(I18N.DICTS.zh).sort().join(',')");
  const enKeys = evalIn(ctx, "Object.keys(I18N.DICTS.en).sort().join(',')");
  assert.strictEqual(zhKeys, enKeys, "zh/en 词典键必须一一对应（缺失键会导致 t() 静默回退）");
  const n = zhKeys.split(",").length;
  assert.ok(n > 60, `词典应覆盖全部 Stage 1 文案（实际 ${n} 键）`);
});
