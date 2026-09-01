"use strict";
/**
 * nav-policy.js — 主窗口外部导航策略（纯函数，便于单测）。
 *
 * 背景（rant 2026-09-01T20:33:44）：聊天区 markdown 经 marked 渲染出的 `<a href>`
 * 点击后在 Electron 主窗口内导航，整个应用变成网页、GUI 功能全部消失。修复 = 主窗口
 * 全局拦截 will-navigate + setWindowOpenHandler，外部 URL 一律交给系统浏览器。
 *
 * 加固（issue #1100，2026-09-02）：此前所有 file:// URL 一律放行，任意本地 HTML 可被
 * 加载进特权主窗口（继承 window.emrg 全量桥，52 invoke + onEvent，daemon 控制），构成
 * 本地文件提权向量。现只放行应用自身 renderer dist 前缀内的 file:// URL
 * （loadFile / reload / SPA 自导航），其余 file:// 一律 deny。
 *
 * 前缀匹配前先经 new URL() 规范化（WHATWG）：`..` / `%2e%2e` 点段在解析期即被折叠，
 * `file://localhost/...` 归一为 `file:///...`——从根上杜绝 `dist/../../etc/passwd`
 * 这类字符串前缀绕过的路径穿越。
 *
 * 策略：
 *  - "allow"          → rendererDistUrl 前缀内的 file:// 内部导航放行（应用自身资源）
 *  - "open-external"  → 拦截并交给 shell.openExternal（系统浏览器）
 *  - "deny"           → 拦截且不打开（空/非法 URL、应用 dist 之外的 file://）
 *
 * @param {string} url 待判定 URL
 * @param {string} [rendererDistUrl] 应用自身 renderer dist 的 file:// 前缀
 *   （形如 file:///.../renderer/dist/，须以 / 结尾）。缺省/空 → 所有 file:// 均 deny。
 */
function externalNavPolicy(url, rendererDistUrl) {
  if (typeof url !== "string" || url.length === 0) return "deny";
  if (url.toLowerCase().startsWith("file:")) {
    const prefix = typeof rendererDistUrl === "string" ? rendererDistUrl : "";
    if (prefix.length === 0) return "deny";
    let normalized;
    try {
      normalized = new URL(url).href;
    } catch {
      return "deny";
    }
    if (normalized.startsWith(prefix)) return "allow";
    return "deny";
  }
  return "open-external";
}

module.exports = { externalNavPolicy };
