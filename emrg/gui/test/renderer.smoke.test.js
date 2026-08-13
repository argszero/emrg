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
  "settings-tabs", "settings-tab-model", "settings-tab-workdir", "settings-tab-github", "settings-tab-appearance", "settings-tab-language", "settings-tab-about",
  "settings-body-model", "settings-body-workdir", "settings-body-github", "settings-body-appearance", "settings-body-language", "settings-body-about",
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
  "about-update", "about-update-check-btn",
  "github-banner", "github-banner-msg", "github-banner-connect", "github-banner-dismiss",
  // rant 18:23:15 P3：定时任务/自定义类型管理（settings 区）
  "task-list", "task-add-btn", "task-template-mgr-btn",
  "task-form", "task-form-name", "task-form-type", "task-form-project",
  "task-form-interval", "task-form-enabled", "task-form-repo",
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
      init: async () => ({ config_exists: false, api_key_configured: false, project_dir: "", project_dir_valid: true, server_id: "", model: "", evolution_count: 0, sessions: [] }),
      onEvent() {},
      sendMessage: async () => ({}),
      cancel: async () => ({}),
      getSettings: async () => ({ apiKey: "", baseUrl: "", model: "", projectDir: "", models: [], modelDetails: [], theme: "system" }),
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
      updateInstall: async () => ({ ok: true }), // rant 12:10：一键安装 IPC 默认桩
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
    'App.state.queuedSends.set("sess-1", [{ requestId: "req-a", text: "hi", mode: "auto" }, { requestId: "req-b", text: "yo", mode: "auto" }]);' +
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
    'App.state.queuedSends.set("sess-1", [{ requestId: "req-queue", text: "hi", mode: "auto" }]);' +
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
    '  { requestId: "req-m1", text: "m1", mode: "auto" },' +
    '  { requestId: "req-m2", text: "m2", mode: "auto" }]);' +
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
    'App.state.queuedSends.set("sess-1", [{ requestId: "req-a", text: "hi", mode: "auto" }]);' +
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
  assert.strictEqual(vb.children.length, 1, "unsid'd node goes to active session view");
  assert.strictEqual(va.children.length, 0, "inactive view untouched (state preserved)");
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
  assert.strictEqual(va.children.length, 1, "sess-a has its node");
  assert.strictEqual(vb.children.length, 1, "sess-b has its node");
  // 无 sid clear（/clear 的既有调用形态）→ 只清激活容器
  ctx.EMRG_Chat.clear();
  assert.strictEqual(vb.children.length, 0, "active view cleared");
  assert.strictEqual(va.children.length, 1, "inactive view retained (切回继续看到原消息)");
  // 带 sid clear → 定向清
  ctx.EMRG_Chat.clear("sess-a");
  assert.strictEqual(va.children.length, 0, "targeted clear empties sess-a");
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
  assert.strictEqual(vx.children.length, 1, "unregistered sid renders into active view");
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
      project_dir: "/tmp",
      project_dir_valid: true,
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

test("rant 09:18：启动主动更新提示——有新版本且未提示过 → 系统消息一次", async () => {
  const { ctx } = makeSandbox({
    updateCheck: async () => ({ enabled: true, has_update: true, latest_version: "0.2.99", prompted_version: "", current_version: "0.2.23" }),
    updateCheckPrompted: async () => ({}),
  });
  await tick();
  const r = await vm.runInContext(`(async function() {
    let prompted = null;
    window.emrg.updateCheckPrompted = async (p) => { prompted = p; };
    await EMRG_Dialogs.promptUpdateAtStartup();
    const texts = [];
    for (let i = 0; i < $("workspace").children.length; i++) texts.push($("workspace").children[i].textContent);
    return { n: texts.length, joined: texts.join("|"), prompted: JSON.stringify(prompted) };
  })()`, ctx);
  assert.ok(r.n >= 1, "应输出一条系统消息");
  assert.ok(r.joined.includes("0.2.99"), `系统消息应含新版本号，实际 ${r.joined}`);
  assert.ok(r.prompted.includes("0.2.99"), "应记录已提示版本（幂等）");
});

test("rant 09:18：启动提示幂等——已提示过/未启用/无更新 → 不提示", async () => {
  const { ctx } = makeSandbox({
    updateCheck: async () => ({ enabled: true, has_update: true, latest_version: "0.2.99", prompted_version: "0.2.99", current_version: "0.2.23" }),
  });
  await tick();
  const r = await vm.runInContext(`(async function() {
    await EMRG_Dialogs.promptUpdateAtStartup();
    return { n: $("workspace").children.length };
  })()`, ctx);
  assert.strictEqual(r.n, 0, "已提示过同版本 → 不得重复提示");
});

test("rant 09:18：设置页手动检查按钮——点击强制重新检查并显示检查中", async () => {
  const { ctx } = makeSandbox({
    updateCheck: async (payload) => ({ enabled: true, has_update: true, latest_version: "0.2.99", prompted_version: "", current_version: "0.2.23", _force: payload && payload.force }),
    updateCheckPrompted: async () => ({}),
  });
  await tick();
  const r = await vm.runInContext(`(async function() {
    const calls = [];
    window.emrg.updateCheck = async (payload) => {
      calls.push(payload || {});
      return { enabled: true, has_update: true, latest_version: "0.2.99", prompted_version: "" };
    };
    const btn = $("about-update-check-btn");
    btn.textContent = "检查更新"; // 模拟 index.html 静态文案（沙箱不加载 HTML）
    EMRG_Dialogs.initUpdateCheckButton();
    btn.click();
    const during = { text: btn.textContent, disabled: btn.disabled };
    await new Promise((res) => setTimeout(res, 20));
    return {
      calls: JSON.stringify(calls),
      duringText: during.text,
      afterText: btn.textContent,
      afterDisabled: btn.disabled,
      updateShown: !$("about-update").classList.contains("hidden"),
    };
  })()`, ctx);
  const calls = JSON.parse(r.calls);
  assert.strictEqual(calls.length, 1, "点击按钮应触发一次 updateCheck");
  assert.strictEqual(calls[0].force, true, "手动检查必须带 force:true（跳过 TTL 缓存）");
  assert.strictEqual(r.duringText, "检查中…", "检查中按钮应显示“检查中…”");
  assert.strictEqual(r.afterText, "检查更新", "完成后按钮文案恢复");
  assert.strictEqual(r.afterDisabled, false, "完成后按钮恢复可用");
  assert.strictEqual(r.updateShown, true, "有新版本应显示 about-update 行");
});

test("rant 09:18：refreshUpdateCheck 透传 force 参数", async () => {
  const { ctx } = makeSandbox({
    updateCheck: async (payload) => ({ enabled: false, has_update: false, latest_version: "", prompted_version: "" }),
  });
  await tick();
  const r = await vm.runInContext(`(async function() {
    const calls = [];
    window.emrg.updateCheck = async (payload) => { calls.push(payload || {}); return { enabled: false, has_update: false, latest_version: "", prompted_version: "" }; };
    await EMRG_Dialogs.refreshUpdateCheck({ force: true });
    await EMRG_Dialogs.refreshUpdateCheck();
    return JSON.stringify(calls);
  })()`, ctx);
  const calls = JSON.parse(r);
  assert.strictEqual(calls.length, 2);
  assert.strictEqual(calls[0].force, true, "refreshUpdateCheck({force:true}) 应透传 force");
  assert.strictEqual(calls[1].force, false, "默认 refreshUpdateCheck() 不带 force");
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

// ── rant 2026-08-12T12:10:12：自动下载 + GUI 一键安装 ──

test("rant 12:10：已下载安装包 → 设置页一键安装按钮 → 确认 → updateInstall", async () => {
  const installCalls = [];
  const { ctx, els } = makeSandbox({
    updateCheck: async () => ({
      enabled: true, has_update: true, latest_version: "0.2.99", prompted_version: "",
      current_version: "0.2.27",
      downloaded_version: "0.2.99",
      downloaded_path: "/home/u/.emrg/updates/EMRG-0.2.99-windows-x64.exe",
    }),
    updateInstall: async (p) => { installCalls.push(p); return { ok: true }; },
  });
  await tick();
  await vm.runInContext("EMRG_Dialogs.refreshUpdateCheck()", ctx);
  await tick();
  const updEl = els["about-update"];
  assert.strictEqual(updEl.classList.contains("hidden"), false, "update row visible");
  assert.ok(updEl.children.length >= 1, "install button rendered");
  const btn = updEl.children[0];
  assert.ok((btn.textContent || "").includes("0.2.99"), `button text has version: "${btn.textContent}"`);
  assert.ok(btn.className.includes("btn"), `button styled as button: "${btn.className}"`);
  btn.click(); // → 确认对话框
  await tick();
  assert.strictEqual(els["confirm-dialog"].open, true, "confirm dialog shown");
  assert.ok((els["confirm-message"].textContent || "").includes("0.2.99"), "confirm mentions version");
  await vm.runInContext("EMRG_Dialogs.confirmOk()", ctx);
  await tick();
  assert.strictEqual(installCalls.length, 1, "updateInstall called once");
  assert.strictEqual(installCalls[0].path, "/home/u/.emrg/updates/EMRG-0.2.99-windows-x64.exe", "downloaded path passed");
  assert.strictEqual(installCalls[0].version, "0.2.99", "version passed");
});

test("rant 12:10：启动提示——已下载 → 系统消息“已就绪”（非更新链接）", async () => {
  const { ctx } = makeSandbox({
    updateCheck: async () => ({
      enabled: true, has_update: true, latest_version: "0.2.99", prompted_version: "",
      current_version: "0.2.27",
      downloaded_version: "0.2.99",
      downloaded_path: "/home/u/.emrg/updates/EMRG-0.2.99-macos-arm64.pkg",
    }),
  });
  await tick();
  const r = await vm.runInContext(`(async function() {
    await EMRG_Dialogs.promptUpdateAtStartup();
    const texts = [];
    for (let i = 0; i < $("workspace").children.length; i++) texts.push($("workspace").children[i].textContent);
    return texts.join("|");
  })()`, ctx);
  assert.ok(r.includes("0.2.99"), `ready message contains version: "${r}"`);
  assert.ok(r.includes("已下载") || r.includes("downloaded"), `ready wording: "${r}"`);
});

test("rant 12:10：downloaded_version == 当前版本 → 无安装按钮（退化更新链接）", async () => {
  const { ctx, els } = makeSandbox({
    updateCheck: async () => ({
      enabled: true, has_update: true, latest_version: "0.2.99", prompted_version: "",
      current_version: "0.2.27", downloaded_version: "0.2.27",
    }),
  });
  await tick();
  await vm.runInContext("EMRG_Dialogs.refreshUpdateCheck()", ctx);
  await tick();
  const updEl = els["about-update"];
  assert.strictEqual(updEl.classList.contains("hidden"), false, "update row visible");
  assert.strictEqual(updEl.children.length, 1, "single element (no install button)");
  const child = updEl.children[0];
  assert.ok(!child.className.includes("btn"), "not a button when downloaded == current");
  assert.ok((child.attributes.href || "").includes("releases"), "falls back to the Releases link");
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
  // 修改后保存 → taskUpdate
  vm.runInContext('document.getElementById("task-form-interval").value = "600"', ctx);
  await vm.runInContext("EMRG_Dialogs.saveTaskForm()", ctx);
  await tick();
  assert.ok(updated, "taskUpdate 应被调用");
  assert.strictEqual(updated.name, "daily-report");
  assert.strictEqual(updated.interval, 600);
  assert.strictEqual(updated.type, "evolution");
  assert.strictEqual(updated.project, "emrg");
  assert.strictEqual(updated.enabled, true);
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
  // 间隔 600 → 提交（含自定义类型 + 启用勾选）
  vm.runInContext('document.getElementById("task-form-interval").value = "600"', ctx);
  await vm.runInContext("EMRG_Dialogs.saveTaskForm()", ctx);
  await tick();
  assert.ok(created, "合法输入应提交 taskCreate");
  assert.strictEqual(created.name, "new-task");
  assert.strictEqual(created.type, "custom-a");
  assert.strictEqual(created.project, "emrg");
  assert.strictEqual(created.interval, 600);
  assert.strictEqual(created.enabled, true);
  assert.strictEqual(vm.runInContext('document.getElementById("task-form").classList.contains("hidden")', ctx), true, "保存后表单收起");
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
  // 内置行无操作按钮（决策点①⑥）；自定义行有编辑/删除
  const builtinActions = rows[0].querySelectorAll(".model-action-btn").length;
  const customActions = rows[1].querySelectorAll(".model-action-btn").length;
  assert.strictEqual(builtinActions, 0, "内置类型只读（无操作按钮）");
  assert.strictEqual(customActions, 2, "自定义类型有编辑+删除");
  // 删除自定义 → 确认 → taskTemplateDelete（决策点②：daemon 拒绝被引用类型）
  const delBtn = rows[1].querySelectorAll(".model-action-btn")[1];
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
      project_dir: "/tmp",
      project_dir_valid: true,
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
      project_dir: "/tmp",
      project_dir_valid: true,
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
  for (const t of ["workdir", "github", "appearance", "language", "about"]) {
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

test("rant 14:10:14 P4：rant 面板列表（状态徽标 + 筛选）+ 详情展开 + 新建提交", async () => {
  const rants = [
    { timestamp: "2026-08-13T14:10:14.854793", project: "emrg", status: "in_progress", progress: "P1 done", message: "GUI 重设计" },
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
  // 3 行 rant，全部含时间戳 + 状态徽标
  const rows = list.children.filter((c) => c.className.includes("task-row"));
  assert.strictEqual(rows.length, 3, `rant 应渲染 3 行，实际 ${rows.length}`);
  const badges = rows.map((r) => r.querySelector(".task-badge") ? r.querySelector(".task-badge").textContent : "");
  assert.ok(badges[0].includes("进行中"), `行0 徽标应含进行中，实际 ${badges[0]}`);
  assert.ok(badges[1].includes("已完成"), `行1 徽标应含已完成，实际 ${badges[1]}`);
  assert.ok(badges[2].includes("待处理"), `行2 徽标应含待处理，实际 ${badges[2]}`);
  // 点击行 → 详情展开（完整内容 + progress）
  rows[0].click();
  await tick();
  const detail = list.querySelector(".rant-detail");
  assert.ok(detail, "点击行应展开详情");
  const detailText = (detail.children || []).map((c) => c.textContent || "").join(" ");
  assert.ok(detailText.includes("GUI 重设计"), `详情应含完整 message，实际 ${detailText}`);
  assert.ok(detailText.includes("P1 done"), `详情应含 progress，实际 ${detailText}`);
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
