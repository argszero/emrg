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

// ── Stage 2（rant 21:19）：动态文案本地化 ────────────────
test("Stage2：动态文案键（app/chat/dlg/panel）双语齐全", () => {
  const { ctx } = makeSandbox({ navigator: { language: "en-US" } });
  // 抽查关键动态文案
  assert.strictEqual(evalIn(ctx, 'I18N.t("app.needSession")'), "Start a conversation first.");
  assert.strictEqual(evalIn(ctx, 'I18N.t("app.versionInfo", { ver: "0.2.8", id: "srv", model: "m", n: 42 })'),
    "EMRG GUI v0.2.8 · Instance srv · Model m · Evolved 42 times");
  assert.strictEqual(evalIn(ctx, 'I18N.t("chat.elapsed", { s: "1.2s" })'), "took 1.2s");
  assert.strictEqual(evalIn(ctx, 'I18N.t("dlg.deleteModelBody", { name: "gpt-4o" })'),
    '"gpt-4o" will be removed from available models.');
  assert.strictEqual(evalIn(ctx, 'I18N.t("app.rewound", { index: 3, n: 5 })'),
    "Rewound to message point #3, removed 5 records.");
  const { ctx: zh } = makeSandbox({ navigator: { language: "zh-CN" } });
  assert.strictEqual(evalIn(zh, 'I18N.t("app.needSession")'), "请先创建一个对话。");
  assert.strictEqual(evalIn(zh, 'I18N.t("dlg.deleteModelBody", { name: "gpt-4o" })'), "「gpt-4o」将从可用模型里移除。");
});

test("Stage2：成长卡/关于区静态文案键（#501 吸收）", () => {
  const { ctx } = makeSandbox({ navigator: { language: "en-US" } });
  assert.strictEqual(evalIn(ctx, 'I18N.t("copy.growthCountPrefix")'), "Self-evolved");
  assert.strictEqual(evalIn(ctx, 'I18N.t("copy.times")'), "times");
  const { ctx: zh } = makeSandbox({ navigator: { language: "zh-CN" } });
  assert.strictEqual(evalIn(zh, 'I18N.t("copy.growthCount", { n: 7 })'), "已自我进化 7 次");
});

// ── Stage 3（cycle 20260806-225341）：index.html 无遗漏硬编码中文 ──
test("Stage3：index.html 中文字符串必须带 data-i18n（防漏网回归）", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "renderer", "index.html"), "utf8");
  const cjk = /[\u4e00-\u9fff]/;
  const leaks = [];
  for (const [i, line] of html.split("\n").entries()) {
    const s = line.trim();
    if (!cjk.test(s)) continue;            // 无中文
    if (s.startsWith("<!--")) continue;    // HTML 注释
    if (s.includes("data-i18n")) continue; // 已接入 i18n（含 data-i18n-title/placeholder）
    leaks.push(`L${i + 1}: ${s.slice(0, 80)}`);
  }
  assert.strictEqual(leaks.length, 0, "index.html 存在未接入 i18n 的中文（补 data-i18n 或 data-i18n-title/placeholder）:\n" + leaks.join("\n"));
});

test("Stage3：新增静态键双语一致（growth-card title / about / status-dot / toast 静态）", () => {
  const { ctx } = makeSandbox({ navigator: { language: "en-US" } });
  assert.strictEqual(evalIn(ctx, 'I18N.t("copy.growthCardTitle")'), "EMRG reports every self-improvement automatically");
  assert.strictEqual(evalIn(ctx, 'I18N.t("sidebar.statusTitle")'), "Connection status");
  assert.strictEqual(evalIn(ctx, 'I18N.t("sidebar.backToBottom")'), "Back to bottom");
  assert.strictEqual(evalIn(ctx, 'I18N.t("settings.aboutTitle")'), "About");
  assert.strictEqual(evalIn(ctx, 'I18N.t("settings.recentTitle")'), "Recent improvements");
  assert.strictEqual(evalIn(ctx, 'I18N.t("settings.modelNamePlaceholder")'), "e.g. deepseek-v3");
  assert.strictEqual(evalIn(ctx, 'I18N.t("copy.evolutionToastMsgStatic")'), "It learned something new and is even better now.");
  const { ctx: zh } = makeSandbox({ navigator: { language: "zh-CN" } });
  assert.strictEqual(evalIn(zh, 'I18N.t("settings.aboutTitle")'), "关于");
  assert.strictEqual(evalIn(zh, 'I18N.t("settings.recentTitle")'), "最近改进");
  assert.strictEqual(evalIn(zh, 'I18N.t("sidebar.backToBottom")'), "回到底部");
});

// ── Stage 3b（cycle 20260806-230036）：renderer JS 运行时中文字符串防漏网 ──
test("Stage3b：renderer JS 无未接入 i18n 的中文字符串（防 JS 侧回归）", () => {
  // 扫描 renderer/js/*.js：含中文且非注释、非 i18n 取词、非词典、非功能正则的行
  const fs2 = require("node:fs");
  const path2 = require("node:path");
  const cjk = /[\u4e00-\u9fff]/;
  const leaks = [];
  for (const f of fs2.readdirSync(path.join(__dirname, "..", "renderer", "js"))) {
    if (!f.endsWith(".js")) continue;
    if (f === "i18n.js") continue; // 词典本身全是中文（合法）
    const src = fs2.readFileSync(path.join(__dirname, "..", "renderer", "js", f), "utf8");
    // 兜底词典块（如 error-boundary.js FALLBACK_DICTS）：与 i18n.js 词典同性质，整块跳过
    let inFallbackDict = false;
    for (const [i, line] of src.split("\n").entries()) {
      let s = line.trim();
      if (s.includes("FALLBACK_DICTS")) { inFallbackDict = true; continue; }
      if (inFallbackDict) {
        if (/^};$/.test(s)) inFallbackDict = false;
        continue;
      }
      if (!cjk.test(s)) continue;
      // 剥离注释：行首 //、行尾 // 注释、/* */ 块
      s = s.replace(/^\/\/.*$/, "").replace(/^\*.*$/, "").replace(/ \/\/.*$/, "").replace(/\/\*.*?\*\//g, "").trim();
      if (!cjk.test(s)) continue;                           // 仅注释含中文 → 跳过
      if (s.includes("_t(") || s.includes("EMRG_I18N")) continue; // 已取词
      if (s.includes("i18n")) continue;                    // i18n 基础设施/注释
      if (s.includes("match(") || s.includes("replace(")) continue; // 功能正则（如文件路径提取）
      if (s.includes("ensure_ascii")) continue;            // 序列化说明
      leaks.push(`${f}:L${i + 1}: ${s.slice(0, 80)}`);
    }
  }
  assert.strictEqual(leaks.length, 0, "renderer JS 存在未接入 i18n 的中文:\n" + leaks.join("\n"));
});

test("Stage3b：最近改进列表文案双语（#502 evolution_summary 侧）", () => {
  const { ctx } = makeSandbox({ navigator: { language: "en-US" } });
  assert.strictEqual(evalIn(ctx, 'I18N.t("app.recentImprovements")'), "Recent improvements");
  assert.strictEqual(evalIn(ctx, 'I18N.t("app.noImprovements")'), "No improvements recorded yet — type /rant to drive the first evolution");
  const { ctx: zh } = makeSandbox({ navigator: { language: "zh-CN" } });
  assert.strictEqual(evalIn(zh, 'I18N.t("app.recentImprovements")'), "最近改进");
});

// ── Orphan-key guard（cycle 20260813-223026：#771 ❌ 教训固化）────────
// #755 清理了 8 组孤儿键、#771 评审 ❌ 检出 7 个孤儿 i18n keys + 12 个未用 CSS 类——
// 两个方向都缺自动守卫：①词典里定义了但全库无引用的孤儿键（删除功能后残留）
// ②标记/代码里引用了但词典未定义的键（拼写错误会静默回退显示原始 key）。
// 本测试扫描 index.html data-i18n* 属性 + renderer JS 全部取词点，双向校验。
test("i18n 守卫：词典键无孤儿（定义了必须被引用）且引用键无缺失（引用必须已定义）", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "renderer", "index.html"), "utf8");
  const { ctx } = makeSandbox({ navigator: { language: "zh-CN" } });
  const dictKeys = new Set(evalIn(ctx, "Object.keys(I18N.DICTS.zh)"));
  assert.ok(dictKeys.size > 60, `词典应有键（实际 ${dictKeys.size}）`);

  // 1) 收集引用：index.html data-i18n / data-i18n-placeholder / data-i18n-title
  const used = new Set();
  const attrRe = /data-i18n(?:-placeholder|-title)?="([^"]+)"/g;
  let m;
  while ((m = attrRe.exec(html)) !== null) used.add(m[1]);

  // 2) 收集引用：renderer JS 全部取词调用（跳过 i18n.js 词典本身）。
  //    取 t-call 整体跨度（含三元前缀 _t(cond ? "A" : "B")），再取其中所有字面键；
  //    模板串 t(`tool.${base}.doing`) → 静态段拼 glob（tool.*.doing）。
  const globs = [];
  const callRe = /(?:_t|I18N\.t|EMRG_I18N\.t|\bt)\([^)]*\)/g;
  for (const f of fs.readdirSync(path.join(__dirname, "..", "renderer", "js"))) {
    if (!f.endsWith(".js") || f === "i18n.js") continue;
    const src = fs.readFileSync(path.join(__dirname, "..", "renderer", "js", f), "utf8");
    let cm;
    while ((cm = callRe.exec(src)) !== null) {
      const body = cm[0];
      if (body.includes("`")) {
        const tplRe = /`([^`]*)`/;
        const tpl = (tplRe.exec(body) || [])[1] || "";
        if (tpl.includes("${")) globs.push(tpl.replace(/\$\{[^}]+\}/g, "*"));
        else if (tpl) used.add(tpl);
        continue;
      }
      const keyRe = /"([a-z][a-zA-Z0-9]*(\.[a-zA-Z][a-zA-Z0-9]*)+)"/g;
      let km;
      while ((km = keyRe.exec(body)) !== null) used.add(km[1]);
    }
    // 对象属性值引用（命令注册表 hint: "cmd.clear.hint" 形态）
    const propRe = /\b(?:hint|title|key|label|placeholder|text|msg):\s*"([a-z][a-zA-Z0-9]*(\.[a-zA-Z][a-zA-Z0-9]*)+)"/g;
    let pm;
    while ((pm = propRe.exec(src)) !== null) used.add(pm[1]);
  }
  // glob → 正则：转义特殊字符，* → .*
  const globRes = globs.map((g) => new RegExp("^" + g.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*") + "$"));

  // 3) 方向 A：词典键必须被引用（孤儿检测）
  const orphans = [...dictKeys].filter(
    (k) => !used.has(k) && !globRes.some((re) => re.test(k))
  );
  assert.strictEqual(orphans.length, 0, `孤儿 i18n 键（词典定义但无引用，应删除）:\n  ${orphans.join("\n  ")}`);

  // 4) 方向 B：引用键必须已在词典定义（拼写错误检测）
  const missing = [...used].filter((k) => !dictKeys.has(k));
  assert.strictEqual(missing.length, 0, `引用了但词典未定义的 i18n 键（拼写错误会静默回退）:\n  ${missing.join("\n  ")}`);
});
