"use strict";
/**
 * utils.js — 通用 DOM / 工具助手
 */

/** getElementById 简写 */
const $ = (id) => document.getElementById(id);

/** 创建元素并设置属性/文本 */
function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

/** HTML 转义 */
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * G143：生成 requestId。
 * secure context 下 crypto.randomUUID；低版本兜底。
 */
function genRequestId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

/** rant 21:19：i18n 取词（i18n.js 缺失时回退 key 本身） */
function _t(key, params) {
  try {
    if (window.EMRG_I18N) return window.EMRG_I18N.t(key, params);
  } catch { /* ignore */ }
  return key;
}

/** 主题应用：system 时移除 data-theme（CSS 跟随 prefers-color-scheme） */
function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "system" || !mode) {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", mode);
  }
}

/**
 * 通用 toast（rant 2026-08-15T09:20:27：面板操作反馈全局可见——tasks/settings
 * 面板视图下聊天流不可见，Chat.addSystemMessage 的反馈=看不见的"没反应"）。
 * 右上角短暂显示、自动消失；type: success|error|info 决定左侧色条。
 */
let _toastTimer = null;
function showToast(message, opts = {}) {
  const { type = "info", durationMs = 3000 } = opts;
  const toast = document.getElementById("toast");
  if (!toast) return;
  const msg = document.getElementById("toast-msg");
  if (msg) msg.textContent = message;
  // 逐个 remove（沙箱 classList.remove 单参；DOM 语义等价）
  ["hidden", "toast-success", "toast-error", "toast-info"].forEach((c) => toast.classList.remove(c));
  toast.classList.add("toast-" + type);
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => toast.classList.add("hidden"), durationMs);
}

/**
 * 相对时间（P6 验收补完：项目行"最近活跃"提示，消费 daemon list_projects 的
 * latest_session_at）。ISO 时间串 → "刚刚 / {n} 分钟前 / {n} 小时前 / {n} 天前"，
 * 走 i18n（zh/en）；缺失/非法输入返回空串（调用点自行隐藏）。
 */
function relTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffMin = Math.floor((Date.now() - then) / 60000);
  const t = (window.EMRG_I18N && window.EMRG_I18N.t) || ((k) => k);
  if (diffMin < 1) return t("relTime.justNow");
  if (diffMin < 60) return t("relTime.minutesAgo", { n: diffMin });
  const hrs = Math.floor(diffMin / 60);
  if (hrs < 24) return t("relTime.hoursAgo", { n: hrs });
  const days = Math.floor(hrs / 24);
  return t("relTime.daysAgo", { n: days });
}

// 显式挂载到 window（多 script 顺序加载共享全局，显式挂载更健壮）
window.$ = $;
window.el = el;
window.escapeHtml = escapeHtml;
window.genRequestId = genRequestId;
window.applyTheme = applyTheme;
window.relTime = relTime;
window.showToast = showToast;
