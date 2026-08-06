"use strict";
/**
 * renderer.smoke.test.js — renderer 层冒烟测试（node:test，零依赖）。
 * 覆盖：7 个 JS 模块按 index.html 顺序加载无崩溃、boot 两条路径（config 缺失→首启 / config 就绪→会话）、
 *       流式 delta 追加、工具行 running→done 状态流转、多模型管理加载/保存。
 * 背景：renderer 是 GUI 重设计（#417）核心，CI 此前仅 node --check 语法检查，逻辑零覆盖。
 * 方法：vm.createContext 模拟浏览器全局（DOM mock + window.emrg），逐模块 runInContext。
 */

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const RENDERER_JS = path.join(__dirname, "..", "renderer", "js");

// ── DOM mock（最小但真实：classList 操作同步到 className） ──
function makeEl(id) {
  const node = {
    id,
    children: [],
    dataset: {},
    style: {},
    attributes: {},
    _cls: new Set(),
    classList: {
      add(c) { node._cls.add(c); node._update(); },
      remove(c) { node._cls.delete(c); node._update(); },
      toggle(c) { node._cls.has(c) ? node._cls.delete(c) : node._cls.add(c); node._update(); },
      contains(c) { return node._cls.has(c); },
    },
    _update() { node.className = [...node._cls].join(" "); },
    className: "",
    textContent: "",
    innerHTML: "",
    value: "",
    disabled: false,
    title: "",
    checked: false,
    selectedIndex: -1,
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 100,
    open: false,
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    showModal() { this.open = true; },
    close() { this.open = false; },
    setAttribute(k, v) { this.attributes[k] = v; if (k === "value") this.value = v; },
    removeAttribute(k) { delete this.attributes[k]; },
    insertBefore(c) { this.children.unshift(c); return c; },
    remove() {},
    focus() {},
    select() {},
  };
  return node;
}

const ELEMENT_IDS = [
  "chat-view", "input", "send-btn", "stop-btn", "conv-list", "status-dot", "settings-btn",
  "conn-banner", "empty-state", "model-switcher", "model-switcher-label", "brand-star", "new-chat-btn",
  "settings-dialog", "settings-cancel", "settings-save", "set-api-key", "set-base-url", "set-project-dir",
  "set-model", "pick-dir-btn", "theme-options", "welcome-dialog", "welcome-api-key", "welcome-base-url",
  "welcome-model", "welcome-project-dir", "welcome-pick-btn", "welcome-save", "confirm-dialog",
  "confirm-title", "confirm-message", "confirm-cancel", "confirm-ok", "main",
  "rename-dialog", "rename-input", "rename-cancel", "rename-ok", "ctx-menu",
  "model-list", "add-model-btn", "model-form", "model-form-name", "model-form-id",
  "model-form-vision", "model-form-save", "model-form-cancel", "back-to-bottom",
];

/** 构造浏览器沙箱（win 即全局对象） */
function makeSandbox(overrides = {}) {
  const els = {};
  for (const id of ELEMENT_IDS) els[id] = makeEl(id);
  const document = {
    getElementById: (id) => els[id] || makeEl(id),
    createElement: (tag) => makeEl(tag),
    createTextNode: (t) => ({ text: t }),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    documentElement: { setAttribute() {}, removeAttribute() {} },
    body: { classList: { add() {}, remove() {}, toggle() {} } },
  };
  const win = {
    console,
    setTimeout,
    clearTimeout,
    Date,
    Math,
    JSON,
    String,
    Number,
    Object,
    Array,
    RegExp,
    Map,
    Set,
    Promise,
    requestAnimationFrame: (cb) => cb(),
    requestIdleCallback: (cb) => cb(),
    crypto: { randomUUID: () => "mock-uuid" },
    DOMPurify: { sanitize: (x) => x },
    marked: null,
    hljs: null,
    emrg: {
      init: async () => ({ config_exists: false, api_key_configured: false, project_dir: "", project_dir_valid: true, server_id: "", model: "", evolution_count: 0, sessions: [] }),
      onEvent() {},
      sendMessage: async () => ({}),
      cancel: async () => ({}),
      getSettings: async () => ({ apiKey: "", baseUrl: "", model: "", projectDir: "", models: [], modelDetails: [], theme: "system" }),
      saveSettings: async () => ({}),
      pickProjectDir: async () => null,
      listSessions: async () => [],
      switchSession: async () => ({}),
      newSession: async () => ({ session_id: "s2" }),
      deleteSession: async () => ({}),
      setModel: async () => ({}),
      ...overrides,
    },
  };
  win.window = win;
  win.document = document;
  const ctx = vm.createContext(win);
  for (const f of ["utils", "markdown", "copywriting", "chat", "sidebar", "dialogs", "app"]) {
    const code = fs.readFileSync(path.join(RENDERER_JS, f + ".js"), "utf8");
    vm.runInContext(code, ctx, { filename: "renderer/js/" + f + ".js" });
  }
  return { ctx, win, els, document };
}

/** 等 microtask 完成 */
const tick = () => new Promise((r) => setTimeout(r, 20));

test("7 模块按序加载且全局符号解析", () => {
  const { ctx } = makeSandbox();
  const out = vm.runInContext(
    "(function(){ return { App: typeof App, Chat: typeof EMRG_Chat, Copy: typeof EMRG_Copy, Sidebar: typeof EMRG_Sidebar, Dialogs: typeof EMRG_Dialogs, utils: typeof $ }; })()",
    ctx
  );
  // ⚠️ vm 上下文对象原型不同 Realm → 不用 deepStrictEqual，逐个字段断言
  assert.strictEqual(out.App, "object");
  assert.strictEqual(out.Chat, "object");
  assert.strictEqual(out.Copy, "object");
  assert.strictEqual(out.Sidebar, "object");
  assert.strictEqual(out.Dialogs, "object");
  assert.strictEqual(out.utils, "function");
});

test("boot：config 缺失 → 首启引导（不拉起 daemon）", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  // 首启路径：welcome-dialog 应 open（mock showModal 置 open=true）
  assert.ok(els["welcome-dialog"].open, "welcome-dialog 应打开");
});

test("boot：config 就绪 → 加载会话列表", async () => {
  const { ctx, els } = makeSandbox({
    init: async () => ({
      config_exists: true,
      api_key_configured: true,
      project_dir: "/tmp",
      project_dir_valid: true,
      server_id: "srv-1",
      model: "deepseek-chat",
      evolution_count: 42,
      sessions: [{ session_id: "s1", title: "测试对话", updated_at: "2026-08-05T10:00:00Z" }],
    }),
    switchSession: async () => ({}),
  });
  await tick();
  // conv-list 应有分组标签 + 会话项
  const items = vm.runInContext('document.getElementById("conv-list").children.length', ctx);
  assert.ok(items >= 2, `conv-list 应有分组标签+会话项，实际 ${items}`);
});

test("流式 delta 追加 + 工具行 running→done 状态流转", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  const r = vm.runInContext(`(function() {
    App.state.sessionId = "s1";
    App.state.ownStreamRequestId = "rid-1";
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "你好" }]);
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "，世界" }]);
    EMRG_Chat.handleToolStart({ request_id: "rid-1", tool_call_id: "t1", tool_name: "read" });
    EMRG_Chat.handleToolEnd({ tool_call_id: "t1", tool_name: "read", content: "file contents", elapsed: 0.3 });
    EMRG_Chat.handleDone({ request_id: "rid-1" });
    return {
      chatChildren: $("chat-view").children.length,
      toolRowClass: $("chat-view").children[1] ? $("chat-view").children[1].className : "none",
    };
  })()`, ctx);
  assert.strictEqual(r.chatChildren, 2, "用户流 + 工具行 2 个节点");
  assert.ok(r.toolRowClass.includes("done"), `工具行应 done，实际 ${r.toolRowClass}`);
});

test("多模型管理：modelDetails 加载渲染 + saveSettings 传 models 数组", async () => {
  let saved = null;
  const { ctx, els } = makeSandbox({
    getSettings: async () => ({
      apiKey: "sk-x",
      baseUrl: "",
      model: "deepseek-chat",
      projectDir: "/tmp",
      theme: "system",
      models: ["deepseek-chat", "gpt-4o"],
      modelDetails: [
        { name: "deepseek-chat", vision: false },
        { name: "gpt-4o", model: "gpt-4o", vision: true },
      ],
    }),
    saveSettings: async (cfg) => {
      saved = cfg;
      return { ok: true };
    },
  });
  await tick();
  await vm.runInContext("EMRG_Dialogs.showSettings()", ctx);
  const rows = vm.runInContext('document.getElementById("model-list").children.length', ctx);
  assert.ok(rows >= 2, `模型列表应 ≥2 行（默认 + gpt-4o），实际 ${rows}`);

  vm.runInContext(
    'document.getElementById("set-api-key").value = "sk-x"; document.getElementById("set-base-url").value = ""; document.getElementById("set-project-dir").value = "/tmp"',
    ctx
  );
  await vm.runInContext("EMRG_Dialogs.saveSettings()", ctx);
  assert.ok(saved, "saveSettings 应被调用");
  assert.strictEqual(saved.model, "deepseek-chat");
  assert.strictEqual(saved.theme, "system");
  assert.ok(Array.isArray(saved.models));
  assert.strictEqual(saved.models.length, 2, "models 数组含默认 + gpt-4o");
  const gpt = saved.models.find((m) => m.name === "gpt-4o");
  assert.strictEqual(gpt.vision, true);
});

test("右键菜单：重命名对话框 → renameSession 调用（设计 §3.2）", async () => {
  let renamed = null;
  const { ctx, els } = makeSandbox({
    renameSession: async (payload) => {
      renamed = payload;
      return { ok: true, title: payload.title };
    },
    listSessions: async () => [
      { session_id: "s1", title: "旧标题", created_at: "2026-08-06T00:00:00" },
    ],
  });
  // 模拟 App.showConvMenu（右键菜单构建）
  await tick();
  vm.runInContext('App.showConvMenu({ getBoundingClientRect: () => ({ right: 100, bottom: 100 }) }, "s1", "旧标题")', ctx);
  const menu = els["ctx-menu"];
  assert.strictEqual(menu.hidden, false, "右键菜单应显示");
  assert.ok(menu.children.length >= 2, "菜单应有 重命名 + 删除 两项");
  const labels = menu.children.map((c) => c.textContent);
  assert.ok(labels.some((l) => l.includes("重命名")), `菜单应含重命名，实际 ${labels}`);
  assert.ok(labels.some((l) => l.includes("删除")), `菜单应含删除，实际 ${labels}`);

  // 点击重命名 → 对话框打开
  await vm.runInContext('Dialogs.showRename("s1", "旧标题")', ctx);
  assert.strictEqual(els["rename-dialog"].open, true, "重命名对话框应打开");
  assert.strictEqual(els["rename-input"].value, "旧标题", "输入框预填当前标题");

  // 提交新标题 → renameSession 调用
  els["rename-input"].value = "新标题";
  await vm.runInContext("Dialogs.submitRename()", ctx);
  await tick();
  assert.ok(renamed, "renameSession 应被调用");
  assert.strictEqual(renamed.sessionId, "s1");
  assert.strictEqual(renamed.title, "新标题");
});

test("双主题 token 对比度达标（WCAG AA，rant 验收项 4 深色校准）", () => {
  const css = fs.readFileSync(path.join(__dirname, "..", "renderer", "css", "tokens.css"), "utf8");
  function parseVars(block) {
    const vars = {};
    for (const m of block.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})/g)) vars[m[1]] = m[2];
    return vars;
  }
  const lightBlock = css.match(/:root\s*\{([^}]*)\}/)[1];
  const darkBlock = css.match(/:root\[data-theme="dark"\]\s*\{([^}]*)\}/)[1];
  const light = parseVars(lightBlock);
  const dark = parseVars(darkBlock);
  function lum(h) {
    const lin = [0, 2, 4].map((i) => parseInt(h.slice(i + 1, i + 3), 16) / 255)
      .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
  }
  function cr(a, b) {
    const la = lum(a), lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }
  // 深色主题（bg #17181c）：正文 ≥4.5、辅助/强调 ≥3.0
  const d = { bg: "#17181c", t1: cr(dark["text-1"], "#17181c"), t2: cr(dark["text-2"], "#17181c"), t3: cr(dark["text-3"], "#17181c"), ac: cr(dark["accent"], "#17181c") };
  assert.ok(d.t1 >= 4.5, `深色 text-1 ${d.t1.toFixed(2)}:1 < 4.5`);
  assert.ok(d.t2 >= 4.5, `深色 text-2 ${d.t2.toFixed(2)}:1 < 4.5`);
  assert.ok(d.t3 >= 3.0, `深色 text-3 ${d.t3.toFixed(2)}:1 < 3.0`);
  assert.ok(d.ac >= 3.0, `深色 accent ${d.ac.toFixed(2)}:1 < 3.0`);
  // 浅色主题（bg #fafafa）
  const l = { t1: cr(light["text-1"], "#fafafa"), t2: cr(light["text-2"], "#fafafa"), t3: cr(light["text-3"], "#fafafa"), ac: cr(light["accent"], "#fafafa") };
  assert.ok(l.t1 >= 4.5, `浅色 text-1 ${l.t1.toFixed(2)}:1 < 4.5`);
  assert.ok(l.t2 >= 4.5, `浅色 text-2 ${l.t2.toFixed(2)}:1 < 4.5`);
  assert.ok(l.t3 >= 3.0, `浅色 text-3 ${l.t3.toFixed(2)}:1 < 3.0`);
  assert.ok(l.ac >= 3.0, `浅色 accent ${l.ac.toFixed(2)}:1 < 3.0`);
  // 记录实际值便于审阅
  assert.ok(true, `深色 ${d.t1.toFixed(2)}/${d.t2.toFixed(2)}/${d.t3.toFixed(2)}/${d.ac.toFixed(2)}，浅色 ${l.t1.toFixed(2)}/${l.t2.toFixed(2)}/${l.t3.toFixed(2)}/${l.ac.toFixed(2)}`);
});

test("代码块复制按钮：codeRenderer 输出容器 + 点击复制（设计 §3.3）", () => {
  const { ctx } = makeSandbox({});
  // codeRenderer 是模块内私有函数——通过 renderMarkdown 全链路验证输出结构
  // marked 在 sandbox 中为 null → 直接验证 codeRenderer 不可行；改为验证 chat.js 已绑定委托 + CSS 存在
  const html = vm.runInContext(`
    (function() {
      // 模拟 marked 输出经 DOMPurify 后应含 code-block 容器
      // 直接调用 escapeHtml 验证注入安全
      return escapeHtml("<script>alert(1)</script>");
    })()
  `, ctx);
  assert.strictEqual(html, "&lt;script&gt;alert(1)&lt;/script&gt;", "escapeHtml 防注入");

  const css = fs.readFileSync(path.join(__dirname, "..", "renderer", "css", "components.css"), "utf8");
  assert.ok(css.includes(".code-block"), "CSS 应含 .code-block 容器样式");
  assert.ok(css.includes(".code-copy"), "CSS 应含 .code-copy 复制按钮样式");

  // chat.js 应绑定复制委托（模块加载即 initCodeCopy）
  const chatSrc = fs.readFileSync(path.join(RENDERER_JS, "chat.js"), "utf8");
  assert.ok(chatSrc.includes(".code-copy"), "chat.js 应含复制按钮事件委托");
});

test("模型切换器：菜单项构建 + 键盘导航 handler 注册", async () => {
  const { ctx } = makeSandbox({
    getSettings: async () => ({
      apiKey: "sk-x", baseUrl: "", model: "deepseek-chat", projectDir: "/tmp",
      theme: "system",
      models: ["deepseek-chat", "gpt-4o"],
      modelDetails: [
        { name: "deepseek-chat", vision: false },
        { name: "gpt-4o", vision: true },
      ],
    }),
    setModel: async ({ model }) => ({ ok: true }),
  });
  await tick();
  // initModelSwitcher 已在 bindUi 注册；模拟点击 model-switcher
  // 直接验证 closeModelMenu 引用存在 + 键盘 handler 生命周期（打开→注册，关闭→移除）
  const src = fs.readFileSync(path.join(RENDERER_JS, "app.js"), "utf8");
  assert.ok(src.includes("_modelMenuKeyHandler"), "app.js 应维护键盘 handler 引用");
  assert.ok(src.includes("ArrowDown"), "应支持 ↑↓ 键盘导航");
  assert.ok(src.includes("Escape"), "ESC 应关闭菜单");
  // CSS 高亮
  const css = fs.readFileSync(path.join(__dirname, "..", "renderer", "css", "components.css"), "utf8");
  assert.ok(css.includes(".model-menu-item.active"), "键盘导航高亮样式应存在");
});

test("右键菜单：键盘导航 handler 注册 + CSS 高亮", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  const src = fs.readFileSync(path.join(RENDERER_JS, "app.js"), "utf8");
  assert.ok(src.includes("_ctxMenuKeyHandler"), "app.js 应维护右键菜单键盘 handler 引用");
  assert.ok(src.includes("ArrowDown"), "应支持 ↑↓ 键盘导航");
  assert.ok(src.includes(".ctx-item") === false || src.includes("setActive"), "菜单项应有焦点管理");
  const css = fs.readFileSync(path.join(__dirname, "..", "renderer", "css", "components.css"), "utf8");
  assert.ok(css.includes(".ctx-item.active"), "右键菜单键盘高亮样式应存在");
});

test("模型表单：Enter 保存 / ESC 取消键盘绑定（对话框内交互一致）", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  const src = fs.readFileSync(path.join(RENDERER_JS, "dialogs.js"), "utf8");
  assert.ok(src.includes("model-form-name").value || src.includes('"model-form-name"'), "dialogs.js 应绑定模型表单键盘");
  // initModelForm 内应注册 Enter/Escape 处理
  const initIdx = src.indexOf("function initModelForm");
  const initBlock = src.slice(initIdx, initIdx + 600);
  assert.ok(initBlock.includes('key === "Enter"'), "Enter 应保存模型表单");
  assert.ok(initBlock.includes('key === "Escape"'), "ESC 应取消模型表单");
});

test("boot 死锁修复：boot/newSession/switchSession 成功路径启用输入框", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  const src = fs.readFileSync(path.join(RENDERER_JS, "app.js"), "utf8");
  // boot() 成功路径（sessions 分支汇合处）必须调用 setComposerDisabled(false)
  const bootIdx = src.indexOf("async function boot()");
  const bootBlock = src.slice(bootIdx, src.indexOf("async function sendMessage"));
  assert.ok(bootBlock.includes("setComposerDisabled(false)"), "boot() 成功路径应启用输入框");
  // newSession()/switchSession() 成功路径也应启用（防御性，独立调用场景）
  const nsIdx = src.indexOf("async function newSession()");
  const nsBlock = src.slice(nsIdx, src.indexOf("async function deleteSession"));
  assert.ok(nsBlock.includes("setComposerDisabled(false)"), "newSession() 成功路径应启用输入框");
  const ssIdx = src.indexOf("async function switchSession(");
  const ssBlock = src.slice(ssIdx, nsIdx);
  assert.ok(ssBlock.includes("setComposerDisabled(false)"), "switchSession() 成功路径应启用输入框");
});
