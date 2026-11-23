import { memo, useEffect, useState } from "react";
import type { MarkdownRenderer } from "../lib/markdown";

/** 渲染完成后的容器类名。正文按下的判定（`BODY_TEXT_SELECTOR`）由它派生，不留第二份拼写。 */
export const MARKDOWN_HTML_CLASS = "assistant-html";

/** 解析完成前纯文本兜底的类名。同上：一个名字一个产出者。 */
export const MARKDOWN_PLAIN_CLASS = "assistant-plain";

/**
 * MarkdownText — markdown 文本的**唯一**渲染入口（TranscriptView 与 Rant 详情共用）。
 *
 * 抽出原因（rant 2026-09-30T09:06:16 / issue #1763）：Rant 详情在 React 迁移时把预处理后的
 * 字符串直接交给 React 落成文本节点，渲染调用整个丢失 ⇒ markdown 以原始字符平铺，
 * `.rant-md h4` 等 CSS 成死代码。回归的形态正是「实现分叉 + 迁移漏接」，所以这里把
 * TranscriptView 里的内部实现提为共用组件：谁要渲染 markdown 都只有这一条路径。
 *
 * 语义与抽取前逐字一致：renderMarkdown 是异步的，解析完成前渲染 `plainClassName` 的纯文本
 * 兜底（不白屏、不抛错）；完成后渲染 `className` 承载的消毒 HTML。
 */
function MarkdownTextImpl({
  text,
  md,
  stripMark = true,
  className = MARKDOWN_HTML_CLASS,
  plainClassName = MARKDOWN_PLAIN_CLASS,
}: {
  text: string;
  md: MarkdownRenderer;
  /** 剥离 ✦ 标记（仅限助手消息） */
  stripMark?: boolean;
  /** 渲染完成后的 HTML 容器类名 */
  className?: string;
  /** 解析完成前的纯文本兜底类名 */
  plainClassName?: string;
}) {
  const [html, setHtml] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    const src = stripMark ? text.replace(/^✦\s*/, "") : text;
    md.renderMarkdown(src).then((h) => {
      if (!cancelled) setHtml(h);
    });
    return () => {
      cancelled = true;
    };
  }, [text, md, stripMark]);
  if (html === null) return <span className={plainClassName}>{text}</span>;
  // 容器是块级（div），不是行内（span）：marked 的输出是 <p>/<pre>/<ul> 这类块级元素，
  // 把它们放进行内盒会触发 block-in-inline 拆分——浏览器在行内盒内部切出若干匿名块，跨这些
  // 匿名块的选区是它最不可靠的一处。宿主三次报障（2026-11-19T15:31:14、2026-11-20T18:08:13、
  // 2026-11-23T14:33:25「chat 页面还是无法选中」）都落在**已完成的回复**上；流式那半边用的是
  // `div.md-stream`（块级容器），输出过程中反而好选——两条路径的差别正是这一处盒模型。
  return <div className={className} dangerouslySetInnerHTML={{ __html: html }} />;
}

/**
 * `memo` 不是优化，是这条缺陷的结构性修复。
 *
 * 实测（2026-11-23，React 19 + jsdom）：组件**重渲染**时 React 会把
 * `dangerouslySetInnerHTML` 再写一遍，即使 html 字符串一字不差——写下去的是全新节点，
 * 而浏览器在节点被换掉时会把边界内的活选区收掉。于是任何让聊天区重渲染的事（新消息、
 * 工具行、一个流式帧）都会重写每一条**已完成**回复：宿主报障「选中后约一秒自动消失、
 * 鼠标没动」就是它，与流式那半边无关（那条路径本来就经 `selectionIn` 推迟写）。
 *
 * 所以「props 不变就不重渲染」必须是结构保证，而不是靠上层恰好没渲染——把 memo 拿掉，
 * `TranscriptView.test.tsx` 的「重渲染不许重写已完成的正文」立刻转红。
 */
export const MarkdownText = memo(MarkdownTextImpl);
MarkdownText.displayName = "MarkdownText";
