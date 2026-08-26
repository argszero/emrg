/**
 * vendorMarkdown.ts — 生产环境 vendor 依赖接线（Batch 5 承诺项：
 * ResultPanel 注释 "Batch 5 接真实 hljs"、markdown.ts "构造注入"）。
 *
 * React 迁移设计把 vendor 依赖（marked/DOMPurify/hljs）定为**构造注入**
 * 而非 vanilla 的 window 全局读取。但 Batch 5 final switch 时没人注入
 * 真实依赖：TranscriptView 走 `renderer ?? createMarkdownRenderer({ t })`
 * 降级为纯转义文本，聊天 markdown / 代码高亮全部失效（vendor/ 目录仍在
 * 打包清单里，却没有任何代码加载它们）。
 *
 * 本模块把 vendor 三件套接进 bundle：
 * - marked.min.js / dompurify.min.js：UMD → default import（vite 内联打包）
 * - highlight.custom.js：IIFE 副作用导入，运行时设 `window.hljs`
 *   （构建期在 Node 中执行时 typeof window === 'undefined' → no-op 安全）
 * - highlight.github-dark.css：代码块主题，随 bundle 输出
 *
 * 接线后 createProdMarkdownRenderer() 交给 Shell → TranscriptView
 * （renderer prop），ResultPanel/PlainCode 的 window.hljs 读取也同步恢复。
 */
import marked from "../../../vendor/marked.min.js";
import DOMPurify from "../../../vendor/dompurify.min.js";
import "../../../vendor/highlight.custom.js";
import "../../../vendor/highlight.github-dark.css";
import { createMarkdownRenderer, type MarkdownRenderer } from "./markdown";

/** 生产环境 markdown 渲染器（真实 marked + DOMPurify + hljs；缺省 t 走 createMarkdownRenderer 的 t 注入） */
export function createProdMarkdownRenderer(): MarkdownRenderer {
  // hljs 由 highlight.custom.js 副作用导入在运行时写入 window（与 vanilla 同语义，
  // ResultPanel/PlainCode 直接读 window.hljs）；此处惰性读取避免模块求值顺序问题。
  const hljs = (typeof window !== "undefined"
    ? (window as unknown as { hljs?: { highlight: (code: string, opts: { language: string; ignoreIllegals: boolean }) => { value: string }; getLanguage: (lang: string) => boolean; highlightAuto: (code: string) => { value: string } } }).hljs
    : undefined);
  return createMarkdownRenderer({ marked, DOMPurify, hljs });
}
