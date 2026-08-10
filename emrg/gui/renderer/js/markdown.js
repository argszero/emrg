"use strict";
/**
 * markdown.js — marked + DOMPurify + highlight.js 封装（G74/G132）。
 * - marked v12：highlight 选项已移除 → 自定义 code renderer（精确 lang 高亮，无 lang 用 highlightAuto 兜底）
 * - highlight.js 只注册 ~20 常用语言（vendor/highlight.custom.js，G132）——未注册降级纯文本
 * - DOMPurify 消毒（marked v8 起无 sanitize 选项，G44）
 * - done 后整体渲染（流式中纯文本追加，G8）
 * - rant 21:00:28：块投影流式渲染（Block Projection，参考 OpenCode markdown-stream.ts）——
 *   marked.lexer 分词：除最后一个外的稳定块完整渲染并缓存 DOM（不打断选中/不闪烁），
 *   尾部 live 块只渲染已稳定部分；代码块围栏未闭合 → 纯文本不高亮（与 TUI fence_count%2 启发式一致），
 *   闭合后转完整渲染；done 时 live 块转 full 一次性校正（streamFinalize 走 renderMarkdown 同源）。
 */

const renderer = {};

// marked 自定义 code renderer（v12 无 highlight 选项）
// 设计 §3.3：代码块带复制按钮（圆角浅底 + 一键复制）
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
  const codeHtml = highlighted
    ? `<code class="${cls}">${highlighted}</code>`
    : `<code class="${cls}">${escaped ? code : escapeHtml(code)}</code>`;
  // 复制按钮：事件委托在 chat-view（CSP 禁内联 handler）；code 文本经 escapeHtml 防注入
  return `<div class="code-block"><div class="code-head"><button type="button" class="code-copy" title="${window.EMRG_I18N ? window.EMRG_I18N.t("md.copyCode") : "复制代码"}">${window.EMRG_I18N ? window.EMRG_I18N.t("chat.copyCode") : "复制"}</button></div><pre>${codeHtml}</pre></div>`;
}

/** marked.use 自定义 renderer 只配置一次（renderMarkdown / streamProject 共用） */
function ensureConfigured() {
  if (!window.marked || window.__emrgMarkedConfigured) return;
  window.marked.use({ renderer: { code: codeRenderer } });
  window.__emrgMarkedConfigured = true;
}

renderer.renderMarkdown = async function (mdText) {
  if (!window.marked) {
    console.warn("markdown vendor missing (marked not loaded)");
    return escapeHtml(mdText || "");
  }
  try {
    ensureConfigured();
    const html = window.marked.parse(mdText || "", { async: true, gfm: true, breaks: false });
    const raw = await html;
    return window.DOMPurify.sanitize(raw);
  } catch (e) {
    console.warn("markdown render failed:", e);
    return escapeHtml(mdText || "");
  }
};

// ── 块投影流式渲染（rant 21:00:28） ──────────────────────────────

/** 围栏结束标记（``` / ~~~，行尾）——判断 fenced code token 是否已闭合 */
const FENCE_END_RE = /(```|~~~)[ \t]*$/;

/** fenced code token 是否已闭合（未闭合 → 纯文本不高亮，与 TUI fence_count%2 启发式一致） */
function isFenceClosed(token) {
  return FENCE_END_RE.test(String(token.raw || "").trimEnd());
}

/** 渲染稳定块（完整，同步 parser；失败降级转义原文） */
function renderStableToken(token) {
  try {
    const html = window.marked.parser([token]);
    return window.DOMPurify ? window.DOMPurify.sanitize(html) : html;
  } catch (e) {
    console.warn("stable token render failed:", e);
    return escapeHtml(token.raw || "");
  }
}

/** 渲染尾部 live 块：未闭合代码块 → 纯文本；其余 → parser 局部渲染 */
function renderLiveToken(token) {
  if (!token) return "";
  if (token.type === "code" && !isFenceClosed(token)) {
    // 围栏未闭合：只追加纯文本，不高亮（闭合后由稳定块完整渲染接管）
    return `<pre class="stream-code">${escapeHtml(token.text || token.raw || "")}</pre>`;
  }
  try {
    const html = window.marked.parser([token]);
    return window.DOMPurify ? window.DOMPurify.sanitize(html) : html;
  } catch (e) {
    console.warn("live token render failed:", e);
    return escapeHtml(token.raw || "");
  }
}

/**
 * 块投影增量更新：body 结构 = [span.msg-assistant-mark, div.md-stream]
 *   div.md-stream = [div.md-block(稳定块，缓存不动)]* + div.md-block.live(每 delta 重渲染)
 * state（挂在消息节点上）：{ stableCount, container, live, rawText }
 * 无 marked.lexer / 分词异常 → 返回 false，调用方回退纯文本追加。
 */
renderer.streamProject = function (body, text, state) {
  if (!window.marked || typeof window.marked.lexer !== "function" || typeof window.marked.parser !== "function") {
    return false;
  }
  ensureConfigured();
  try {
    state.rawText = text;
    // 首次进入流式：重建 body（mark span 元素 + md-stream 容器）
    if (!state.container) {
      body.innerHTML = "";
      body.appendChild(el("span", { class: "msg-assistant-mark" }, "✦ "));
      state.container = el("div", { class: "md-stream" });
      body.appendChild(state.container);
    }
    const tokens = window.marked.lexer(text);
    const stable = tokens.slice(0, -1);
    const live = tokens[tokens.length - 1] || null;
    // 结构收缩（如尾随换行改变分词）→ 整体重投影，防错位
    if (stable.length < state.stableCount) {
      state.container.innerHTML = "";
      state.live = null;
      state.stableCount = 0;
    }
    // 旧 live 内容已被完整稳定块取代 → 清空待重建（保持 live 始终是最后一个子节点）
    if (state.live && state.stableCount < stable.length) {
      state.live.innerHTML = "";
    }
    // 只渲染新增的稳定块（缓存不动 → 不打断选中/复制、不闪烁）
    for (let i = state.stableCount; i < stable.length; i++) {
      const block = el("div", { class: "md-block" });
      block.innerHTML = renderStableToken(stable[i]);
      if (state.live) state.container.insertBefore(block, state.live);
      else state.container.appendChild(block);
    }
    state.stableCount = stable.length;
    // 尾部 live 块
    if (live) {
      if (!state.live) {
        state.live = el("div", { class: "md-block live" });
        state.container.appendChild(state.live);
      }
      state.live.innerHTML = renderLiveToken(live);
    } else if (state.live) {
      state.live.remove();
      state.live = null;
    }
    return true;
  } catch (e) {
    console.warn("streamProject failed:", e);
    return false;
  }
};

/**
 * done 收尾：live 块转 full（一次性校正）。
 * 与既有 done 渲染同源（renderMarkdown 全量渲染），仅容器结构保留 mark span。
 */
renderer.streamFinalize = async function (body, text) {
  const html = await renderer.renderMarkdown(text);
  const container = body.querySelector ? body.querySelector(".md-stream") : null;
  if (container) {
    container.innerHTML = html;
  } else {
    // 兜底：整段替换（重建 mark span + 容器）
    body.innerHTML = "";
    body.appendChild(el("span", { class: "msg-assistant-mark" }, "✦ "));
    const c = el("div", { class: "md-stream" });
    c.innerHTML = html;
    body.appendChild(c);
  }
};

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

window.emrgMarkdown = renderer;
