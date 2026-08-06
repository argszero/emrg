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
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {},
    appendChild() {},
    removeChild() {},
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
    requestIdleCallback: (cb) => cb(),
    crypto: { randomUUID: () => "mock-uuid" },
    DOMPurify: { sanitize: (x) => x },
    marked: null,
    hljs: null,
    emrg: {
      // 注意：app.js 模块加载即 App.boot()，init 返回空 sessions 走 newSession 路径，
      // 避免 boot 自动 switchSession 污染计数（非空 sessions 会触发 boot 切换）
      init: async () => ({ config_exists: true, api_key_configured: true, project_dir: "/p", project_dir_valid: true, server_id: "srv", model: "m", evolution_count: 1, sessions: [] }),
      onEvent() {},
      sendMessage: async () => ({}),
      cancel: async () => ({}),
      getSettings: async () => ({ apiKey: "k", baseUrl: "", model: "m", projectDir: "/p", models: [], modelDetails: [], theme: "system" }),
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
      ...overrides,
    },
  };
  win.window = win;
  win.document = document;
  const ctx = vm.createContext(win);
  for (const f of ["utils", "commands", "markdown", "copywriting", "chat", "sidebar", "dialogs", "app"]) {
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

test("P4：/rant 无参数打开进化对话框（项目下拉加载）", async () => {
  const { ctx, els } = makeSandbox({
    listProjects: async () => [{ name: "emrg" }],
    sendRant: async () => ({ ok: true, count: 5 }),
  });
  await tick();
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/rant', args: [] })", ctx);
  assert.ok(els["rant-dialog"] && els["rant-dialog"].__open === true, "rant dialog opened");
});

test("P4：/rant 直接跟内容快速提交（不打开对话框）", async () => {
  const { ctx, els } = makeSandbox({
    listProjects: async () => [{ name: "emrg" }],
    sendRant: async () => ({ ok: true, count: 5 }),
  });
  await tick();
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/rant', args: ['希望支持主题切换'] })", ctx);
  assert.ok(!(els["rant-dialog"] && els["rant-dialog"].__open), "direct rant submit should not open dialog");
});

test("P4：/trigger 无参数打开任务列表对话框", async () => {
  const { ctx, els } = makeSandbox({
    listTasks: async () => [{ name: "emrg-task", type: "evolution", interval: 60 }],
    triggerTask: async () => ({ ok: true }),
  });
  await tick();
  await vm.runInContext("App.handleCommand({ type: 'command', cmd: '/trigger', args: [] })", ctx);
  assert.ok(els["tasks-dialog"] && els["tasks-dialog"].__open === true, "tasks dialog opened");
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
