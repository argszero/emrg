"use strict";
/**
 * sidebar.js — 对话列表：时间分组（今天/昨天/更早）、标题显示、右键重命名/删除、折叠（⌘B）。
 * 去黑话：只显示标题，不显示 session ID / 消息数。
 */

const Sidebar = (() => {
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
      item.addEventListener("click", () => App.switchSession(entry.sid, { scopeNav: nav }));
      item.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        App.showOpenSessionsMenu(item, entry);
      });
      nav.appendChild(item);
    }
    highlight(App.state.sessionId);
  }

  /** 会话条目统一格式：有 name 显示 project/name，无 name 显示 project/id（rant 2026-08-20T22:04:57） */
  function sessionLabel(project, title, sid) {
    return title ? `${project}/${title}` : `${project}/${sid}`;
  }

  /** 高亮当前会话条目（rant 11:44:52：历史会话列表已移除，仅剩打开会话区） */
  function highlight(sid, navEl) {
    const targets = navEl ? [navEl] : [$("open-sessions")];
    for (const nav of targets) {
      if (!nav) continue;
      for (const item of nav.querySelectorAll(".conv-item")) {
        item.classList.toggle("active", item.dataset.sid === sid);
      }
    }
  }

  return { renderOpenSessions, highlight };
})();

window.EMRG_Sidebar = Sidebar;
