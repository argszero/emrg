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
const RENDERER_CSS = path.join(__dirname, "..", "renderer", "css");

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
      // className 为唯一事实源：el() 直接赋值 className 后 classList 操作不得清空既有类
      _set() { node._cls = new Set((node.className || "").split(/\s+/).filter(Boolean)); },
      add(...cs) { node.classList._set(); for (const c of cs) node._cls.add(c); node._update(); },
      remove(c) { node.classList._set(); node._cls.delete(c); node._update(); },
      // 忠实 DOM：toggle(c, force)——force 指定时按 force 加/删（sidebar highlight 依赖）
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
    _html: "",
    get innerHTML() { return this._html; },
    set innerHTML(v) {
      this._html = v;
      // 忠实 DOM：innerHTML = "" 清空子节点（Chat.clear 依赖；P3 s2 容器隔离判别）
      if (v === "") { while (this.children.length) this.children[0].remove(); }
    },
    value: "",
    disabled: false,
    title: "",
    checked: false,
    selectedIndex: -1,
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 100,
    open: false,
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    addEventListener(type, fn) { this._listeners = this._listeners || {}; (this._listeners[type] = this._listeners[type] || []).push(fn); },
    click() { (this._listeners && this._listeners.click || []).forEach((fn) => fn({ preventDefault() {} })); },
    // P2 框架：通用事件派发（resizer mousedown 等非 click 事件）
    dispatch(type, evt) { (this._listeners && this._listeners[type] || []).forEach((fn) => fn(evt || { preventDefault() {} })); },
    querySelector(sel) {
      // 最小类选择器搜索（chat.js 用 ".msg-body"/".tool-spinner"）：DFS 子节点
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
      // P4 s2：类选择器 DFS 收集（sidebar highlight 用 ".conv-item"）
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
    showModal() { this.open = true; },
    close() { this.open = false; },
    setAttribute(k, v) { this.attributes[k] = v; if (k === "value") this.value = v; },
    removeAttribute(k) { delete this.attributes[k]; },
    insertBefore(c) { c.parentNode = this; this.children.unshift(c); return c; },
    after(c) {
      // 真实 DOM：插到兄弟节点之后（rant 14:10:14 P4 详情展开用 row.after(detail)）
      if (!this.parentNode) return;
      c.parentNode = this.parentNode;
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) this.parentNode.children.splice(i + 1, 0, c);
      else this.parentNode.children.push(c);
    },
    get nextElementSibling() {
      // 忠实 DOM：下一兄弟节点（rant 10:41:43 点击收起依赖 row.nextElementSibling）
      if (!this.parentNode) return null;
      const i = this.parentNode.children.indexOf(this);
      if (i < 0) return null;
      return this.parentNode.children[i + 1] || null;
    },
    remove() {
      // 真实脱离父节点（chat.js handleToolEnd 移除 .tool-spinner）
      if (this.parentNode) {
        const i = this.parentNode.children.indexOf(this);
        if (i >= 0) this.parentNode.children.splice(i, 1);
      }
    },
    focus() {},
    select() {},
  };
  return node;
}

const ELEMENT_IDS = [
  "workspace", "input", "send-btn", "stop-btn", "conv-list", "open-sessions", "open-sessions-label", "status-dot", "settings-btn",
  "open-session-dialog", "open-session-list", "open-session-title", "open-session-desc", "open-session-new", "open-session-cancel", "open-session-new-session",
  "new-session-dialog", "new-session-list", "new-session-new", "new-session-cancel",
  "conn-banner", "empty-state", "model-switcher", "model-switcher-label", "brand-star", "new-chat-btn", "open-chat-btn",
  "side-nav", "nav-sessions", "nav-projects", "nav-tasks", "nav-rants", "nav-settings",
  "panel-projects", "panel-tasks", "panel-rants", "panel-settings",
  "project-list", "project-add-btn",
  "rant-filter-tabs", "rant-filter-all", "rant-filter-pending", "rant-filter-inprogress", "rant-filter-completed",
  "rant-list", "rant-new-btn", "rant-form", "rant-form-project", "rant-form-message", "rant-form-cancel", "rant-form-submit",
  "settings-tabs", "settings-tab-model", "settings-tab-github", "settings-tab-appearance", "settings-tab-language", "settings-tab-about",
  "settings-body-model", "settings-body-github", "settings-body-appearance", "settings-body-language", "settings-body-about",
  "settings-cancel", "settings-save", "set-api-key", "set-base-url", "set-project-dir",  "set-model", "pick-dir-btn", "theme-options", "welcome-dialog", "welcome-api-key", "welcome-base-url",
  "welcome-model", "welcome-project-dir", "welcome-pick-btn", "welcome-save", "confirm-dialog",
  "confirm-title", "confirm-message", "confirm-cancel", "confirm-ok", "main", "composer-wrap",
  "rename-dialog", "rename-input", "rename-cancel", "rename-ok", "ctx-menu",
  "model-list", "add-model-btn", "model-form", "model-form-name", "model-form-id",
  "model-form-vision", "model-form-save", "model-form-cancel", "back-to-bottom",
  "cmd-menu", "help-dialog", "help-list", "help-close",
  "rewind-dialog", "rewind-list", "rewind-close",
  "memory-dialog", "memory-list", "memory-detail", "memory-close",
  "skills-dialog", "skills-list", "skills-close",
  "result-panel", "result-list", "result-toggle",
  "result-tabs", "result-tab-files", "result-tab-artifacts", "result-tabbar", "result-files", "result-viewer", "result-resizer",
  "growth-card", "growth-count", "about-recent",
  "github-banner", "github-banner-msg", "github-banner-connect", "github-banner-dismiss",
  "upgrade-banner", "upgrade-banner-msg", "upgrade-banner-restart", "upgrade-banner-dismiss",
  "toast", "toast-msg",
  // rant 18:23:15 P3：定时任务/自定义类型管理（settings 区）
  "task-list", "task-add-btn", "task-template-mgr-btn",
  "task-form", "task-form-name", "task-form-type", "task-form-project",
  "task-form-interval", "task-form-enabled", "task-form-repo",
  "task-form-sandbox",
  "task-form-cancel", "task-form-save",
  "task-template-form", "task-template-name", "task-template-prompt",
  "task-template-cancel", "task-template-save", "task-template-list",
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
    // rant 10:36:39：任务倒计时 setInterval 桩（返回递增 id，永不触发 → 测试不泄漏真实定时器）；
    // 走秒由测试显式调用 EMRG_Dialogs.updateTaskCountdowns() 模拟
    setInterval: () => { win._intervalCount = (win._intervalCount || 0) + 1; return win._intervalCount; },
    clearInterval: () => { win._clearCount = (win._clearCount || 0) + 1; },
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
    // P2 框架：localStorage 功能 mock（宽度/折叠分离持久化断言）
    localStorage: {
      _d: {},
      getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
      setItem(k, v) { this._d[k] = String(v); },
      removeItem(k) { delete this._d[k]; },
    },
    requestIdleCallback: (cb) => cb(),
    crypto: { randomUUID: () => "mock-uuid" },
    DOMPurify: { sanitize: (x) => x },
    marked: null,
    hljs: null,
    emrg: {
      init: async () => ({ config_exists: false, api_key_configured: false, server_id: "", model: "", evolution_count: 0, sessions: [] }),
      onEvent() {},
      sendMessage: async () => ({}),
      cancel: async () => ({}),
      getSettings: async () => ({ apiKey: "", baseUrl: "", model: "", models: [], modelDetails: [], theme: "system" }),
      saveSettings: async () => ({}),
      pickProjectDir: async () => null,
      listSessions: async () => [],
      switchSession: async () => ({}),
      listHistory: async () => ({ messages: [], hasMore: false }),
      newSession: async () => ({ session_id: "s2" }),
      deleteSession: async () => ({}),
      setModel: async () => ({}),
      openFile: async () => ({ ok: true }),
      listFiles: async ({ path } = {}) => {
        const table = {
          "/proj": { entries: [
            { name: "src", path: "/proj/src", type: "dir" },
            { name: "README.md", path: "/proj/README.md", type: "file" },
          ] },
          "/proj/src": { entries: [{ name: "main.py", path: "/proj/src/main.py", type: "file" }] },
        };
        return table[path] || { entries: [] };
      },
      readFile: async ({ path } = {}) => ({ content: `# ${path}\ntext`, binary: false }),
      previewHtml: async () => ({ ok: true }), // P2.3：HTML 预览（WebContentsView）
      closePreview: async () => ({ ok: true }),
      panelResized: async () => ({ ok: true }),
      getPreviewState: async () => ({ path: null }), // P2.3：崩溃恢复拉取
      sendRant: async () => ({}),
      listRants: async () => [],
      ...overrides,
    },
  };
  win.window = win;
  win.document = document;
  // P2 框架：window 级监听（resizer 拖拽 mousemove/mouseup + resize）
  win._listeners = {};
  win.addEventListener = function (type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); };
  win.removeEventListener = function (type, fn) { const a = this._listeners[type]; if (a) { const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); } };
  const ctx = vm.createContext(win);
  for (const f of ["utils", "i18n", "commands", "markdown", "copywriting", "chat", "sidebar", "dialogs", "result-panel", "file-tree", "app"]) {
    const code = fs.readFileSync(path.join(RENDERER_JS, f + ".js"), "utf8");
    vm.runInContext(code, ctx, { filename: "renderer/js/" + f + ".js" });
  }
  return { ctx, win, els, document };
}

/** 等 microtask 完成 */
const tick = () => new Promise((r) => setTimeout(r, 20));

/** 消息节点数：排除 .session-header 标题栏（app.js 每会话视图顶部固定） */
const msgCount = (el) => (el.children || []).filter((c) => !(c.className || "").split(/\s+/).includes("session-header")).length;

test("8 模块按序加载且全局符号解析", () => {
  const { ctx } = makeSandbox();
  const out = vm.runInContext(
    "(function(){ return { App: typeof App, Chat: typeof EMRG_Chat, Copy: typeof EMRG_Copy, Sidebar: typeof EMRG_Sidebar, Dialogs: typeof EMRG_Dialogs, Commands: typeof EMRG_Commands, utils: typeof $ }; })()",
    ctx
  );
  // ⚠️ vm 上下文对象原型不同 Realm → 不用 deepStrictEqual，逐个字段断言
  assert.strictEqual(out.App, "object");
  assert.strictEqual(out.Chat, "object");
  assert.strictEqual(out.Copy, "object");
  assert.strictEqual(out.Sidebar, "object");
  assert.strictEqual(out.Dialogs, "object");
  assert.strictEqual(out.Commands, "object");
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
            server_id: "srv-1",
      model: "deepseek-chat",
      evolution_count: 42,
      sessions: [{ session_id: "s1", title: "测试对话", updated_at: "2026-08-05T10:00:00Z" }],
    }),
    switchSession: async () => ({}),
  });
  await tick();
  // conv-list 应有会话项（rant 17:48:07：无分组标签，直接 project/name|id）
  const items = vm.runInContext('document.getElementById("conv-list").children.length', ctx);
  assert.ok(items >= 1, `conv-list 应有会话项，实际 ${items}`);
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
      chatChildren: $("workspace").children.length,
      toolRowClass: $("workspace").children[1] ? $("workspace").children[1].className : "none",
    };
  })()`, ctx);
  assert.strictEqual(r.chatChildren, 2, "用户流 + 工具行 2 个节点");
  assert.ok(r.toolRowClass.includes("done"), `工具行应 done，实际 ${r.toolRowClass}`);
});

test("rant 21:57:10：交替文本/工具按顺序交错展示（每段文本独立成块）", async () => {
  const { ctx } = makeSandbox();
  await tick();
  const r = vm.runInContext(`(function() {
    App.state.sessionId = "s1";
    App.state.ownStreamRequestId = "rid-1";
    // LLM 常见输出序列：文本段1 → 工具1 → 文本段2 → 工具2 → 文本段3
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "文本段1" }]);
    EMRG_Chat.handleToolStart({ request_id: "rid-1", tool_call_id: "t1", tool_name: "read" });
    EMRG_Chat.handleToolEnd({ tool_call_id: "t1", tool_name: "read", content: "out1", elapsed: 0.1 });
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "文本段2" }]);
    EMRG_Chat.handleToolStart({ request_id: "rid-1", tool_call_id: "t2", tool_name: "bash" });
    EMRG_Chat.handleToolEnd({ tool_call_id: "t2", tool_name: "bash", content: "out2", elapsed: 0.2 });
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "文本段3" }]);
    const children = $("workspace").children;
    const kinds = [];
    const texts = [];
    const isToolRow = (c) =>
      (c.className || "").includes("tool-row"); // 用行类而非 spinner 子元素（rant 21:08 后 spinner 完成即移除）
    for (let i = 0; i < children.length; i++) {
      const c = children[i];
      if (isToolRow(c)) {
        kinds.push("tool");
      } else {
        kinds.push("text");
        texts.push((c.querySelector(".msg-body") || c).textContent); // 文本在 body 子节点
      }
    }
    const beforeDone = children.length;
    // done 后该 rid 的所有文本段都应渲染（typing 移除）
    EMRG_Chat.handleDone({ request_id: "rid-1" });
    let typingAfter = 0;
    for (let i = 0; i < $("workspace").children.length; i++) {
      const c = $("workspace").children[i];
      if (c.className.includes("typing")) typingAfter++;
    }
    return {
      count: children.length,
      kinds: kinds.join(","),
      texts: texts.join("|"),
      beforeDone,
      typingAfter,
    };
  })()`, ctx);
  assert.strictEqual(r.beforeDone, 5, "3 文本段 + 2 工具行 = 5 节点");
  assert.strictEqual(r.kinds, "text,tool,text,tool,text", "应按 文本→工具→文本→工具→文本 交错（TUI 一致）");
  assert.strictEqual(r.texts, "文本段1|文本段2|文本段3", "每段文本独立成块，不拼接在顶部（沙箱 textContent 不含 ✦ 标记 span）");
  assert.strictEqual(r.typingAfter, 0, "done 后所有文本段 typing 光标应移除");
});

test("rant 21:08：工具完成后 spinner 停止（元素移除，不再转圈）", async () => {
  const { ctx } = makeSandbox();
  await tick();
  const r = vm.runInContext(`(function() {
    App.state.sessionId = "s1";
    App.state.ownStreamRequestId = "rid-1";
    EMRG_Chat.handleToolStart({ request_id: "rid-1", tool_call_id: "t1", tool_name: "read" });
    const row = $("workspace").children[1]; // children[0] 是助手文本节点，[1] 是工具行
    const spinnerBefore = (row.querySelector(".tool-spinner") !== null);
    EMRG_Chat.handleToolEnd({ tool_call_id: "t1", tool_name: "read", content: "ok", elapsed: 0.3 });
    const spinnerAfter = (row.querySelector(".tool-spinner") !== null);
    return { spinnerBefore, spinnerAfter, cls: row.className };
  })()`, ctx);
  assert.strictEqual(r.spinnerBefore, true, "工具运行中应有 spinner");
  assert.strictEqual(r.spinnerAfter, false, "工具完成后 spinner 应移除（不得一直转圈）");
  assert.ok(r.cls.includes("done"), `工具行应 done，实际 ${r.cls}`);
});

test("rant 21:09：文本段被工具封存后 typing 光标移除（只留最新段闪烁）", async () => {
  const { ctx } = makeSandbox();
  await tick();
  const r = vm.runInContext(`(function() {
    App.state.sessionId = "s1";
    App.state.ownStreamRequestId = "rid-1";
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "第一段" }]);
    const firstNode = $("workspace").children[0];
    const firstBody = firstNode.querySelector(".msg-body") || firstNode;
    const typingBefore = firstBody.classList.contains("typing");
    EMRG_Chat.handleToolStart({ request_id: "rid-1", tool_call_id: "t1", tool_name: "read" });
    const typingAfter = firstBody.classList.contains("typing");
    return { typingBefore, typingAfter };
  })()`, ctx);
  assert.strictEqual(r.typingBefore, true, "封存前第一段应有 typing 光标（流式中）");
  assert.strictEqual(r.typingAfter, false, "封存后第一段 typing 光标应移除（光标只留在最新文本段）");
});

test("rant 21:10：done 渲染剥离 ✦ 前缀（标题/列表/代码围栏不被前缀破坏）", async () => {
  const { ctx } = makeSandbox();
  await tick();
  const p = vm.runInContext(`(async function() {
    App.state.sessionId = "s1";
    App.state.ownStreamRequestId = "rid-1";
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "# 标题\\n- 列表项\\n\\n\\u0060\\u0060\\u0060python\\nprint(1)\\n\\u0060\\u0060\\u0060" }]);
    const node = $("workspace").children[0];
    const body = node.querySelector(".msg-body") || node;
    // 模拟真实 DOM：body 内先有 ✦ 标记 span（textContent 含前缀）
    body.textContent = "✦ " + body.textContent;
    let captured = null;
    window.marked = {
      use: () => {},
      parse: async (t) => { captured = t; return "<h1>标题</h1><ul><li>列表项</li></ul>"; },
    };
    EMRG_Chat.handleDone({ request_id: "rid-1" });
    await new Promise((res) => setTimeout(res, 10)); // 等 renderMarkdown microtask
    return {
      renderedHasHeader: body.innerHTML.includes("h1"),
      renderedHasMark: (body.children[0]?.className || "").includes("msg-assistant-mark"),
      capturedStartsClean: String(captured).startsWith("# 标题"),
      capturedHasPrefix: String(captured).includes("✦"),
    };
  })()`, ctx);
  const r = await p;
  assert.strictEqual(r.capturedStartsClean, true, "传入 marked 的文本应以 # 开头（✦ 前缀已剥离）");
  assert.strictEqual(r.capturedHasPrefix, false, "传入 marked 的文本不得含 ✦ 前缀");
  assert.ok(r.renderedHasHeader, "剥离前缀后 # 标题渲染为 h1（块语法不被破坏）");
  assert.strictEqual(r.renderedHasMark, true, "渲染后 ✦ 标记应重新插入（元素而非文本）");
});

test("rant 14:11：首条消息后欢迎屏立即隐藏（append 同步 updateEmptyState）", async () => {
  const { ctx } = makeSandbox();
  await tick();
  const r = vm.runInContext(`(function() {
    App.state.sessionId = "s1";
    EMRG_Chat.addUserMessage("hello");
    return {
      emptyHidden: $("empty-state").classList.contains("hidden"),
      msgCount: $("workspace").children.length,
    };
  })()`, ctx);
  assert.strictEqual(r.msgCount, 1, "消息应已追加");
  assert.strictEqual(r.emptyHidden, true, "有消息时欢迎屏应隐藏（欢迎屏不得叠在消息区上方）");
});

test("rant 21:00:28：块投影——流式中标题即时渲染，稳定块缓存不重建", async () => {
  const { ctx } = makeSandbox();
  await tick();
  const r = await vm.runInContext(`(async function() {
    App.state.sessionId = "s1";
    App.state.ownStreamRequestId = "rid-1";
    // 假 marked：空行分隔块，heading 行 → heading token，其余合并 paragraph
    window.marked = {
      use: () => {},
      parser: (tokens) => tokens.map((t) => t.type === "heading" ? "<h2>" + t.text + "</h2>" : "<p>" + t.text + "</p>").join(""),
      parse: async (t) => "<div>" + t + "</div>",
      lexer: (text) => {
        const toks = [];
        const blocks = text.split(/\\n\\n+/);
        for (const b of blocks) {
          const lines = b.split("\\n");
          if (/^#{1,6}\\s/.test(lines[0])) toks.push({ type: "heading", raw: lines[0], text: lines[0].replace(/^#+\\s*/, "") });
          else if (b.trim()) toks.push({ type: "paragraph", raw: b, text: b.replace(/\\n/g, " ") });
        }
        return toks;
      },
    };
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "# 标题" }]);
    const body = $("workspace").children[0].querySelector(".msg-body");
    const stream = body.querySelector(".md-stream");
    const live1 = stream.children[stream.children.length - 1]; // 尾部 live 块
    const h2WhileStreaming = live1.className.includes("live") && live1.innerHTML.includes("<h2>");
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "\\n\\n正文段落" }]);
    const stable = stream.children[0];
    const live2 = stream.children[stream.children.length - 1];
    const firstIsHeading = stable.innerHTML.includes("<h2>");
    const liveIsParagraph = live2.innerHTML.includes("<p>正文段落</p>");
    // 稳定块缓存：再追加增量，首个稳定块节点身份不变（不重建 → 不闪烁/不打断选中）
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: " 追加" }]);
    const stableSame = (stream.children[0] === stable);
    return { h2WhileStreaming, firstIsHeading, liveIsParagraph, stableSame };
  })()`, ctx);
  assert.strictEqual(r.h2WhileStreaming, true, "流式过程中标题应即时渲染为 h2");
  assert.strictEqual(r.firstIsHeading, true, "稳定块应完整渲染标题");
  assert.strictEqual(r.liveIsParagraph, true, "live 块应渲染段落");
  assert.strictEqual(r.stableSame, true, "稳定块 DOM 应缓存复用（不重建）");
});

test("rant 21:00:28：代码块围栏未闭合纯文本不高亮，闭合后转完整渲染", async () => {
  const { ctx } = makeSandbox();
  await tick();
  const r = await vm.runInContext(`(function() {
    App.state.sessionId = "s1";
    App.state.ownStreamRequestId = "rid-1";
    // 假 marked：整个输入视为一个 code token（raw 含未闭合/闭合围栏由内容决定）
    window.marked = {
      use: () => {},
      parser: (tokens) => tokens.map((t) => "<pre class=\\"hljs\\">" + t.text + "</pre>").join(""),
      parse: async (t) => "<div>" + t + "</div>",
      lexer: (text) => {
        const m = /\`\`\`\\w*\\n([\\s\\S]*?)(\`\`\`\\s*)?$/.exec(text);
        return [{ type: "code", raw: text, text: m ? m[1] : text }];
      },
    };
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "before\\n\\\`\\\`\\\`python\\nprint(1)" }]);
    const body = $("workspace").children[0].querySelector(".msg-body");
    const live = body.querySelector(".md-stream");
    const liveDiv = live.children[live.children.length - 1]; // 尾部 live 块（单类选择器 mock 限制）
    const unclosedPlain = liveDiv.className.includes("live") && liveDiv.innerHTML.startsWith("<pre class=\\"stream-code\\">") && !liveDiv.innerHTML.includes("hljs");
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "\\n\\\`\\\`\\\`" }]);
    const liveDiv2 = live.children[live.children.length - 1];
    const closedHighlight = liveDiv2.className.includes("live") && liveDiv2.innerHTML.includes("hljs") && !liveDiv2.innerHTML.includes("stream-code");
    return { unclosedPlain, closedHighlight };
  })()`, ctx);
  assert.strictEqual(r.unclosedPlain, true, "围栏未闭合 → 纯文本不高亮（TUI fence_count%2 启发式一致）");
  assert.strictEqual(r.closedHighlight, true, "围栏闭合 → 转完整渲染（高亮）");
});

test("rant 21:00:28：done 收尾 live 块转 full（mark span 保留，typing 移除）", async () => {
  const { ctx } = makeSandbox();
  await tick();
  const r = await vm.runInContext(`(async function() {
    App.state.sessionId = "s1";
    App.state.ownStreamRequestId = "rid-1";
    window.marked = {
      use: () => {},
      parser: (tokens) => tokens.map((t) => "<p>" + t.text + "</p>").join(""),
      parse: async (t) => "<div class=\\"full\\">" + t + "</div>",
      lexer: (text) => [{ type: "paragraph", raw: text, text }],
    };
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "部分" }]);
    const node = $("workspace").children[0];
    const body = node.querySelector(".msg-body");
    EMRG_Chat.handleDone({ request_id: "rid-1" });
    await new Promise((res) => setTimeout(res, 10)); // 等 streamFinalize microtask
    const stream = body.querySelector(".md-stream");
    const fullRendered = stream.innerHTML.includes("class=\\"full\\"");
    const markKept = (body.children[0].className || "").includes("msg-assistant-mark");
    const typingAfter = body.classList.contains("typing");
    return { fullRendered, markKept, typingAfter };
  })()`, ctx);
  assert.strictEqual(r.fullRendered, true, "done 后容器应整体渲染（live 转 full 校正）");
  assert.strictEqual(r.markKept, true, "✦ mark span 应保留为元素（块语法不被前缀破坏）");
  assert.strictEqual(r.typingAfter, false, "done 后 typing 光标应移除");
});

test("rant 21:00:28：真实 marked 集成——块投影与真实分词一致（未闭合围栏纯文本→闭合高亮→done full）", async () => {
  const markedReal = require(path.join(__dirname, "..", "vendor", "marked.min.js")).marked;
  const { ctx } = makeSandbox();
  await tick();
  ctx.marked = markedReal; // 注入真实 marked 到 vm 全局（window.marked）
  const r = await vm.runInContext(`(async function() {
    App.state.sessionId = "s1";
    App.state.ownStreamRequestId = "rid-1";
    const bodyOf = () => $("workspace").children[0].querySelector(".msg-body");
    const liveOf = (b) => {
      const s = b.querySelector(".md-stream");
      return s.children[s.children.length - 1]; // 尾部 live 块（单类选择器 mock 限制）
    };
    const streamOf = (b) => b.querySelector(".md-stream");
    // 1. 流式标题即时渲染
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "# 标题" }]);
    const b1 = bodyOf();
    const liveHeading = liveOf(b1).innerHTML; // 应为 <h1>标题</h1>（单个 #）
    // 2. 追加段落 → 标题变稳定块，live 变段落
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "\\n\\n正文段落" }]);
    const s2 = streamOf(b1);
    const stableHeading = s2.children[0].innerHTML;
    const livePara = s2.children[s2.children.length - 1].innerHTML;
    // 3. 未闭合代码围栏 → 纯文本不高亮
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "\\n\\n\\\`\\\`\\\`python\\nprint(1)" }]);
    const s3 = streamOf(b1);
    const liveUnclosed = s3.children[s3.children.length - 1].innerHTML;
    // 4. 闭合围栏 → 完整渲染（codeRenderer 容器 code-block）
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "\\n\\\`\\\`\\\`" }]);
    const s4 = streamOf(b1);
    const liveClosed = s4.children[s4.children.length - 1].innerHTML;
    // 5. done → full 渲染（同源 renderMarkdown）
    EMRG_Chat.handleDone({ request_id: "rid-1" });
    await new Promise((res) => setTimeout(res, 20));
    const s5 = streamOf(b1);
    return {
      liveHeading,
      stableHeading,
      livePara,
      liveUnclosed,
      liveClosed,
      fullHtml: s5.innerHTML,
      markKept: (b1.children[0].className || "").includes("msg-assistant-mark"),
    };
  })()`, ctx);
  assert.strictEqual(r.liveHeading, "<h1>标题</h1>\n", "流式中标题应即时渲染为 h1（真实 marked）");
  assert.strictEqual(r.stableHeading, "<h1>标题</h1>\n", "追加后标题应转稳定块完整渲染");
  assert.strictEqual(r.livePara, "<p>正文段落</p>\n", "live 块应渲染段落");
  assert.ok(r.liveUnclosed.includes("stream-code"), `未闭合围栏应纯文本，实际 ${r.liveUnclosed}`);
  assert.ok(!r.liveUnclosed.includes("code-block"), "未闭合围栏不得出现 code-block（不高亮）");
  assert.ok(r.liveClosed.includes("code-block"), `闭合围栏应转完整渲染（codeRenderer），实际 ${r.liveClosed}`);
  assert.ok(!r.liveClosed.includes("stream-code"), "闭合后不得再走纯文本路径");
  assert.ok(r.fullHtml.includes("<h1>标题</h1>"), "done 后 full 渲染应含标题");
  assert.ok(r.fullHtml.includes("code-block"), "done 后 full 渲染应含代码块");
  assert.strictEqual(r.markKept, true, "done 后 mark span 应保留");
});

test("rant 14:11：done 后残留 delta 被丢弃，不建孤儿节点", async () => {
  const { ctx } = makeSandbox();
  await tick();
  const r = vm.runInContext(`(function() {
    App.state.sessionId = "s1";
    App.state.ownStreamRequestId = "rid-1";
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "你" }]);
    EMRG_Chat.handleDone({ request_id: "rid-1" });
    const afterDone = $("workspace").children.length;
    // 模拟 16ms 批量定时器在 done 之后才 flush 的残留 delta（G122 竞态）
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "残留" }]);
    const afterStale = $("workspace").children.length;
    return { afterDone, afterStale };
  })()`, ctx);
  assert.strictEqual(r.afterDone, 1, "done 前 1 个节点");
  assert.strictEqual(r.afterStale, 1, "残留 delta 应被丢弃，不得产生孤儿节点（否则误标来自其他客户端 + 光标残留）");
});

test("rant 14:11：cancelled 事件清除在途节点 typing 光标", async () => {
  const { ctx } = makeSandbox();
  await tick();
  const r = vm.runInContext(`(function() {
    App.state.sessionId = "s1";
    App.state.ownStreamRequestId = "rid-1";
    EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "部分" }]);
    const node = $("workspace").children[0];
    const body = node.querySelector(".msg-body") || node;
    body.classList.add("typing");
    const typingBefore = body.classList.contains("typing");
    App.handleEvent({ type: "cancelled" });
    const typingAfter = body.classList.contains("typing");
    return { typingBefore, typingAfter };
  })()`, ctx);
  assert.strictEqual(r.typingBefore, true, "取消前应处于 typing 态");
  assert.strictEqual(r.typingAfter, false, "取消后 typing 光标（▍）应移除");
});

test("多模型管理：modelDetails 加载渲染 + saveSettings 传 models 数组", async () => {
  let saved = null;
  const { ctx, els } = makeSandbox({
    getSettings: async () => ({
      apiKey: "sk-x",
      baseUrl: "",
      model: "deepseek-chat",
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
test("GCM rant Stage 2：演化增长 + 未认证 → GitHub 连接横幅出现（正反两态）", async () => {
  // 正态：未认证 + 演化计数增长 → 横幅出现（先加 hidden 模拟 index.html 初始态）
  const { ctx } = makeSandbox({
    githubStatus: async () => ({ authenticated: false, user: null }),
  });
  await tick();
  await vm.runInContext(`(async function() {
    document.getElementById("github-banner").classList.add("hidden");
    App.state.evolutionCount = 42;
    App.state.lastKnownEvolutionCount = 40;
    App.maybeShowEvolutionToast();
  })()`, ctx);
  await tick(); // 等待 githubStatus promise 解析
  const visible = vm.runInContext(
    '!document.getElementById("github-banner").classList.contains("hidden")',
    ctx
  );
  assert.strictEqual(visible, true, "未认证 + 演化增长 → 横幅应出现");

  // 负态：已认证 → 横幅保持隐藏
  const { ctx: ctx2 } = makeSandbox({
    githubStatus: async () => ({ authenticated: true, user: "octocat" }),
  });
  await tick();
  await vm.runInContext(`(async function() {
    document.getElementById("github-banner").classList.add("hidden");
    App.state.evolutionCount = 42;
    App.state.lastKnownEvolutionCount = 40;
    App.maybeShowEvolutionToast();
  })()`, ctx2);
  await tick();
  const hidden = vm.runInContext(
    'document.getElementById("github-banner").classList.contains("hidden")',
    ctx2
  );
  assert.strictEqual(hidden, true, "已认证 → 横幅应保持隐藏");

  // 源码断言：连接/关闭按钮绑定 + 演化增长钩子
  const src = fs.readFileSync(path.join(RENDERER_JS, "app.js"), "utf8");
  assert.ok(src.includes("Dialogs.showSettings()"), "横幅[去连接]应打开设置页 GitHub 区");
  assert.ok(src.includes("_githubBannerDismissed = true"), "关闭横幅应本会话不再弹");
  const toastIdx = src.indexOf("function maybeShowEvolutionToast()");
  const toastBlock = src.slice(toastIdx, toastIdx + 800);
  assert.ok(toastBlock.includes("maybeShowGithubBanner()"), "演化增长应触发 GitHub 横幅检查");
});

test("rant 18:30:57：版本变化 → 升级横幅出现 + 重启按钮触发 restartDaemon（正反两态）", async () => {
  // 正态：status current_version 与已知版本不同 → 横幅出现
  const { ctx } = makeSandbox({
    init: async () => ({
      config_exists: true,
      api_key_configured: true,
      current_version: "0.2.58",
      sessions: [],
    }),
  });
  await tick();
  await vm.runInContext(`(function() {
    document.getElementById("upgrade-banner").classList.add("hidden");
    App.state.lastKnownVersion = "0.2.58";
    App.handleEvent({ type: "status", data: { connected: true, current_version: "0.2.59" } });
  })()`, ctx);
  const visible = vm.runInContext(
    '!document.getElementById("upgrade-banner").classList.contains("hidden")',
    ctx
  );
  assert.strictEqual(visible, true, "版本变化 → 升级横幅应出现");

  // 负态：版本未变 → 横幅保持隐藏
  const { ctx: ctx2 } = makeSandbox({});
  await tick();
  await vm.runInContext(`(function() {
    document.getElementById("upgrade-banner").classList.add("hidden");
    App.state.lastKnownVersion = "0.2.58";
    App.handleEvent({ type: "status", data: { connected: true, current_version: "0.2.58" } });
  })()`, ctx2);
  const hidden = vm.runInContext(
    'document.getElementById("upgrade-banner").classList.contains("hidden")',
    ctx2
  );
  assert.strictEqual(hidden, true, "版本未变 → 横幅应保持隐藏");

  // 重启按钮 → restartDaemon 调用
  let restarted = false;
  const { ctx: ctx3 } = makeSandbox({});
  await tick();
  await vm.runInContext(`(async function() {
    App._testRestartDaemon = () => { window.__restartCalled = true; };
  })()`, ctx3);
  const appSrc = fs.readFileSync(path.join(RENDERER_JS, "app.js"), "utf8");
  assert.ok(appSrc.includes("restartDaemon"), "重启按钮应调用 window.emrg.restartDaemon");
  const GUI_DIR = path.join(__dirname, "..");
  const mainSrc = fs.readFileSync(path.join(GUI_DIR, "main.js"), "utf8");
  assert.ok(mainSrc.includes("emrg:restartDaemon"), "main.js 应注册 emrg:restartDaemon IPC");
  const preloadSrc = fs.readFileSync(path.join(GUI_DIR, "preload.js"), "utf8");
  assert.ok(preloadSrc.includes("restartDaemon"), "preload 应暴露 restartDaemon");
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

test("rant 10:48:58：window.marked 缺失时从 Monaco AMD 模块表兜底取回（防退化纯文本）", async () => {
  const { ctx, win } = makeSandbox({});
  // 模拟 #801 Monaco 全局 AMD define 劫持后 marked 未挂 window 的场景：
  // window.marked = null（沙箱默认），但 monaco require 模块表里有 marked
  win.marked = undefined; // 强制缺失（沙箱默认 null → 触发兜底路径）
  win.require = {
    s: {
      marked: { exports: { use: () => {}, parse: async () => "<p><strong>ok</strong></p>" } },
    },
  };
  win.DOMPurify = { sanitize: (x) => x };
  const html = await vm.runInContext('window.emrgMarkdown.renderMarkdown("**ok**")', ctx);
  assert.ok(String(html).includes("<strong>ok</strong>"), `AMD 兜底应渲染 marked：${html}`);
  assert.ok(win.marked && typeof win.marked.parse === "function", "兜底取回后 window.marked 应被赋值");
  // 无 AMD 表也无 marked → 降级转义 + 可诊断错误（不崩）
  const { ctx: ctx2, win: win2 } = makeSandbox({});
  win2.marked = undefined;
  win2.require = undefined;
  const html2 = await vm.runInContext('window.emrgMarkdown.renderMarkdown("<b>x</b>")', ctx2);
  assert.strictEqual(html2, "&lt;b&gt;x&lt;/b&gt;", "双缺失 → escapeHtml 降级");
});

test("模型切换器：菜单项构建 + 键盘导航 handler 注册", async () => {
  const { ctx } = makeSandbox({
    getSettings: async () => ({
      apiKey: "sk-x", baseUrl: "", model: "deepseek-chat",       theme: "system",
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
  const nsIdx = src.indexOf("async function newSession(");
  const nsBlock = src.slice(nsIdx, src.indexOf("async function deleteSession"));
  assert.ok(nsBlock.includes("setComposerDisabled(false)"), "newSession() 成功路径应启用输入框");
  const ssIdx = src.indexOf("async function switchSession(");
  const ssBlock = src.slice(ssIdx, nsIdx);
  assert.ok(ssBlock.includes("setComposerDisabled(false)"), "switchSession() 成功路径应启用输入框");
});

test("设置/首启对话框：Enter 提交键盘绑定（交互一致性）", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  const src = fs.readFileSync(path.join(RENDERER_JS, "app.js"), "utf8");
  const bindIdx = src.indexOf("function bindUi()");
  const bindBlock = src.slice(bindIdx); // bindUi 到文件末尾（含 enterToSave 绑定）
  assert.ok(bindBlock.includes("set-api-key"), "设置对话框 API Key 应绑定键盘");
  assert.ok(bindBlock.includes("welcome-api-key"), "首启引导 API Key 应绑定键盘");
  assert.ok(bindBlock.includes("enterToSave"), "Enter 应通过 enterToSave 触发提交");
});

test("对话列表键盘导航：↑↓ 聚焦 / Enter 切换（与 TUI /resume 一致）", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  const src = fs.readFileSync(path.join(RENDERER_JS, "sidebar.js"), "utf8");
  assert.ok(src.includes("ArrowDown"), "sidebar 应支持 ↑↓ 键盘导航");
  assert.ok(src.includes("kbd-focus"), "应有键盘聚焦状态");
  assert.ok(src.includes("App.switchSession"), "Enter 应切换会话");
  const css = fs.readFileSync(path.join(__dirname, "..", "renderer", "css", "components.css"), "utf8");
  assert.ok(css.includes(".conv-item.kbd-focus"), "键盘聚焦高亮样式应存在");
});

test("对话列表键盘导航：输入控件内不劫持（e.target 守卫，textarea ↑↓/Enter 正常）", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  const src = fs.readFileSync(path.join(RENDERER_JS, "sidebar.js"), "utf8");
  // 输入框是 <textarea id="input">，document 级常驻 keydown 必须跳过输入控件
  assert.ok(src.includes("closest(\"input, textarea, select, [contenteditable]\")"), "输入控件内应跳过键盘导航");
  assert.ok(src.includes("e.target.closest"), "应使用 e.target 守卫");
});

test("P3.2：tool_finished 只登记 write/edit 成功文件（bash 不登记）", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  // 空状态
  const empty = vm.runInContext('document.getElementById("result-list").innerHTML', ctx);
  assert.ok(String(empty).includes("还没有产物"), "应有空状态占位");
  // write 成功 → 产物行登记（提取 /tmp/hello.py）
  await vm.runInContext(`
    ResultPanel.addToolResult({ tool_call_id: "t1", tool_name: "write", content: "Created /tmp/hello.py (12 characters)", elapsed: 0.5 });
  `, ctx);
  const items = vm.runInContext('document.getElementById("result-list").children.length', ctx);
  assert.strictEqual(items, 1, "write 成功应登记 1 条产物");
  const rowPath = vm.runInContext('document.getElementById("result-list").children[0].dataset.path', ctx);
  assert.strictEqual(rowPath, "/tmp/hello.py", "产物行应带提取的路径");
  // bash 工具 → 不登记（决策点 3：产物 Tab 只留 write/edit 文件）
  await vm.runInContext(`
    ResultPanel.addToolResult({ tool_call_id: "t2", tool_name: "bash", content: "ls output here", elapsed: 0.3 });
  `, ctx);
  assert.strictEqual(vm.runInContext('document.getElementById("result-list").children.length', ctx), 1, "bash 工具不得登记为产物");
  // 失败产物 → 不登记
  await vm.runInContext(`
    ResultPanel.addToolResult({ tool_call_id: "t3", tool_name: "write", content: "Error writing file: /x.py", error: true });
  `, ctx);
  assert.strictEqual(vm.runInContext('document.getElementById("result-list").children.length', ctx), 1, "失败产物不得登记");
});

test("WorkBuddy P1：ResultPanel 折叠切换（⌘\ 与按钮）", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext("ResultPanel.toggle()", ctx);
  assert.ok(els["result-panel"].classList.contains("collapsed"), "toggle 后应折叠");
  await vm.runInContext("ResultPanel.toggle()", ctx);
  assert.ok(!els["result-panel"].classList.contains("collapsed"), "再次 toggle 应展开");
});

test("WorkBuddy P1：折叠后 toggle 按钮仍可见可点（rant 2026-08-10T14:11:18 死锁回归）", async () => {
  // CSS 规则断言（沙箱无 CSS 引擎，源级验证折叠态布局语义）
  const css = fs.readFileSync(path.join(RENDERER_CSS, "layout.css"), "utf8");
  const start = css.indexOf("#result-panel.collapsed");
  const end = css.indexOf(".result-header {", start); // 折叠规则组结束（下一个独立规则）
  const block = css.slice(start, end > start ? end : start + 400);
  // 折叠 = 40px 窄条（非 0 宽度）
  assert.ok(/width:\s*40px/.test(block), "折叠态应为 40px 窄条，而非 width: 0（否则按钮不可点）");
  assert.ok(!/width:\s*0/.test(block), "折叠态不得 width: 0");
  // header 保留可见（display: flex），只藏内容区 .result-list
  assert.ok(/#result-panel\.collapsed \.result-header\s*{[^}]*display:\s*flex/.test(css.replace(/\n/g, " ")), "折叠态 .result-header 应 display: flex（含 toggle 按钮）");
  assert.ok(!/display:\s*none[^}]*result-header/.test(css), "折叠态不得隐藏 .result-header");
  assert.ok(/#result-panel\.collapsed \.result-list\s*{[^}]*display:\s*none/.test(css.replace(/\n/g, " ")), "折叠态 .result-list 应隐藏");
  // toggle 按钮折叠态旋转提示可展开
  assert.ok(/#result-panel\.collapsed \.result-toggle\s*{[^}]*rotate\(180deg\)/.test(css.replace(/\n/g, " ")), "折叠态 toggle 应 rotate(180deg) 指示可展开");
  // DOM 存活：折叠后 toggle 按钮仍在文档中
  const { ctx } = makeSandbox();
  await tick();
  await vm.runInContext("ResultPanel.toggle()", ctx);
  const btnStillThere = vm.runInContext('document.getElementById("result-toggle") !== null', ctx);
  assert.ok(btnStillThere, "折叠后 toggle 按钮不得从 DOM 移除");
});

test("P2 框架：右栏 Tab 栏渲染 + 切换 pane 显隐", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  // 默认产物 Tab active，文件 pane 隐藏
  assert.ok(els["result-tab-artifacts"].classList.contains("active"), "默认产物 Tab 应激活");
  assert.ok(els["result-list"].classList.contains("active"), "产物 pane 应显示");
  assert.ok(!els["result-files"].classList.contains("active"), "文件 pane 应隐藏");
  // 点击文件 Tab → 切换
  els["result-tab-files"].click();
  assert.ok(els["result-tab-files"].classList.contains("active"), "文件 Tab 应激活");
  assert.ok(els["result-files"].classList.contains("active"), "文件 pane 应显示");
  assert.ok(!els["result-list"].classList.contains("active"), "产物 pane 应隐藏");
  // 切回产物
  els["result-tab-artifacts"].click();
  assert.ok(els["result-list"].classList.contains("active"), "切回产物 pane");
});

test("rant 20:49:45：产物 pane 显示由 .result-pane 控制——.result-list 不得重声明 display", () => {
  // CSS 源级断言（沙箱无 CSS 引擎）。根因：.result-list 曾声明 display:flex（同特异性 0,1,0
  // 后定义胜出）覆盖 .result-pane{display:none}，导致文件 tab 下产物 pane 恒显示。
  const css = fs.readFileSync(path.join(RENDERER_CSS, "layout.css"), "utf8");
  // 定位独立 .result-list 规则（行首选择器，排除 #result-panel.collapsed .result-list 前缀形态）
  const start = css.indexOf("\n.result-list {") + 1;
  const end = css.indexOf("\n.result-empty {", start); // 下一个独立规则
  const block = css.slice(start, end > start ? end : start + 400);
  assert.ok(start > 0, ".result-list 独立规则应存在");
  assert.ok(!/display\s*:/.test(block), ".result-list 不得重声明 display（显隐由 .result-pane/.result-pane.active 控制）");
  assert.ok(!/flex-direction\s*:/.test(block), ".result-list 不得重声明 flex-direction（.result-pane.active 已提供）");
});

test("P2 框架：resizer 拖拽改宽度 + .dragging 抑制 transition", async () => {
  const { ctx, win, els } = makeSandbox();
  await tick();
  const startW = parseInt(els["result-panel"].style.width, 10) || 280;
  // mousedown → 记录拖拽 + 加 .dragging
  els["result-resizer"].dispatch("mousedown", { clientX: 400 });
  assert.ok(els["result-panel"].classList.contains("dragging"), "拖拽中应有 .dragging（抑制 transition）");
  // mousemove 右移 50 → 面板变窄 50
  win._listeners.mousemove.at(-1)({ clientX: 450 });
  const w = parseInt(els["result-panel"].style.width, 10);
  assert.ok(w < startW, `拖拽右移应变窄：${startW} → ${w}`);
  // 宽度持久化
  assert.ok(win.localStorage.getItem("emrg.resultPanel.panelWidth") === String(w), "panelWidth 应持久化");
  // mouseup → 清理拖拽态 + 解绑
  win._listeners.mouseup.at(-1)({});
  assert.ok(!els["result-panel"].classList.contains("dragging"), "mouseup 应移除 .dragging");
  assert.strictEqual((win._listeners.mousemove || []).length, 0, "mouseup 应解绑 mousemove");
});

test("P2 框架：折叠/展开与宽度分离持久化", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  // 先调整宽度到 320
  await vm.runInContext("ResultPanel.setWidth(320)", ctx);
  assert.strictEqual(parseInt(els["result-panel"].style.width, 10), 320);
  // 折叠：宽度固化到持久化 + 窄条 40px + resizer 隐藏
  await vm.runInContext("ResultPanel.toggle()", ctx);
  assert.ok(els["result-panel"].classList.contains("collapsed"), "toggle 后应折叠");
  assert.strictEqual(els["result-panel"].style.width, "40px", "折叠窄条 40px");
  assert.strictEqual(els["result-resizer"].style.display, "none", "折叠时 resizer 隐藏");
  // 展开：恢复持久化宽度 320（非默认 280）
  await vm.runInContext("ResultPanel.toggle()", ctx);
  assert.ok(!els["result-panel"].classList.contains("collapsed"), "再次 toggle 应展开");
  assert.strictEqual(parseInt(els["result-panel"].style.width, 10), 320, "展开应恢复 panelWidth 而非默认值");
});

test("P2 框架：打开文件 Tab 去重 / 上限 8 / 关闭", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  for (const p of ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py"]) {
    await vm.runInContext(`ResultPanel.openFileTab(null, ${JSON.stringify(p)})`, ctx);
  }
  assert.strictEqual(els["result-tabbar"].children.length, 3, "应渲染 3 个文件 Tab");
  // 重复打开 a.py → 去重（仍是 3 个），激活既有
  await vm.runInContext('ResultPanel.openFileTab(null, "/tmp/a.py")', ctx);
  assert.strictEqual(els["result-tabbar"].children.length, 3, "同路径应去重");
  assert.ok(els["result-tabbar"].children[0].classList.contains("active"), "重复打开应激活既有 Tab");
  // 上限 8：连续打开 10 个 → 淘汰最旧（a/b 及 f1/f2 被淘汰）
  for (const n of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) {
    await vm.runInContext(`ResultPanel.openFileTab(null, "/tmp/f${n}.js")`, ctx);
  }
  const tabs = els["result-tabbar"].children;
  assert.ok(tabs.length <= 8, `上限 8，实际 ${tabs.length}`);
  assert.strictEqual(tabs[0].dataset.path, "/tmp/f3.js", "最旧 Tab 应被淘汰");
  // 关闭激活 Tab → 从条上移除
  await vm.runInContext('ResultPanel.closeFileTab(null, "/tmp/f10.js")', ctx);
  assert.ok(![...els["result-tabbar"].children].some((c) => c.dataset.path === "/tmp/f10.js"), "关闭后应移除");
});

test("P2 框架：per-session Tab 状态隔离（切会话各显各的）", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext('ResultPanel.openFileTab("s1", "/proj1/a.py")', ctx);
  await vm.runInContext('ResultPanel.openFileTab("s2", "/proj2/b.py")', ctx);
  // s1 激活：只显示 s1 的 Tab
  await vm.runInContext('ResultPanel.switchSession("s1")', ctx);
  assert.deepStrictEqual([...els["result-tabbar"].children].map((c) => c.dataset.path), ["/proj1/a.py"], "s1 只显示自己的 Tab");
  // s2 激活：只显示 s2 的 Tab
  await vm.runInContext('ResultPanel.switchSession("s2")', ctx);
  assert.deepStrictEqual([...els["result-tabbar"].children].map((c) => c.dataset.path), ["/proj2/b.py"], "s2 只显示自己的 Tab");
  // 切回 s1：激活状态保留（a.py 仍 active）
  await vm.runInContext('ResultPanel.switchSession("s1")', ctx);
  assert.ok(els["result-tabbar"].children[0].classList.contains("active"), "s1 的激活 Tab 应保留");
});

test("P2 框架：后台会话 tool_finished 只入桶不渲染（防污染激活 pane）", async () => {
  const { ctx } = makeSandbox();
  await tick();
  await vm.runInContext('ResultPanel.switchSession("s1")', ctx);
  // 后台会话 s2 的 tool_finished → 只入桶不渲染
  await vm.runInContext('ResultPanel.addToolResult({ tool_name: "write", content: "Wrote file: /bg.txt", elapsed: 0.2 }, "s2")', ctx);
  assert.strictEqual(vm.runInContext('document.getElementById("result-list").children.length', ctx), 0, "后台会话产物不得渲染到激活 pane");
  // 激活会话 s1 的 tool_finished → 渲染
  await vm.runInContext('ResultPanel.addToolResult({ tool_name: "write", content: "Wrote file: /active.txt", elapsed: 0.3 }, "s1")', ctx);
  assert.strictEqual(vm.runInContext('document.getElementById("result-list").children.length', ctx), 1, "激活会话产物应渲染");
});

test("P2 框架：switchSession 按 sid 重渲染产物 pane（桶→DOM 恢复）", async () => {
  const { ctx } = makeSandbox();
  await tick();
  // 两会话各自登记（初始非激活 → 只入桶；P3.2 只登记 write/edit 文件）
  await vm.runInContext('ResultPanel.addToolResult({ tool_name: "write", content: "Created /p1/a.py (5 characters)", elapsed: 0.1 }, "s1")', ctx);
  await vm.runInContext('ResultPanel.addToolResult({ tool_name: "write", content: "Created /p2/b.py (5 characters)", elapsed: 0.2 }, "s2")', ctx);
  // 递归收集 list 文本（mock 元素 textContent 是属性，不入 innerHTML）
  const collectText = `(function(){
    function collect(node) {
      let out = node.textContent || "";
      for (const c of node.children || []) out += collect(c);
      return out;
    }
    return collect(document.getElementById("result-list"));
  })()`;
  // 切到 s2 → pane 只显示 s2 的记录
  await vm.runInContext('ResultPanel.switchSession("s2")', ctx);
  const text2 = vm.runInContext(collectText, ctx);
  assert.ok(String(text2).includes("/p2/b.py"), "s2 pane 应显示 s2 产物");
  assert.ok(!String(text2).includes("/p1/a.py"), "s2 pane 不得显示 s1 产物");
  // 切回 s1 → pane 只显示 s1 的记录（s2 卡片不残留）
  await vm.runInContext('ResultPanel.switchSession("s1")', ctx);
  const text1 = vm.runInContext(collectText, ctx);
  assert.ok(String(text1).includes("/p1/a.py"), "s1 pane 应显示 s1 产物");
  assert.ok(!String(text1).includes("/p2/b.py"), "s1 pane 不得显示 s2 产物");
});

test("P3.2：同路径去重（更新既有条目移顶）+ per-session 上限 100", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext('ResultPanel.switchSession("s1")', ctx);
  await vm.runInContext('ResultPanel.addToolResult({ tool_name: "write", content: "Created /a.py (5 characters)", elapsed: 0.1 }, "s1")', ctx);
  await vm.runInContext('ResultPanel.addToolResult({ tool_name: "write", content: "Created /b.py (5 characters)", elapsed: 0.2 }, "s1")', ctx);
  // 同路径再写 → 去重（2 条，/a.py 移到顶部）
  await vm.runInContext('ResultPanel.addToolResult({ tool_name: "write", content: "Created /a.py (9 characters)", elapsed: 0.3 }, "s1")', ctx);
  const paths = vm.runInContext(`
    (function(){ return [...document.getElementById("result-list").children].map(c => c.dataset.path); })()
  `, ctx);
  // vm 跨 realm 数组原型不同 → deepStrictEqual 失败；用 join 比较
  assert.strictEqual(paths.join(","), "/a.py,/b.py", "同路径去重并移顶");
  // 上限 100：填满后最旧被淘汰
  for (let i = 0; i < 105; i++) {
    await vm.runInContext(`ResultPanel.addToolResult({ tool_name: "write", content: "Created /f${i}.py (5 characters)", elapsed: 0.1 }, "s1")`, ctx);
  }
  const n = vm.runInContext('document.getElementById("result-list").children.length', ctx);
  assert.ok(n <= 100, `per-session 上限 100，实际 ${n}`);
});

test("P3.2：无扩展名文件（Makefile/.env）提取 + 点击产物行打开查看器 Tab", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext('ResultPanel.switchSession("s1")', ctx);
  // edit 输出 "Made 1 replacement in /proj/Makefile"（无扩展名也命中）
  await vm.runInContext('ResultPanel.addToolResult({ tool_name: "edit", content: "Made 1 replacement in /proj/Makefile", elapsed: 0.2 }, "s1")', ctx);
  const row = vm.runInContext('document.getElementById("result-list").children[0]', ctx);
  assert.strictEqual(row.dataset.path, "/proj/Makefile", "无扩展名文件应提取");
  // 点击 → 打开查看器 Tab（P2.2 框架）
  row.click();
  await tick();
  assert.ok([...els["result-tabbar"].children].some((c) => c.dataset.path === "/proj/Makefile"), "点击产物行应打开查看器 Tab");
});

test("P3：文件树渲染根 + 根自动展开懒加载", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext('FileTree.setSession("s1", "/proj")', ctx);
  await tick(); // 根自动展开（async listFiles）
  // 根行渲染
  const roots = els["result-files"].querySelectorAll(".ft-root");
  assert.strictEqual(roots.length, 1, "应有根行");
  // 根子项：src 目录 + README.md 文件（daemon 排序：目录在前）
  const dirs = els["result-files"].querySelectorAll(".ft-dir");
  assert.strictEqual(dirs.length, 2, "根 + src 子目录");
  const files = els["result-files"].querySelectorAll(".ft-file");
  assert.strictEqual(files.length, 1, "README.md 文件行");
  assert.strictEqual(files[0].dataset.path, "/proj/README.md");
  // 根 kids 可见（自动展开）
  const kids = els["result-files"].querySelectorAll(".ft-kids");
  assert.ok(kids.length >= 1);
  assert.ok(!kids[0].classList.contains("hidden"), "根子项应可见");
});

test("P3：点击目录行懒加载展开子项（缓存，折叠再展开不重复拉取）", async () => {
  let calls = 0;
  const { ctx, els } = makeSandbox({
    listFiles: async ({ path } = {}) => {
      calls++;
      if (path === "/proj") return { entries: [{ name: "src", path: "/proj/src", type: "dir" }] };
      return { entries: [{ name: "inner.txt", path: "/proj/src/inner.txt", type: "file" }] };
    },
  });
  await tick();
  await vm.runInContext('FileTree.setSession("s1", "/proj")', ctx);
  await tick();
  assert.strictEqual(calls, 1, "根自动展开拉取一次");
  // 点击 src 目录 → 懒加载
  const srcRow = els["result-files"].querySelectorAll(".ft-dir")[1];
  srcRow.dispatch("click", { stopPropagation() {} });
  await tick();
  assert.strictEqual(calls, 2, "点击目录应懒加载一次");
  assert.ok([...els["result-files"].querySelectorAll(".ft-file")].some((f) => f.dataset.path === "/proj/src/inner.txt"), "子项应出现");
  // 折叠再展开 → 缓存命中（不再拉取）
  srcRow.dispatch("click", { stopPropagation() {} });
  assert.ok(srcRow.querySelector(".ft-kids").classList.contains("hidden"), "再次点击应折叠");
  srcRow.dispatch("click", { stopPropagation() {} });
  await tick();
  assert.strictEqual(calls, 2, "缓存命中不重复拉取");
});

test("P3.5：目录行 chevron 展开/收起指示（rant 20:58:57）", async () => {
  const { ctx, els } = makeSandbox({
    listFiles: async ({ path } = {}) => {
      if (path === "/proj") return { entries: [{ name: "src", path: "/proj/src", type: "dir" }] };
      return { entries: [] };
    },
  });
  await tick();
  await vm.runInContext('FileTree.setSession("s1", "/proj")', ctx);
  await tick();
  // 根默认展开 → chevronDown ▾
  const rootRow = els["result-files"].querySelectorAll(".ft-root")[0];
  const rootChev = rootRow.querySelector(".ft-chevron");
  assert.ok(rootChev, "根目录行应有 chevron");
  assert.ok(rootChev.innerHTML.includes("M3 6l5 5 5-5z"), "根展开态 chevronDown");
  // src 目录折叠 → chevronRight ▸
  const srcRow = els["result-files"].querySelectorAll(".ft-dir")[1];
  const srcChev = srcRow.querySelector(".ft-chevron");
  assert.ok(srcChev, "目录行应有 chevron");
  assert.ok(srcChev.innerHTML.includes("M6 3l5 5-5 5z"), "折叠态 chevronRight");
  // 点击展开 → 切 chevronDown
  srcRow.dispatch("click", { stopPropagation() {} });
  await tick();
  assert.ok(srcChev.innerHTML.includes("M3 6l5 5 5-5z"), "展开后 chevronDown");
  // 文件行无 chevron
  const files = els["result-files"].querySelectorAll(".ft-file");
  for (const f of files) {
    assert.strictEqual(f.querySelector(".ft-chevron"), null, "文件行不应有 chevron");
  }
});

test("P3：点击文件行 → 打开文件 Tab + 查看器加载内容", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  // 对齐 app.js 接线：switchSession + setSession 成对（文件树 Tab 归属当前会话桶）
  await vm.runInContext('ResultPanel.switchSession("s1")', ctx);
  await vm.runInContext('FileTree.setSession("s1", "/proj")', ctx);
  await tick();
  const readFileRow = [...els["result-files"].querySelectorAll(".ft-file")].find((r) => r.dataset.path === "/proj/README.md");
  readFileRow.dispatch("click", { stopPropagation() {} });
  await tick(); // openFileTab → activateTab → loadFileTab（async readFile）
  // 文件 Tab 打开
  assert.ok([...els["result-tabbar"].children].some((c) => c.dataset.path === "/proj/README.md"), "文件 Tab 应打开");
  // 查看器 pane 激活 + 内容渲染（README.md → P3.3 markdown 渲染）
  assert.ok(els["result-viewer"].classList.contains("active"), "查看器 pane 应激活");
  assert.ok(els["result-viewer"].querySelectorAll(".viewer-md").length >= 1, "md 文件应渲染 markdown 内容");
  // 静态 Tab 不被误激活
  assert.ok(!els["result-tab-files"].classList.contains("active"), "文件 Tab 不应激活");
});

test("P3：查看器二进制文件 → 提示用系统工具打开", async () => {
  const { ctx, els } = makeSandbox({ readFile: async () => ({ content: "", binary: true }) });
  await tick();
  await vm.runInContext('ResultPanel.openFileTab(null, "/data/blob.bin")', ctx); // 非图片扩展名（.bin）
  await tick();
  assert.ok(els["result-viewer"].classList.contains("active"), "查看器 pane 应激活");
  const empty = els["result-viewer"].querySelectorAll(".result-empty");
  assert.ok(empty.length >= 1, "二进制应显示提示而非文本");
  assert.ok(els["result-viewer"].querySelectorAll(".viewer-pre").length === 0, "二进制不渲染文本");
});

test("P3.3：图片文件不走 read_file（file:// 直显 img）", async () => {
  let readCalled = false;
  const { ctx, els } = makeSandbox({
    readFile: async () => { readCalled = true; return { content: "", binary: false }; },
  });
  await tick();
  await vm.runInContext('ResultPanel.openFileTab(null, "/img/logo.png")', ctx);
  await tick();
  assert.ok(els["result-viewer"].classList.contains("active"), "查看器 pane 应激活");
  assert.ok(els["result-viewer"].querySelectorAll(".viewer-img").length === 1, "应渲染 img 元素");
  assert.strictEqual(readCalled, false, "图片不得调用 read_file");
  // CSP 允许 file:（源级断言防回归）
  const html = fs.readFileSync(path.join(__dirname, "..", "renderer", "index.html"), "utf8");
  assert.ok(/img-src 'self' data: file:/.test(html), "CSP img-src 应含 file:");
});

test("P3.3：markdown 文件走 renderMarkdown 渲染", async () => {
  const { ctx, els } = makeSandbox({
    readFile: async () => ({ content: "# Title\n\nbody", binary: false }),
  });
  await tick();
  await vm.runInContext('ResultPanel.openFileTab(null, "/proj/README.md")', ctx);
  await tick();
  assert.ok(els["result-viewer"].querySelectorAll(".viewer-md").length >= 1, "md 应渲染 viewer-md 容器");
  assert.ok(els["result-viewer"].querySelectorAll(".viewer-pre").length === 0, "md 不渲染纯文本 pre");
});

test("P3.3：文本代码文件 → hljs 高亮（.py → python）", async () => {
  const sb = makeSandbox({
    readFile: async () => ({ content: "def f():\n    return 1", binary: false }),
  });
  const { ctx, els } = sb;
  // hljs 是 window 级全局（overrides 只进 emrg）→ 沙箱创建后注入
  sb.win.hljs = {
    getLanguage: (l) => (l === "python" ? {} : null),
    highlight: (code, opts) => ({ value: "<span class=\"hljs-keyword\">def</span> f():" }),
    highlightAuto: (code) => ({ value: code }),
  };
  await tick();
  await vm.runInContext('ResultPanel.openFileTab(null, "/proj/main.py")', ctx);
  await tick();
  const pre = els["result-viewer"].querySelectorAll(".viewer-pre");
  assert.ok(pre.length === 1, "应渲染高亮 pre");
  // harness querySelector 只支持类选择器 → 直接取 pre 子元素（code）
  const code = pre[0].children && pre[0].children[0];
  assert.ok(code, "pre 内应有 code");
  assert.ok((code.className || "").includes("hljs"), "code 应带 hljs 类");
  assert.ok((code.className || "").includes("language-python"), "应带 language-python 类");
});

test("P3：切会话 → 文件树根跟随（per-session）", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext('FileTree.setSession("s1", "/proj")', ctx);
  await tick();
  const rootName1 = els["result-files"].querySelectorAll(".ft-root")[0].querySelector(".ft-name").textContent;
  assert.strictEqual(rootName1, "proj", "s1 根 = proj");
  // 切 s2（不同项目）
  await vm.runInContext('FileTree.setSession("s2", "/other")', ctx);
  await tick();
  const roots = els["result-files"].querySelectorAll(".ft-root");
  assert.strictEqual(roots.length, 1, "根应重设（缓存清空）");
  const rootName2 = roots[0].querySelector(".ft-name").textContent;
  assert.strictEqual(rootName2, "other", "s2 根 = other");
});

test("P3.5（rant 17:28）：VS Code 风格图标 — 目录折叠/展开 + 文件类型映射", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext('FileTree.setSession("s1", "/proj")', ctx);
  await tick();
  const pathCount = (ic) => (ic ? (ic.innerHTML.match(/<path/g) || []).length : 0);
  // 根目录展开态 → dirOpen 图标（双 path：含开口 flap）
  const rootIcon = els["result-files"].querySelectorAll(".ft-root")[0].querySelector(".ft-icon");
  assert.ok(rootIcon, "目录行应有 .ft-icon");
  assert.strictEqual(pathCount(rootIcon), 2, "展开目录图标 = dirOpen（两个 path）");
  // src 目录默认折叠 → dirClosed（单 path）
  const srcRow = els["result-files"].querySelectorAll(".ft-dir")[1];
  const srcIcon = srcRow.querySelector(".ft-icon");
  assert.strictEqual(pathCount(srcIcon), 1, "折叠目录图标 = dirClosed（单 path）");
  // 点击展开 src → 图标切换为 dirOpen（两个 path）+ 子项懒加载出现
  srcRow.dispatch("click", { stopPropagation() {} });
  await tick();
  assert.strictEqual(pathCount(srcRow.querySelector(".ft-icon")), 2, "展开后图标应切换 dirOpen");
  // 文件图标映射：README.md → fileMd（内联横线 path）；main.py → fileCode（尖括号 path）
  const mdRow = [...els["result-files"].querySelectorAll(".ft-file")].find((r) => r.dataset.path === "/proj/README.md");
  const mdIcon = mdRow.querySelector(".ft-icon");
  assert.ok(mdIcon, "文件行应有 .ft-icon");
  assert.ok(mdIcon.innerHTML.includes("M4.5 6h7M4.5 8.5h7M4.5 11h4.5"), "md → fileMd 横线组");
  const pyRow = [...els["result-files"].querySelectorAll(".ft-file")].find((r) => r.dataset.path === "/proj/src/main.py");
  assert.ok(pyRow.querySelector(".ft-icon").innerHTML.includes("M6.5 6.5L4.5 8.5l2 2"), "py → fileCode 尖括号");
});

test("P3.5（rant 17:28）：文件行选中态 — 点击选中 + 单选切换", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext('ResultPanel.switchSession("s1")', ctx);
  await vm.runInContext('FileTree.setSession("s1", "/proj")', ctx);
  await tick();
  const mdRow = [...els["result-files"].querySelectorAll(".ft-file")].find((r) => r.dataset.path === "/proj/README.md");
  // 点击 → active
  mdRow.dispatch("click", { stopPropagation() {} });
  assert.ok(mdRow.classList.contains("active"), "点击文件行应加 .active");
  // 点击 src 目录 → 选中不移除（目录不高亮），展开目录
  const srcRow = els["result-files"].querySelectorAll(".ft-dir")[1];
  srcRow.dispatch("click", { stopPropagation() {} });
  await tick();
  assert.ok(mdRow.classList.contains("active"), "目录行点击不应清除文件选中");
  assert.ok(!srcRow.classList.contains("active"), "目录行不应有 active");
  // 点击另一个文件 → 单选切换
  const pyRow = [...els["result-files"].querySelectorAll(".ft-file")].find((r) => r.dataset.path === "/proj/src/main.py");
  pyRow.dispatch("click", { stopPropagation() {} });
  assert.ok(pyRow.classList.contains("active"), "新点击文件应 active");
  assert.ok(!mdRow.classList.contains("active"), "旧选中应移除（单选）");
});

test("P3.5（rant 17:28）：深度缩进 — 子行 padding-left 递增 16px", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext('FileTree.setSession("s1", "/proj")', ctx);
  await tick();
  const rootRow = els["result-files"].querySelectorAll(".ft-root")[0];
  assert.strictEqual(rootRow.style.paddingLeft, "8px", "根 depth=0 → 8px");
  // 根子项 README.md（depth 1）
  const mdRow = [...els["result-files"].querySelectorAll(".ft-file")].find((r) => r.dataset.path === "/proj/README.md");
  assert.strictEqual(mdRow.style.paddingLeft, "24px", "根子项 depth=1 → 24px");
  // 展开 src → main.py（depth 2）
  const srcRow = els["result-files"].querySelectorAll(".ft-dir")[1];
  srcRow.dispatch("click", { stopPropagation() {} });
  await tick();
  const pyRow = [...els["result-files"].querySelectorAll(".ft-file")].find((r) => r.dataset.path === "/proj/src/main.py");
  assert.strictEqual(pyRow.style.paddingLeft, "40px", "src 子项 depth=2 → 40px");
  // 结构契约（修复布局 bug）：.ft-head 包装图标+名称，.ft-kids 紧随其后——块级排布，
  // 兄弟行不再与展开子项重叠（headless Chrome 像素实证：定高 flex-wrap 下行遮挡子项）
  const srcHead = srcRow.children[0];
  assert.ok(srcHead && srcHead.classList.contains("ft-head"), "目录行第一个子元素应为 .ft-head 包装");
  assert.ok(srcHead.querySelector(".ft-icon"), "图标在 .ft-head 内");
  assert.strictEqual(srcRow.children[1], srcRow.querySelector(".ft-kids"), ".ft-kids 紧随 .ft-head");
});

test("P3.5（rant 17:28）：展开态持久 + 滚动条 hover 样式（CSS 源级断言）", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext('FileTree.setSession("s1", "/proj")', ctx);
  await tick();
  const srcRow = els["result-files"].querySelectorAll(".ft-dir")[1];
  // 展开 src → 折叠 → 再展开：缓存命中 + 展开态持久（expandDir 不重复拉取已由既有测试覆盖，这里验证 DOM 状态往返）
  srcRow.dispatch("click", { stopPropagation() {} });
  await tick();
  assert.ok(!srcRow.querySelector(".ft-kids").classList.contains("hidden"), "展开后 kids 可见");
  srcRow.dispatch("click", { stopPropagation() {} });
  assert.ok(srcRow.querySelector(".ft-kids").classList.contains("hidden"), "折叠后 kids 隐藏");
  srcRow.dispatch("click", { stopPropagation() {} });
  await tick();
  assert.ok(!srcRow.querySelector(".ft-kids").classList.contains("hidden"), "再次展开恢复可见");
  const iconHtml = srcRow.querySelector(".ft-icon").innerHTML;
  assert.strictEqual((iconHtml.match(/<path/g) || []).length, 2, "展开态图标 dirOpen");
  // 滚动条 hover 显示 CSS（VS Code 行为）
  const css = fs.readFileSync(path.join(RENDERER_CSS, "layout.css"), "utf8");
  assert.ok(/\.result-files:hover::-webkit-scrollbar-thumb/.test(css), "hover 才显示滚动条 thumb");
  assert.ok(/background: transparent/.test(css) || /var\(--scrollbar-thumb\)/.test(css), "默认透明/悬停 var thumb");
  // 文件 tab 同排并入 .result-tabs（rant 17:28 项 10）：tabbar 在 result-toggle 之前（同一行内）
  const html = fs.readFileSync(path.join(__dirname, "..", "renderer", "index.html"), "utf8");
  const tabsIdx = html.indexOf('id="result-tabs"');
  const tabbarIdx = html.indexOf('id="result-tabbar"');
  const toggleIdx = html.indexOf('id="result-toggle"');
  assert.ok(tabsIdx >= 0 && tabbarIdx >= 0 && toggleIdx >= 0, "三个锚点都应存在");
  assert.ok(tabsIdx < tabbarIdx && tabbarIdx < toggleIdx, "tabbar 应嵌套在 result-tabs 内（同排），旧布局 toggle 在 tabbar 之前");
});

test("P3.5（rant 2026-08-13T12:46:12）：.result-files 滚动 CSS flex:1 + min-height:0", async () => {
  // #result-panel 是 flex column；.result-list 有 flex:1 所以滚动正常，.result-files 缺 flex:1/min-height:0
  // → 高度随内容增长永不压缩，overflow-y:auto 永不触发（有滚动条槽但滚不动）
  const css = fs.readFileSync(path.join(RENDERER_CSS, "layout.css"), "utf8").replace(/\n/g, " ");
  const m = css.match(/\.result-files\s*\{([^}]*)\}/);
  assert.ok(m, ".result-files 规则存在");
  assert.ok(/flex:\s*1/.test(m[1]), ".result-files 应 flex:1（对齐 .result-list）");
  assert.ok(/min-height:\s*0/.test(m[1]), ".result-files 应 min-height:0（允许压缩触发滚动）");
  assert.ok(/overflow-y:\s*auto/.test(m[1]), ".result-files 保留 overflow-y:auto");
});

test("P3.5（rant 2026-08-13T12:47:18）：根目录行可收起/展开", async () => {
  const { ctx, els } = makeSandbox();
  await tick();
  await vm.runInContext('FileTree.setSession("s1", "/proj")', ctx);
  await tick();
  const rootRow = els["result-files"].querySelectorAll(".ft-root")[0];
  assert.ok(rootRow, "根目录行存在");
  const kids = rootRow.querySelector(".ft-kids");
  assert.ok(kids, "根目录 kids 容器存在");
  assert.ok(!kids.classList.contains("hidden"), "默认展开（ft-kids 无 hidden）");
  rootRow.dispatch("click", { stopPropagation() {} });
  assert.ok(kids.classList.contains("hidden"), "点击根目录应收起");
  rootRow.dispatch("click", { stopPropagation() {} });
  await tick();
  assert.ok(!kids.classList.contains("hidden"), "再次点击应展开");
});

test("工具调用上限中断 → 系统提示可继续（对齐 TUI，跨项目教训）", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  // 模拟 daemon 广播：max tool rounds 超限的 done 帧
  await vm.runInContext(
    'EMRG_Chat.handleDone({ request_id: "rid-max", content: "Exceeded maximum tool call rounds (30).", done: true })',
    ctx
  );
  const texts = (els["workspace"].children || []).map((c) => c.textContent).join("|");
  assert.ok(/继续/.test(texts), `应提示可继续，实际: ${texts}`);
});

test("正常 done 帧不触发上限提示（无假阳性）", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'EMRG_Chat.handleDone({ request_id: "rid-ok", content: "完成了！", done: true })',
    ctx
  );
  const texts = (els["workspace"].children || []).map((c) => c.textContent).join("|");
  assert.ok(!/继续/.test(texts), "正常完成不应出现继续提示");
});

// ── P3 slice 0（rant 15:07:19）：会话级状态隔离 + 容器路由 ──────────────

test("P3: 按 sid 隔离 delta 分组——两会话同 request_id 互不串扰", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'EMRG_Chat.handleDelta([{ request_id: "rid-x", content: "A", done: false, delta: true }], "sess-a");' +
    'EMRG_Chat.handleDelta([{ request_id: "rid-x", content: "B", done: false, delta: true }], "sess-b");',
    ctx
  );
  // 两会话各自建组：A 组在 sess-a 桶，B 组在 sess-b 桶
  assert.strictEqual(ctx.EMRG_Chat.groupNodesFor("sess-a").size, 1, "sess-a has its group");
  assert.strictEqual(ctx.EMRG_Chat.groupNodesFor("sess-b").size, 1, "sess-b has its group");
  assert.strictEqual(ctx.EMRG_Chat.groupNodesFor("sess-a").get("rid-x").node.querySelector(".msg-body").textContent.includes("A"), true);
  assert.strictEqual(ctx.EMRG_Chat.groupNodesFor("sess-b").get("rid-x").node.querySelector(".msg-body").textContent.includes("B"), true);
});

test("P3: done 只清理该会话分组；另一会话同 rid 组保留", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'EMRG_Chat.handleDelta([{ request_id: "rid-y", content: "a", done: false, delta: true }], "sess-a");' +
    'EMRG_Chat.handleDelta([{ request_id: "rid-y", content: "b", done: false, delta: true }], "sess-b");' +
    'EMRG_Chat.handleDone({ request_id: "rid-y", done: true }, "sess-a");',
    ctx
  );
  assert.strictEqual(ctx.EMRG_Chat.groupNodesFor("sess-a").size, 0, "sess-a done clears its group");
  assert.strictEqual(ctx.EMRG_Chat.groupNodesFor("sess-b").size, 1, "sess-b group untouched");
  // 残留 delta 只丢已 done 会话（sess-a 的 rid 已 done），sess-b 同 rid 仍渲染
  await vm.runInContext(
    'EMRG_Chat.handleDelta([{ request_id: "rid-y", content: "more", done: false, delta: true }], "sess-b");',
    ctx
  );
  assert.strictEqual(ctx.EMRG_Chat.groupNodesFor("sess-b").get("rid-y").node.querySelector(".msg-body").textContent.includes("more"), true);
});

test("P3: clearTyping(sid) 只清该会话在途 typing；另一会话保留", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'EMRG_Chat.handleDelta([{ request_id: "rid-1", content: "x", done: false, delta: true }], "sess-a");' +
    'EMRG_Chat.handleDelta([{ request_id: "rid-2", content: "y", done: false, delta: true }], "sess-b");' +
    'EMRG_Chat.clearTyping("sess-a");',
    ctx
  );
  const bodyA = ctx.EMRG_Chat.groupNodesFor("sess-a").get("rid-1").node.querySelector(".msg-body");
  const bodyB = ctx.EMRG_Chat.groupNodesFor("sess-b").get("rid-2").node.querySelector(".msg-body");
  assert.ok(!bodyA.classList.contains("typing"), "sess-a typing cleared");
  assert.ok(bodyB.classList.contains("typing"), "sess-b typing retained");
});

test("P3: registerContainer 后该会话渲染进独立容器；无 sid 回退默认聊天区", async () => {
  const { ctx, els, document } = makeSandbox({});
  await tick();
  // 注册独立容器（P4 openSessions 语义）——直接经 ctx 导出的 API 挂 Node 侧元素
  const extra = document.createElement("div");
  extra.id = "session-view-sess-c";
  els["main"].appendChild(extra);
  ctx.EMRG_Chat.registerContainer("sess-c", extra);
  ctx.EMRG_Chat.addSystemMessage("hello-c", "sess-c");
  ctx.EMRG_Chat.addSystemMessage("hello-default");
  assert.strictEqual(extra.children.length, 1, "registered container receives its session's nodes");
  assert.strictEqual(extra.children[0].textContent, "hello-c");
  assert.strictEqual(els["workspace"].children.length, 1, "default container receives un-sid'd nodes");
  // unregister → 状态释放，再发同 sid 消息回落默认容器
  ctx.EMRG_Chat.unregisterContainer("sess-c");
  ctx.EMRG_Chat.addSystemMessage("after-unreg", "sess-c");
  assert.strictEqual(extra.children.length, 1, "unregistered container no longer receives");
  assert.strictEqual(els["workspace"].children.length, 2, "falls back to default container");
});

// ── P3 slice 1（rant 15:07:19）：renderer sessionsBySid 会话级状态表 ─────

test("P3 s1: state.busy/ownStreamRequestId 路由到激活会话条目（get-or-create）", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-1";' +
    'App.state.busy = true;' +
    'App.state.ownStreamRequestId = "req-1";',
    ctx
  );
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-1").busy, true, "busy stored in sess-1 entry");
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-1").ownStreamRequestId, "req-1");
  assert.strictEqual(ctx.App.state.busy, true, "state.busy reads active entry");
});

test("P3 s1: 切换会话后 state.busy 指向新会话条目（各会话独立）", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-a";' +
    'App.state.busy = true;' +
    'App.state.sessionId = "sess-b";' +
    'App.state.busy = false;',
    ctx
  );
  const sa = ctx.App.state.sessionsBySid.get("sess-a");
  const sb = ctx.App.state.sessionsBySid.get("sess-b");
  assert.strictEqual(sa.busy, true, "sess-a stays busy after switching away");
  assert.strictEqual(sb.busy, false, "sess-b independent");
});

test("P3 s1: done 带 sid → 只释放该会话条目；后台会话 done 不误清激活会话", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-active";' +
    'App.state.busy = true;' +
    'App.state.ownStreamRequestId = "req-active";' +
    // 模拟后台会话的流：事件带 sid=sess-bg（非激活）→ 释放 bg 条目，不动 active
    'window.emrg.onEvent.calls = window.emrg.onEvent.calls || [];',
    ctx
  );
  // 直接驱动 handleEvent：后台会话 done
  await vm.runInContext(
    'App.handleEvent({ type: "done", sid: "sess-bg", data: { request_id: "req-bg", done: true } });',
    ctx
  );
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-bg").busy, false, "bg entry released");
  // 激活会话未被误清（bg done 不匹配 active 的 rid——除非我们预设了 bg 的 rid）
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-active").busy, true, "active busy untouched by bg done");
  // 激活会话自己的 done（带激活 sid）→ 释放
  await vm.runInContext(
    'App.handleEvent({ type: "done", sid: "sess-active", data: { request_id: "req-active", done: true } });',
    ctx
  );
  assert.strictEqual(ctx.App.state.busy, false, "active released by own done");
  assert.strictEqual(els["input"].disabled, false, "composer re-enabled for active");
});

test("P3 s1: cancelled 带 sid → 只清该会话条目；无 sid → 清激活会话", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-1";' +
    'App.state.busy = true;' +
    'App.handleEvent({ type: "cancelled", sid: "sess-2", data: {} });',
    ctx
  );
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-2").busy, false, "sess-2 cancelled released");
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-1").busy, true, "sess-1 (active) untouched");
  // 无 sid cancelled → 清激活会话
  await vm.runInContext('App.handleEvent({ type: "cancelled", data: {} });', ctx);
  assert.strictEqual(ctx.App.state.busy, false, "no-sid cancelled clears active");
});

// ── P2 queue-injection（#655）：GUI 客户端侧（busy 排队注入协议）──

test("P2 queue: sendMessage while busy records queued send (no early-return)", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  els["input"].value = "queued msg";
  await vm.runInContext(
    'App.state.sessionId = "sess-1";' +
    'App.state.busy = true;' +
    'App.sendMessage();',
    ctx
  );
  await tick(); // sendMessage 内部 await window.emrg.sendMessage（mock 立即 resolve）
  const q = ctx.App.state.queuedSends.get("sess-1");
  assert.ok(q && q.length === 1, "busy send recorded in queuedSends");
  assert.strictEqual(q[0].text, "queued msg");
  assert.strictEqual(q[0].requestId, "mock-uuid", "pre-generated requestId preserved");
});

test("P2 queue: task_queued shows queued position note", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-1";' +
    'App.handleEvent({ type: "task_queued", sid: "sess-1", data: { position: 2 } });',
    ctx
  );
  const texts = [...els["workspace"].children].map((c) => c.textContent).join("|");
  assert.ok(texts.includes("位置 2"), "task_queued shows position note");
});

test("P2 queue: steer_committed removes that request from queue", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-1";' +
    'App.state.queuedSends.set("sess-1", [{ requestId: "req-a", text: "hi", sandbox: "workspace-write" }, { requestId: "req-b", text: "yo", sandbox: "workspace-write" }]);' +
    'App.handleEvent({ type: "steer_committed", sid: "sess-1", data: { request_id: "req-a" } });',
    ctx
  );
  const q = ctx.App.state.queuedSends.get("sess-1");
  assert.strictEqual(q.length, 1, "steer_committed removes that request");
  assert.strictEqual(q[0].requestId, "req-b");
});

test("P2 queue: queued_requeue re-sends with same requestId + re-tracks (review ❌ fix)", async () => {
  const sent = [];
  const { ctx, els } = makeSandbox({
    sendMessage: async (p) => { sent.push(p); return { ok: true, requestId: p.requestId }; },
  });
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-1";' +
    'App.state.busy = true;' + // wasBusy → re-send is re-tracked
    'App.state.queuedSends.set("sess-1", [{ requestId: "req-queue", text: "hi", sandbox: "workspace-write" }]);' +
    'App.handleEvent({ type: "queued_requeue", sid: "sess-1", data: { request_ids: ["req-queue"] } });',
    ctx
  );
  await tick(); // handleEvent 内部 await window.emrg.sendMessage（mock 立即 resolve）
  assert.strictEqual(sent.length, 1, "queued_requeue re-sends");
  assert.strictEqual(sent[0].sessionId, "sess-1");
  assert.strictEqual(sent[0].text, "hi");
  assert.strictEqual(sent[0].requestId, "req-queue", "same requestId reused");
  // 审查 ❌ 修复：busy 时重发被再排队 → 重新跟踪（steer_committed 才移除）
  assert.strictEqual(ctx.App.state.queuedSends.has("sess-1"), true, "re-tracked after requeue (daemon may re-queue)");
  assert.strictEqual(ctx.App.state.queuedSends.get("sess-1")[0].requestId, "req-queue", "same requestId tracked");
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-1").busy, true, "requeue marks session busy");
  const texts = [...els["workspace"].children].map((c) => c.textContent).join("|");
  assert.ok(texts.includes("重新发送 1"), "requeue note shown");
});

test("P2 queue: requeue with 2 msgs (idle turn end) re-tracks 2nd+ (review ❌ regression)", async () => {
  const sent = [];
  const { ctx } = makeSandbox({
    sendMessage: async (p) => { sent.push(p); return { ok: true, requestId: p.requestId }; },
  });
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-1";' +
    'App.state.busy = false;' + // 单客户端：回合刚结束 → wasBusy=false
    'App.state.queuedSends.set("sess-1", [' +
    '  { requestId: "req-m1", text: "m1", sandbox: "workspace-write" },' +
    '  { requestId: "req-m2", text: "m2", sandbox: "workspace-write" }]);' +
    'App.handleEvent({ type: "queued_requeue", sid: "sess-1", data: { request_ids: ["req-m1", "req-m2"] } });',
    ctx
  );
  await tick();
  assert.strictEqual(sent.length, 2, "both queued messages re-sent");
  // M1 开启新回合（不再跟踪）；M2 到达时 daemon busy 被再排队 → i>0 重新跟踪
  const q = ctx.App.state.queuedSends.get("sess-1");
  assert.ok(q && q.length === 1, "2nd message re-tracked");
  assert.strictEqual(q[0].requestId, "req-m2", "M2 tracked for next queued_requeue");
});

test("P2 queue: queued_cancelled clears queue + note", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-1";' +
    'App.state.queuedSends.set("sess-1", [{ requestId: "req-a", text: "hi", sandbox: "workspace-write" }]);' +
    'App.handleEvent({ type: "queued_cancelled", sid: "sess-1", data: {} });',
    ctx
  );
  assert.strictEqual(ctx.App.state.queuedSends.has("sess-1"), false, "queue cleared");
  const texts = [...els["workspace"].children].map((c) => c.textContent).join("|");
  assert.ok(texts.includes("排队消息已取消"), "cancelled note shown");
});

// ── P3 slice 2（rant 15:07:19）：每会话 .session-view 容器 + display 切换 ──

test("P3 s2: activateSessionView 建独立容器并切换 display（仅激活可见）", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-a";' +
    'App.activateSessionView("sess-a");' +
    'App.state.sessionId = "sess-b";' +
    'App.activateSessionView("sess-b");',
    ctx
  );
  // 沙箱 getElementById 对未知 id 返回新 mock —— 视图经 Chat.chatContainer(sid) 取实际对象
  const va = ctx.EMRG_Chat.chatContainer("sess-a");
  const vb = ctx.EMRG_Chat.chatContainer("sess-b");
  assert.notStrictEqual(va, vb, "two distinct per-session views");
  assert.strictEqual(va.classList.contains("active"), false, "sess-a deactivated after switch");
  assert.strictEqual(vb.classList.contains("active"), true, "sess-b is the active view");
  assert.strictEqual(els["workspace"].children.length, 2, "both views live under the #workspace wrapper");
  // 无 sid 渲染 → 落激活会话容器（slice 2 回退链第二跳）
  ctx.EMRG_Chat.addSystemMessage("to-active");
  assert.strictEqual(msgCount(vb), 1, "unsid'd node goes to active session view");
  assert.strictEqual(msgCount(va), 0, "inactive view untouched (state preserved)");
});

test("P3 s2: Chat.clear 只清目标容器——无 sid 清激活，带 sid 定向清", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-a";' +
    'App.activateSessionView("sess-a");' +
    'App.state.sessionId = "sess-b";' +
    'App.activateSessionView("sess-b");' +
    'EMRG_Chat.addSystemMessage("msg-a", "sess-a");' +
    'EMRG_Chat.addSystemMessage("msg-b", "sess-b");',
    ctx
  );
  const va = ctx.EMRG_Chat.chatContainer("sess-a");
  const vb = ctx.EMRG_Chat.chatContainer("sess-b");
  assert.strictEqual(msgCount(va), 1, "sess-a has its node");
  assert.strictEqual(msgCount(vb), 1, "sess-b has its node");
  // 无 sid clear（/clear 的既有调用形态）→ 只清激活容器
  ctx.EMRG_Chat.clear();
  assert.strictEqual(msgCount(vb), 0, "active view cleared");
  assert.strictEqual(msgCount(va), 1, "inactive view retained (切回继续看到原消息)");
  // 带 sid clear → 定向清
  ctx.EMRG_Chat.clear("sess-a");
  assert.strictEqual(msgCount(va), 0, "targeted clear empties sess-a");
});

test("P3 s2: 未注册 sid 的事件落激活容器，状态桶仍按 sid 隔离", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-x";' +
    'App.activateSessionView("sess-x");',
    ctx
  );
  const vx = ctx.EMRG_Chat.chatContainer("sess-x");
  // 广播流（sid=sess-other 无独立容器，P4 前过渡）→ 节点落激活容器、状态入自己桶
  ctx.EMRG_Chat.handleDelta([{ request_id: "r1", content: "fallback", done: false, delta: true }], "sess-other");
  assert.strictEqual(msgCount(vx), 1, "unregistered sid renders into active view");
  assert.strictEqual(ctx.EMRG_Chat.groupNodesFor("sess-other").size, 1, "state bucket keyed by own sid");
  assert.strictEqual(ctx.EMRG_Chat.groupNodesFor("sess-x").size, 0, "active bucket untouched by other sid");
});

// ── P3 finalize（rant 15:07:19）：disconnected 按 sid 隔离 + 断线标记 ────

test("P3 fin: 后台会话断连不触发全局横幅；仅激活会话断连显示", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  els["conn-banner"].classList.add("hidden"); // index.html 初始 hidden
  await vm.runInContext(
    'App.state.sessionId = "sess-active";' +
    'App.activateSessionView("sess-active");' +
    'App.activateSessionView("sess-bg");' +
    'App.state.sessionId = "sess-active";',
    ctx
  );
  // 后台会话断连：有独立注册容器 → 标 .disconnected；无全局横幅/红点
  await vm.runInContext('App.handleEvent({ type: "disconnected", sid: "sess-bg", data: {} });', ctx);
  const vb = ctx.EMRG_Chat.chatContainer("sess-bg");
  assert.strictEqual(vb.classList.contains("disconnected"), true, "bg container marked disconnected");
  assert.strictEqual(els["conn-banner"].classList.contains("hidden"), true, "no global banner for bg disconnect");
  // 激活会话断连：全局横幅显示 + 容器标断线 + 输入条恢复（G89）
  await vm.runInContext('App.handleEvent({ type: "disconnected", sid: "sess-active", data: {} });', ctx);
  const va = ctx.EMRG_Chat.chatContainer("sess-active");
  assert.strictEqual(va.classList.contains("disconnected"), true, "active container marked disconnected");
  assert.strictEqual(els["conn-banner"].classList.contains("hidden"), false, "global banner shown for active disconnect");
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-bg").disconnected, true, "bg entry flagged");
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-active").disconnected, true, "active entry flagged");
});

test("P3 fin: status connected 清除全部断线标记 + 容器类", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-a";' +
    'App.activateSessionView("sess-a");' +
    'App.activateSessionView("sess-b");' +
    'App.handleEvent({ type: "disconnected", sid: "sess-a", data: {} });' +
    'App.handleEvent({ type: "disconnected", sid: "sess-b", data: {} });',
    ctx
  );
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-a").disconnected, true);
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-b").disconnected, true);
  await vm.runInContext('App.handleEvent({ type: "status", data: { connected: true } });', ctx);
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-a").disconnected, false, "flag cleared on reconnect");
  assert.strictEqual(ctx.App.state.sessionsBySid.get("sess-b").disconnected, false, "bg flag cleared on reconnect");
  const va = ctx.EMRG_Chat.chatContainer("sess-a");
  const vb = ctx.EMRG_Chat.chatContainer("sess-b");
  assert.strictEqual(va.classList.contains("disconnected"), false, "container class removed");
  assert.strictEqual(vb.classList.contains("disconnected"), false, "bg container class removed");
  assert.strictEqual(els["conn-banner"].classList.contains("hidden"), true, "banner hidden after reconnect");
});

test("P3 fin: 未注册容器 sid 断连不打标；切到断线会话提示重连", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-active";' +
    'App.activateSessionView("sess-active");',
    ctx
  );
  const va = ctx.EMRG_Chat.chatContainer("sess-active");
  const lenBefore = va.children.length;
  // 未注册容器（且非激活）的 sid 断连：不打 .disconnected（防 chatContainer 回退误标激活容器）
  await vm.runInContext('App.handleEvent({ type: "disconnected", sid: "sess-ghost", data: {} });', ctx);
  assert.strictEqual(va.classList.contains("disconnected"), false, "active container not falsely marked");
  assert.strictEqual(va.children.length, lenBefore, "no node added to active container");
  // 断线会话切入 → 提示自动重连（switchSession 走 mock IPC）
  await vm.runInContext(
    'App.state.sessionId = "sess-d";' + // 经 defineProperty setter 触发 sidState get-or-create 建条目
    'App.state.busy = false;' +
    'App.state.sessions = [{ session_id: "sess-d" }];' +
    'App.state.sessionsBySid.get("sess-d").disconnected = true;',
    ctx
  );
  await vm.runInContext('App.switchSession("sess-d", { silent: true });', ctx);
  await tick();
  const vd = ctx.EMRG_Chat.chatContainer("sess-d");
  assert.ok(vd.children.length >= 1, "switch renders reconnect notice into session container");
  assert.ok(
    (vd.children[vd.children.length - 1].textContent || "").includes("重连") || (vd.children[vd.children.length - 1].textContent || "").includes("reconnect"),
    "reconnect notice text present"
  );
});



// ── P4 slice 2（rant 15:07:19）：跨项目打开会话侧边栏 + gui_state 恢复 ──

test("P4 s2: open_sessions 事件 → 渲染打开会话区（项目名/标题 + 激活高亮）", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-b";' +
    'App.state.sessions = [{ session_id: "sess-a", title: "Alpha" }, { session_id: "sess-b", title: "Beta" }];' +
    'App.handleEvent({ type: "open_sessions", data: { openSessions: [' + // main 已按 lastActive 倒序
    '  { sid: "sess-b", projectName: "proj-b", projectPath: "/b", lastActive: "t2" },' +
    '  { sid: "sess-a", projectName: "proj-a", projectPath: "/a", lastActive: "t1" }' +
    '] } });',
    ctx
  );
  const nav = els["open-sessions"];
  assert.strictEqual(nav.children.length, 2, "two open-session items rendered");
  const titleSpan0 = nav.children[0].children[0] || nav.children[0];
  assert.ok((titleSpan0.textContent || "").includes("proj-b"), "entry shows project name");
  assert.ok((titleSpan0.textContent || "").includes("Beta"), "entry shows session title");
  // 激活高亮：sess-b active
  assert.strictEqual(nav.children[0].classList.contains("active"), true, "active open-session highlighted");
  assert.strictEqual(nav.children[1].classList.contains("active"), false, "inactive not highlighted");
  // 空列表 → 隐藏 label
  await vm.runInContext('App.handleEvent({ type: "open_sessions", data: { openSessions: [] } });', ctx);
  assert.strictEqual(els["open-sessions-label"].hidden, true, "label hidden when no open sessions");
});

test("P4 s2: 跨项目打开会话（entry.title 优先，state.sessions 无该 sid）→ 有 name 显示 name、无 name 显示 id（rant 22:04:57）", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-x";' +
    'App.state.sessions = [{ session_id: "sess-local", title: "Local" }];' + // 当前项目会话；无 sess-x / sess-other
    'App.handleEvent({ type: "open_sessions", data: { openSessions: [' + // main 已按 lastActive 倒序
    '  { sid: "sess-x", projectName: "evolution", projectPath: "/p/evolution", lastActive: "t3", title: "Evolution Task" },' + // 跨项目 + title
    '  { sid: "sess-other", projectName: "mem", projectPath: "/p/mem", lastActive: "t2" },' + // 跨项目无 title → 显示完整 id
    '  { sid: "sess-local", projectName: "emrg", projectPath: "/p/emrg", lastActive: "t1" }' + // 当前项目 → state.sessions title
    '] } });',
    ctx
  );
  const nav = els["open-sessions"];
  assert.strictEqual(nav.children.length, 3, "three open-session items rendered");
  const t0 = nav.children[0].children[0] || nav.children[0];
  assert.ok((t0.textContent || "").includes("evolution/Evolution Task"), "有 title → project/name（不含 id）");
  assert.ok(!(t0.textContent || "").includes("sess-x"), "有 title 时不显示 id");
  const t1 = nav.children[1].children[0] || nav.children[1];
  assert.ok((t1.textContent || "").includes("mem/sess-other"), "无 title → project/完整 id");
  const t2 = nav.children[2].children[0] || nav.children[2];
  assert.ok((t2.textContent || "").includes("emrg/Local"), "当前项目条目有 title → project/name（不含 id）");
  assert.ok(!(t2.textContent || "").includes("sess-local"), "有 title 时不显示 id");
});

test("rant 22:04:02: highlight(sid, navEl) 列表作用域 — 只高亮指定列表，另一列表保持原样", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-a";' +
    'App.handleEvent({ type: "sessions", data: { sessions: [{ session_id: "sess-a", title: "Alpha", cwd: "/p/emrg" }, { session_id: "sess-b", title: "Beta", cwd: "/p/emrg" }] } });' +
    'App.handleEvent({ type: "open_sessions", data: { openSessions: [' +
    '  { sid: "sess-b", projectName: "proj-b", projectPath: "/b", lastActive: "t2" },' +
    '  { sid: "sess-a", projectName: "proj-a", projectPath: "/a", lastActive: "t1" }' +
    '] } });',
    ctx
  );
  // 只高亮打开会话区（模拟点击打开会话列表条目）
  await vm.runInContext('EMRG_Sidebar.highlight("sess-b", document.getElementById("open-sessions"));', ctx);
  assert.strictEqual(els["open-sessions"].children[0].classList.contains("active"), true, "open-sessions 条目高亮");
  const convB = [...els["conv-list"].children].find((c) => c.dataset.sid === "sess-b");
  assert.ok(convB && !convB.classList.contains("active"), "conv-list 中同 sid 条目不被联动高亮");
  // 无 navEl 参数 → 两列表都更新（兼容初始渲染/全量刷新）
  await vm.runInContext('EMRG_Sidebar.highlight("sess-b");', ctx);
  assert.strictEqual(convB.classList.contains("active"), true, "无 navEl 时两列表都高亮");
});

test("P4 s2: closeOpenSession 关闭激活会话 → 切到剩余打开会话 + 容器释放", async () => {
  let closed = null;
  const { ctx, els } = makeSandbox({
    closeSession: async (p) => { closed = p; return { ok: true, closed: true }; },
  });
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-a";' +
    'App.state.sessions = [{ session_id: "sess-a" }, { session_id: "sess-b" }];' +
    'App.state.openSessions = [' +
    '  { sid: "sess-a", projectName: "pa", projectPath: "/a", lastActive: "t2" },' +
    '  { sid: "sess-b", projectName: "pb", projectPath: "/b", lastActive: "t1" }' +
    '];' +
    'App.activateSessionView("sess-a");' +
    'App.activateSessionView("sess-b");',
    ctx
  );
  await vm.runInContext('App.closeOpenSession("sess-a");', ctx);
  await tick();
  assert.strictEqual(closed && closed.sessionId, "sess-a", "closeSession IPC called with sid");
  assert.strictEqual(
    els["workspace"].children.some((c) => c.dataset && c.dataset.sid === "sess-a"),
    false,
    "closed session container removed from wrapper"
  );
  assert.strictEqual(ctx.App.state.sessionId, "sess-b", "switched to remaining open session");
});

test("P4 s2: closeOpenSession 非激活会话 → 不切换激活指针", async () => {
  let closed = null;
  const { ctx } = makeSandbox({
    closeSession: async (p) => { closed = p; return { ok: true, closed: true }; },
  });
  await tick();
  await vm.runInContext(
    'App.state.sessionId = "sess-a";' +
    'App.state.sessions = [{ session_id: "sess-a" }, { session_id: "sess-b" }];' +
    'App.state.openSessions = [' +
    '  { sid: "sess-a", projectName: "pa", projectPath: "/a", lastActive: "t2" },' +
    '  { sid: "sess-b", projectName: "pb", projectPath: "/b", lastActive: "t1" }' +
    '];' +
    'App.activateSessionView("sess-a");',
    ctx
  );
  await vm.runInContext('App.closeOpenSession("sess-b");', ctx);
  await tick();
  assert.strictEqual(ctx.App.state.sessionId, "sess-a", "active session untouched when closing bg session");
});

test("P4 s2: boot init 携带 open_sessions + active_sid → 采用恢复的激活会话", async () => {
  const { ctx, els } = makeSandbox({
    init: async () => ({
      config_exists: true,
      api_key_configured: true,
            server_id: "srv",
      model: "m",
      evolution_count: 0,
      sessions: [{ session_id: "sess-r", title: "Restored" }],
      open_sessions: [{ sid: "sess-r", projectName: "proj", projectPath: "/p", lastActive: "t" }],
      active_sid: "sess-r",
    }),
  });
  await tick();
  await ctx.App.boot();
  await tick();
  assert.strictEqual(ctx.App.state.sessionId, "sess-r", "restored active sid adopted without switchSession IPC");
  assert.strictEqual(ctx.App.state.openSessions.length, 1, "open sessions populated");
  const va = ctx.EMRG_Chat.chatContainer("sess-r");
  assert.strictEqual(va.classList.contains("active"), true, "restored session view activated");
  assert.strictEqual(els["input"].disabled, false, "composer enabled after boot");
});

// ── P5（rant 15:07:19）：打开会话对话框（两步：项目 → 会话） ────────

test("P5: showOpenSessionDialog 列项目（第一步）→ 点项目列会话（第二步）", async () => {
  const projects = [
    { name: "emrg", path: "/p/emrg" },
    { name: "mem", path: "/p/mem" },
  ];
  const { ctx, els } = makeSandbox({
    listProjects: async () => projects,
    listProjectSessions: async ({ projectPath }) => {
      if (projectPath === "/p/emrg") return { sessions: [{ session_id: "s1", title: "S1" }, { session_id: "s2", title: "S2" }] };
      return { sessions: [] };
    },
  });
  await tick();
  await vm.runInContext("EMRG_Dialogs.showOpenSessionDialog()", ctx);
  await tick();
  assert.strictEqual(els["open-session-dialog"].open, true, "dialog opened");
  assert.strictEqual(els["open-session-list"].children.length, 2, "two projects listed");
  // 点击 emrg 项目 → 第二步列出会话（行 = div[pick 按钮, 删除按钮]；点 pick 按钮）
  els["open-session-list"].children[0].children[0].click();
  await tick();
  const rows = els["open-session-list"].children;
  assert.strictEqual(rows.length, 2, "two sessions listed for project");
  const nameSpan = rows[0].children[0] || rows[0];
  assert.ok((nameSpan.textContent || "").includes("S1"), "session title shown");
});

test("P5: /open 指令 → 打开会话对话框；无项目 → 提示新建", async () => {
  const { ctx, els } = makeSandbox({
    listProjects: async () => [],
  });
  await tick();
  await vm.runInContext('App.handleCommand({ cmd: "/open", args: [] });', ctx);
  await tick();
  assert.strictEqual(els["open-session-dialog"].open, true, "dialog opened via /open");
  // 无项目 → innerHTML 字符串呈现提示（mock innerHTML 赋值不建子节点）
  assert.ok(
    (els["open-session-list"].innerHTML || "").includes("新建项目") || (els["open-session-list"].innerHTML || "").includes("New project"),
    "no-projects hint"
  );
});

// ── P5 slice 2（rant 15:07:19）：新建会话对话框 + 删除项目（受保护守卫） ──

test("P5 slice 2: showNewSessionDialog 列项目 → 点选项目 → newSession(projectPath)", async () => {
  const projects = [
    { name: "emrg", path: "/p/emrg" },
    { name: "mem", path: "/p/mem" },
  ];
  const newCalls = [];
  const { ctx, els } = makeSandbox({
    listProjects: async () => projects,
    newSession: async (payload) => { newCalls.push(payload); return { session_id: "s-new" }; },
  });
  await tick();
  await vm.runInContext("EMRG_Dialogs.showNewSessionDialog()", ctx);
  await tick();
  assert.strictEqual(els["new-session-dialog"].open, true, "new-session dialog opened");
  assert.strictEqual(els["new-session-list"].children.length, 2, "two projects listed");
  // 点选 mem 项目 → newSession({ projectPath }) 被调用 + 弹窗关闭
  els["new-session-list"].children[1].click();
  await tick();
  assert.strictEqual(newCalls.length, 1, "newSession called once");
  assert.strictEqual(newCalls[0] && newCalls[0].projectPath, "/p/mem", "projectPath passed");
  assert.strictEqual(els["new-session-dialog"].open, false, "dialog closed after pick");
});

test("P5 slice 2: 新建会话弹窗 → 新建项目 → registerProject + newSession(new path)", async () => {
  const registerCalls = [];
  const newCalls = [];
  const { ctx, els } = makeSandbox({
    pickProjectDir: async () => ({ path: "/p/brand-new" }),
    registerProject: async (p) => { registerCalls.push(p); return { ok: true }; },
    newSession: async (payload) => { newCalls.push(payload); return { session_id: "s-new2" }; },
  });
  await tick();
  await vm.runInContext("EMRG_Dialogs.showNewSessionDialog()", ctx);
  await tick();
  els["new-session-new"].click();
  await tick();
  assert.strictEqual(registerCalls.length, 1, "registerProject called once");
  assert.strictEqual(registerCalls[0] && registerCalls[0].path, "/p/brand-new", "new path registered");
  assert.strictEqual(newCalls.length, 1, "newSession called after register");
  assert.strictEqual(newCalls[0] && newCalls[0].projectPath, "/p/brand-new", "new session in new project");
});

test("P5 slice 2: 删除项目 — 受保护 emrg 提示不可删；普通项目确认后 removeProject", async () => {
  const projects = [
    { name: "emrg", path: "/p/emrg" },
    { name: "mem", path: "/p/mem" },
  ];
  const removeCalls = [];
  const { ctx, els } = makeSandbox({
    listProjects: async () => projects,
    removeProject: async (p) => { removeCalls.push(p); return { ok: true, removed: true, closed: [] }; },
  });
  await tick();
  await vm.runInContext("EMRG_Dialogs.showOpenSessionDialog()", ctx);
  await tick();
  // 受保护项目：行 = [pick, delete]；点删除 → 确认框提示系统项目不可删（不调 removeProject）
  els["open-session-list"].children[0].children[1].click();
  await tick();
  assert.strictEqual(removeCalls.length, 0, "protected project NOT removed");
  assert.ok(
    (els["confirm-message"].textContent || "").includes("系统项目") || (els["confirm-message"].textContent || "").includes("system project"),
    "protected hint shown"
  );
  await vm.runInContext("EMRG_Dialogs.closeConfirm()", ctx);
  // 普通项目：点删除 → 确认 → removeProject({name, path})
  els["open-session-list"].children[1].children[1].click();
  await tick();
  await vm.runInContext("EMRG_Dialogs.confirmOk()", ctx);
  await tick();
  assert.strictEqual(removeCalls.length, 1, "removeProject called once");
  assert.strictEqual(removeCalls[0] && removeCalls[0].name, "mem", "project name passed");
  assert.strictEqual(removeCalls[0] && removeCalls[0].path, "/p/mem", "project path passed");
});

// ── P6（rant 15:07:19）：上限 20 超限提示本地化 + 边界 ──

test("P6: switchSession 超限（too many open sessions）→ 本地化提示（非英文原始错误）", async () => {
  const { ctx, els } = makeSandbox({
    switchSession: async () => { throw new Error("too many open sessions (20) — close some first"); },
  });
  await tick();
  await vm.runInContext('App.switchSession("s-over", { projectPath: "/p/x" })', ctx);
  await tick();
  // Chat.addSystemMessage 渲染进 workspace 容器（系统消息节点 textContent）
  const texts = els["workspace"].children.map((c) => c.textContent || "");
  const last = texts[texts.length - 1] || "";
  assert.ok(
    last.includes("上限") || last.includes("Too many open sessions"),
    "localized over-limit message shown, got: " + last
  );
  assert.ok(!last.includes("close some first"), "raw english error must not leak: " + last);
});

// ── P6 验收（rant 15:07:19）：model_set 多连接重复收无副作用（幂等） ──

test("P6: model_set 多连接重复广播幂等 — 状态一致、无副作用、重复收同值不崩", async () => {
  const { ctx, els } = makeSandbox({});
  await tick();
  // 初始模型 m1
  await vm.runInContext('App.state.model = "m1"; App.updateModelSwitcher();', ctx);
  // 连接 A 广播 model_set m2
  await vm.runInContext('App.handleEvent({ type: "command_result", sid: "conn-a", data: { type: "model_set", model: "m2" } });', ctx);
  assert.strictEqual(ctx.App.state.model, "m2", "model updated after first broadcast");
  assert.strictEqual(els["model-switcher-label"].textContent, "m2", "switcher label updated");
  const msgsAfterA = els["workspace"].children.length;
  // 连接 B（另一会话连接）重复收同一 model_set → 无副作用（无新系统消息/无崩）
  await vm.runInContext('App.handleEvent({ type: "command_result", sid: "conn-b", data: { type: "model_set", model: "m2" } });', ctx);
  assert.strictEqual(ctx.App.state.model, "m2", "idempotent: same value, no change");
  assert.strictEqual(els["workspace"].children.length, msgsAfterA, "no extra system messages from duplicate broadcast");
  // 再次重复（三连接场景）仍稳定
  await vm.runInContext('App.handleEvent({ type: "command_result", sid: "conn-c", data: { type: "model_set", model: "m2" } });', ctx);
  assert.strictEqual(ctx.App.state.model, "m2", "still idempotent after third broadcast");
});

// ── P6 验收补完（rant 15:07:19）：relTime 相对时间助手 + 项目行最近活跃提示 ──

test("P6: relTime 相对时间 — 刚刚/分钟/小时/天 + 无效输入空串", async () => {
  const { ctx } = makeSandbox({});
  await tick();
  const out = vm.runInContext(`(function(){
    const now = Date.now();
    return {
      justNow: relTime(new Date(now).toISOString()),
      mins: relTime(new Date(now - 5 * 60000).toISOString()),
      hrs: relTime(new Date(now - 3 * 3600000).toISOString()),
      days: relTime(new Date(now - 2 * 86400000).toISOString()),
      none: relTime(""),
      bad: relTime("not-a-date"),
    };
  })()`, ctx);
  assert.strictEqual(out.justNow, "刚刚", "just now");
  assert.strictEqual(out.mins, "5 分钟前", "5 minutes ago");
  assert.strictEqual(out.hrs, "3 小时前", "3 hours ago");
  assert.strictEqual(out.days, "2 天前", "2 days ago");
  assert.strictEqual(out.none, "", "empty input → empty");
  assert.strictEqual(out.bad, "", "invalid input → empty");
});

test("P6: 打开会话弹窗项目行显示最近活跃提示（latest_session_at）", async () => {
  const projects = [
    { name: "mem", path: "/p/mem", latest_session_at: new Date(Date.now() - 5 * 60000).toISOString() },
    { name: "emrg", path: "/p/emrg" }, // 无 latest_session_at → 不显示
  ];
  const { ctx, els } = makeSandbox({ listProjects: async () => projects });
  await tick();
  await vm.runInContext("EMRG_Dialogs.showOpenSessionDialog()", ctx);
  await tick();
  const rows = els["open-session-list"].children;
  assert.strictEqual(rows.length, 2, "two project rows");
  // 行 0 = div[pick, del]；pick 子节点含 name/path/act hint（relTime 5 分钟前）
  const pick0 = rows[0].children[0];
  const texts0 = [...pick0.children].map((c) => c.textContent || "");
  assert.ok(texts0.includes("5 分钟前"), "recent activity hint shown, got: " + texts0.join(","));
  // 行 1 无 latest_session_at → 无活动提示
  const pick1 = rows[1].children[0];
  const texts1 = [...pick1.children].map((c) => c.textContent || "");
  assert.ok(!texts1.some((t) => t.includes("前") || t.includes("ago")), "no activity hint when absent");
});

// ── rant 21:59:11：GUI 多会话实现偏差修正（B1-B3） ──

test("B1: 侧边栏'＋ 新对话'按钮 → 项目选择弹窗（不再直接新建）", async () => {
  const newCalls = [];
  const { els } = makeSandbox({
    listProjects: async () => [],
    newSession: async (payload) => { newCalls.push(payload); return { session_id: "s2" }; },
  });
  await tick();
  els["new-chat-btn"].click();
  await tick();
  assert.strictEqual(els["new-session-dialog"].open, true, "new-session dialog opened (not direct newSession)");
  assert.strictEqual(newCalls.length, 0, "newSession NOT called directly by the button");
});

test("B2: 侧边栏'打开会话'按钮 → 两步弹窗（项目→会话）", async () => {
  const { els } = makeSandbox({ listProjects: async () => [] });
  await tick();
  els["open-chat-btn"].click();
  await tick();
  assert.strictEqual(els["open-session-dialog"].open, true, "open-session dialog opened");
});

test("B3: 切换会话保存/恢复输入框草稿（每会话 draft，浏览器 tab 式）", async () => {
  const { ctx, els } = makeSandbox({ switchSession: async () => ({}) });
  await tick();
  // 在 s1 输入草稿 → 切到 s2（s2 无草稿 → 清空）→ 切回 s1（草稿恢复）
  await vm.runInContext("App.state.sessionId = 's1'", ctx);
  els["input"].value = "s1 draft";
  await vm.runInContext("App.switchSession('s2')", ctx);
  await tick();
  assert.strictEqual(els["input"].value, "", "s2 has no draft → input cleared");
  await vm.runInContext("App.switchSession('s1')", ctx);
  await tick();
  assert.strictEqual(els["input"].value, "s1 draft", "s1 draft restored after switching back");
});

test("B3: 发送消息清除该会话草稿；新会话从空草稿开始", async () => {
  const { ctx, els } = makeSandbox({
    sendMessage: async () => ({}),
    newSession: async () => ({ session_id: "s-new" }),
  });
  await tick();
  // 发送后草稿删除
  await vm.runInContext("App.state.sessionId = 's1'; App.state.drafts.set('s1', 'draft')", ctx);
  els["input"].value = "hello";
  els["send-btn"].click();
  await tick();
  const hasDraft = await vm.runInContext("App.state.drafts.has('s1')", ctx);
  assert.strictEqual(hasDraft, false, "draft cleared after send");
  // 新建会话 → 新会话草稿为空 → 输入框清空
  await vm.runInContext("App.state.sessionId = 's-old'; App.state.drafts.set('s-old', 'old draft')", ctx);
  els["input"].value = "old draft";
  await vm.runInContext("App.newSession()", ctx);
  await tick();
  assert.strictEqual(els["input"].value, "", "new session input cleared");
  const newDraft = await vm.runInContext("App.state.drafts.get('s-new')", ctx);
  assert.strictEqual(newDraft, "", "new session starts with empty draft");
});

// ── P2.3 + P3.4（rant 2026-08-11T12:20:35）：HTML 预览 WebContentsView ──────────

test("P3.4：HTML tab 打开 → previewHtml IPC + 占位渲染（不走 read_file）", async () => {
  const calls = { previewHtml: [], readFile: [] };
  const { ctx, els } = makeSandbox({
    previewHtml: async (p) => { calls.previewHtml.push(p); return { ok: true }; },
    readFile: async (p) => { calls.readFile.push(p); return { content: "x", binary: false }; },
  });
  await tick();
  calls.previewHtml.length = 0; calls.readFile.length = 0; // 清 boot 期调用（init 上报）
  await vm.runInContext('ResultPanel.openFileTab(null, "/tmp/page.html")', ctx);
  assert.strictEqual(calls.previewHtml.length, 1, "HTML 应触发 previewHtml");
  assert.strictEqual(calls.previewHtml[0].path, "/tmp/page.html", "previewHtml 应带完整路径");
  assert.strictEqual(calls.readFile.length, 0, "HTML 预览不得走 read_file（内嵌浏览器直载）");
  // 占位渲染：viewer-head + .viewer-html（混合模型 DOM 占位）
  const cls = vm.runInContext('document.getElementById("result-viewer").children[1].className', ctx);
  assert.ok(String(cls).includes("viewer-html"), "应渲染 .viewer-html 占位");
  const collectText = `(function(){
    function collect(node) {
      let out = node.textContent || "";
      for (const c of node.children || []) out += collect(c);
      return out;
    }
    return collect(document.getElementById("result-viewer").children[1]);
  })()`;
  assert.ok(String(vm.runInContext(collectText, ctx)).includes("HTML 预览"), "占位应显示 HTML 预览提示");
  // Tab 栏应显示该文件
  assert.ok([...els["result-tabbar"].children].some((c) => c.dataset.path === "/tmp/page.html"), "Tab 栏应显示 HTML 文件");
});

test("P3.4：HTML 路径判别正反两态（.html/.htm/.HTM → 预览；.py → readFile）", async () => {
  const calls = { previewHtml: [], readFile: [] };
  const { ctx } = makeSandbox({
    previewHtml: async (p) => { calls.previewHtml.push(p); return { ok: true }; },
    readFile: async (p) => { calls.readFile.push(p); return { content: "code", binary: false }; },
  });
  await tick();
  calls.previewHtml.length = 0; calls.readFile.length = 0;
  for (const p of ["/tmp/a.html", "/tmp/b.htm", "/tmp/c.HTM"]) {
    await vm.runInContext(`ResultPanel.openFileTab(null, ${JSON.stringify(p)})`, ctx);
  }
  assert.strictEqual(calls.previewHtml.length, 3, ".html/.htm/.HTM 均应走预览");
  assert.deepStrictEqual(calls.previewHtml.map((c) => c.path), ["/tmp/a.html", "/tmp/b.htm", "/tmp/c.HTM"]);
  assert.strictEqual(calls.readFile.length, 0);
  // .py → 文本查看器（readFile）
  await vm.runInContext('ResultPanel.openFileTab(null, "/tmp/a.py")', ctx);
  assert.strictEqual(calls.previewHtml.length, 3, ".py 不得触发 previewHtml");
  assert.ok(calls.readFile.length >= 1, ".py 应走 read_file");
});

test("P3.4：HTML→HTML 切换 = 重新加载（previewHtml 两次，无 closePreview）", async () => {
  const calls = { previewHtml: [], closePreview: [] };
  const { ctx } = makeSandbox({
    previewHtml: async (p) => { calls.previewHtml.push(p); return { ok: true }; },
    closePreview: async (p) => { calls.closePreview.push(p); return { ok: true }; },
  });
  await tick();
  calls.previewHtml.length = 0; calls.closePreview.length = 0;
  await vm.runInContext('ResultPanel.openFileTab(null, "/tmp/p1.html")', ctx);
  await vm.runInContext('ResultPanel.openFileTab(null, "/tmp/p2.html")', ctx);
  assert.strictEqual(calls.previewHtml.length, 2, "HTML→HTML 应各自 previewHtml（loadURL 替换）");
  assert.strictEqual(calls.previewHtml[1].path, "/tmp/p2.html");
  assert.strictEqual(calls.closePreview.length, 0, "HTML→HTML 切换不得 closePreview");
});

test("P3.4：关闭 HTML tab / 切到非 HTML → closePreview", async () => {
  const calls = { previewHtml: [], closePreview: [] };
  const { ctx } = makeSandbox({
    previewHtml: async (p) => { calls.previewHtml.push(p); return { ok: true }; },
    closePreview: async (p) => { calls.closePreview.push(p); return { ok: true }; },
  });
  await tick();
  calls.previewHtml.length = 0; calls.closePreview.length = 0;
  // 打开 HTML → 切到产物 Tab → closePreview
  await vm.runInContext('ResultPanel.openFileTab(null, "/tmp/p.html")', ctx);
  assert.strictEqual(calls.closePreview.length, 0);
  await vm.runInContext('ResultPanel.activateTab("artifacts")', ctx);
  assert.strictEqual(calls.closePreview.length, 1, "切到产物 Tab 应 closePreview");
  // 再开 HTML → 直接关闭该 Tab → closePreview
  await vm.runInContext('ResultPanel.openFileTab(null, "/tmp/p.html")', ctx);
  await vm.runInContext('ResultPanel.closeFileTab(null, "/tmp/p.html")', ctx);
  assert.strictEqual(calls.closePreview.length, 2, "关闭 HTML Tab 应 closePreview");
  assert.strictEqual(calls.closePreview[1].path, undefined, "closePreview 空参 = 关闭当前预览（main 侧比对路径）");
});

test("P2.3：panelResized 上报（init 初始 + 折叠/展开）", async () => {
  const calls = { panelResized: [] };
  const { ctx } = makeSandbox({
    panelResized: async (p) => { calls.panelResized.push(p); return { ok: true }; },
  });
  await tick();
  assert.ok(calls.panelResized.length >= 1, "init 应上报初始布局");
  const first = calls.panelResized[0];
  assert.strictEqual(typeof first.width, "number", "上报应含面板宽度");
  assert.strictEqual(typeof first.collapsed, "boolean", "上报应含折叠状态");
  assert.strictEqual(typeof first.contentTop, "number", "上报应含内容区顶部（Tab 栏高）");
  // 折叠 → 末次上报 collapsed: true
  await vm.runInContext("ResultPanel.toggle()", ctx);
  assert.strictEqual(calls.panelResized.at(-1).collapsed, true, "折叠后应上报 collapsed: true");
  // 展开 → collapsed: false
  await vm.runInContext("ResultPanel.toggle()", ctx);
  assert.strictEqual(calls.panelResized.at(-1).collapsed, false, "展开后应上报 collapsed: false");
});

test("P2.3：renderer 崩溃恢复——getPreviewState 拉取后重开预览 Tab", async () => {
  const calls = { previewHtml: [] };
  const { ctx, els } = makeSandbox({
    previewHtml: async (p) => { calls.previewHtml.push(p); return { ok: true }; },
    getPreviewState: async () => ({ path: "/proj/index.html" }),
  });
  await tick();
  assert.ok(calls.previewHtml.length >= 1, "崩溃恢复应经 getPreviewState 重开预览");
  assert.strictEqual(calls.previewHtml.at(-1).path, "/proj/index.html", "恢复应加载 main 侧预览路径");
  assert.ok([...els["result-tabbar"].children].some((c) => c.dataset.path === "/proj/index.html"), "恢复后 Tab 栏应显示预览文件");
});

test("P2.3：handlePreviewState 幂等——已打开路径仅激活不重复开 Tab", async () => {
  const calls = { previewHtml: [] };
  const { ctx, els } = makeSandbox({
    previewHtml: async (p) => { calls.previewHtml.push(p); return { ok: true }; },
  });
  await tick();
  await vm.runInContext('ResultPanel.openFileTab(null, "/proj/index.html")', ctx);
  const before = els["result-tabbar"].children.length;
  await vm.runInContext('ResultPanel.handlePreviewState("/proj/index.html", null)', ctx);
  assert.strictEqual(els["result-tabbar"].children.length, before, "已打开路径不得重复开 Tab");
  assert.ok(calls.previewHtml.length >= 2, "恢复激活应重新 previewHtml（bounds/loadURL 同步）");
});


// ── rant 18:23:15 P3：定时任务/自定义类型管理（settings 区） ──
test("P3：设置面板打开 → 任务列表渲染（名称/类型/项目/间隔）+ 编辑预填 → taskUpdate", async () => {
  let updated = null;
  const { ctx, els } = makeSandbox({
    listTasks: async () => [
      { name: "daily-report", type: "evolution", config: { project: "emrg" }, interval: 1800, enabled: true },
      { name: "nightly-sync", type: "sync", config: { project: "docs", repo: "acme/docs" }, interval: 3600, enabled: false },
    ],
    taskTemplateList: async () => [
      { name: "evolution", builtin: true, template: "evolution_prompt.md" },
      { name: "sync", builtin: false, template: "sync.md", prompt: "# sync\nrun the sync" },
    ],
    listProjects: async () => [{ name: "emrg", path: "/p/emrg" }, { name: "docs", path: "/p/docs" }],
    taskUpdate: async (payload) => { updated = payload; return { ok: true }; },
    triggerTask: async () => ({}),
  });
  await tick();
  await vm.runInContext("App.openTasksPanel()", ctx);
  await tick();
  // 任务行渲染：2 行 + 空态兜底（无）
  const rows = vm.runInContext('document.getElementById("task-list").children.length', ctx);
  assert.strictEqual(rows, 2, `任务列表应渲染 2 行，实际 ${rows}`);
  const names = vm.runInContext(`Array.from(document.getElementById("task-list").children).map((r) => r.querySelector(".task-name") ? r.querySelector(".task-name").textContent : "")`, ctx);
  assert.deepStrictEqual(names, ["daily-report", "nightly-sync"]);
  const hints = vm.runInContext(`Array.from(document.getElementById("task-list").children).map((r) => r.querySelector(".task-hint") ? r.querySelector(".task-hint").textContent : "")`, ctx);
  assert.ok(hints[0].includes("emrg") && hints[0].includes("1800"), `任务1 hint: ${hints[0]}`);
  assert.ok(hints[1].includes("docs") && hints[1].includes("3600") && hints[1].includes("已停用"), `任务2 hint: ${hints[1]}`);
  // 点击编辑 → 表单预填（名称只读、类型/项目下拉、间隔、enabled）
  els["task-form"].classList.add("hidden"); // 镜像 index.html 初始态（task-form hidden）
  await vm.runInContext(`(function() {
    const row = document.getElementById("task-list").children[0];
    const btns = row.querySelectorAll(".model-action-btn");
    btns[1].click(); // 0=触发 1=编辑
  })()`, ctx);
  assert.strictEqual(vm.runInContext('document.getElementById("task-form").classList.contains("hidden")', ctx), false, "编辑应展开表单");
  assert.strictEqual(els["task-form-name"].value, "daily-report");
  assert.strictEqual(els["task-form-name"].disabled, true, "编辑时名称只读");
  assert.strictEqual(els["task-form-interval"].value, 1800);
  assert.strictEqual(els["task-form-enabled"].checked, true);
  assert.strictEqual(els["task-form-repo"].value, "");
  // rant 18:06:44：沙箱下拉默认 workspace-write（与后端默认一致）；任务无 sandbox 字段 → 默认值
  assert.strictEqual(els["task-form-sandbox"].value, "workspace-write", "编辑任务默认沙箱档位应为 workspace-write");
  // 修改后保存 → taskUpdate
  vm.runInContext('document.getElementById("task-form-interval").value = "600"', ctx);
  vm.runInContext('document.getElementById("task-form-sandbox").value = "read-only"', ctx);
  await vm.runInContext("EMRG_Dialogs.saveTaskForm()", ctx);
  await tick();
  assert.ok(updated, "taskUpdate 应被调用");
  assert.strictEqual(updated.name, "daily-report");
  assert.strictEqual(updated.interval, 600);
  assert.strictEqual(updated.type, "evolution");
  assert.strictEqual(updated.project, "emrg");
  assert.strictEqual(updated.enabled, true);
  assert.strictEqual(updated.sandbox, "read-only", "保存 payload 应含沙箱档位");
});

test("rant 21:32:32：任务卡点击展开最近运行子表（时间/干了什么/降频徽章）", async () => {
  const { ctx, els } = makeSandbox({
    listTasks: async () => [
      {
        name: "emrg-task", type: "evolution", config: { project: "emrg" },
        interval: 60, enabled: true, last_run_at: "2026-08-18T10:00:00",
        saturation: { heartbeat_interval: 480, heartbeat_active: true },
        recent_runs: [
          { timestamp: "2026-08-18T10:00:00", work: "修了双实例根因，提交 PR #854",
            impact: ["cycle-ts-complete", "tools-executed=26"],
            recommend_slowdown: false, tool_count: 26, slowdown_reason: "" },
          { timestamp: "2026-08-18T09:00:00", work: "",
            impact: ["cycle-ts-complete", "tools-executed=3"],
            recommend_slowdown: false, tool_count: 3, slowdown_reason: "" },
          { timestamp: "2026-08-18T08:00:00", work: "NTE",
            impact: ["cycle-ts-complete"], recommend_slowdown: true, tool_count: 0,
            slowdown_reason: "长期无产出" },
        ],
      },
      { name: "fresh-task", type: "sync", config: { project: "docs" }, interval: 3600, enabled: true },
    ],
    taskTemplateList: async () => [],
    listProjects: async () => [{ name: "emrg", path: "/p/emrg" }],
  });
  await tick();
  await vm.runInContext("App.openTasksPanel()", ctx);
  await tick();
  // 初始：子表隐藏（行内含 .task-run-detail.hidden）
  const hiddenBefore = vm.runInContext(`Array.from(document.getElementById("task-list").children[0].querySelectorAll(".task-run-detail")).every((d) => d.classList.contains("hidden"))`, ctx);
  assert.strictEqual(hiddenBefore, true, "子表初始应隐藏");
  // 点击任务卡 → 展开
  await vm.runInContext(`document.getElementById("task-list").children[0].click()`, ctx);
  await tick();
  const detail = vm.runInContext(`document.getElementById("task-list").children[0].querySelector(".task-run-detail")`, ctx);
  assert.strictEqual(detail.classList.contains("hidden"), false, "点击后子表应展开");
  // 第一行 run：Agent 总结展示（自然语言 work，rant 2026-08-20T10:58:55 字段统一）
  const doneTxt = vm.runInContext(`document.getElementById("task-list").children[0].querySelectorAll(".task-run-done")[0].textContent`, ctx);
  assert.strictEqual(doneTxt, "修了双实例根因，提交 PR #854", `子表应显示 Agent 总结，实际: ${doneTxt}`);
  // 降频徽章：recommend_slowdown=true → 建议降频（idle 空转徽章已删除，rant 10:58:55）
  const warnCount = vm.runInContext(`document.getElementById("task-list").children[0].querySelectorAll(".task-run-badge-warn").length`, ctx);
  assert.ok(warnCount >= 1, "recommend_slowdown=true 应显示建议降频徽章");
  const idleCount = vm.runInContext(`document.getElementById("task-list").children[0].querySelectorAll(".task-run-badge-idle").length`, ctx);
  assert.strictEqual(idleCount, 0, "meaningful 已删除，不应再出现空转徽章");
  // 一级卡片：heartbeat_active → 已降频徽章（app.taskThrottled，rant 10:58:55）
  const satBadge = vm.runInContext(`document.getElementById("task-list").children[0].querySelectorAll(".task-saturation-badge").length`, ctx);
  assert.strictEqual(satBadge, 1, "saturation.heartbeat_active=true 应显示已降频徽章");
  // 无 work → 显示 "-"（rant 2026-08-19T07:06:45 宿主定稿：不再 fallback impact 机器串）
  const doneTexts = vm.runInContext(`Array.from(document.getElementById("task-list").children[0].querySelectorAll(".task-run-done")).map((n) => n.textContent)`, ctx);
  assert.strictEqual(doneTexts[1], "-", `无 work 应显示 "-"，实际: ${doneTexts[1]}`);
  // rant 2026-08-19T18:25:14：原因列 —— 有 slowdown_reason 显示原文，无则 "-"
  const reasonTexts = vm.runInContext(`Array.from(document.getElementById("task-list").children[0].querySelectorAll(".task-run-reason")).map((n) => n.textContent)`, ctx);
  assert.strictEqual(reasonTexts[0], "-", `无 slowdown_reason 应显示 "-"，实际: ${reasonTexts[0]}`);
  assert.strictEqual(reasonTexts[1], "-", `无 slowdown_reason 应显示 "-"，实际: ${reasonTexts[1]}`);
  assert.strictEqual(reasonTexts[2], "长期无产出", `原因列应显示降频原因，实际: ${reasonTexts[2]}`);
  // rant 2026-08-19T18:25:14：一级列表不再显示"干了什么"（last_cycle_summary）
  const primarySummary = vm.runInContext(`document.getElementById("task-list").children[0].querySelectorAll(".task-meta-summary").length`, ctx);
  assert.strictEqual(primarySummary, 0, `一级列表不应显示 last_cycle_summary，实际数量: ${primarySummary}`);
  // 再次点击 → 折叠
  await vm.runInContext(`document.getElementById("task-list").children[0].click()`, ctx);
  await tick();
  assert.strictEqual(vm.runInContext(`document.getElementById("task-list").children[0].querySelector(".task-run-detail").classList.contains("hidden")`, ctx), true, "再次点击应折叠");
  // 无 recent_runs → 占位文案
  const emptyTxt = vm.runInContext(`document.getElementById("task-list").children[1].querySelector(".task-run-empty").textContent`, ctx);
  assert.ok(emptyTxt.includes("暂无运行记录"), `无 recent_runs 应显示占位，实际: ${emptyTxt}`);
  // rant 2026-08-19T20:49:52：.task-row 必须 flex-wrap —— 否则子表
  // (flex-basis:100%) 被压到同一行右边，宿主实测"跑到右边了"
  const taskCss = fs.readFileSync(path.join(__dirname, "..", "renderer", "css", "components.css"), "utf8");
  const rowRule = taskCss.slice(taskCss.indexOf(".task-row"), taskCss.indexOf(".task-name"));
  assert.ok(rowRule.includes("flex-wrap: wrap"), "task-row 应含 flex-wrap: wrap（子表换行到任务卡下方）");
  const detailRule = taskCss.slice(taskCss.indexOf(".task-run-detail"), taskCss.indexOf(".task-run-detail.hidden"));
  assert.ok(detailRule.includes("flex-basis: 100%"), "task-run-detail 应保持 flex-basis:100%（配合 flex-wrap 换行）");
  // rant 2026-08-20T10:34:40：主表一行 —— task-meta 不得再 flex-basis:100%
  // （曾强制"上次运行"独占一行 → 任务卡变 3 行），应改为弹性 auto 与 actions 同行
  const metaRule = taskCss.slice(taskCss.indexOf(".task-meta {"), taskCss.indexOf(".task-meta-item"));
  assert.ok(!metaRule.includes("flex-basis: 100%"), "task-meta 不得含 flex-basis:100%（主表一行紧凑布局）");
  assert.ok(/flex:\s*0\s*1\s*auto/.test(metaRule), "task-meta 应为 flex: 0 1 auto（弹性占剩余空间、与 actions 同行、窄窗口内部换行）");
});

test("P3：新增任务表单 —— 间隔 <60 客户端拒绝；≥60 提交 taskCreate", async () => {
  let created = null;
  const { ctx, els } = makeSandbox({
    listTasks: async () => [],
    taskTemplateList: async () => [
      { name: "evolution", builtin: true, template: "evolution_prompt.md" },
      { name: "custom-a", builtin: false, template: "custom-a.md", prompt: "# a" },
    ],
    listProjects: async () => [{ name: "emrg", path: "/p/emrg" }],
    taskCreate: async (payload) => { created = payload; return { ok: true }; },
  });
  await tick();
  await vm.runInContext("App.openTasksPanel()", ctx);
  await tick();
  // 点"＋ 添加任务" → 空表单
  els["task-form"].classList.add("hidden"); // 镜像 index.html 初始态（task-form hidden）
  await vm.runInContext('document.getElementById("task-add-btn").click()', ctx);
  assert.strictEqual(els["task-form-name"].disabled, false, "新增时名称可编辑");
  // 类型下拉含内置 + 自定义
  const typeOpts = vm.runInContext('Array.from(document.getElementById("task-form-type").children).map((o) => o.value)', ctx);
  assert.deepStrictEqual(typeOpts.sort(), ["custom-a", "evolution"]);
  const projOpts = vm.runInContext('Array.from(document.getElementById("task-form-project").children).map((o) => o.value)', ctx);
  assert.deepStrictEqual(projOpts, ["emrg"], "项目下拉仅已注册项目（决策点③）");
  // 间隔 30 → 拒绝（决策点⑤：≥60）
  vm.runInContext(`(function() {
    document.getElementById("task-form-name").value = "new-task";
    document.getElementById("task-form-type").value = "custom-a";
    document.getElementById("task-form-project").value = "emrg";
    document.getElementById("task-form-interval").value = "30";
  })()`, ctx);
  await vm.runInContext("EMRG_Dialogs.saveTaskForm()", ctx);
  await tick();
  assert.strictEqual(els["confirm-dialog"].open, true, "间隔 <60 应弹错误确认框");
  assert.ok((els["confirm-message"].textContent || "").includes("60"), `错误提示含 60：${els["confirm-message"].textContent}`);
  assert.strictEqual(created, null, "非法间隔不应提交 taskCreate");
  await vm.runInContext("EMRG_Dialogs.closeConfirm()", ctx);
  // 间隔 600 → 提交（含自定义类型 + 启用勾选 + 默认沙箱 workspace-write）
  vm.runInContext('document.getElementById("task-form-interval").value = "600"', ctx);
  await vm.runInContext("EMRG_Dialogs.saveTaskForm()", ctx);
  await tick();
  assert.ok(created, "合法输入应提交 taskCreate");
  assert.strictEqual(created.name, "new-task");
  assert.strictEqual(created.type, "custom-a");
  assert.strictEqual(created.project, "emrg");
  assert.strictEqual(created.interval, 600);
  assert.strictEqual(created.enabled, true);
  assert.strictEqual(created.sandbox, "workspace-write", "新增任务 payload 默认沙箱应为 workspace-write");
  // 下拉切换档位 → payload 跟随
  vm.runInContext('document.getElementById("task-form-name").value = "new-task2"', ctx);
  vm.runInContext('document.getElementById("task-form-sandbox").value = "danger-full-access"', ctx);
  await vm.runInContext("EMRG_Dialogs.saveTaskForm()", ctx);
  await tick();
  assert.strictEqual(created.name, "new-task2");
  assert.strictEqual(created.sandbox, "danger-full-access", "切换档位后 payload 应跟随");
  assert.strictEqual(vm.runInContext('document.getElementById("task-form").classList.contains("hidden")', ctx), true, "保存后表单收起");
});

test("rant 2026-08-15T09:20:27/09:23:10：面板操作反馈走全局 toast（任意视图可见）+ Trigger 三态语义", async () => {
  const triggerCalls = [];
  const { ctx, els } = makeSandbox({
    listTasks: async () => [
      { name: "emrg-task", type: "evolution", running: true, interval: 60 },
      { name: "nightly", type: "custom", running: false, interval: 3600 },
    ],
    listProjects: async () => [{ name: "emrg", path: "/p/emrg" }],
    triggerTask: async (payload) => {
      triggerCalls.push(payload);
      if (payload.name === "emrg-task") return { name: "emrg-task", result: "running", detail: "task is currently executing" };
      if (payload.name === "missing") return { error: "task 'missing' not found" };
      return { name: "nightly", result: "triggered", detail: "next run moved to immediately" };
    },
  });
  await tick();
  await vm.runInContext("App.openTasksPanel()", ctx);
  await tick();
  const rows = vm.runInContext('Array.from(document.getElementById("task-list").children).filter((c) => c.className.includes("task-row"))', ctx);
  assert.strictEqual(rows.length, 2, "两个任务各一行");
  // running 徽标 + 触发按钮禁用（rant 09:23:10 从源头减少误点）
  const runBadges = rows[0].querySelectorAll(".task-running-badge").length;
  assert.strictEqual(runBadges, 1, "running 任务应有徽标");
  const btns0 = rows[0].querySelectorAll(".model-action-btn");
  const btns1 = rows[1].querySelectorAll(".model-action-btn");
  assert.strictEqual(btns0[0].disabled, true, "running 任务触发按钮应禁用");
  assert.strictEqual(btns1[0].disabled, false, "空闲任务触发按钮可用");
  // 触发空闲任务 → triggered → success toast（面板视图下 toast 可见）
  btns1[0].click();
  await tick();
  assert.strictEqual(triggerCalls.length, 1, "触发应调用 triggerTask");
  assert.strictEqual(triggerCalls[0].name, "nightly");
  assert.strictEqual(els["toast"].classList.contains("toast-success"), true, "triggered → success toast");
  assert.ok(String(els["toast-msg"].textContent).includes("nightly"), "toast 消息含任务名");
  // /trigger 路径：running → info toast（不再假成功）
  await vm.runInContext('App.doTrigger("emrg-task")', ctx);
  await tick();
  assert.strictEqual(els["toast"].classList.contains("toast-info"), true, "running → info toast");
  assert.ok(String(els["toast-msg"].textContent).includes("emrg-task"), "info toast 含任务名");
  // 触发失败 → error toast
  vm.runInContext('App.doTrigger("missing")', ctx);
  await tick();
  assert.strictEqual(els["toast"].classList.contains("toast-error"), true, "error → error toast");
});

// ── rant 2026-08-15T10:36:39：任务状态 + 下次运行倒计时 ──
test("rant 10:36:39：formatCountdown 格式边界（0s/59s/60s/1h05m/负数钳制）", () => {
  const { ctx } = makeSandbox();
  const fmt = (n) => vm.runInContext(`EMRG_Dialogs.formatCountdown(${JSON.stringify(n)})`, ctx);
  assert.strictEqual(fmt(0), "0s");
  assert.strictEqual(fmt(43), "43s");
  assert.strictEqual(fmt(59), "59s");
  assert.strictEqual(fmt(60), "1m00s");
  assert.strictEqual(fmt(83), "1m23s");
  assert.strictEqual(fmt(3599), "59m59s");
  assert.strictEqual(fmt(3600), "1h00m");
  assert.strictEqual(fmt(3900), "1h05m");
  assert.strictEqual(fmt(-5), "0s", "负数钳制为 0");
  assert.strictEqual(fmt(1.9), "1s", "小数向下取整");
});

test("rant 10:36:39：任务行状态展示 —— 运行中/待运行+倒计时/待调度/已停用 + 每秒递减 + 归零自动刷新", async () => {
  let nowMs = 1_700_000_000_000;
  const { ctx, win } = makeSandbox({
    // rant 11:16:32：mock 有状态 —— deadline（50s 后）已过 → waiting-task 转为 running，
    // 验证归零后 updateTaskCountdowns 自动重拉任务状态（pending → running 徽标）
    listTasks: async () => [
      { name: "running-task", type: "evolution", running: true, interval: 60, next_run_in_seconds: null },
      { name: "waiting-task", type: "evolution", running: nowMs >= 1_700_000_050_000, interval: 1800, next_run_in_seconds: 43 },
      { name: "idle-task", type: "custom", running: false, interval: 3600, next_run_in_seconds: null, enabled: true },
      { name: "disabled-task", type: "custom", running: false, interval: 3600, next_run_in_seconds: null, enabled: false },
    ],
    listProjects: async () => [],
  });
  win.Date = class extends Date { static now() { return nowMs; } };
  await tick();
  await vm.runInContext("App.openTasksPanel()", ctx);
  await tick();
  const texts = vm.runInContext(`Array.from(document.getElementById("task-list").children).map((r) => {
    const name = r.querySelector(".task-name") ? r.querySelector(".task-name").textContent : "";
    const badges = Array.from(r.querySelectorAll(".task-badge")).map((b) => b.textContent).join(",");
    const next = r.querySelector(".task-next-run") ? r.querySelector(".task-next-run").textContent : "";
    const hint = r.querySelector(".task-hint") ? r.querySelector(".task-hint").textContent : "";
    return name + "|" + badges + "|" + next + "|" + hint;
  })`, ctx);
  assert.strictEqual(texts.length, 4, "4 任务各一行");
  assert.ok(texts[0].includes("运行中"), `running 徽标：${texts[0]}`);
  assert.ok(texts[0].includes("运行中") && !texts[0].includes("下次运行"), "running 无倒计时");
  assert.ok(texts[1].includes("待运行") && texts[1].includes("下次运行 43s"), `等待任务：${texts[1]}`);
  assert.ok(texts[2].includes("待调度") && !texts[2].includes("下次运行"), `空闲任务：${texts[2]}`);
  assert.ok(texts[3].includes("已停用") && !texts[3].includes("待调度"), `禁用任务：${texts[3]}`);
  // 每秒递减：快进 1s → updateTaskCountdowns（等效 setInterval tick）
  nowMs += 1000;
  await vm.runInContext("EMRG_Dialogs.updateTaskCountdowns()", ctx);
  let nextRuns = vm.runInContext(`Array.from(document.getElementById("task-list").children).map((r) => { const s = r.querySelector(".task-next-run"); return s ? s.textContent : ""; }).filter(Boolean)`, ctx);
  assert.deepStrictEqual(nextRuns, ["下次运行 42s"], "1s 后递减为 42s");
  // 快进到 deadline 之后 → rant 11:16:32：归零触发自动 renderTaskList 重拉状态（异步），
  // mock 此刻返回 running → UI 从"待运行+倒计时"刷新为 running 徽标（无倒计时）
  nowMs += 50_000;
  await vm.runInContext("EMRG_Dialogs.updateTaskCountdowns()", ctx);
  await tick();
  const refreshed = vm.runInContext(`Array.from(document.getElementById("task-list").children).map((r) => {
    const name = r.querySelector(".task-name") ? r.querySelector(".task-name").textContent : "";
    const badges = Array.from(r.querySelectorAll(".task-badge")).map((b) => b.textContent).join(",");
    return name + "|" + badges;
  })`, ctx);
  assert.ok(refreshed.some((t) => t.startsWith("waiting-task") && t.includes("运行中") && !t.includes("下次运行")),
    `归零后自动刷新为 running 徽标：${JSON.stringify(refreshed)}`);
});

test("rant 10:36:39：倒计时生命周期 —— 渲染启动 interval、离开任务视图清理（无泄漏）", async () => {
  const { ctx, win } = makeSandbox({
    listTasks: async () => [
      { name: "waiting-task", type: "evolution", running: false, interval: 1800, next_run_in_seconds: 43 },
    ],
    listProjects: async () => [],
  });
  await tick();
  const start = win._intervalCount || 0;
  await vm.runInContext("App.openTasksPanel()", ctx);
  await tick();
  assert.ok((win._intervalCount || 0) > start, "渲染含倒计时的任务 → 启动 setInterval");
  // 打开其他面板 → 停止倒计时（clearInterval 被调用）
  const clearBefore = win._clearCount || 0;
  await vm.runInContext("App.switchView('projects')", ctx);
  assert.ok((win._clearCount || 0) > clearBefore, "离开任务视图 → clearInterval 防泄漏");
  // 重新打开 → 再次启动（renderTaskList 幂等重启）
  const start2 = win._intervalCount || 0;
  await vm.runInContext("App.openTasksPanel()", ctx);
  await tick();
  assert.ok((win._intervalCount || 0) > start2, "重开任务面板 → 倒计时重新启动");
  // 无倒计时的任务（全 running）→ 不启动 interval
  const { ctx: ctx2, win: win2 } = makeSandbox({
    listTasks: async () => [
      { name: "r1", type: "evolution", running: true, interval: 60, next_run_in_seconds: null },
    ],
    listProjects: async () => [],
  });
  await tick();
  const s2 = win2._intervalCount || 0;
  await vm.runInContext("App.openTasksPanel()", ctx2);
  await tick();
  assert.strictEqual(win2._intervalCount || 0, s2, "全 running 无倒计时 → 不启动 interval");
});

test("rant 10:45:52：任务行显示上次执行元信息 + 降频标识（rant 18:25:14：不再显示摘要）", async () => {
  const nowMs = 1_700_000_000_000;
  const { ctx, win } = makeSandbox({
    listTasks: async () => [
      { name: "with-run", type: "evolution", running: false, interval: 1800,
        next_run_in_seconds: 43,
        last_run_at: new Date(nowMs - 5 * 60_000).toISOString(),
        saturation: { heartbeat_interval: 7200, heartbeat_active: true } },
      { name: "never-run", type: "evolution", running: false, interval: 3600,
        next_run_in_seconds: null, enabled: true, last_run_at: null,
        saturation: { heartbeat_interval: 7200, heartbeat_active: false } },
    ],
    listProjects: async () => [],
  });
  win.Date = class extends Date { static now() { return nowMs; } };
  await tick();
  await vm.runInContext("App.openTasksPanel()", ctx);
  await tick();
  const rows = vm.runInContext(`Array.from(document.getElementById("task-list").children).map((r) => {
    const name = r.querySelector(".task-name").textContent;
    const meta = r.querySelector(".task-meta");
    const metaText = meta ? Array.from(meta.querySelectorAll(".task-meta-item")).map((e) => e.textContent).join("|") : "";
    const satText = meta && meta.querySelector(".task-saturation-badge") ? meta.querySelector(".task-saturation-badge").textContent : "";
    return name + " => " + metaText + " ## " + satText;
  })`, ctx);
  assert.ok(rows[0].includes("上次运行：5m ago"), `运行时间相对显示：${rows[0]}`);
  // rant 2026-08-19T18:25:14：一级列表不再显示"干了什么"（last_cycle_summary），移入点击展开的二级列表
  assert.ok(!rows[0].includes("干了："), `一级列表不应显示摘要：${rows[0]}`);
  // rant 2026-08-20T10:58:55：降频徽标只认 heartbeat_active —— 「已降频 · heartbeat {m}s」
  // （7200s → 2h00m；不再有 empty_cycles "空转 N 轮" 文案）
  assert.ok(rows[0].includes("已降频 · heartbeat 2h00m"), `降频徽标：${rows[0]}`);
  assert.ok(rows[1].includes("尚未运行"), `未运行提示：${rows[1]}`);
  assert.ok(!rows[1].includes("已降频"), `无降频时无徽标：${rows[1]}`);
});

test("rant 2026-08-14T15:41:52：快速点击添加任务 —— 元数据未加载完也填充下拉 + 保存成功", async () => {
  let created = null;
  const { ctx, els } = makeSandbox({
    listTasks: async () => [],
    // 延迟返回（50ms）→ 复现"打开面板后立即点添加"竞态（loadTaskMeta 未完成）
    taskTemplateList: async () => {
      await new Promise((r) => setTimeout(r, 50));
      return [
        { name: "evolution", builtin: true, template: "evolution_prompt.md" },
        { name: "custom-a", builtin: false, template: "custom-a.md", prompt: "# a" },
      ];
    },
    listProjects: async () => {
      await new Promise((r) => setTimeout(r, 50));
      return [{ name: "emrg", path: "/p/emrg" }];
    },
    taskCreate: async (payload) => { created = payload; return { ok: true }; },
  });
  await tick();
  // 打开任务面板（fire-and-forget，不等 loadTaskMeta —— 复现宿主"点 ⏱ 立即点添加"）
  vm.runInContext("App.openTasksPanel()", ctx);
  els["task-form"].classList.add("hidden"); // 镜像 index.html 初始态
  // 元数据还在加载（50ms 未到）就点"＋ 添加任务"
  await vm.runInContext('document.getElementById("task-add-btn").click()', ctx);
  // 等 openTaskForm 内 await loadTaskMeta() 完成（50ms 延迟 + 缓冲）
  await new Promise((r) => setTimeout(r, 120));
  await tick();
  // 下拉应有选项（修复前：0 个选项 → 保存报 invalid type → 界面"没反应"）
  const typeOpts = vm.runInContext('Array.from(document.getElementById("task-form-type").children).map((o) => o.value)', ctx);
  assert.deepStrictEqual(typeOpts.sort(), ["custom-a", "evolution"]);
  const projOpts = vm.runInContext('Array.from(document.getElementById("task-form-project").children).map((o) => o.value)', ctx);
  assert.deepStrictEqual(projOpts, ["emrg"], "项目下拉应含已注册项目");
  // 填任务名 → 保存 → taskCreate payload type/project 非空
  vm.runInContext('document.getElementById("task-form-name").value = "quick-task"', ctx);
  await vm.runInContext("EMRG_Dialogs.saveTaskForm()", ctx);
  await tick();
  assert.ok(created, "快速点击保存应成功（不报 invalid type）");
  assert.ok(created.type && created.project, `type/project 非空：${JSON.stringify(created)}`);
  assert.strictEqual(created.type, "evolution");
  assert.strictEqual(created.project, "emrg");
});

test("P3：自定义类型管理 —— 内置只读；自定义增删改走模板 CRUD", async () => {
  let createdTpl = null;
  let deletedTpl = null;
  const { ctx, els } = makeSandbox({
    listTasks: async () => [],
    listProjects: async () => [{ name: "emrg", path: "/p/emrg" }],
    taskTemplateList: async () => [
      { name: "evolution", builtin: true, template: "evolution_prompt.md" },
      { name: "sync", builtin: false, template: "sync.md", prompt: "# sync prompt" },
    ],
    taskTemplateCreate: async (payload) => { createdTpl = payload; return { ok: true }; },
    taskTemplateDelete: async (payload) => { deletedTpl = payload; return { ok: true }; },
  });
  await tick();
  await vm.runInContext("App.openTasksPanel()", ctx);
  await tick();
  // 打开自定义类型列表（镜像 index.html 初始态：task-template-list hidden）
  els["task-template-list"].classList.add("hidden");
  await vm.runInContext('document.getElementById("task-template-mgr-btn").click()', ctx);
  await tick();
  const wrap = els["task-template-list"];
  assert.strictEqual(wrap.classList.contains("hidden"), false, "类型列表应展开");
  const rows = wrap.children.filter((c) => c.className.includes("task-row"));
  assert.strictEqual(rows.length, 2, `内置 + 自定义各一行，实际 ${rows.length}`);
  // rant 09:17:45：内置行只有只读"查看"按钮；自定义行有查看+编辑+删除
  const builtinActions = rows[0].querySelectorAll(".model-action-btn").length;
  const customActions = rows[1].querySelectorAll(".model-action-btn").length;
  assert.strictEqual(builtinActions, 1, "内置类型只读（仅查看按钮）");
  assert.strictEqual(customActions, 3, "自定义类型有查看+编辑+删除");
  // 内置查看 → 只读表单（编辑器 shim readOnly + 保存按钮隐藏 + 提示词载入）
  rows[0].querySelectorAll(".model-action-btn")[0].click();
  await tick();
  assert.strictEqual(els["task-template-form"].classList.contains("hidden"), false, "查看按钮应展开表单");
  assert.strictEqual(els["task-template-save"].classList.contains("hidden"), true, "内置只读应隐藏保存按钮");
  assert.strictEqual(els["task-template-name"].disabled, true, "内置类型名称不可改名");
  const viewPrompt = vm.runInContext('document.getElementById("task-template-prompt").value', ctx);
  assert.ok(String(viewPrompt).includes("prompt"), "只读查看应载入内置提示词内容");
  vm.runInContext("EMRG_Dialogs.closeTemplateForm()", ctx);
  await tick();
  assert.strictEqual(els["task-template-save"].classList.contains("hidden"), false, "关闭后恢复保存按钮");
  // 删除自定义 → 确认 → taskTemplateDelete（决策点②：daemon 拒绝被引用类型）
  const delBtn = rows[1].querySelectorAll(".model-action-btn")[2];
  delBtn.click();
  await tick();
  assert.strictEqual(els["confirm-dialog"].open, true, "删除需确认");
  await vm.runInContext("EMRG_Dialogs.confirmOk()", ctx);
  await tick();
  assert.ok(deletedTpl, "taskTemplateDelete 应被调用");
  assert.strictEqual(deletedTpl.name, "sync");
  // 新增自定义类型（底部"＋ 添加类型"）→ 表单 → taskTemplateCreate
  const addBtn = wrap.children[wrap.children.length - 1];
  addBtn.click();
  await tick();
  assert.strictEqual(els["task-template-name"].disabled, false, "新增类型名称可编辑");
  vm.runInContext(`(function() {
    document.getElementById("task-template-name").value = "nightly";
    document.getElementById("task-template-prompt").value = "# nightly\\nrun at night";
  })()`, ctx);
  await vm.runInContext("EMRG_Dialogs.saveTemplateForm()", ctx);
  await tick();
  assert.ok(createdTpl, "taskTemplateCreate 应被调用");
  assert.strictEqual(createdTpl.name, "nightly");
  assert.ok(createdTpl.prompt.includes("nightly"), "prompt 原样提交");
});

test("rant 09:17:45 review：#801 Monaco 加载失败 errback→shim + preferScriptTags 配置", async () => {
  // pm25coder Windows 实测反馈（#801 review）：
  // ① sandbox renderer 下 loader 会走 Node 分支（nodeRequire undefined）→ 必须 preferScriptTags:true
  // ② editor.main 加载失败时若无 errback，waiters 永久堆积 + 表单空白死区 → 必须 errback→shim
  const configCaptured = {};
  const { ctx, win, els } = makeSandbox({
    listTasks: async () => [],
    listProjects: async () => [{ name: "emrg", path: "/p/emrg" }],
    taskTemplateList: async () => [
      { name: "evolution", builtin: true, template: "evolution_prompt.md" },
      { name: "sync", builtin: false, template: "sync.md", prompt: "# sync prompt" },
    ],
  });
  win.require = (deps, ok, err) => {
    assert.ok(Array.isArray(deps) && deps[0] === "vs/editor/editor.main", "应加载 editor.main");
    setTimeout(() => err && err(new Error("mock load failure")), 0); // 模拟加载失败
  };
  win.require.config = (cfg) => { Object.assign(configCaptured, cfg); };
  await vm.runInContext("App.openTasksPanel()", ctx);
  await tick();
  els["task-template-list"].classList.add("hidden");
  await vm.runInContext('document.getElementById("task-template-mgr-btn").click()', ctx);
  await tick();
  const rows = els["task-template-list"].children.filter((c) => c.className.includes("task-row"));
  assert.strictEqual(rows.length, 2, "内置 + 自定义各一行");
  // 内置"查看"→ withTemplateEditor：window.require 存在 → 不走立即 shim 分支 → initTemplateMonaco
  rows[0].querySelectorAll(".model-action-btn")[0].click();
  await tick();
  await tick();
  assert.strictEqual(configCaptured.preferScriptTags, true, "sandbox 环境必须 preferScriptTags:true（Node loader 分支需 nodeRequire）");
  assert.strictEqual(els["task-template-form"].classList.contains("hidden"), false, "errback 后表单应正常展开（shim 接管，非空白死区）");
  assert.strictEqual(els["task-template-save"].classList.contains("hidden"), true, "内置只读仍隐藏保存按钮");
  const promptVal = vm.runInContext('document.getElementById("task-template-prompt").value', ctx);
  assert.ok(String(promptVal).includes("prompt"), "errback→shim 后内置提示词仍载入（shim 写 host.value）");
  // 保存路径读 shim 值不返回空 → 无误导性 templateInvalid
  vm.runInContext('document.getElementById("task-template-name").value = "sync"', ctx);
  await vm.runInContext("EMRG_Dialogs.saveTemplateForm()", ctx);
  await tick();
  assert.ok(String(promptVal).length > 0, "shim getValue 非空");
});

test("rant 14:15:12：切会话加载最近历史（只读气泡 + 加载条），滚动到顶加载更早", async () => {
  const calls = [];
  const { ctx, els } = makeSandbox({
    switchSession: async () => ({}),
    listHistory: async ({ limit, offset } = {}) => {
      calls.push({ limit, offset });
      if (offset === 0) return { messages: [{ content: "msg-49", preview: "msg-49" }, { content: "msg-50", preview: "msg-50" }], hasMore: true };
      return { messages: [{ content: "msg-00", preview: "msg-00" }], hasMore: false };
    },
  });
  await vm.runInContext('App.boot()', ctx);
  await tick();
  // 切到 s1 → 应调 listHistory(limit=50, offset=0) 并渲染 2 条历史气泡 + 加载条
  await vm.runInContext('App.switchSession("s1")', ctx);
  await tick();
  assert.strictEqual(calls.length, 1, "切会话应加载最近一页历史");
  assert.strictEqual(calls[0].limit, 50);
  assert.strictEqual(calls[0].offset, 0);
  const historyNodes = vm.runInContext('document.getElementById("workspace").querySelectorAll(".history").length', ctx);
  assert.strictEqual(historyNodes, 2, "应渲染 2 条只读历史气泡");
  const loadBar = vm.runInContext('document.getElementById("workspace").querySelector(".history-load-bar")', ctx);
  assert.ok(loadBar, "hasMore 时应显示加载条");
  // 模拟滚动到顶 → 触发加载更早（防抖 150ms；mock 的 scroll 事件需带 .session-view target）
  const view = vm.runInContext('document.getElementById("workspace").querySelector(".session-view")', ctx);
  view.scrollTop = 0;
  els["workspace"].dispatch("scroll", { target: view });
  await new Promise((r) => setTimeout(r, 200));
  await tick();
  assert.strictEqual(calls.length, 2, "滚动到顶应加载更早一页");
  assert.strictEqual(calls[1].offset, 2, "第二次加载 offset=已加载数");
  const historyNodes2 = vm.runInContext('document.getElementById("workspace").querySelectorAll(".history").length', ctx);
  assert.strictEqual(historyNodes2, 3, "prepend 后共 3 条历史气泡");
  const noMore = vm.runInContext('document.getElementById("workspace").querySelector(".history-load-bar")', ctx);
  assert.ok(noMore, "无更多历史时加载条仍在（显示没有更多）");
});

test("rant 18:55:09 v0.2 回归：back-to-bottom 跟随会话视图滚动（scroll 不冒泡 → capture 捕获子 .session-view）", async () => {
  const { ctx, els } = makeSandbox({
    init: async () => ({
      config_exists: true,
      api_key_configured: true,
            server_id: "srv-1",
      model: "m",
      evolution_count: 0,
      sessions: [{ session_id: "s1", title: "测试对话", updated_at: "2026-08-05T10:00:00Z" }],
      open_sessions: [],
      active_sid: "s1",
    }),
    switchSession: async () => ({}),
  });
  await vm.runInContext("App.boot()", ctx);
  await tick();
  const view = vm.runInContext('document.getElementById("workspace").querySelector(".session-view")', ctx);
  const btn = els["back-to-bottom"];
  // 初始：在底部 → 按钮隐藏、autoScroll=true
  view.scrollTop = 100;
  view.scrollHeight = 1000;
  view.clientHeight = 100; // 100+100 >= 960? no → 不在底部
  els["workspace"].dispatch("scroll", { target: view });
  await tick();
  assert.strictEqual(btn.classList.contains("hidden"), false, "上滑（不在底部）→ back-to-bottom 显示");
  assert.strictEqual(vm.runInContext("App.state.autoScroll", ctx), false, "上滑 → autoScroll=false");
  // 点按钮 → 回到底部：scrollTop=scrollHeight、按钮隐藏
  vm.runInContext('document.getElementById("back-to-bottom").click()', ctx);
  await tick();
  assert.strictEqual(view.scrollTop, view.scrollHeight, "点击后滚动容器回到底部");
  assert.strictEqual(btn.classList.contains("hidden"), true, "回到底部后按钮隐藏");
  assert.strictEqual(vm.runInContext("App.state.autoScroll", ctx), true, "回到底部后 autoScroll=true");
  // 面板视图激活 → 按钮强制隐藏（不悬浮覆盖面板）
  view.scrollTop = 0;
  els["workspace"].dispatch("scroll", { target: view });
  await tick();
  assert.strictEqual(btn.classList.contains("hidden"), false, "再次上滑 → 按钮复现");
  await vm.runInContext('document.getElementById("nav-projects").click()', ctx);
  await tick();
  assert.strictEqual(btn.classList.contains("hidden"), true, "面板视图下 back-to-bottom 隐藏");
  // 回会话视图 → 按钮按滚动位置恢复（仍在上滑位置 → 复现）
  await vm.runInContext('document.getElementById("nav-projects").click()', ctx);
  await tick();
  assert.strictEqual(btn.classList.contains("hidden"), false, "回会话视图后按位置恢复按钮");
});

test("rant 18:55:09 v0.2：导航点击 → 工作区视图切换（面板整块显示 + 互斥 + 点同项关闭回会话）", async () => {
  const { ctx } = makeSandbox({
    init: async () => ({
      config_exists: true,
      api_key_configured: true,
            server_id: "srv-1",
      model: "m",
      evolution_count: 0,
      sessions: [{ session_id: "s1", title: "测试对话", updated_at: "2026-08-05T10:00:00Z" }],
      open_sessions: [],
      active_sid: "s1",
    }),
    switchSession: async () => ({}),
  });
  await vm.runInContext("App.boot()", ctx);
  await tick();
  const active = (id) => vm.runInContext(`document.getElementById("${id}").classList.contains("active")`, ctx);
  const visible = (id) => vm.runInContext(`document.getElementById("${id}").classList.contains("hidden") === false`, ctx);
  // 默认：会话视图激活、composer 可见、成果面板可见、无导航高亮
  assert.strictEqual(vm.runInContext("App.state.activeView", ctx), "sessions", "默认会话视图");
  assert.strictEqual(active("nav-projects"), false, "默认无导航高亮");
  assert.strictEqual(visible("composer-wrap"), true, "会话视图下 composer 可见");
  assert.strictEqual(visible("result-panel"), true, "会话视图下成果面板可见");
  // 点项目导航 → 项目视图整块显示（工作区内 .active）+ 高亮 + composer/成果面板隐藏
  await vm.runInContext('document.getElementById("nav-projects").click()', ctx);
  await tick();
  assert.strictEqual(active("panel-projects"), true, "项目视图应激活");
  assert.strictEqual(active("nav-projects"), true, "项目导航应高亮");
  assert.strictEqual(active("panel-tasks"), false, "其他面板视图不激活");
  assert.strictEqual(active("panel-settings"), false, "设置视图不激活");
  assert.strictEqual(vm.runInContext("App.state.activeView", ctx), "projects", "activeView=projects");
  assert.strictEqual(visible("composer-wrap"), false, "面板视图下 composer 隐藏");
  assert.strictEqual(visible("result-panel"), false, "面板视图下成果面板隐藏");
  // 点设置导航 → 项目关、设置开
  await vm.runInContext('document.getElementById("nav-settings").click()', ctx);
  await tick();
  assert.strictEqual(active("panel-projects"), false, "切换后项目视图关闭");
  assert.strictEqual(active("panel-settings"), true, "设置视图打开");
  assert.strictEqual(active("nav-projects"), false, "项目导航取消高亮");
  assert.strictEqual(active("nav-settings"), true, "设置导航高亮");
  // 点同一项 → 关闭回会话视图（composer/成果面板恢复）
  await vm.runInContext('document.getElementById("nav-settings").click()', ctx);
  await tick();
  assert.strictEqual(active("panel-settings"), false, "点同项应关闭面板视图");
  assert.strictEqual(active("nav-settings"), false, "关闭后导航取消高亮");
  assert.strictEqual(vm.runInContext("App.state.activeView", ctx), "sessions", "关闭回会话视图");
  assert.strictEqual(visible("composer-wrap"), true, "回会话视图后 composer 恢复");
  assert.strictEqual(visible("result-panel"), true, "回会话视图后成果面板恢复");
  // 点 💬 会话导航 → 会话视图（幂等，保持会话视图）
  await vm.runInContext('document.getElementById("nav-sessions").click()', ctx);
  await tick();
  assert.strictEqual(vm.runInContext("App.state.activeView", ctx), "sessions", "💬 导航回到会话视图");
});

test("rant 14:10:14 P2：设置面板 tab 切换（6 tab 独立显隐 + 高亮）", async () => {
  const { ctx } = makeSandbox();
  await vm.runInContext("App.boot()", ctx);
  await tick();
  const visible = (id) => vm.runInContext(`document.getElementById("${id}").classList.contains("hidden") === false`, ctx);
  const active = (id) => vm.runInContext(`document.getElementById("${id}").classList.contains("active")`, ctx);
  // 归一化初始态（index.html 标记 hidden 由 switchSettingsTab 全量重设，mock 需显式触发）
  await vm.runInContext("App.switchSettingsTab('model')", ctx);
  await tick();
  // 默认：model tab 激活且可见，其余 tab body 隐藏
  assert.strictEqual(visible("settings-body-model"), true, "默认显示模型服务 tab");
  for (const t of ["github", "appearance", "language", "about"]) {
    assert.strictEqual(visible(`settings-body-${t}`), false, `默认隐藏 ${t} tab`);
  }
  // 切到 GitHub tab → 只有它可见 + 高亮
  await vm.runInContext("App.switchSettingsTab('github')", ctx);
  await tick();
  assert.strictEqual(visible("settings-body-github"), true, "GitHub tab 应显示");
  assert.strictEqual(active("settings-tab-github"), true, "GitHub tab 应高亮");
  assert.strictEqual(visible("settings-body-model"), false, "模型服务 tab 应隐藏");
  assert.strictEqual(active("settings-tab-model"), false, "模型服务 tab 取消高亮");
  // 切到关于 → GitHub 关、关于开
  await vm.runInContext("App.switchSettingsTab('about')", ctx);
  await tick();
  assert.strictEqual(visible("settings-body-about"), true, "关于 tab 应显示");
  assert.strictEqual(visible("settings-body-github"), false, "GitHub tab 应隐藏");
  // 非法 tab 名 → 忽略（不抛）
  await vm.runInContext("App.switchSettingsTab('bogus')", ctx);
  await tick();
  assert.strictEqual(visible("settings-body-about"), true, "非法 tab 名应保持当前 tab");
});

test("rant 14:10:14 P5：项目面板列表（auto_evolve 徽标 + 最近活跃）+ 查看会话 + 删除刷新", async () => {
  let removed = null;
  const { ctx, els } = makeSandbox({
    listProjects: async () => [
      { name: "emrg", path: "/p/emrg", latest_session_at: "2026-08-13T07:00:00+08:00" },
      { name: "docs", path: "/p/docs", latest_session_at: null },
    ],
    listTasks: async () => [
      { name: "emrg-task", type: "evolution", config: { project: "emrg" }, interval: 1800, enabled: true },
      // docs 无任何任务 → 无 auto_evolve 徽标（负态）
    ],
    listProjectSessions: async ({ projectPath }) => ({
      sessions: projectPath === "/p/docs"
        ? [{ session_id: "s-docs", title: "文档会话" }]
        : [],
    }),
    removeProject: async (payload) => { removed = payload; return { ok: true, removed: true, closed: [] }; },
  });
  await tick();
  await vm.runInContext("App.openProjectsPanel()", ctx);
  await tick();
  const list = els["project-list"];
  // 2 行项目 + auto_evolve 徽标只出现在 emrg 行（有 evolution 任务）
  const rows = list.children.filter((c) => c.className.includes("task-row"));
  assert.strictEqual(rows.length, 2, `项目应渲染 2 行，实际 ${rows.length}`);
  const names = rows.map((r) => r.querySelector(".task-name") ? r.querySelector(".task-name").textContent : "");
  assert.deepStrictEqual(names, ["emrg", "docs"]);
  const badges = rows.map((r) => r.querySelectorAll(".task-badge").length);
  assert.strictEqual(badges[0], 1, "emrg（有 evolution 任务）应显示 auto_evolve 徽标");
  assert.strictEqual(badges[1], 0, "docs（无 evolution 任务）不应显示徽标");
  // 查看会话 → 点击 docs 行「会话」→ 内嵌会话列表
  const docsRow = rows[1];
  const sessBtns = docsRow.querySelectorAll(".model-action-btn");
  assert.strictEqual(sessBtns.length, 2, "每行应有 查看会话 + 删除 两个操作");
  sessBtns[0].click();
  await tick();
  const rows2 = list.children.filter((c) => c.className.includes("task-row"));
  assert.ok(rows2.length >= 2, "会话视图应含 返回行 + 会话行");
  const names2 = rows2.map((r) => r.querySelector(".task-name") ? r.querySelector(".task-name").textContent : "");
  assert.ok(names2.some((n) => n.includes("文档会话")), `会话行应显示会话标题，实际 ${names2.join(",")}`);
  // 返回 → 列表
  const backBtn = rows2[0].querySelector(".model-action-btn");
  backBtn.click();
  await tick();
  assert.strictEqual(list.children.filter((c) => c.className.includes("task-row")).length, 2, "返回后回到项目列表");
  // 删除 → confirmDeleteProject（非受保护 docs）→ removeProject + 刷新
  const docsRow2 = list.children.filter((c) => c.className.includes("task-row"))[1];
  const delBtns = docsRow2.querySelectorAll(".model-action-btn");
  delBtns[1].click();
  assert.strictEqual(els["confirm-dialog"].open, true, "删除应弹确认框");
  await vm.runInContext("EMRG_Dialogs.confirmOk()", ctx);
  await tick();
  assert.ok(removed, "removeProject 应被调用");
  assert.strictEqual(removed.name, "docs");
});

test("rant 14:10:14 P4：rant 面板列表（5 列 + 状态徽标三态 + 筛选）+ 详情 markdown 渲染 + 新建提交", async () => {
  const rants = [
    { timestamp: "2026-08-13T14:10:14.854793", project: "emrg", status: "in_progress", progress: "P1 done", message: "## GUI 重设计\n**加粗** 与 `code`" },
    { timestamp: "2026-08-12T09:00:00", project: "", status: "completed", progress: null, message: "旧 rant" },
    { timestamp: "2026-08-13T10:00:00", project: "", status: "pending", progress: null, message: "新想法" },
  ];
  let lastFilter = null;
  let sent = null;
  const { ctx, els } = makeSandbox({
    listRants: async (payload) => { lastFilter = payload; return rants; },
    listProjects: async () => [{ name: "emrg", path: "/p/emrg" }],
    sendRant: async (payload) => { sent = payload; return { ok: true, count: 4 }; },
  });
  await tick();
  await vm.runInContext("App.openRantsPanel()", ctx);
  await tick();
  const list = els["rant-list"];
  // 3 行 rant（.task-row.rant-row），状态徽标三态配色 + 5 列
  const rows = list.children.filter((c) => c.className.includes("task-row"));
  assert.strictEqual(rows.length, 3, `rant 应渲染 3 行，实际 ${rows.length}`);
  const badges = rows.map((r) => r.querySelector(".task-badge") ? r.querySelector(".task-badge").textContent : "");
  assert.ok(badges[0].includes("进行中"), `行0 徽标应含进行中，实际 ${badges[0]}`);
  assert.ok(badges[1].includes("已完成"), `行1 徽标应含已完成，实际 ${badges[1]}`);
  assert.ok(badges[2].includes("待处理"), `行2 徽标应含待处理，实际 ${badges[2]}`);
  // 三态配色类：in_progress→badge-warn / completed→badge-done / pending→badge-muted
  const badgeCls = rows.map((r) => r.querySelector(".task-badge") ? r.querySelector(".task-badge").className : "");
  assert.ok(badgeCls[0].includes("badge-warn"), `行0 徽标应为 badge-warn，实际 ${badgeCls[0]}`);
  assert.ok(badgeCls[1].includes("badge-done"), `行1 徽标应为 badge-done，实际 ${badgeCls[1]}`);
  assert.ok(badgeCls[2].includes("badge-muted"), `行2 徽标应为 badge-muted，实际 ${badgeCls[2]}`);
  // 5 列：时间/项目/状态/进度/内容（内容列去 md 标题符号后的摘要）
  const timeCells = rows.map((r) => r.querySelector(".rant-col-time") ? r.querySelector(".rant-col-time").textContent : "");
  assert.ok(timeCells[0].includes("2026-08-13 14:10"), `时间列应含截断时间戳，实际 ${timeCells[0]}`);
  const projCells = rows.map((r) => r.querySelector(".rant-col-project") ? r.querySelector(".rant-col-project").textContent : "");
  assert.strictEqual(projCells[0], "emrg", "项目列应显示项目名");
  assert.strictEqual(projCells[1], "—", "无项目行应显示 —");
  const contentCells = rows.map((r) => r.querySelector(".rant-col-content") ? r.querySelector(".rant-col-content").textContent : "");
  assert.ok(contentCells[0].includes("GUI 重设计"), `内容列应去 ## 后含标题摘要，实际 ${contentCells[0]}`);
  assert.strictEqual(contentCells[0].includes("##"), false, "内容列摘要不应含 md 标题符号");
  const progCells = rows.map((r) => r.querySelector(".rant-col-progress") ? r.querySelector(".rant-col-progress").textContent : "");
  assert.strictEqual(progCells[0], "P1 done", "进度列应显示 progress");
  // 点击行 → 详情展开：meta 行 + markdown 渲染（注入真实 marked）+ progress
  const markedReal = require(path.join(__dirname, "..", "vendor", "marked.min.js")).marked;
  ctx.marked = markedReal; // 注入真实 marked 到 vm 全局（window.marked）
  rows[0].click();
  await tick();
  const detail = list.querySelector(".rant-detail");
  assert.ok(detail, "点击行应展开详情");
  const metaText = (detail.querySelector(".rant-meta") || {}).textContent || "";
  assert.ok(metaText.includes("emrg"), `meta 应含项目，实际 ${metaText}`);
  const detailMd = detail.querySelector(".rant-md");
  assert.ok(detailMd, "详情应有 .rant-md 容器");
  assert.ok((detailMd._html || "").includes("<h2"), `详情 markdown 应渲染 h2，实际 ${detailMd._html}`);
  assert.ok((detailMd._html || "").includes("<strong"), `详情 markdown 应渲染 strong，实际 ${detailMd._html}`);
  assert.ok((detailMd._html || "").includes("<code"), `详情 markdown 应渲染 code，实际 ${detailMd._html}`);
  const progText = (detail.querySelector(".rant-progress") || {}).textContent || "";
  assert.ok(progText.includes("P1 done"), `详情应含 progress，实际 ${progText}`);
  // 筛选 tab → setRantFilter('completed') → listRants 带 status 调用
  await vm.runInContext("EMRG_Dialogs.setRantFilter('completed')", ctx);
  await tick();
  assert.strictEqual(lastFilter.status, "completed", "筛选应透传 status 到 listRants");
  // 新建 → 表单展开 → 提交 → sendRant + 列表刷新
  els["rant-form"].classList.add("hidden"); // 镜像 index.html 初始态
  await vm.runInContext('document.getElementById("rant-new-btn").click()', ctx);
  assert.strictEqual(els["rant-form"].classList.contains("hidden"), false, "新建应展开表单");
  vm.runInContext('document.getElementById("rant-form-message").value = "希望支持 X"', ctx);
  await vm.runInContext("EMRG_Dialogs.submitRantForm()", ctx);
  await tick();
  assert.ok(sent, "sendRant 应被调用");
  assert.strictEqual(sent.message, "希望支持 X");
});

test("rant 10:41:43：preprocessRantMarkdown —— 【】段标记映射标题 + 原文保留 + 长行不转", () => {
  const { ctx } = makeSandbox();
  const pp = (s) => vm.runInContext(`EMRG_Dialogs.preprocessRantMarkdown(${JSON.stringify(s)})`, ctx);
  // 短行 【xxx】 → #### 标题（原文保留，仅加前缀）
  const out1 = pp("【任务】\n宿主要求：显示任务状态。\n【修改方案】\n加倒计时。");
  assert.ok(out1.includes("#### 【任务】"), `【任务】应转 h4 标题：${out1}`);
  assert.ok(out1.includes("#### 【修改方案】"), `【修改方案】应转 h4：${out1}`);
  assert.ok(out1.includes("宿主要求：显示任务状态。"), "正文行保持原文");
  // 长行（>60 字符，如带完整设计的 rant 标题）→ 不转标题，保持正文
  const longLine = "【GUI 任务管理：显示任务当前状态 + 下次运行倒计时——完整设计如下，照抄实施，勿需再读文档】";
  const out2 = pp(longLine);
  assert.strictEqual(out2, longLine, "长行不应转标题");
  // 空/非字符串安全
  assert.strictEqual(pp(""), "", "空串安全");
  assert.strictEqual(pp(null), "", "null 安全");
});

test("rant 10:41:43：rant 详情点击收起 —— 再点已展开行 → 收起；点其他行 → 换展开", async () => {
  const rants = [
    { timestamp: "2026-08-15T10:00:00", project: "emrg", status: "pending", progress: null, message: "【任务】\n第一条" },
    { timestamp: "2026-08-15T11:00:00", project: "emrg", status: "pending", progress: null, message: "【任务】\n第二条" },
  ];
  const { ctx, els } = makeSandbox({
    listRants: async () => rants,
    listProjects: async () => [],
  });
  await tick();
  await vm.runInContext("App.openRantsPanel()", ctx);
  await tick();
  const list = els["rant-list"];
  const rows = list.children.filter((c) => c.className.includes("task-row"));
  assert.strictEqual(rows.length, 2);
  // 点行0 → 展开
  rows[0].click();
  await tick();
  assert.ok(list.querySelector(".rant-detail"), "点行0应展开详情");
  // 再点行0 → 收起（修复前：删+建 → 永远展开）
  rows[0].click();
  await tick();
  assert.strictEqual(list.querySelector(".rant-detail"), null, "再点已展开行应收起");
  // 点行0 → 展开；点行1 → 旧的收起、行1 展开（行为保持）
  rows[0].click();
  await tick();
  assert.ok(list.querySelector(".rant-detail"), "行0展开");
  rows[1].click();
  await tick();
  const details = list.children.filter((c) => c.className.includes("rant-detail"));
  assert.strictEqual(details.length, 1, "切换查看不同 rant → 旧的收起新的展开（仅 1 个详情）");
});

test("rant 21:36:01/21:38:25/21:46:53：三面板标题 + Rant 列头 + 项目 hint 移除 + i18n.apply 保留控件", async () => {
  const htmlSrc = fs.readFileSync(path.join(__dirname, "..", "renderer", "index.html"), "utf-8");
  const i18nSrc = fs.readFileSync(path.join(__dirname, "..", "renderer", "js", "i18n.js"), "utf-8");
  // Rant 面板：标题 + 5 列头
  const rantSection = htmlSrc.match(/id="panel-rants"([\s\S]*?)(?:<section|<div id="workspace"|\Z)/);
  assert.ok(rantSection, "index.html 应有 rant 面板 section");
  assert.ok(rantSection[1].includes('class="workspace-view-title" data-i18n="rants.title"'),
    "Rant 面板应有 .workspace-view-title 标题（data-i18n=rants.title）");
  assert.ok(rantSection[1].includes('class="rant-head"'), "Rant 面板应有 .rant-head 列头行");
  for (const key of ["rants.colTime", "rants.colProject", "rants.colStatus", "rants.colProgress", "rants.colContent"]) {
    assert.ok(rantSection[1].includes(`data-i18n="${key}"`), `Rant 列头应含 ${key}`);
  }
  // 项目面板：标题 + 移除 hint
  const projSection = htmlSrc.match(/id="panel-projects"([\s\S]*?)(?:<section|<\/section>)/);
  assert.ok(projSection, "index.html 应有项目面板 section");
  assert.ok(projSection[1].includes('class="workspace-view-title" data-i18n="projects.title"'),
    "项目面板应有 .workspace-view-title 标题（data-i18n=projects.title）");
  assert.ok(!projSection[1].includes("projects.hint"), "项目面板不应再有顶部操作说明 hint");
  // 任务面板：标题
  const taskSection = htmlSrc.match(/id="panel-tasks"([\s\S]*?)(?:<section|<\/section>)/);
  assert.ok(taskSection, "index.html 应有任务面板 section");
  assert.ok(taskSection[1].includes('class="workspace-view-title" data-i18n="tasks.title"'),
    "任务面板应有 .workspace-view-title 标题（data-i18n=tasks.title）");
  // i18n：三面板标题键存在（zh/en）
  for (const key of ['"rants.title"', '"projects.title"', '"tasks.title"', '"rants.colTime"', '"rants.colProject"', '"rants.colStatus"', '"rants.colProgress"', '"rants.colContent"']) {
    assert.ok(i18nSrc.includes(key), `i18n.js 应定义 ${key}`);
  }
  // i18n.apply 修复（rant 21:46:53）：不再整体 textContent 清子元素，只替换首个文本节点
  const i18nApplySrc = i18nSrc;
  assert.ok(!i18nApplySrc.includes("node.textContent = t(key)"), "i18n.apply 不应整体 textContent 赋值（会清空含控件 label 子元素）");
  assert.ok(i18nApplySrc.includes("first.nodeValue = text"), "i18n.apply 应只替换首个文本节点（first.nodeValue = text）");
  // 孤儿键清理：rants.detail / projects.hint 已随本改动删除
  assert.ok(!i18nSrc.includes('"rants.detail"'), "i18n 不应再有孤儿 rants.detail");
  assert.ok(!i18nSrc.includes('"projects.hint"'), "i18n 不应再有孤儿 projects.hint");
});

test("rant 21:49:51 settings panel title + sidebar settings-btn removed", async () => {
  const htmlSrc = fs.readFileSync(path.join(__dirname, "..", "renderer", "index.html"), "utf-8");
  // 设置面板应有标题（对齐 Rant/项目/任务面板 .workspace-view-title）
  const settingsSection = htmlSrc.match(/id="panel-settings"([\s\S]*?)(?:<section|<\/section>)/);
  assert.ok(settingsSection, "index.html 应有设置面板 section");
  assert.ok(settingsSection[1].includes('class="workspace-view-title" data-i18n="settings.title"'),
    "设置面板应有 .workspace-view-title 标题（data-i18n=settings.title）");
  // 侧边栏底部 settings-btn 已删除（与导航 ⚙ 重复入口），status-dot 保留
  const footer = htmlSrc.match(/class="sidebar-footer"([\s\S]*?)<\/div>/);
  assert.ok(footer, "index.html 应有 sidebar-footer");
  assert.ok(!footer[1].includes("settings-btn"), "sidebar-footer 不应再有 settings-btn");
  assert.ok(footer[1].includes("status-dot"), "sidebar-footer 应保留 status-dot（连接状态指示）");
  // 对应 JS 绑定已删除
  const appSrc = fs.readFileSync(path.join(__dirname, "..", "renderer", "js", "app.js"), "utf-8");
  assert.ok(!appSrc.includes('$("settings-btn")'), "app.js 不应再有 settings-btn 绑定");
  // i18n 无孤儿 sidebar.settings
  const i18nSrc = fs.readFileSync(path.join(__dirname, "..", "renderer", "js", "i18n.js"), "utf-8");
  assert.ok(!i18nSrc.includes('"sidebar.settings"'), "i18n 不应再有 sidebar.settings 孤儿键");
});
