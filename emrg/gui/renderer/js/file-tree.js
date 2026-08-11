"use strict";
/**
 * file-tree.js — 工作区文件浏览器（workspace panel P3.1，rant 2026-08-11T12:20:35）
 *
 * Tab「文件」内容：懒加载目录树。
 *   - 根 = 当前激活会话 projectPath（app.js 经 FileTree.setSession(sid, path) 注入）
 *   - 目录行点击 → emrg.listFiles(path) 懒加载子项（已加载目录缓存，折叠不重新拉取）
 *   - 文件行点击 → ResultPanel.openFileTab(sid, path)（P2.2 框架 Tab + P3.3 查看器）
 *   - 排序（目录在前）/5000 截断/符号链接不展开/绝对路径校验由 daemon 保证（P1 #661）
 * 切会话 → setSession 重设根 + 清缓存（per-session 隔离）
 */

const FileTree = (() => {
  let sid = null;
  let root = "";
  const cache = new Map(); // path -> { loaded, loading, error, entries }

  function container() { return $("result-files"); }
  function t(key) { return window.EMRG_I18N ? window.EMRG_I18N.t(key) : key; }

  function ensure(path) {
    let st = cache.get(path);
    if (!st) { st = { loaded: false, loading: false, error: false, entries: [] }; cache.set(path, st); }
    return st;
  }

  /** 设置会话根目录（切会话调用：重设根 + 清缓存） */
  function setSession(s, rootPath) {
    sid = s || null;
    setRoot(rootPath);
  }

  function setRoot(path) {
    root = path || "";
    cache.clear();
    render();
  }

  /** 拉取 + 渲染目录子项（st 为 ensure(path) 的缓存态） */
  async function expandDir(path, kidsEl, st) {
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
    for (const e of st.entries) kidsEl.appendChild(renderEntry(e));
  }

  function renderEntry(entry) {
    if (entry.type === "dir") {
      const row = el("div", { class: "ft-row ft-dir", dataset: { path: entry.path } });
      const arrow = el("span", { class: "ft-arrow" }, "▸");
      row.appendChild(arrow);
      row.appendChild(el("span", { class: "ft-name" }, entry.name));
      const kids = el("div", { class: "ft-kids hidden" });
      row.appendChild(kids);
      const st = ensure(entry.path);
      row.addEventListener("click", (e) => {
        if (e && e.stopPropagation) e.stopPropagation();
        toggleDir(entry.path, arrow, kids, st);
      });
      return row;
    }
    const row = el("div", { class: "ft-row ft-file", dataset: { path: entry.path } });
    row.appendChild(el("span", { class: "ft-name" }, entry.name));
    row.addEventListener("click", (e) => {
      if (e && e.stopPropagation) e.stopPropagation();
      openFile(entry.path);
    });
    return row;
  }

  async function toggleDir(path, arrow, kidsEl, st) {
    const wasHidden = kidsEl.classList.contains("hidden");
    if (wasHidden) {
      await expandDir(path, kidsEl, st);
      kidsEl.classList.remove("hidden");
      arrow.textContent = "▾";
    } else {
      kidsEl.classList.add("hidden");
      arrow.textContent = "▸";
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
    const rootRow = el("div", { class: "ft-row ft-dir ft-root", dataset: { path: root } });
    rootRow.appendChild(el("span", { class: "ft-arrow" }, "▾"));
    rootRow.appendChild(el("span", { class: "ft-name" }, rootName));
    const kids = el("div", { class: "ft-kids" });
    rootRow.appendChild(kids);
    c.appendChild(rootRow);
    // 根自动展开（fire-and-forget）
    expandDir(root, kids, ensure(root));
  }

  return { setRoot, setSession };
})();

// ⚠️ 必须暴露到 window（app.js 独立 <script> 加载，模块级 const 不跨 script 共享）
window.FileTree = FileTree;
