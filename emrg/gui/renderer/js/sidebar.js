"use strict";
/**
 * sidebar.js — 对话列表：时间分组（今天/昨天/更早）、标题显示、右键重命名/删除、折叠（⌘B）。
 * 去黑话：只显示标题，不显示 session ID / 消息数。
 */

const Sidebar = (() => {
  let sessions = [];
  let openSessions = [];

  /**
   * P4 slice 2：渲染跨项目打开会话区（侧边栏顶部）。
   * 条目 = 项目名 / 会话标题（lastActive 倒序，main 已排序）；激活高亮；
   * 点击 → 切换；右键 → 关闭（保留数据）/ 重命名 / 删除。
   */
  function renderOpenSessions(list) {
    openSessions = list || [];
    const nav = $("open-sessions");
    const label = $("open-sessions-label");
    if (!nav) return;
    nav.innerHTML = "";
    if (!openSessions.length) {
      if (label) label.hidden = true;
      return;
    }
    if (label) label.hidden = false;
    const known = (App.state && App.state.sessions) || [];
    for (const entry of openSessions) {
      const cur = known.find((s) => s.session_id === entry.sid) || {};
      const title = entry.title || cur.title || ""; // entry.title 优先（跨项目），再 cur.title；id 单独显示，不降级为 sid
      const item = el("div", { class: "conv-item open-session-item" });
      item.dataset.sid = entry.sid;
      item.appendChild(el("span", { class: "conv-title" }, sessionLabel(entry.projectName || "", title, entry.sid)));
      item.addEventListener("click", () => App.switchSession(entry.sid));
      item.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        App.showOpenSessionsMenu(item, entry);
      });
      nav.appendChild(item);
    }
    highlight(App.state.sessionId);
  }

  /** 会话条目统一格式 project/name|id（rant 2026-08-20T17:48:07 三处统一） */
  function sessionLabel(project, title, sid) {
    return `${project}/${title}|${sid}`;
  }

  /** cwd 末段作项目名（Path(s.cwd).name 语义，兼容 \\ 与 /） */
  function cwdProjectName(cwd) {
    if (!cwd) return "";
    const norm = String(cwd).replace(/\\/g, "/").replace(/\/+$/, "");
    const seg = norm.split("/");
    return seg[seg.length - 1] || "";
  }

  /** 渲染会话列表（rant 17:48:07：去掉今天/昨天/更早分组，按最后活跃倒序，project/name|id） */
  function render(list) {
    sessions = list || [];
    const nav = $("conv-list");
    nav.innerHTML = "";
    if (!sessions.length) {
      nav.appendChild(el("div", { class: "conv-item placeholder" }, EMRG_Copy.COPY.noSessions));
      return;
    }
    const sorted = [...sessions].sort((a, b) =>
      String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || "")));
    for (const s of sorted) {
      const item = el("div", { class: "conv-item" });
      item.dataset.sid = s.session_id;
      const title = s.title || ""; // G27：title 优先，无 title 则空（id 已单独显示）
      const project = cwdProjectName(s.cwd);
      item.appendChild(el("span", { class: "conv-title" }, sessionLabel(project, title, s.session_id)));
      item.addEventListener("click", () => App.switchSession(s.session_id));
      // 右键菜单：重命名 / 删除（友好确认）
      item.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        App.showConvMenu(item, s.session_id, title || s.session_id);
      });
      nav.appendChild(item);
    }
    highlight(App.state.sessionId);
  }

  /** 高亮当前对话（含打开会话区） */
  function highlight(sid) {
    for (const nav of [$("conv-list"), $("open-sessions")]) {
      if (!nav) continue;
      for (const item of nav.querySelectorAll(".conv-item")) {
        item.classList.toggle("active", item.dataset.sid === sid);
      }
    }
  }

  // ── 键盘导航：↑↓ 切换高亮 / Enter 切换会话（与 TUI /resume 选择器一致）──
  let _keyHandler = null;
  let _focusIdx = -1;

  function initKeyboard() {
    const nav = $("conv-list");
    _keyHandler = (e) => {
      // 输入控件（textarea/input/select/contenteditable）内不劫持 ↑↓/Enter——
      // 输入框是 <textarea id="input">，多行输入需 ↑↓ 移动光标、Shift+Enter 换行
      if (e.target.closest("input, textarea, select, [contenteditable]")) return;
      const items = nav.querySelectorAll(".conv-item");
      if (!items.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (_focusIdx < 0) {
          // 未聚焦：从当前会话开始
          _focusIdx = items.findIndex((it) => it.classList.contains("active"));
          if (_focusIdx < 0) _focusIdx = 0;
        }
        _focusIdx = e.key === "ArrowDown"
          ? (_focusIdx + 1) % items.length
          : (_focusIdx - 1 + items.length) % items.length;
        items.forEach((it, j) => it.classList.toggle("kbd-focus", j === _focusIdx));
        items[_focusIdx].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter" && _focusIdx >= 0) {
        e.preventDefault();
        const target = items[_focusIdx];
        if (target.dataset.sid) App.switchSession(target.dataset.sid);
        clearFocus();
      } else if (e.key === "Escape") {
        clearFocus();
      }
    };
    document.addEventListener("keydown", _keyHandler);
  }

  function clearFocus() {
    _focusIdx = -1;
    for (const it of $("conv-list").querySelectorAll(".kbd-focus")) {
      it.classList.remove("kbd-focus");
    }
  }

  function init() {
    if (!_keyHandler) initKeyboard();
  }
  init(); // 模块级绑定一次

  return { render, renderOpenSessions, highlight, clearFocus };
})();

window.EMRG_Sidebar = Sidebar;
