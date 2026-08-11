"use strict";
/**
 * result-panel.js — 结果面板（右栏工作区，rant 2026-08-11T12:20:35 workspace panel）
 *
 * P2 slice 1（框架层，PR #664）：
 *   - Tab 栏：文件 / 产物 两个静态 Tab + 打开文件 Tab（P3 查看器接入，API 已就绪：
 *     openFileTab/closeFileTab/activateTab，上限 8 淘汰最旧、同路径去重复用）
 *   - 可拖拽调整宽度（#result-resizer，绝对定位不进 flex 流 R6-②）；
 *     panelWidth 与 collapsed 分离持久化（localStorage 两个键）
 *   - per-session 容器状态（openedTabsBySid / artifactsBySid / activeTabBySid），
 *     切换会话隔离（缺口 5；对齐 app.js ensureSessionView 容器模式）
 *   - 拖拽期间 #result-panel.dragging 抑制 width transition（R1-①）
 * P3.2（PR #668）：产物 Tab 改为只登记 write/edit 成功文件（去重按 path、per-session 上限
 * 100、点击打开查看器 Tab）；extractFilePath 改进（优先首个绝对路径段，去扩展名依赖 R4-①）；
 * 移除工具输出卡片模式（决策点 3——bash/read 工具卡不再显示）。
 */

const ResultPanel = (() => {
  const MAX_ITEMS = 50;           // 产物卡片上限（P1 遗留，P3.2 改 per-session 100）
  const MAX_OPEN_TABS = 8;        // 打开文件 Tab 上限（决策点 6）
  const DEFAULT_WIDTH = 280;
  const MIN_WIDTH = 240;
  const MAX_WIDTH_RATIO = 0.45;   // 不超过视口 45%
  const LS_WIDTH = "emrg.resultPanel.panelWidth";
  const LS_COLLAPSED = "emrg.resultPanel.collapsed";

  // per-session 状态（P2.2 缺口 5：容器模式）
  const openedTabsBySid = new Map();  // sid -> { tabs: [{path,name}], active: tabId }
  const artifactsBySid = new Map();   // sid -> 登记记录（P3.2 消费）
  let currentSid = null;

  function panel() { return $("result-panel"); }
  function listEl() { return $("result-list"); }
  function filesEl() { return $("result-files"); }
  function viewerEl() { return $("result-viewer"); }
  function tabbarEl() { return $("result-tabbar"); }
  function resizerEl() { return $("result-resizer"); }

  // ── 宽度 / 折叠（分离持久化） ──
  function getWidth() {
    const p = panel();
    if (p && p.style && p.style.width) {
      const n = parseInt(p.style.width, 10);
      if (!Number.isNaN(n)) return n;
    }
    return storedWidth();
  }
  function storedWidth() {
    try {
      const v = parseInt(localStorage.getItem(LS_WIDTH) || "", 10);
      if (!Number.isNaN(v)) return clampWidth(v);
    } catch { /* ignore */ }
    return DEFAULT_WIDTH;
  }
  function clampWidth(w) {
    const vw = (window.innerWidth || 1200) * MAX_WIDTH_RATIO;
    return Math.round(Math.min(Math.max(w, MIN_WIDTH), Math.max(MIN_WIDTH, vw)));
  }
  function setWidth(w) {
    const p = panel();
    if (!p) return;
    const cw = clampWidth(w);
    p.style.width = cw + "px";
    try { localStorage.setItem(LS_WIDTH, String(cw)); } catch { /* ignore */ }
    updateResizerPos();
  }
  function isCollapsed() {
    const p = panel();
    return p ? p.classList.contains("collapsed") : true;
  }
  function setCollapsed(collapsed) {
    const p = panel();
    if (!p) return;
    if (collapsed) {
      // 折叠：先固化当前宽度到持久化，再应用窄条（inline width 覆盖 CSS 的 40px 需显式设置）
      setWidth(getWidth());
      p.style.width = "40px";
    } else {
      // 展开：恢复持久化宽度（勿读 style.width——折叠时为 40px）
      setWidth(storedWidth());
    }
    p.classList.toggle("collapsed", collapsed);
    try { localStorage.setItem(LS_COLLAPSED, collapsed ? "1" : "0"); } catch { /* ignore */ }
    updateResizerPos();
  }
  function toggle() {
    setCollapsed(!isCollapsed());
  }

  // ── 拖拽调整宽度（R1-①：.dragging 抑制 transition） ──
  function updateResizerPos() {
    const r = resizerEl();
    if (!r) return;
    if (isCollapsed()) { r.style.display = "none"; return; }
    r.style.display = "block";
    r.style.right = (getWidth() - 3) + "px";
  }
  function initResizer() {
    const r = resizerEl();
    const p = panel();
    if (!r || !p) return;
    let drag = null;
    r.addEventListener("mousedown", (e) => {
      drag = { startX: e.clientX, startWidth: getWidth() };
      p.classList.add("dragging");
      r.classList.add("dragging");
      const onMove = (ev) => {
        if (!drag) return;
        setWidth(drag.startWidth - (ev.clientX - drag.startX));
      };
      const onUp = () => {
        drag = null;
        p.classList.remove("dragging");
        r.classList.remove("dragging");
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
  }

  // ── Tab 管理（静态 文件/产物 + 打开文件 Tab，per-session） ──
  function tabIdFor(path) { return "file:" + path; }
  function stateFor(sid) {
    const key = sid || null;
    let s = openedTabsBySid.get(key);
    if (!s) { s = { tabs: [], active: "artifacts" }; openedTabsBySid.set(key, s); }
    return s;
  }
  function artifactsFor(sid) {
    const key = sid || null;
    let a = artifactsBySid.get(key);
    if (!a) { a = []; artifactsBySid.set(key, a); }
    return a;
  }

  function renderTabbar() {
    const bar = tabbarEl();
    if (!bar) return;
    const st = stateFor(currentSid);
    bar.innerHTML = "";
    if (st.tabs.length === 0) { bar.classList.remove("has-tabs"); return; }
    bar.classList.add("has-tabs");
    for (const t of st.tabs) {
      const active = st.active === tabIdFor(t.path);
      const btn = el("div", { class: "result-filetab" + (active ? " active" : ""), dataset: { path: t.path } }, t.name);
      const close = el("span", { class: "filetab-close", dataset: { path: t.path } }, "×");
      close.addEventListener("click", (e) => {
        e.stopPropagation();
        closeFileTab(currentSid, t.path);
      });
      btn.appendChild(close);
      btn.addEventListener("click", () => activateTab(tabIdFor(t.path)));
      bar.appendChild(btn);
    }
  }

  /** 激活 Tab（"files" | "artifacts" | "file:<path>"）；sid 缺省 = 当前会话 */
  function activateTab(tabId, sid) {
    const st = stateFor(sid !== undefined ? sid : currentSid);
    st.active = tabId;
    // 后台会话：只更新状态，不动全局 DOM（切回时由 switchSession 恢复）
    if ((sid !== undefined ? sid : currentSid) !== currentSid) return;
    const filesBtn = $("result-tab-files");
    const artBtn = $("result-tab-artifacts");
    if (filesBtn) filesBtn.classList.toggle("active", tabId === "files");
    if (artBtn) artBtn.classList.toggle("active", tabId === "artifacts");
    const isFileTab = typeof tabId === "string" && tabId.startsWith("file:");
    const fp = filesEl(), lp = listEl(), vp = viewerEl();
    if (fp) fp.classList.toggle("active", tabId === "files");
    if (lp) lp.classList.toggle("active", tabId === "artifacts");
    if (vp) vp.classList.toggle("active", isFileTab);
    renderTabbar();
    if (isFileTab) loadFileTab(tabId.slice(5));
  }

  /** 打开文件 Tab：同路径去重（激活既有）/ 上限 8 淘汰最旧；sid 缺省 = 当前会话 */
  function openFileTab(sid, path) {
    const key = sid == null ? currentSid : sid;
    const st = stateFor(key);
    const existing = st.tabs.find((t) => t.path === path);
    if (existing) { activateTab(tabIdFor(path), key); return existing; }
    const name = String(path).split(/[\\/]/).pop() || path;
    const tab = { path, name };
    st.tabs.push(tab);
    while (st.tabs.length > MAX_OPEN_TABS) st.tabs.shift();
    if ((key || null) === currentSid) renderTabbar();
    activateTab(tabIdFor(path), key);
    return tab;
  }
  function closeFileTab(sid, path) {
    const key = sid == null ? currentSid : sid;
    const st = stateFor(key);
    const idx = st.tabs.findIndex((t) => t.path === path);
    if (idx < 0) return;
    st.tabs.splice(idx, 1);
    if (st.active === tabIdFor(path)) {
      st.active = st.tabs.length ? tabIdFor(st.tabs[st.tabs.length - 1].path) : "artifacts";
    }
    if ((key || null) === currentSid) { renderTabbar(); activateTab(st.active, key); }
  }

  // ── 文件查看器（P3.3：文本高亮 / md 渲染 / 图片直显 / 二进制提示） ──
  const IMAGE_EXT = /\.(png|jpe?g|gif|svg|webp|bmp|ico)$/i;

  function isImagePath(path) {
    return IMAGE_EXT.test(String(path).split(/[?#]/)[0]);
  }
  function isMarkdownPath(path) {
    return /\.(md|markdown|mdown)$/i.test(String(path));
  }

  async function loadFileTab(path) {
    const vp = viewerEl();
    if (!vp) return;
    const st = stateFor(currentSid);
    const tab = st.tabs.find((t) => t.path === path);
    if (!tab || tab.loading) return;
    if (tab.content !== undefined || tab.image || tab.readError) { await renderViewer(vp, tab); return; }
    // 图片不走 read_file（file:// 直显，P3.3）
    if (isImagePath(path)) { tab.image = true; await renderViewer(vp, tab); return; }
    tab.loading = true;
    vp.innerHTML = "";
    vp.appendChild(el("div", { class: "result-empty" }, _t("result.viewerLoading")));
    try {
      const res = await window.emrg.readFile({ path });
      tab.content = res && typeof res.content === "string" ? res.content : "";
      tab.binary = !!(res && res.binary);
      tab.readError = false;
      tab.totalLines = (res && res.totalLines) || undefined;
    } catch {
      tab.content = "";
      tab.binary = false;
      tab.readError = true;
    } finally {
      tab.loading = false;
    }
    await renderViewer(vp, tab);
  }

  async function renderViewer(vp, tab) {
    vp.innerHTML = "";
    const head = el("div", { class: "viewer-head" });
    head.appendChild(el("span", { class: "viewer-path" }, tab.path));
    const openBtn = el("button", { class: "viewer-open", type: "button" }, _t("result.viewerOpen"));
    openBtn.addEventListener("click", async () => {
      try { await window.emrg.openFile({ filePath: tab.path }); } catch { /* ignore */ }
    });
    head.appendChild(openBtn);
    vp.appendChild(head);
    if (tab.readError) {
      vp.appendChild(el("div", { class: "result-empty" }, _t("result.viewerError")));
      return;
    }
    if (tab.binary) {
      vp.appendChild(el("div", { class: "result-empty" }, _t("result.viewerBinary")));
      return;
    }
    if (tab.image) {
      const img = el("img", { class: "viewer-img", src: "file://" + tab.path, alt: tab.name || "" });
      img.addEventListener("error", () => {
        img.style.display = "none";
        const hint = el("div", { class: "result-empty" }, _t("result.viewerError"));
        vp.appendChild(hint);
      });
      vp.appendChild(img);
      return;
    }
    // md → markdown 渲染（DOMPurify sanitize，与聊天区同源）
    if (isMarkdownPath(tab.path)) {
      try {
        const html = await window.emrgMarkdown.renderMarkdown(tab.content);
        const mdBox = el("div", { class: "viewer-md" });
        mdBox.innerHTML = html;
        vp.appendChild(mdBox);
        return;
      } catch { /* fall through to plain text */ }
    }
    // 文本：hljs 高亮（精确 lang，无 lang highlightAuto 兜底——与 markdown.js code renderer 同策略）
    const pre = el("pre", { class: "viewer-pre" });
    const code = el("code", {}, tab.content);
    try {
      const hljs = window.hljs;
      if (hljs) {
        const lang = detectLang(tab.path);
        if (lang && hljs.getLanguage(lang)) {
          code.innerHTML = hljs.highlight(tab.content, { language: lang, ignoreIllegals: true }).value;
        } else {
          code.innerHTML = hljs.highlightAuto(tab.content).value;
        }
        code.classList.add("hljs", "language-" + (lang || "plaintext"));
      }
    } catch { /* highlight failure → fall back to escaped text */ }
    pre.appendChild(code);
    vp.appendChild(pre);
  }

  /** 按文件扩展名推断语言（对齐 highlight.custom.js 已注册语言） */
  function detectLang(path) {
    const ext = String(path).split(".").pop().toLowerCase();
    const map = {
      py: "python", js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
      sh: "bash", bash: "bash", zsh: "bash", css: "css", html: "html", htm: "html",
      json: "json", yml: "yaml", yaml: "yaml", toml: "ini", md: "markdown",
      go: "go", rs: "rust", java: "java", c: "c", h: "c", cpp: "cpp", cc: "cpp",
      sql: "sql", xml: "xml", ini: "ini", dockerfile: "dockerfile", diff: "diff",
    };
    if (path.toLowerCase().includes("dockerfile")) return "dockerfile";
    return map[ext] || "";
  }

  /** 切换会话 → Tab/产物状态按 sid 隔离（缺口 5） */
  function switchSession(sid) {
    currentSid = sid || null;
    stateFor(currentSid);
    renderTabbar();
    activateTab(stateFor(currentSid).active);
    renderArtifacts(); // 产物 pane 按当前会话桶恢复（后台 tool_finished 只入桶不渲染）
  }

  // ── 产物登记（P3.2：只登记 write/edit 成功文件，per-session 去重，点击打开查看器 Tab） ──
  function renderEmpty() {
    const list = listEl();
    if (!list) return;
    if (list.children.length === 0) {
      list.innerHTML = `<div class="result-empty">${window.EMRG_I18N ? window.EMRG_I18N.t("result.empty") : "还没有产物，对话中生成的文件会出现在这里"}</div>`;
    }
  }

  /**
   * 登记一个产物（tool_finished 事件；sid = 事件桥会话）。
   * P3.2：只登记 write/edit 成功且提取到路径的文件（bash/read 工具卡不再显示）；
   * 同路径去重（更新既有条目移顶）；per-session 上限 100（决策点 3 / R7-⑦）。
   */
  function addToolResult(data, sid) {
    if (data.error) return; // 只登记成功产物
    const tool = data.tool_name || "";
    if (tool !== "write" && tool !== "edit") return; // 只留 write/edit 文件
    const content = String(data.content || "");
    const path = extractFilePath(tool, content);
    if (!path) return;
    const key = sid || currentSid;
    const arr = artifactsFor(key);
    const rec = {
      path,
      name: String(path).split(/[\\/]/).pop() || path,
      tool_name: tool,
      elapsed: data.elapsed,
    };
    // 去重：同路径更新既有条目并移顶
    const idx = arr.findIndex((r) => r.path === path);
    if (idx >= 0) arr.splice(idx, 1);
    arr.unshift(rec);
    if (arr.length > 100) arr.pop();
    // 后台会话：只入桶不渲染（防污染激活会话产物 pane；切回时由 renderArtifacts 恢复）
    if ((key || null) !== currentSid) return;
    renderArtifacts();
  }

  /** 按当前会话桶重渲染产物 pane（switchSession/init 时从桶恢复 DOM，镜像 renderTabbar 模式） */
  function renderArtifacts() {
    const list = listEl();
    if (!list) return;
    list.innerHTML = "";
    const arr = artifactsFor(currentSid);
    if (arr.length === 0) { renderEmpty(); return; }
    for (const rec of arr.slice(0, MAX_ITEMS)) renderArtifactRow(rec);
  }

  /** 产物行：文件名 + 相对路径；点击 → 打开查看器 Tab（P3.2，系统工具在查看器头部） */
  function renderArtifactRow(rec) {
    const list = listEl();
    if (!list) return;
    const row = el("div", { class: "artifact-row", dataset: { path: rec.path } });
    const name = el("span", { class: "artifact-name" }, rec.name);
    row.appendChild(name);
    const rel = el("span", { class: "artifact-rel" }, rec.path);
    row.appendChild(rel);
    row.addEventListener("click", () => openFileTab(currentSid, rec.path));
    list.appendChild(row);
  }

  /** 从工具输出提取生成的文件路径（P3.2 改进 R4-①：优先首个绝对路径段，去扩展名依赖） */
  function extractFilePath(toolName, content) {
    if (!content) return "";
    if (toolName === "write" || toolName === "edit") {
      // write: "Created /abs/path (N characters)" / "Updated /abs/path (N chars)"
      // edit:  "Made 1 replacement in /abs/path" / "Made 3 replacements in /abs/path"
      // 取首个 / 开头的绝对路径段（Makefile/.env/Dockerfile 等无扩展名文件也命中）
      const m = content.match(/\/[^\s()]+/);
      if (m) return cleanPath(m[0]);
      // 兜底：旧关键词格式
      const km = content.match(/(?:Wrote|wrote|Written|created|已写入|写入)[^\n:：]*[:：]\s*([^\s\n]+)/);
      if (km) return cleanPath(km[1]);
      return "";
    }
    if (toolName === "bash") {
      // bash 输出通常不带路径；带 "Created" 或重定向时尝试
      const m = content.match(/(?:Created|created|Generated|generated)[^\n:：]*[:：]\s*([^\s\n]+)/);
      if (m) return cleanPath(m[1]);
    }
    return "";
  }

  function cleanPath(p) {
    return String(p).replace(/[`"'，,。、;；]/g, "").trim();
  }

  function init() {
    const p = panel();
    if (!p) return;
    // 静态 Tab 点击（文件 / 产物）
    const filesBtn = $("result-tab-files");
    const artBtn = $("result-tab-artifacts");
    if (filesBtn) filesBtn.addEventListener("click", () => activateTab("files"));
    if (artBtn) artBtn.addEventListener("click", () => activateTab("artifacts"));
    // 折叠按钮
    const btn = $("result-toggle");
    if (btn) btn.addEventListener("click", toggle);
    // 键盘 ⌘\ 或 Ctrl\
    document.addEventListener("keydown", (e) => {
      if (e.key === "\\" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        toggle();
      }
    });
    // 窄屏自动隐藏（<900px 折叠）
    const mq = window.matchMedia ? window.matchMedia("(max-width: 900px)") : null;
    if (mq) {
      const onMq = (ev) => { if (ev.matches) setCollapsed(true); };
      if (mq.addEventListener) mq.addEventListener("change", onMq);
      if (mq.matches) setCollapsed(true);
    }
    // 恢复折叠状态 + 宽度（分离持久化）
    try {
      if (localStorage.getItem(LS_COLLAPSED) === "1") setCollapsed(true);
      else setWidth(storedWidth());
    } catch { /* ignore */ }
    // 拖拽手柄
    initResizer();
    window.addEventListener("resize", updateResizerPos);
    renderTabbar();
    activateTab(stateFor(currentSid).active);
    renderArtifacts();
  }

  return { init, addToolResult, toggle, isCollapsed, switchSession, openFileTab, closeFileTab, activateTab, getWidth, setWidth };
})();

// ⚠️ 必须暴露到 window：app.js 作为独立 <script> 加载，模块级 const 不跨 script 共享。
// 缺失会导致真实 GUI 打开即 ReferenceError（测试沙箱因共享 vm context 掩盖此问题）。
window.ResultPanel = ResultPanel;
