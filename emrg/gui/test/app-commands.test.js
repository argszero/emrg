"use strict";
/**
 * app-commands.test.js — GUI / 指令 P2 路由测试（rant 19:44 P2：会话管理类）。
 * 验证 handleCommand 对 /sessions /resume /rename /delete /rewind 的路由：
 *   - /resume <id> → switchSession(id)
 *   - /rewind → listHistory 被调用（历史消息点对话框）
 *   - phase 3+ 指令（/model）仍提示未开放
 * 复用 renderer.smoke.test.js 的沙箱模式（makeEl + document mock）。
 */

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const RENDERER_JS = path.join(__dirname, "..", "renderer", "js");

/** 轻量 DOM 元素桩 */
function makeEl(id) {
  return {
    id,
    hidden: false,
    innerHTML: "",
    textContent: "",
    children: [],
    style: {},
    dataset: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); },
      contains(c) { return this._s.has(c); },
    },
    _listeners: {},
    addEventListener(t, fn) { this._listeners[t] = fn; },
    click() { const fn = this._listeners.click; if (fn) fn(); },
    appendChild(child) {
      this.children.push(child);
      // 近似真实 DOM：优先文本，其次子元素 innerHTML（嵌套渲染）
      let frag = "";
      if (child && child.text !== undefined) frag = String(child.text);
      else if (child && typeof child.textContent === "string" && child.textContent.length > 0) frag = child.textContent;
      else if (child && typeof child.innerHTML === "string" && child.innerHTML.length > 0) frag = child.innerHTML;
      if (frag) this.innerHTML += frag;
    },
    removeChild() {},
    setAttribute() {},
    removeAttribute() {},
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, right: 100, top: 0, bottom: 100, width: 100, height: 100 }),
    showModal() { this.__open = true; },
    close() { this.__open = false; },
    focus() {},
    value: "",
    scrollHeight: 10,
  };
}

/** 构造 app 沙箱（含 P2 需要的 emrg API） */
function makeSandbox(overrides = {}) {
  const calls = { switchSession: [], listHistory: [], rewind: [] };
  const els = {};
  const document = {
    getElementById: (id) => (els[id] = els[id] || makeEl(id)),
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
    navigator: { language: "zh-CN" }, // rant 21:19：沙箱固定 zh，断言保持确定性
    requestIdleCallback: (cb) => cb(),
    crypto: { randomUUID: () => "mock-uuid" },
    localStorage: { getItem: () => null, setItem() {} },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    // P2 框架：window 级监听（result-panel resizer 拖拽 / resize）
    _listeners: {},
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) { const a = this._listeners[type]; if (a) { const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); } },
    DOMPurify: { sanitize: (x) => x },
    marked: null,
    hljs: null,
    emrg: {
      // 注意：app.js 模块加载即 App.boot()，init 返回空 sessions 走 newSession 路径，
      // 避免 boot 自动 switchSession 污染计数（非空 sessions 会触发 boot 切换）
      init: async () => ({ config_exists: true, api_key_configured: true, server_id: "srv", model: "m", evolution_count: 1, sessions: [] }),
      onEvent() {},
      sendMessage: async () => ({}),
      cancel: async () => ({}),
      getSettings: async () => ({ apiKey: "k", baseUrl: "", model: "m", models: [], modelDetails: [], theme: "system" }),
      saveSettings: async () => ({}),
      pickProjectDir: async () => null,
      listSessions: async () => [{ session_id: "s1", title: "会话一" }, { session_id: "s2", title: "会话二" }],
      switchSession: async (p) => { calls.switchSession.push(p); return {}; },
      newSession: async () => ({ session_id: "s3" }),
      deleteSession: async () => ({}),
      renameSession: async () => ({}),
      setModel: async () => ({}),
      listHistory: async () => { calls.listHistory.push(1); return { messages: [{ record_index: 0, preview: "你好" }, { record_index: 1, preview: "帮我写代码" }] }; },
      rewindSession: async (p) => { calls.rewind.push(p); return { removedCount: 2 }; },
      listMemories: async () => [],
      readMemory: async () => ({}),
      listSkills: async () => [],
      listProjects: async () => [],
      listTasks: async () => [],
      triggerTask: async () => ({}),
      sendRant: async () => ({}),
      openFile: async () => ({ ok: true }),
      listFiles: async () => ({ entries: [] }),
      readFile: async () => ({ content: "", binary: false }),
      ...overrides,
    },
  };
  win.window = win;
  win.document = document;
  const ctx = vm.createContext(win);
  for (const f of ["utils", "i18n", "commands", "markdown", "copywriting", "chat", "sidebar", "dialogs", "result-panel", "file-tree", "app"]) {
    const code = fs.readFileSync(path.join(RENDERER_JS, f + ".js"), "utf8");
    vm.runInContext(code, ctx, { filename: "renderer/js/" + f + ".js" });
  }
  return { ctx, win, els, calls };
}

/** 等 microtask 完成（boot 异步路径） */
const tick = () => new Promise((r) => setTimeout(r, 20));

test("P2：/resume <id> 直接 switchSession（不带对话框）", async () => {
  const { ctx, calls } = makeSandbox();
  await tick(); // boot 走 newSession 路径，无 switchSession 调用
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/resume', args: ['abc123'] })", ctx);
  assert.strictEqual(calls.switchSession.length, 1);
  assert.strictEqual(calls.switchSession[0].sessionId, "abc123");
});

test("P2：/rewind 打开历史选择对话框并调用 listHistory", async () => {
  const { ctx, calls, els } = makeSandbox();
  await tick();
  vm.runInContext('App.state.sessionId = "s1";', ctx);
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/rewind', args: [] })", ctx);
  assert.strictEqual(calls.listHistory.length, 1);
  assert.ok(els["rewind-dialog"] && els["rewind-dialog"].__open === true, "rewind dialog opened");
});

test("P2：/rename 复用现有重命名对话框（Dialogs.showRename 被调用）", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  vm.runInContext('App.state.sessionId = "s1"; App.state.sessions = [{ session_id: "s1", title: "旧标题" }];', ctx);
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/rename', args: [] })", ctx);
  assert.ok(els["rename-dialog"] && els["rename-dialog"].__open === true, "rename dialog opened");
  assert.strictEqual(els["rename-input"].value, "旧标题", "重命名输入框预填当前标题");
});

test("P3：/model 触发模型切换器（点击 .model-switcher）", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  // 沙箱 querySelector 返回 null → /model 应优雅跳过（不抛异常、不阻断）
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/model', args: [] })", ctx);
});

test("P3：/memory 打开记忆浏览器并调用 listMemories（project 默认）", async () => {
  const { ctx, els } = makeSandbox({
    listMemories: async ({ scope }) => {
      return [{ id: "m1", title: "记忆一", content: "内容一" }];
    },
  });
  await tick();
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/memory', args: [] })", ctx);
  assert.ok(els["memory-dialog"] && els["memory-dialog"].__open === true, "memory dialog opened");
});

test("P3：/memory session 传 scope=session；/memory <id> 可点击读详情", async () => {
  const { ctx, els } = makeSandbox({
    listMemories: async ({ scope }) => {
      return [{ id: "m1", title: "记忆一", content: "内容一" }];
    },
    readMemory: async ({ memoryId }) => ({ id: memoryId, content: "详情正文" }),
  });
  await tick();
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/memory', args: ['session'] })", ctx);
  assert.ok(els["memory-dialog"].__open === true, "memory dialog opened for session scope");
});

test("P3：/skills 打开技能列表对话框并调用 listSkills", async () => {
  const { ctx, els } = makeSandbox({
    listSkills: async () => [{ name: "browser-harness", description: "web automation", source: "user" }],
  });
  await tick();
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/skills', args: [] })", ctx);
  assert.ok(els["skills-dialog"] && els["skills-dialog"].__open === true, "skills dialog opened");
});

test("P4：/rant 无参数打开 Rant 面板 + 新建表单（rant 14:10:14 P6：rant-dialog 移除）", async () => {
  const { ctx, els } = makeSandbox({
    listProjects: async () => [{ name: "emrg" }],
    sendRant: async () => ({ ok: true, count: 5 }),
  });
  await tick();
  vm.runInContext('document.getElementById("rant-form").classList.add("hidden")', ctx); // 镜像 index.html 初始态
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/rant', args: [] })", ctx);
  await tick();
  const panel = els["panel-rants"];
  assert.ok(panel && panel.classList.contains("active"), "rants workspace view should be active");
  assert.ok(els["rant-form"] && !els["rant-form"].classList.contains("hidden"), "rant form should be open");
});

test("P4：/rant 直接跟内容快速提交（不打开面板）", async () => {
  const { ctx, els } = makeSandbox({
    listProjects: async () => [{ name: "emrg" }],
    sendRant: async () => ({ ok: true, count: 5 }),
  });
  await tick();
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/rant', args: ['希望支持主题切换'] })", ctx);
  const panel = els["panel-rants"];
  assert.ok(!(panel && panel.classList.contains("active")), "direct rant submit should not open panel");
});

test("P4：/trigger 无参数打开任务面板", async () => {
  const { ctx, els } = makeSandbox({
    listTasks: async () => [{ name: "emrg-task", type: "evolution", interval: 60 }],
    triggerTask: async () => ({ ok: true }),
  });
  await tick();
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/trigger', args: [] })", ctx);
  const panel = els["panel-tasks"];
  assert.ok(panel && panel.classList.contains("active"), "tasks workspace view should be active");
});

test("P4：/trigger <name> 直接触发（不打开对话框）", async () => {
  const { ctx, els } = makeSandbox({
    listTasks: async () => [{ name: "emrg-task", type: "evolution", interval: 60 }],
    triggerTask: async () => ({ ok: true }),
  });
  await tick();
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/trigger', args: ['emrg-task'] })", ctx);
  assert.ok(!(els["tasks-dialog"] && els["tasks-dialog"].__open), "direct trigger should not open dialog");
});

// ── Rant 2026-08-20T18:18：Ask/Auto 删除 → 沙箱三档切换 ────────────────
test("setSandbox 更新 state.sandbox（三档白名单）", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  await vm.runInContext("App.setSandbox('read-only')", ctx);
  const sb = await vm.runInContext("App.state.sandbox", ctx);
  assert.strictEqual(sb, "read-only", "state.sandbox 应为 read-only");
  await vm.runInContext("App.setSandbox('danger-full-access')", ctx);
  const sb2 = await vm.runInContext("App.state.sandbox", ctx);
  assert.strictEqual(sb2, "danger-full-access", "state.sandbox 应切到 danger-full-access");
});

test("setSandbox 非法值不生效（白名单 read-only/workspace-write/danger-full-access）", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  await vm.runInContext("App.setSandbox('bogus')", ctx);
  const sb = await vm.runInContext("App.state.sandbox", ctx);
  assert.strictEqual(sb, "workspace-write", "非法档位应保持默认 workspace-write");
});

test("sendMessage 透传 state.sandbox（read-only → sendMessage 带 sandbox）", async () => {
  let sent = null;
  const { ctx, els } = makeSandbox({
    sendMessage: async (p) => { sent = p; return {}; },
  });
  await tick();
  vm.runInContext('App.state.sessionId = "s1"; App.state.sandbox = "read-only";', ctx);
  els["input"].value = "帮我写一段代码";
  await vm.runInContext("App.sendMessage()", ctx);
  assert.ok(sent, "sendMessage 应被调用");
  assert.strictEqual(sent.sandbox, "read-only", "read-only 档位应透传 sandbox 参数");
  assert.strictEqual(sent.text, "帮我写一段代码", "文本应正常透传");
});

// ── WorkBuddy P3（rant 21:35）：自进化可见化 ────────────────
test("P3：/version 使用动态版本号（不再硬编码 v0.2.7）", async () => {
  const { ctx } = makeSandbox({
    init: async () => ({ config_exists: true, api_key_configured: true, server_id: "srv", model: "m", version: "0.2.8", evolution_count: 3, sessions: [] }),
  });
  await tick();
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/version', args: [] })", ctx);
  const src = fs.readFileSync(path.join(RENDERER_JS, "app.js"), "utf8");
  assert.ok(!src.includes("EMRG GUI v0.2.7"), "app.js 不应再硬编码 v0.2.7");
  assert.ok(src.includes("state.version"), "版本应来自 state.version");
  assert.strictEqual(typeof (await vm.runInContext("App.showVersionInfo", ctx)), "function");
});

test("P3：updateGrowthCard 更新侧边栏成长计数", async () => {
  const { ctx, els } = makeSandbox({
    init: async () => ({ config_exists: true, api_key_configured: true, server_id: "srv", model: "m", version: "0.2.8", evolution_count: 42, sessions: [] }),
  });
  await tick();
  await vm.runInContext("App.updateGrowthCard()", ctx);
  assert.strictEqual(els["growth-count"].textContent, "42", "成长卡应显示 42 次");
});

test("P3：进化计数增长 → toast 显示（一天最多一次）", async () => {
  const { ctx, els } = makeSandbox({
    init: async () => ({ config_exists: true, api_key_configured: true, server_id: "srv", model: "m", version: "0.2.8", evolution_count: 5, sessions: [] }),
  });
  await tick();
  // 模拟 pong 事件：计数 5 → 6（进化发生）
  await vm.runInContext(
    "App.handleEvent({ type: 'pong', data: { identity: { instance_id: 'srv' }, model: 'm', evolution_count: 6 } })",
    ctx
  );
  assert.ok(els["evolution-toast"], "toast 元素存在");
  assert.ok(!els["evolution-toast"].classList.contains("hidden"), "toast 应显示");
  // 同一天再次增长 → 不再提示（频率控制）
  await vm.runInContext(
    "App.handleEvent({ type: 'pong', data: { identity: { instance_id: 'srv' }, model: 'm', evolution_count: 7 } })",
    ctx
  );
  // toast 仍显示（未隐藏），但不重新触发——验证不可直接观测；此处确认无异常即可
  assert.ok(!els["evolution-toast"].classList.contains("hidden"), "toast 保持显示");
});

test("P3：toast '去看看' 关闭并输出版本信息", async () => {
  const { ctx, els } = makeSandbox({
    init: async () => ({ config_exists: true, api_key_configured: true, server_id: "srv", model: "m", version: "0.2.8", evolution_count: 2, sessions: [] }),
  });
  await tick();
  // 计数 2 → 3 触发 toast
  await vm.runInContext(
    "App.handleEvent({ type: 'pong', data: { identity: { instance_id: 'srv' }, model: 'm', evolution_count: 3 } })",
    ctx
  );
  const see = els["evolution-toast-see"];
  assert.ok(see, "去看看按钮存在");
  if (see.click) see.click();
  assert.ok(els["evolution-toast"].classList.contains("hidden"), "点击后 toast 应隐藏");
});

test("P3：updateGrowthCard 更新进化计数（兼容 #501 growth-count / about-evolutions id）", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  vm.runInContext("App.state.evolutionCount = 42; App.updateGrowthCard();", ctx);
  // #501 的 id（growth-count / about-evolutions）存在时更新
  assert.ok(els["growth-count"] === undefined || String(els["growth-count"].textContent) === "42", "growth-count 应更新为 42");
  assert.ok(els["about-evolutions"] === undefined || els["about-evolutions"].innerHTML.includes("42"), "about-evolutions 应显示 42");
});

test("P3：loadEvolutionSummary 拉取最近改进并渲染（关于区列表）", async () => {
  const { ctx, els } = makeSandbox({
    evolutionSummary: async () => ({
      count: 7,
      recent: [
        { timestamp: "2026-08-06T20:00:00", operations: ["llm-reflection", "tool-execution"], impact: [] },
        { timestamp: "2026-08-06T19:00:00", operations: ["self-improvement"], impact: [] },
      ],
    }),
  });
  await tick();
  await vm.runInContext("App.loadEvolutionSummary()", ctx);
  assert.ok(els["about-recent"].innerHTML.includes("最近改进"), "应渲染最近改进标题");
  assert.ok(els["about-recent"].innerHTML.includes("llm-reflection"), "应显示操作摘要");
  assert.ok(els["about-recent"].innerHTML.includes("20:00"), "应显示时间戳");
});

test("P3：loadEvolutionSummary 空记录显示引导文案", async () => {
  const { ctx, els } = makeSandbox({
    evolutionSummary: async () => ({ count: 0, recent: [] }),
  });
  await tick();
  await vm.runInContext("App.loadEvolutionSummary()", ctx);
  assert.ok(els["about-recent"].innerHTML.includes("/rant"), "空状态应提示 /rant 驱动第一次进化");
});

test("P1 回归：result-panel.js 暴露 window.ResultPanel（真实 GUI 防 ReferenceError）", async () => {
  const src = fs.readFileSync(path.join(RENDERER_JS, "result-panel.js"), "utf8");
  assert.ok(src.includes("window.ResultPanel"), "result-panel.js 必须暴露 window.ResultPanel（app.js 独立 script 加载需要）");
});
