import { escapeHtml } from "./utils";

/**
 * markdown.ts — marked + DOMPurify + highlight.js 封装（Batch 1 remainder，设计 §5 项 5）。
 * 源：vanilla renderer/js/markdown.js（G74/G132 / rant 21:00:28 块投影流式渲染）。
 * 与 vanilla 的差异：vendor 依赖（marked/DOMPurify/hljs）改为**构造注入**而非 window
 * 全局读取 —— 组件与测试均可显式传入；浏览器入口在 Batch 2 聊天区落地时接线。
 * - marked v12：highlight 选项已移除 → 自定义 code renderer（精确 lang 高亮，无 lang 用 highlightAuto 兜底）
 * - DOMPurify 消毒（marked v8 起无 sanitize 选项，G44）
 * - 块投影（Block Projection）：稳定块完整渲染缓存，尾部 live 块只渲染已稳定部分；
 *   代码块围栏未闭合 → 纯文本不高亮（与 TUI fence_count%2 启发式一致）
 */

/** 围栏结束标记（``` / ~~~，行尾）——判断 fenced code token 是否已闭合 */
export const FENCE_END_RE = /(```|~~~)[ \t]*$/;

/** fenced code token 是否已闭合（未闭合 → 纯文本不高亮，与 TUI fence_count%2 启发式一致） */
export function isFenceClosed(token: { raw?: unknown; text?: unknown }): boolean {
  return FENCE_END_RE.test(String(token.raw ?? "").trimEnd());
}

/** marked/DOMPurify/highlight.js 的最小依赖接口（按使用面声明，避免强类型耦合） */
export interface MarkedLike {
  parse(text: string, opts?: Record<string, unknown>): string | Promise<string>;
  lexer(text: string): Array<Record<string, unknown>>;
  parser(tokens: Array<Record<string, unknown>>): string;
  use(opts: { renderer?: { code?: (code: string, infostring: string, escaped: boolean) => string } }): unknown;
}
export interface DomPurifyLike {
  sanitize(html: string): string;
}
export interface HljsLike {
  getLanguage(lang: string): boolean;
  highlight(code: string, opts: { language: string; ignoreIllegals: boolean }): { value: string };
  highlightAuto(code: string): { value: string };
}

/** 代码块复制按钮文案（默认英文；与 vanilla "复制代码"/"Copy code" 对齐） */
type Translate = (key: string) => string;

export interface MarkdownRenderer {
  renderMarkdown(mdText: string): Promise<string>;
  streamProject(body: HTMLElement, text: string, state: StreamState): boolean;
  streamFinalize(body: HTMLElement, text: string): Promise<void>;
}

/** 块投影流式状态（挂在消息节点上） */
export interface StreamState {
  container?: HTMLElement | null;
  live?: HTMLElement | null;
  stableCount: number;
  rawText: string;
}

/** 创建 markdown 渲染器（vendor 依赖构造注入；无 marked 时 renderMarkdown 转义降级） */
export function createMarkdownRenderer(deps: {
  marked?: MarkedLike;
  DOMPurify?: DomPurifyLike;
  hljs?: HljsLike;
  t?: Translate;
}): MarkdownRenderer {
  const { marked, DOMPurify, hljs, t } = deps;
  const sanitize = (html: string): string => (DOMPurify ? DOMPurify.sanitize(html) : html);
  const copyLabel = (): string => (t ? t("md.copyCode") : "Copy code");

  /** marked 自定义 code renderer（v12 无 highlight 选项） */
  function codeRenderer(code: string, infostring: string, escaped: boolean): string {
    const lang = (infostring || "").split(/\s+/)[0];
    let highlighted = "";
    if (hljs) {
      try {
        if (lang && hljs.getLanguage(lang)) {
          highlighted = hljs.highlight(code, { language: lang, ignoreIllegals: true }).value;
        } else {
          highlighted = hljs.highlightAuto(code).value; // 无 lang 兜底自动检测
        }
      } catch {
        /* highlight 失败 → 降级纯文本 */
      }
    }
    const cls = `hljs language-${escapeHtml(lang || "plaintext")}`;
    const codeHtml = highlighted
      ? `<code class="${cls}">${highlighted}</code>`
      : `<code class="${cls}">${escaped ? code : escapeHtml(code)}</code>`;
    // 复制按钮：事件委托在聊天区（CSP 禁内联 handler）；code 文本经 escapeHtml 防注入
    return `<div class="code-block"><div class="code-head"><button type="button" class="code-copy" title="${copyLabel()}">${copyLabel()}</button></div><pre>${codeHtml}</pre></div>`;
  }

  /** marked.use 自定义 renderer 只配置一次 */
  let configured = false;
  function ensureConfigured(): void {
    if (!marked || configured) return;
    marked.use({ renderer: { code: codeRenderer } });
    configured = true;
  }

  async function renderMarkdown(mdText: string): Promise<string> {
    if (!marked || typeof marked.parse !== "function") {
      return escapeHtml(mdText || "");
    }
    try {
      ensureConfigured();
      const html = marked.parse(mdText || "", { async: true, gfm: true, breaks: false });
      const raw = await html;
      return sanitize(raw);
    } catch {
      return escapeHtml(mdText || "");
    }
  }

  /** 渲染稳定块（完整，同步 parser；失败降级转义原文） */
  function renderStableToken(token: Record<string, unknown>): string {
    try {
      const html = marked!.parser([token]);
      return sanitize(html);
    } catch {
      return escapeHtml(String(token.raw ?? ""));
    }
  }

  /** 渲染尾部 live 块：未闭合代码块 → 纯文本；其余 → parser 局部渲染 */
  function renderLiveToken(token: Record<string, unknown> | null): string {
    if (!token) return "";
    if (token.type === "code" && !isFenceClosed(token)) {
      // 围栏未闭合：只追加纯文本，不高亮（闭合后由稳定块完整渲染接管）
      return `<pre class="stream-code">${escapeHtml(String(token.text ?? token.raw ?? ""))}</pre>`;
    }
    try {
      const html = marked!.parser([token]);
      return sanitize(html);
    } catch {
      return escapeHtml(String(token.raw ?? ""));
    }
  }

  /** el() 辅助（vanilla utils.js 同语义） */
  function el(tag: string, attrs: Record<string, string> = {}, text?: string): HTMLElement {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else node.setAttribute(k, v);
    }
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /**
   * 块投影增量更新：body 结构 = [span.msg-assistant-mark, div.md-stream]
   *   div.md-stream = [div.md-block(稳定块，缓存不动)]* + div.md-block.live(每 delta 重渲染)
   * state（挂在消息节点上）：{ stableCount, container, live, rawText }
   * 无 marked.lexer / 分词异常 → 返回 false，调用方回退纯文本追加。
   */
  function streamProject(body: HTMLElement, text: string, state: StreamState): boolean {
    if (!marked || typeof marked.lexer !== "function" || typeof marked.parser !== "function") {
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
      const tokens = marked.lexer(text);
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
    } catch {
      return false;
    }
  }

  /**
   * done 收尾：live 块转 full（一次性校正）。
   * 与既有 done 渲染同源（renderMarkdown 全量渲染），仅容器结构保留 mark span。
   */
  async function streamFinalize(body: HTMLElement, text: string): Promise<void> {
    const html = await renderMarkdown(text);
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
  }

  return { renderMarkdown, streamProject, streamFinalize };
}
