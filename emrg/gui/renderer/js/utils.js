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

/** 会话时间分组：今天 / 昨天 / 更早 */
function groupLabel(ts) {
  if (!ts) return _t("util.groupEarlier");
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return _t("util.groupEarlier");
  const now = new Date();
  const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
  if (dayDiff <= 0) return _t("util.groupToday");
  if (dayDiff === 1) return _t("util.groupYesterday");
  return _t("util.groupEarlier");
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

// 显式挂载到 window（多 script 顺序加载共享全局，显式挂载更健壮）
window.$ = $;
window.el = el;
window.escapeHtml = escapeHtml;
window.genRequestId = genRequestId;
window.groupLabel = groupLabel;
window.applyTheme = applyTheme;
