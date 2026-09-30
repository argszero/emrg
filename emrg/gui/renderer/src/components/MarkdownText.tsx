import { useEffect, useState } from "react";
import type { MarkdownRenderer } from "../lib/markdown";

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
export function MarkdownText({
  text,
  md,
  stripMark = true,
  className = "assistant-html",
  plainClassName = "assistant-plain",
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
  return <span className={className} dangerouslySetInnerHTML={{ __html: html }} />;
}
