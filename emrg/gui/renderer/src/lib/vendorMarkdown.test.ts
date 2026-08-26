import { describe, expect, it } from "vitest";
import { createProdMarkdownRenderer } from "./vendorMarkdown";

/**
 * vendorMarkdown.test.ts — 生产 markdown 接线测试（Batch 5 承诺项回归）。
 * 断言 createProdMarkdownRenderer() 返回的渲染器使用**真实** vendor 依赖
 * （marked/DOMPurify/hljs），而非降级转义：
 * - 粗体/行内代码/链接 → 真实 HTML 标签（降级路径只会返回转义文本）
 * - 代码块 → hljs 语言类 + 高亮 token 类
 * - window.hljs 由 highlight.custom.js 副作用导入写入（ResultPanel/PlainCode 依赖）
 */
describe("vendorMarkdown", () => {
  it("production renderer renders real markdown, not escaped fallback", async () => {
    const md = createProdMarkdownRenderer();
    const html = await md.renderMarkdown("**bold** and `code` and [link](https://x.dev)");
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("<code>code</code>");
    expect(html).toContain('<a href="https://x.dev">link</a>');
  });

  it("code block gets hljs language + highlight token classes", async () => {
    const md = createProdMarkdownRenderer();
    const html = await md.renderMarkdown("```python\nprint('hi')\n```");
    expect(html).toContain("language-python");
    expect(html).toContain("hljs");
    // hljs 真实高亮输出带 class 的 token span（降级纯文本路径不会产生）
    expect(html).toMatch(/<span class="hljs-[a-z_]+">/);
  });

  it("hljs global is available (ResultPanel/PlainCode window.hljs contract)", () => {
    const win = window as unknown as { hljs?: unknown };
    expect(win.hljs).toBeDefined();
    expect(typeof (win.hljs as { highlight?: unknown }).highlight).toBe("function");
  });

  it("xss is sanitized by DOMPurify", async () => {
    const md = createProdMarkdownRenderer();
    // CommonMark: HTML 块延续到空行 —— script 块后须空行才能解析 markdown
    const html = await md.renderMarkdown("<script>alert(1)</script>\n\n**ok**");
    expect(html).not.toContain("<script");
    expect(html).toContain("<strong>ok</strong>");
  });
});
