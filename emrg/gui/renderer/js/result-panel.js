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
 * 产物卡片（WorkBuddy P1）暂保留在「产物」pane 内——P3.2 再改为 write/edit 文件登记，
 * 避免框架先于内容上线导致面板空窗。
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
    const fp = filesEl(), lp = listEl();
    if (fp) fp.classList.toggle("active", tabId === "files");
    if (lp) lp.classList.toggle("active", tabId === "artifacts");
    renderTabbar();
  }

  /** 打开文件 Tab：同路径去重（激活既有）/ 上限 8 淘汰最旧 */
  function openFileTab(sid, path) {
    const st = stateFor(sid);
    const existing = st.tabs.find((t) => t.path === path);
    if (existing) { activateTab(tabIdFor(path), sid); return existing; }
    const name = String(path).split(/[\\/]/).pop() || path;
    const tab = { path, name };
    st.tabs.push(tab);
    while (st.tabs.length > MAX_OPEN_TABS) st.tabs.shift();
    if ((sid || null) === currentSid) renderTabbar();
    activateTab(tabIdFor(path), sid);
    return tab;
  }
  function closeFileTab(sid, path) {
    const st = stateFor(sid);
    const idx = st.tabs.findIndex((t) => t.path === path);
    if (idx < 0) return;
    st.tabs.splice(idx, 1);
    if (st.active === tabIdFor(path)) {
      st.active = st.tabs.length ? tabIdFor(st.tabs[st.tabs.length - 1].path) : "artifacts";
    }
    if ((sid || null) === currentSid) { renderTabbar(); activateTab(st.active, sid); }
  }

  /** 切换会话 → Tab/产物状态按 sid 隔离（缺口 5） */
  function switchSession(sid) {
    currentSid = sid || null;
    stateFor(currentSid);
    renderTabbar();
    activateTab(stateFor(currentSid).active);
    renderArtifacts(); // 产物 pane 按当前会话桶恢复（后台 tool_finished 只入桶不渲染）
  }

  // ── 产物登记（P1 卡片渲染保留；P3.2 改 write/edit 文件登记） ──
  function renderEmpty() {
    const list = listEl();
    if (!list) return;
    if (list.children.length === 0) {
      list.innerHTML = `<div class="result-empty">${window.EMRG_I18N ? window.EMRG_I18N.t("result.empty") : "还没有产物，对话中生成的文件会出现在这里"}</div>`;
    }
  }

  /** 登记一个产物条目（tool_finished 事件；sid = 事件桥会话，P3.2 消费） */
  function addToolResult(data, sid) {
    const arr = artifactsFor(sid || currentSid);
    const record = {
      tool_name: data.tool_name || "tool",
      content: String(data.content || ""),
      error: !!data.error,
      elapsed: data.elapsed,
    };
    arr.unshift(record);
    if (arr.length > 100) arr.pop();
    // 后台会话：只入桶不渲染（防污染激活会话产物 pane；切回时由 renderArtifacts 恢复）
    if ((sid || null) !== currentSid) return;
    renderCard(record);
  }

  /** 按当前会话桶重渲染产物 pane（switchSession/init 时从桶恢复 DOM，镜像 renderTabbar 模式） */
  function renderArtifacts() {
    const list = listEl();
    if (!list) return;
    list.innerHTML = "";
    const arr = artifactsFor(currentSid);
    if (arr.length === 0) { renderEmpty(); return; }
    for (const rec of arr.slice(0, MAX_ITEMS)) renderCard(rec);
  }

  function renderCard(data) {
    const list = listEl();
    if (!list) return;
    // 空状态占位清除
    const empty = list.querySelector(".result-empty");
    if (empty) empty.remove();

    const card = el("div", { class: "result-item" });

    // 标题行：工具名 + 耗时
    const head = el("div", { class: "result-head" });
    const name = el("span", { class: "result-tool" }, data.tool_name || "tool");
    head.appendChild(name);
    if (data.elapsed !== undefined) {
      head.appendChild(el("span", { class: "result-elapsed" }, `${data.elapsed.toFixed(1)}s`));
    }
    head.appendChild(el("span", { class: "result-chevron" }, "⌄"));
    card.appendChild(head);

    const ok = !data.error;
    card.classList.add(ok ? "done" : "failed");

    // 尝试提取文件路径（write/bash 输出中的生成文件）
    const content = String(data.content || "");
    const filePath = extractFilePath(data.tool_name, content);

    if (filePath) {
      // 文件条目：点击打开
      const fileRow = el("div", { class: "result-file" });
      const namePart = el("span", { class: "result-filename" }, filePath.split(/[\\/]/).pop());
      fileRow.appendChild(namePart);
      fileRow.addEventListener("click", async () => {
        const res = await window.emrg.openFile({ filePath });
        if (res && res.ok === false) {
          // 打开失败提示（文件可能已删除）
          fileRow.title = res.error || (window.EMRG_I18N ? window.EMRG_I18N.t("panel.openFailed") : "无法打开文件");
        }
      });
      card.appendChild(fileRow);
    }

    // 内容摘要（可展开）
    const preview = content.slice(0, 200);
    if (preview.trim()) {
      const body = el("div", { class: "result-body hidden" }, preview);
      card.appendChild(body);
      // 复制按钮
      const copyBtn = el("button", { class: "result-copy", type: "button" }, _t("chat.copyCode"));
      copyBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        try {
          navigator.clipboard.writeText(content).then(() => {
            copyBtn.textContent = _t("panel.copied");
            setTimeout(() => { copyBtn.textContent = _t("chat.copyCode"); }, 1500);
          });
        } catch { /* clipboard unavailable */ }
      });
      body.appendChild(copyBtn);
      // 点击头部展开/收起
      head.addEventListener("click", () => {
        body.classList.toggle("hidden");
        card.classList.toggle("expanded", !body.classList.contains("hidden"));
      });
    }

    // 裁剪到 MAX_ITEMS
    while (list.children.length >= MAX_ITEMS) {
      list.removeChild(list.firstChild);
    }
    list.appendChild(card);
  }

  /** 从工具输出提取生成的文件路径（write 工具 / bash 中的 >> 重定向 / 路径模式） */
  function extractFilePath(toolName, content) {
    if (!content) return "";
    const lines = content.split("\n");
    if (toolName === "write" || toolName === "edit") {
      // 常见格式：Wrote file: /path 或 /path/to/file.txt
      const m = content.match(/(?:Wrote|wrote|Written|created|已写入|写入)[^\n:：]*[:：]\s*([^\s\n]+)/);
      if (m) return cleanPath(m[1]);
      const fm = content.match(/([^\s\n]+\/[^\s\n]+\.(?:py|js|ts|md|txt|json|yml|yaml|toml|sh|css|html|png|jpg|svg|log))/);
      if (fm) return cleanPath(fm[1]);
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
