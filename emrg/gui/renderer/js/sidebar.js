"use strict";
/**
 * sidebar.js — 对话列表：时间分组（今天/昨天/更早）、标题显示、右键重命名/删除、折叠（⌘B）。
 * 去黑话：只显示标题，不显示 session ID / 消息数。
 */

const Sidebar = (() => {
  let sessions = [];

  /** 渲染分组对话列表 */
  function render(list) {
    sessions = list || [];
    const nav = $("conv-list");
    nav.innerHTML = "";
    if (!sessions.length) {
      nav.appendChild(el("div", { class: "conv-item placeholder" }, EMRG_Copy.COPY.noSessions));
      return;
    }
    const groups = { 今天: [], 昨天: [], 更早: [] };
    for (const s of sessions) {
      const g = groupLabel(s.updated_at || s.created_at);
      if (!groups[g]) groups[g] = [];
      groups[g].push(s);
    }
    for (const [label, items] of Object.entries(groups)) {
      if (!items.length) continue;
      nav.appendChild(el("div", { class: "conv-group-label" }, label));
      for (const s of items) {
        const item = el("div", { class: "conv-item" });
        item.dataset.sid = s.session_id;
        const title = s.title || s.session_id; // G27：title 优先
        item.appendChild(el("span", { class: "conv-title" }, title));
        item.addEventListener("click", () => App.switchSession(s.session_id));
        // 右键菜单：重命名 / 删除（友好确认）
        item.addEventListener("contextmenu", (e) => {
          e.preventDefault();
          App.showConvMenu(item, s.session_id, title);
        });
        nav.appendChild(item);
      }
    }
    highlight(App.state.sessionId);
  }

  /** 高亮当前对话 */
  function highlight(sid) {
    for (const item of $("conv-list").querySelectorAll(".conv-item")) {
      item.classList.toggle("active", item.dataset.sid === sid);
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

  return { render, highlight, clearFocus };
})();

window.EMRG_Sidebar = Sidebar;
