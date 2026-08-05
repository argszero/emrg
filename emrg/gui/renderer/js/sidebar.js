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

  return { render, highlight };
})();

window.EMRG_Sidebar = Sidebar;
