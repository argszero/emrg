"use strict";
/**
 * markdown.js — marked + DOMPurify + highlight.js 封装（G74/G132）。
 * - marked v12：highlight 选项已移除 → 自定义 code renderer（精确 lang 高亮，无 lang 用 highlightAuto 兜底）
 * - highlight.js 只注册 ~20 常用语言（vendor/highlight.custom.js，G132）——未注册降级纯文本
 * - DOMPurify 消毒（marked v8 起无 sanitize 选项，G44）
 * - done 后整体渲染（流式中纯文本追加，G8）
 */

const renderer = {};

// marked 自定义 code renderer（v12 无 highlight 选项）
function codeRenderer(code, infostring, escaped) {
  const lang = (infostring || "").split(/\s+/)[0];
  let highlighted = "";
  const hljs = window.hljs;
  if (hljs) {
    try {
      if (lang && hljs.getLanguage(lang)) {
        highlighted = hljs.highlight(code, { language: lang, ignoreIllegals: true }).value;
      } else {
        highlighted = hljs.highlightAuto(code).value; // 无 lang 兜底自动检测
      }
    } catch (e) {
      console.warn("highlight failed:", e);
    }
  }
  const cls = `hljs language-${escapeHtml(lang || "plaintext")}`;
  if (highlighted) {
    return `<pre><code class="${cls}">${highlighted}</code></pre>`;
  }
  return `<pre><code class="${cls}">${escaped ? code : escapeHtml(code)}</code></pre>`;
}

renderer.renderMarkdown = async function (mdText) {
  if (!window.marked) return escapeHtml(mdText || "");
  try {
    if (!window.__emrgMarkedConfigured) {
      window.marked.use({ renderer: { code: codeRenderer } });
      window.__emrgMarkedConfigured = true;
    }
    const html = window.marked.parse(mdText || "", { async: true, gfm: true, breaks: false });
    const raw = await html;
    return window.DOMPurify.sanitize(raw);
  } catch (e) {
    console.warn("markdown render failed:", e);
    return escapeHtml(mdText || "");
  }
};

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

window.emrgMarkdown = renderer;
