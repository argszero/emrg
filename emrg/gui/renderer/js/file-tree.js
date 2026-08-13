"use strict";
/**
 * file-tree.js — 工作区文件浏览器（workspace panel P3.1，rant 2026-08-11T12:20:35；
 * VS Code 风格 UI 对齐，rant 2026-08-12T17:28:19）
 *
 * Tab「文件」内容：懒加载目录树，视觉对齐 VS Code Explorer：
 *   - 根 = 当前激活会话 projectPath（app.js 经 FileTree.setSession(sid, path) 注入）
 *   - 目录行点击 → emrg.listFiles(path) 懒加载子项（已加载目录缓存，折叠不重新拉取）
 *   - 文件行点击 → ResultPanel.openFileTab(sid, path)（P2.2 框架 Tab + P3.3 查看器）
 *   - 文件类型图标（mono 内联 SVG，fill=currentColor 跟随文字色）：目录折叠/展开
 *     文件夹图标 + 按扩展名映射文件图标（js/ts/py/md/json/css/html/yml/图片/文本），
 *     默认通用文件图标
 *   - 递归渲染 + 深度缩进（每层 padding-left = 8 + depth*16，行内 --indent CSS 变量）
 *   - 选中态：文件行点击 → .active（背景 --bg-soft + 左侧 2px accent 竖条，单选）
 *   - 紧凑 24px 行高 + 超长名省略号（.ft-name ellipsis）
 *   - 缩进引导线（.ft-kids::before 1px 竖线，嵌套结构天然对齐父箭头列）
 *   - 展开态持久：Map<path, expanded>（重新 render 不丢，切会话清空）
 *   - 滚动条 hover 显示（.result-files overlay thumb，默认透明）
 *   - 排序（目录在前）/5000 截断/符号链接不展开/绝对路径校验由 daemon 保证（P1 #661）
 * 切会话 → setSession 重设根 + 清缓存（per-session 隔离）
 */

const FileTree = (() => {
  let sid = null;
  let root = "";
  const cache = new Map(); // path -> { loaded, loading, error, entries }
  const expanded = new Map(); // path -> bool（展开态持久，VS Code 行为）
  let selectedPath = null; // 当前选中文件行（单选）

  function container() { return $("result-files"); }
  function t(key) { return window.EMRG_I18N ? window.EMRG_I18N.t(key) : key; }

  // ── mono 内联 SVG 图标（16x16 viewBox，fill: currentColor 跟随文字色） ──
  const ICON = {
    dirClosed: '<path d="M1.5 2.5h4.1c.32 0 .62.13.84.36l1.1 1.14h6.96c.83 0 1.5.67 1.5 1.5V12.5c0 .83-.67 1.5-1.5 1.5H1.5A1.5 1.5 0 0 1 0 12.5V4c0-.83.67-1.5 1.5-1.5z"/>',
    dirOpen: '<path d="M1.5 2.5h4.1c.32 0 .62.13.84.36l1.1 1.14h6.96c.83 0 1.5.67 1.5 1.5V6H2.1c-.6 0-1.13.36-1.34.92L0 12.8V4c0-.83.67-1.5 1.5-1.5z"/><path d="M14.6 6.5H2.3c-.42 0-.8.27-.94.66L0 13.5h14.5c.83 0 1.5-.67 1.5-1.5V8c0-.83-.67-1.5-1.5-1.5z"/>',
    fileDefault: '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    fileImg: '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="6" cy="6.5" r="1.2"/><path d="M4.5 12.5l2.5-2.5 2 2 1.5-1.5 1.5 2z"/>',
    fileMd: '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M4.5 6h7M4.5 8.5h7M4.5 11h4.5"/>',
    fileJson: '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M7.2 6.5L5.5 8.5l1.7 2M8.8 6.5l1.7 2-1.7 2"/>',
    fileCode: '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6.5 6.5L4.5 8.5l2 2M9.5 6.5l2 2-2 2"/>',
    fileYml: '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M5 6.5h.01M5 9h.01M5 11.5h.01M8 6.5h3M8 9h3M8 11.5h3"/>',
    fileTxt: '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M4.5 6h7M4.5 8.5h7M4.5 11h7"/>',
  };
  const EXT_ICON = {
    png: "fileImg", jpg: "fileImg", jpeg: "fileImg", gif: "fileImg", svg: "fileImg",
    webp: "fileImg", bmp: "fileImg", ico: "fileImg",
    md: "fileMd", markdown: "fileMd",
    json: "fileJson",
    js: "fileCode", jsx: "fileCode", mjs: "fileCode", cjs: "fileCode",
    ts: "fileCode", tsx: "fileCode",
    html: "fileCode", htm: "fileCode", css: "fileCode", scss: "fileCode",
    py: "fileCode", rb: "fileCode", go: "fileCode", rs: "fileCode", java: "fileCode", c: "fileCode", cpp: "fileCode",
    yml: "fileYml", yaml: "fileYml", toml: "fileYml", cfg: "fileYml", ini: "fileYml",
    txt: "fileTxt", log: "fileTxt", csv: "fileTxt", tsv: "fileTxt",
  };

  function iconSvg(d) {
    const svg = el("svg", { class: "ft-icon", viewBox: "0 0 16 16", width: 14, height: 14, "aria-hidden": "true" });
    svg.innerHTML = d; // 静态白名单路径，无注入风险
    return svg;
  }
  function iconFor(entry) {
    if (entry.type === "dir") return expanded.get(entry.path) ? ICON.dirOpen : ICON.dirClosed;
    const ext = String(entry.name).split(".").pop().toLowerCase();
    return ICON[EXT_ICON[ext] || "fileDefault"];
  }

  function ensure(path) {
    let st = cache.get(path);
    if (!st) { st = { loaded: false, loading: false, error: false, entries: [] }; cache.set(path, st); }
    return st;
  }

  /** 设置会话根目录（切会话调用：重设根 + 清缓存 + 清展开态/选中态） */
  function setSession(s, rootPath) {
    sid = s || null;
    setRoot(rootPath);
  }

  function setRoot(path) {
    root = path || "";
    cache.clear();
    expanded.clear();
    selectedPath = null;
    render();
  }

  /** 拉取 + 渲染目录子项（st 为 ensure(path) 的缓存态；childDepth = 子项缩进层级） */
  async function expandDir(path, kidsEl, st, childDepth) {
    if (!st.loaded && !st.loading) {
      st.loading = true;
      kidsEl.innerHTML = "";
      kidsEl.appendChild(el("div", { class: "ft-hint" }, t("result.treeLoading")));
      try {
        const res = await window.emrg.listFiles({ path });
        st.entries = (res && res.entries) || [];
        st.loaded = true;
        st.error = false;
      } catch {
        st.entries = [];
        st.loaded = true;
        st.error = true;
      } finally {
        st.loading = false;
      }
    }
    kidsEl.innerHTML = "";
    if (st.error) {
      kidsEl.appendChild(el("div", { class: "ft-hint" }, t("result.treeLoadFailed")));
      return;
    }
    for (const e of st.entries) kidsEl.appendChild(renderEntry(e, childDepth));
  }

  /** 渲染一条目行（depth 相对其父容器；padding-left = 8 + depth*16 对齐箭头列） */
  function renderEntry(entry, depth) {
    const row = el("div", {
      class: "ft-row" + (entry.type === "dir" ? " ft-dir" : " ft-file"),
      dataset: { path: entry.path },
    });
    row.style.paddingLeft = (8 + depth * 16) + "px";
    // 行头（图标+名称）单独包装：.ft-row 为块级容器（head + kids 纵向排布），
    // 修复定高 flex-wrap 下兄弟行与展开子项重叠的布局 bug（headless Chrome 像素实证）
    const head = el("div", { class: "ft-head" });
    head.appendChild(iconSvg(iconFor(entry)));
    head.appendChild(el("span", { class: "ft-name" }, entry.name));
    row.appendChild(head);
    if (entry.type === "dir") {
      const kids = el("div", { class: "ft-kids" + (expanded.get(entry.path) ? "" : " hidden") });
      row.appendChild(kids);
      const st = ensure(entry.path);
      row.addEventListener("click", (e) => {
        if (e && e.stopPropagation) e.stopPropagation();
        toggleDir(entry.path, kids, st, depth + 1);
      });
      if (expanded.get(entry.path) && !st.loaded) expandDir(entry.path, kids, st, depth + 1);
    } else {
      if (selectedPath === entry.path) row.classList.add("active");
      row.addEventListener("click", (e) => {
        if (e && e.stopPropagation) e.stopPropagation();
        selectFile(entry.path);
        openFile(entry.path);
      });
    }
    return row;
  }

  /** 目录展开/折叠：状态持久到 expanded Map（VS Code 行为，重新 render 不丢） */
  async function toggleDir(path, kidsEl, st, childDepth) {
    const wasHidden = kidsEl.classList.contains("hidden");
    if (wasHidden) {
      expanded.set(path, true);
      await expandDir(path, kidsEl, st, childDepth);
      kidsEl.classList.remove("hidden");
    } else {
      expanded.set(path, false);
      kidsEl.classList.add("hidden");
    }
    // 目录行图标随展开态切换（重新渲染子行不影响兄弟行）
    const row = kidsEl.parentNode;
    if (row) {
      const ic = row.querySelector(".ft-icon");
      if (ic) ic.innerHTML = expanded.get(path) ? ICON.dirOpen : ICON.dirClosed;
    }
  }

  /** 文件行选中（单选）：去旧 active 加新 active */
  function selectFile(path) {
    const c = container();
    if (!c) return;
    const rows = c.querySelectorAll(".ft-file");
    for (const r of rows) {
      if (r.dataset && r.dataset.path === selectedPath) r.classList.remove("active");
    }
    selectedPath = path;
    for (const r of rows) {
      if (r.dataset && r.dataset.path === path) r.classList.add("active");
    }
  }

  function openFile(path) {
    if (window.ResultPanel && window.ResultPanel.openFileTab) {
      window.ResultPanel.openFileTab(sid, path);
    }
  }

  function render() {
    const c = container();
    if (!c) return;
    c.innerHTML = "";
    if (!root) {
      c.appendChild(el("div", { class: "result-empty" }, t("result.filesEmpty")));
      return;
    }
    const rootName = String(root).split(/[\\/]/).filter(Boolean).pop() || root;
    expanded.set(root, true); // 根默认展开
    const rootRow = el("div", { class: "ft-row ft-dir ft-root", dataset: { path: root } });
    rootRow.style.paddingLeft = "8px";
    const rootHead = el("div", { class: "ft-head" });
    rootHead.appendChild(iconSvg(ICON.dirOpen));
    rootHead.appendChild(el("span", { class: "ft-name" }, rootName));
    rootRow.appendChild(rootHead);
    const kids = el("div", { class: "ft-kids" });
    rootRow.appendChild(kids);
    // 根目录行与普通目录行一致的可展开/收起（rant 2026-08-13T12:47:18：render() 手写根行漏绑
    // click → 点击根目录无反应永远展开；toggleDir 已负责 expanded Map 持久 + 图标切换）
    const st = ensure(root);
    rootRow.addEventListener("click", (e) => {
      if (e && e.stopPropagation) e.stopPropagation();
      toggleDir(root, kids, st, 1);
    });
    c.appendChild(rootRow);
    // 根自动展开（fire-and-forget；根子项 depth=1）
    expandDir(root, kids, st, 1);
  }

  return { setRoot, setSession };
})();

// ⚠️ 必须暴露到 window（app.js 独立 <script> 加载，模块级 const 不跨 script 共享）
window.FileTree = FileTree;
