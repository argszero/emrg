"use strict";
/**
 * tool-group.test.js — 连续工具合并组（rant 2026-08-13T21:28:49 方案 A）
 * 独立测试文件：与 renderer.smoke.test.js 分开，避免与其他并行周期的
 * 在途改动（rant 21:36:01 GUI Rant 面板优化）相互污染。
 * 最小沙箱只加载 chat.js 依赖链：utils → i18n → copywriting → chat。
 */
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const RENDERER_JS = path.join(__dirname, "..", "renderer", "js");

// ── DOM mock（与 renderer.smoke.test.js 同构：classList 同步到 className） ──
function makeEl(id) {
  const node = {
    id,
    children: [],
    dataset: {},
    style: {},
    attributes: {},
    _cls: new Set(),
    classList: {
      _set() { node._cls = new Set((node.className || "").split(/\s+/).filter(Boolean)); },
      add(...cs) { node.classList._set(); for (const c of cs) node._cls.add(c); node._update(); },
      remove(c) { node.classList._set(); node._cls.delete(c); node._update(); },
      toggle(c, force) {
        node.classList._set();
        const want = force === undefined ? !node._cls.has(c) : !!force;
        if (want) node._cls.add(c); else node._cls.delete(c);
        node._update();
      },
      contains(c) { return (node.className || "").split(/\s+/).includes(c); },
    },
    _update() { node.className = [...node._cls].join(" "); },
    className: "",
    textContent: "",
    title: "",
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 100,
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    addEventListener(type, fn) { this._listeners = this._listeners || {}; (this._listeners[type] = this._listeners[type] || []).push(fn); },
    remove() {
      if (this.parentNode) {
        const i = this.parentNode.children.indexOf(this);
        if (i >= 0) this.parentNode.children.splice(i, 1);
      }
    },
    querySelector(sel) {
      if (!sel || !sel.startsWith(".")) return null;
      const cls = sel.slice(1);
      const stack = [...(this.children || [])];
      while (stack.length) {
        const n = stack.shift();
        if ((n.className || "").split(/\s+/).includes(cls)) return n;
        stack.push(...(n.children || []));
      }
      return null;
    },
    querySelectorAll(sel) {
      if (!sel || !sel.startsWith(".")) return [];
      const cls = sel.slice(1);
      const out = [];
      const stack = [...(this.children || [])];
      while (stack.length) {
        const n = stack.shift();
        if ((n.className || "").split(/\s+/).includes(cls)) out.push(n);
        stack.push(...(n.children || []));
      }
      return out;
    },
    closest() { return null; },
    after(c) {
      if (!this.parentNode) return;
      c.parentNode = this.parentNode;
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) this.parentNode.children.splice(i + 1, 0, c);
      else this.parentNode.children.push(c);
    },
    insertBefore(c) { c.parentNode = this; this.children.unshift(c); return c; },
    click() { (this._listeners && this._listeners.click || []).forEach((fn) => fn({ preventDefault() {} })); },
  };
  return node;
}

/** 构造最小沙箱：只加载 chat.js 依赖链 */
function makeSandbox(overrides = {}) {
  const workspace = makeEl("workspace");
  const els = { workspace };
  const document = {
    getElementById: (id) => els[id] || makeEl(id),
    createElement: (tag) => makeEl(tag),
    createTextNode: (t) => ({ text: t }),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    body: { appendChild() {}, removeChild() {}, classList: { add() {}, remove() {} } },
    documentElement: { setAttribute() {}, removeAttribute() {} },
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
    navigator: { language: "zh-CN" },
    localStorage: {
      _d: {},
      getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
      setItem(k, v) { this._d[k] = String(v); },
      removeItem(k) { delete this._d[k]; },
    },
    DOMPurify: { sanitize: (x) => x },
    marked: null,
    hljs: null,
    emrgMarkdown: { renderMarkdown: async () => "", streamProject: () => false, streamFinalize: async () => {} },
    App: {
      state: { sessionId: "s1", ownStreamRequestId: null },
      updateEmptyState() {},
    },
    // i18n/copywriting 的刷新钩子
    EMRG_I18N: null,
    EMRG_Copy: null,
    EMRG_Chat: null,
    ...overrides,
  };
  win.window = win;
  win.document = document;
  const ctx = vm.createContext(win);
  for (const f of ["utils", "i18n", "copywriting", "chat"]) {
    const code = fs.readFileSync(path.join(RENDERER_JS, f + ".js"), "utf8");
    vm.runInContext(code, ctx, { filename: "renderer/js/" + f + ".js" });
  }
  return { ctx, win, els, document };
}

const tick = () => new Promise((r) => setTimeout(r, 20));

test("工具合并组：连续工具（无文本穿插）合并 + 摘要 + 收起/展开", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext(
    'Chat.registerContainer("s1", document.getElementById("workspace")); App.state.sessionId = "s1";',
    ctx
  );
  await tick();
  // 工具 1 完成
  await vm.runInContext('Chat.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1")', ctx);
  await tick();
  await vm.runInContext('Chat.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 0.4, content: "" }, "s1")', ctx);
  await tick();
  // 工具 2 完成
  await vm.runInContext('Chat.handleToolStart({ tool_call_id: "t2", tool_name: "read", request_id: "r1" }, "s1")', ctx);
  await tick();
  await vm.runInContext('Chat.handleToolEnd({ tool_call_id: "t2", tool_name: "read", elapsed: 1.2, content: "" }, "s1")', ctx);
  await tick();
  // 工具 3 完成
  await vm.runInContext('Chat.handleToolStart({ tool_call_id: "t3", tool_name: "edit", request_id: "r1" }, "s1")', ctx);
  await tick();
  await vm.runInContext('Chat.handleToolEnd({ tool_call_id: "t3", tool_name: "edit", elapsed: 0.3, content: "" }, "s1")', ctx);
  await tick();

  const ws = els["workspace"];
  // 容器应只有 1 个 .tool-group（3 行全部收编），无独立 .tool-row
  const groups = ws.querySelectorAll(".tool-group");
  assert.strictEqual(groups.length, 1, `应合并为 1 个组，实际 ${groups.length}`);
  const rows = ws.querySelectorAll(".tool-row");
  assert.strictEqual(rows.length, 3, `组内应有 3 个工具行，实际 ${rows.length}`);
  // 组收起（全部完成且未手动展开）
  assert.ok(groups[0].classList.contains("collapsed"), "全部完成后应自动收起");
  // 摘要：3 个工具执行 · 1.9s（0.4+1.2+0.3）
  const summary = groups[0].querySelector(".tool-group-summary");
  assert.ok(summary, "组应有摘要");
  assert.ok(summary.textContent.includes("3"), `摘要应含数量 3，实际 ${summary.textContent}`);
  assert.ok(summary.textContent.includes("1.9"), `摘要应含总耗时 1.9s，实际 ${summary.textContent}`);
  // 每行有 .tool-time（· 0.4s 等）
  const times = ws.querySelectorAll(".tool-time");
  assert.strictEqual(times.length, 3, `3 行都应有耗时，实际 ${times.length}`);
  assert.ok(times[0].textContent.includes("0.4"), `行1 耗时 0.4s，实际 ${times[0].textContent}`);

  // bar 点击 → 展开（user-expanded，不再自动收起）
  const bar = groups[0].querySelector(".tool-group-bar");
  assert.ok(bar, "组应有 bar");
  bar.click();
  await tick();
  assert.ok(!groups[0].classList.contains("collapsed"), "点击 bar 后应展开");
  assert.strictEqual(groups[0].dataset.userExpanded, "1", "展开后应标 user-expanded");
  // 展开后再来新工具并完成 → 不自动收起（user-expanded）
  await vm.runInContext('Chat.handleToolStart({ tool_call_id: "t4", tool_name: "bash", request_id: "r1" }, "s1")', ctx);
  await tick();
  await vm.runInContext('Chat.handleToolEnd({ tool_call_id: "t4", tool_name: "bash", elapsed: 0.5, content: "" }, "s1")', ctx);
  await tick();
  assert.ok(!groups[0].classList.contains("collapsed"), "user-expanded 后不再自动收起");
  const rowsAfter = ws.querySelectorAll(".tool-row");
  assert.strictEqual(rowsAfter.length, 4, `新工具应并入组，实际 ${rowsAfter.length}`);
});

test("文本穿插不合并：工具前有文本 → 独立行", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext(
    'Chat.registerContainer("s1", document.getElementById("workspace")); App.state.sessionId = "s1";',
    ctx
  );
  await tick();
  // 先来一个工具并完成
  await vm.runInContext('Chat.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1")', ctx);
  await tick();
  await vm.runInContext('Chat.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 0.2, content: "" }, "s1")', ctx);
  await tick();
  // 文本 delta（穿插）→ 封存当前文本段
  await vm.runInContext('Chat.handleDelta([{ request_id: "r1", text: "思考中…" }], "s1")', ctx);
  await tick();
  // 再来工具 → 因上一节点是文本，独立行
  await vm.runInContext('Chat.handleToolStart({ tool_call_id: "t2", tool_name: "read", request_id: "r2" }, "s1")', ctx);
  await tick();
  const ws = els["workspace"];
  const groups = ws.querySelectorAll(".tool-group");
  assert.strictEqual(groups.length, 0, `文本穿插后不应合并组，实际 ${groups.length} 组`);
  const rows = ws.querySelectorAll(".tool-row");
  assert.strictEqual(rows.length, 2, `两个工具应各自独立，实际 ${rows.length}`);
});
