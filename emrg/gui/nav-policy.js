"use strict";
/**
 * nav-policy.js — 主窗口外部导航策略（纯函数，便于单测）。
 *
 * 背景（rant 2026-09-01T20:33:44）：聊天区 markdown 经 marked 渲染出的 `<a href>`
 * 点击后在 Electron 主窗口内导航，整个应用变成网页、GUI 功能全部消失。修复 = 主窗口
 * 全局拦截 will-navigate + setWindowOpenHandler，外部 URL 一律交给系统浏览器。
 *
 * 策略：
 *  - "allow"          → file:// 内部导航放行（loadFile / reload / SPA 自身）
 *  - "open-external"  → 拦截并交给 shell.openExternal（系统浏览器）
 *  - "deny"           → 拦截且不打开（空/非法 URL）
 */
function externalNavPolicy(url) {
  if (typeof url !== "string" || url.length === 0) return "deny";
  if (url.startsWith("file:")) return "allow";
  return "open-external";
}

module.exports = { externalNavPolicy };
