"use strict";
/**
 * error-boundary.js — 渲染进程全局错误边界（rant 2026-08-25T21:13:18）。
 *
 * 最小侵入设计：不包裹/改写任何现有模块，只在全局监听层兜底。
 * - 独立文件，最先加载（排在 utils.js 之前）；挂 window "error" + "unhandledrejection" 两个全局监听
 * - 捕获未处理错误：保留 console.error 原样日志 → 显示 fixed 全屏覆盖层（平时 hidden 零干扰）
 * - 覆盖层：标题「渲染器出错」+ 错误摘要（截断 500 字符）+ 时间戳 + 两按钮（复制错误 / 重新加载）
 * - 不做自动重载（防崩溃循环，参考 grok-bot error-boundary 只提供手动按钮）
 * - 监听器不阻止错误传播（不 preventDefault / 不返回 false），页面其余行为不变
 * - 错误 handler 自身全程 try/catch，绝不在错误处理里再抛错
 * - i18n 双语（zh/en）：show 时经 window.EMRG_I18N.t 解析（i18n.js 已加载）；
 *   若 i18n 未就绪则用内建 fallback 词典（navigator.language 判定）
 * - 覆盖层 z-index 置顶；非错误时 DOM 隐藏，不拦截现有 UI 事件
 *
 * DOM 获取统一走 getElementById（存在则复用）|| createElement（兜底创建），
 * 使真实 DOM 与 vm 沙箱（getElementById 返回稳定元素）行为一致。
 */

(function () {
  const OVERLAY_ID = "error-boundary-overlay";
  const TITLE_ID = "error-boundary-title";
  const SUMMARY_ID = "error-boundary-summary";
  const STAMP_ID = "error-boundary-timestamp";
  const COPY_ID = "error-boundary-copy";
  const RELOAD_ID = "error-boundary-reload";
  const MAX_SUMMARY = 500;

  // ── i18n fallback（window.EMRG_I18N 未就绪时兜底；正常路径走 EMRG_I18N.t） ──
  const FALLBACK_DICTS = {
    zh: {
      "errorBoundary.title": "渲染器出错",
      "errorBoundary.copy": "复制错误",
      "errorBoundary.reload": "重新加载",
    },
    en: {
      "errorBoundary.title": "Renderer error",
      "errorBoundary.copy": "Copy error",
      "errorBoundary.reload": "Reload",
    },
  };

  let overlay = null;

  /** 取词：优先 window.EMRG_I18N（i18n.js 已加载），否则内建 fallback。 */
  function _t(key) {
    try {
      if (typeof window !== "undefined" && window.EMRG_I18N && window.EMRG_I18N.t) {
        const s = window.EMRG_I18N.t(key);
        if (s && s !== key) return s;
      }
    } catch { /* fall through */ }
    const lang = (typeof navigator !== "undefined" && navigator.language || "").toLowerCase().startsWith("zh")
      ? "zh" : "en";
    const dict = FALLBACK_DICTS[lang] || FALLBACK_DICTS.en;
    return dict[key] || key;
  }

  /** 取或建元素（getElementById 优先，createElement 兜底）。 */
  function _getOrCreate(id, tag) {
    try {
      const existing = document.getElementById(id);
      if (existing) return existing;
    } catch { /* fall through */ }
    const el = document.createElement(tag || "div");
    el.id = id;
    return el;
  }

  /** 构建覆盖层 DOM（惰性，仅首次出错时创建；失败则仅保留日志）。 */
  function _ensureOverlay() {
    if (overlay) return overlay;
    try {
      if (typeof document === "undefined") return null;
      overlay = _getOrCreate(OVERLAY_ID, "div");
      overlay.style.position = "fixed";
      overlay.style.inset = "0";
      overlay.style.zIndex = "2147483647";
      overlay.style.display = "none";
      overlay.style.background = "rgba(0, 0, 0, 0.82)";
      overlay.style.color = "#fff";
      overlay.style.fontFamily = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
      overlay.style.padding = "40px";
      overlay.style.boxSizing = "border-box";
      overlay.style.overflow = "auto";

      const title = _getOrCreate(TITLE_ID, "div");
      title.style.fontSize = "22px";
      title.style.fontWeight = "600";
      title.style.marginBottom = "16px";
      overlay.appendChild(title);

      const summary = _getOrCreate(SUMMARY_ID, "pre");
      summary.style.whiteSpace = "pre-wrap";
      summary.style.wordBreak = "break-word";
      summary.style.fontSize = "13px";
      summary.style.lineHeight = "1.5";
      summary.style.maxHeight = "50vh";
      summary.style.overflow = "auto";
      summary.style.background = "rgba(255, 255, 255, 0.08)";
      summary.style.padding = "12px";
      summary.style.borderRadius = "6px";
      overlay.appendChild(summary);

      const stamp = _getOrCreate(STAMP_ID, "div");
      stamp.style.fontSize = "12px";
      stamp.style.opacity = "0.7";
      stamp.style.marginTop = "12px";
      overlay.appendChild(stamp);

      const actions = document.createElement("div");
      actions.style.marginTop = "20px";
      actions.style.display = "flex";
      actions.style.gap = "12px";

      const copyBtn = _getOrCreate(COPY_ID, "button");
      copyBtn.type = "button";
      copyBtn.textContent = _t("errorBoundary.copy");
      copyBtn.style.padding = "8px 16px";
      copyBtn.style.borderRadius = "6px";
      copyBtn.style.border = "1px solid rgba(255,255,255,0.4)";
      copyBtn.style.background = "rgba(255,255,255,0.12)";
      copyBtn.style.color = "#fff";
      copyBtn.style.cursor = "pointer";
      copyBtn.addEventListener("click", function () {
        try {
          const summaryEl = document.getElementById(SUMMARY_ID);
          const text = (summaryEl && summaryEl.textContent) || "";
          if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).catch(function () {});
          }
        } catch { /* never throw in handler */ }
      });
      actions.appendChild(copyBtn);

      const reloadBtn = _getOrCreate(RELOAD_ID, "button");
      reloadBtn.type = "button";
      reloadBtn.textContent = _t("errorBoundary.reload");
      reloadBtn.style.padding = "8px 16px";
      reloadBtn.style.borderRadius = "6px";
      reloadBtn.style.border = "none";
      reloadBtn.style.background = "#2563eb";
      reloadBtn.style.color = "#fff";
      reloadBtn.style.cursor = "pointer";
      reloadBtn.addEventListener("click", function () {
        try {
          if (typeof location !== "undefined" && location.reload) location.reload();
        } catch { /* never throw in handler */ }
      });
      actions.appendChild(reloadBtn);

      overlay.appendChild(actions);
      // 沙箱/无 body 环境容忍：附加失败不阻断（真实 DOM 中挂到 body）
      if (typeof document !== "undefined" && document.body && document.body.appendChild && !overlay.parentNode) {
        document.body.appendChild(overlay);
      }
    } catch { overlay = null; }
    return overlay;
  }

  /** 显示覆盖层（惰性建 DOM，全程 try/catch；重复出错时更新内容）。 */
  function _show(rawMessage, rawError) {
    try {
      const el = _ensureOverlay();
      if (!el) return;
      const msg = String(rawMessage || (rawError && rawError.message) || "unknown error");
      const detail = rawError && rawError.stack ? String(rawError.stack) : "";
      const full = (detail ? detail + "\n" : "") + msg;
      const truncated = full.length > MAX_SUMMARY ? full.slice(0, MAX_SUMMARY) + "\n…" : full;

      const titleEl = document.getElementById(TITLE_ID) || el;
      const summaryEl = document.getElementById(SUMMARY_ID) || el;
      const stampEl = document.getElementById(STAMP_ID) || el;
      titleEl.textContent = _t("errorBoundary.title");
      summaryEl.textContent = truncated;
      try {
        stampEl.textContent = new Date().toLocaleString();
      } catch {
        stampEl.textContent = String(Date.now());
      }
      // 重新渲染按钮文案（语言可能已切换）
      const copyBtn = document.getElementById(COPY_ID);
      if (copyBtn) copyBtn.textContent = _t("errorBoundary.copy");
      const reloadBtn = document.getElementById(RELOAD_ID);
      if (reloadBtn) reloadBtn.textContent = _t("errorBoundary.reload");

      el.style.display = "block";
    } catch { /* 错误处理里绝不抛错 */ }
  }

  // ── 全局监听：保留 console.error 原样日志；不 preventDefault / 不返回 false（不阻断传播） ──
  if (typeof window !== "undefined") {
    window.addEventListener("error", function (event) {
      try {
        console.error("uncaught error:", event && event.error, event && event.message);
        _show((event && event.message) || "", event && event.error);
      } catch { /* never throw in handler */ }
    });

    window.addEventListener("unhandledrejection", function (event) {
      try {
        const reason = event && event.reason;
        const msg = reason instanceof Error ? reason.message : String(reason);
        console.error("unhandled rejection:", reason);
        _show(msg, reason instanceof Error ? reason : null);
      } catch { /* never throw in handler */ }
    });
  }
})();
