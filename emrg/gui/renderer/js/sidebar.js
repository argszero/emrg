"use strict";
/**
 * sidebar.js — 侧边栏：会话时间分组 / 右键菜单 / 重命名 / 删除确认 / 折叠
 * 设计：docs/design/gui-redesign.md §3.2 侧边栏 + §5.1 文案规范（友好确认，零黑话）
 * 依赖：app.js 的 state / renderSessions / switchSession / refreshSessions（反向依赖：app.js 调用本文件函数）
 */

// ── 时间分组 ─────────────────────────────────────────────

/** 按 created_at 分三组：今天 / 昨天 / 更早。返回 [ [组名, sessions[]], ... ]（保留降序） */
function groupSessionsByTime(sessions) {
  const groups = { 今天: [], 昨天: [], 更早: [] };
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfToday.getDate() - 1);

  for (const s of sessions || []) {
    const ts = s.created_at ? new Date(s.created_at) : null;
    if (!ts || isNaN(ts)) {
      groups["更早"].push(s); // 无时间戳 → 更早兜底
      continue;
    }
    if (ts >= startOfToday) groups["今天"].push(s);
    else if (ts >= startOfYesterday) groups["昨天"].push(s);
    else groups["更早"].push(s);
  }
  return [["今天", groups["今天"]], ["昨天", groups["昨天"]], ["更早", groups["更早"]]];
}

// ── 确认对话框（友好文案，替代 confirm()）───────────────

let _confirmCallback = null;

function showConfirmDialog({ title, desc, okText = "确认", danger = true }) {
  const dlg = document.getElementById("confirm-dialog");
  document.getElementById("confirm-title").textContent = title;
  document.getElementById("confirm-desc").textContent = desc;
  const ok = document.getElementById("confirm-ok");
  ok.textContent = okText;
  ok.className = danger ? "danger" : "primary";
  _confirmCallback = null;
  dlg.showModal();
  return new Promise((resolve) => {
    _confirmCallback = resolve;
  });
}

function _confirmResolve(result) {
  const dlg = document.getElementById("confirm-dialog");
  if (dlg.open) dlg.close();
  if (_confirmCallback) {
    const cb = _confirmCallback;
    _confirmCallback = null;
    cb(result);
  }
}

// ── 右键菜单 ─────────────────────────────────────────────

function showContextMenu(sessionId, x, y) {
  const menu = document.getElementById("ctx-menu");
  menu.innerHTML = "";
  const items = [
    { label: "✏️ 重命名", action: () => openRenameDialog(sessionId) },
    { label: "🗑 删除对话", action: () => requestDeleteSession(sessionId), danger: true },
  ];
  for (const it of items) {
    const el = document.createElement("div");
    el.className = "ctx-item" + (it.danger ? " danger" : "");
    el.textContent = it.label;
    el.addEventListener("click", () => {
      hideContextMenu();
      it.action();
    });
    menu.appendChild(el);
  }
  menu.hidden = false;
  // 定位：限制在视口内
  const mw = 160, mh = items.length * 40 + 8;
  menu.style.left = Math.min(x, window.innerWidth - mw - 8) + "px";
  menu.style.top = Math.min(y, window.innerHeight - mh - 8) + "px";
}

function hideContextMenu() {
  const menu = document.getElementById("ctx-menu");
  menu.hidden = true;
}

// ── 重命名 ───────────────────────────────────────────────

let _renameSessionId = null;

async function openRenameDialog(sessionId) {
  _renameSessionId = sessionId;
  const input = document.getElementById("rename-input");
  const s = (state.sessions || []).find((x) => x.session_id === sessionId);
  input.value = s && s.title ? s.title : "";
  document.getElementById("rename-dialog").showModal();
  input.focus();
  input.select();
}

async function submitRename() {
  if (!_renameSessionId) return;
  const input = document.getElementById("rename-input");
  const title = input.value.trim();
  if (!title) {
    input.focus();
    return;
  }
  try {
    await window.emrg.renameSession({ sessionId: _renameSessionId, title });
  } catch (e) {
    addSystemMessage(`重命名失败: ${e.message}`);
  }
  document.getElementById("rename-dialog").close();
  _renameSessionId = null;
  await refreshSessions();
}

// ── 删除（友好确认）──────────────────────────────────────

async function requestDeleteSession(sessionId) {
  const ok = await showConfirmDialog({
    title: "删除这段对话？",
    desc: "删除后无法恢复。",
    okText: "删除",
    danger: true,
  });
  if (!ok) return;
  try {
    await window.emrg.deleteSession({ sessionId });
  } catch (e) {
    addSystemMessage(`删除失败: ${e.message}`);
    return;
  }
  // 若删除的是当前会话 → 切到剩余最近会话（app.js 逻辑），否则仅刷新列表
  if (state.sessionId === sessionId) {
    const remaining = (state.sessions || []).filter((s) => s.session_id !== sessionId);
    if (remaining.length > 0) {
      await switchSession(remaining[0].session_id, { silent: true });
    } else {
      await newSession();
    }
  }
  await refreshSessions();
}

// ── 折叠侧边栏 ───────────────────────────────────────────

let sidebarCollapsed = false;

function toggleSidebar() {
  sidebarCollapsed = !sidebarCollapsed;
  document.getElementById("sidebar").classList.toggle("collapsed", sidebarCollapsed);
  const btn = document.getElementById("sidebar-toggle");
  btn.textContent = sidebarCollapsed ? "▸" : "◂";
  btn.title = sidebarCollapsed ? "展开侧边栏 (⌘B)" : "折叠侧边栏 (⌘B)";
}

// ── 事件绑定 ─────────────────────────────────────────────

function bindSidebarUi() {
  const menu = document.getElementById("ctx-menu");

  // 右键：会话项 → 自定义菜单；其他区域 → 隐藏
  document.addEventListener("contextmenu", (e) => {
    const item = e.target.closest(".session-item");
    if (item && item.dataset.sid) {
      e.preventDefault();
      showContextMenu(item.dataset.sid, e.clientX, e.clientY);
    } else if (!menu.hidden) {
      hideContextMenu();
    }
  });

  // 左键点击空白 → 隐藏菜单
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target)) hideContextMenu();
  });

  // ESC → 隐藏菜单 / 关闭对话框
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      hideContextMenu();
    }
    // ⌘B / Ctrl+B 折叠侧边栏
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "b") {
      e.preventDefault();
      toggleSidebar();
    }
  });

  // 确认对话框
  document.getElementById("confirm-cancel").addEventListener("click", () => _confirmResolve(false));
  document.getElementById("confirm-ok").addEventListener("click", () => _confirmResolve(true));

  // 重命名对话框
  document.getElementById("rename-cancel").addEventListener("click", () => {
    document.getElementById("rename-dialog").close();
    _renameSessionId = null;
  });
  document.getElementById("rename-ok").addEventListener("click", submitRename);
  document.getElementById("rename-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submitRename(); }
  });

  // 折叠按钮
  document.getElementById("sidebar-toggle").addEventListener("click", toggleSidebar);
}

bindSidebarUi();
