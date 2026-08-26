import { describe, expect, it } from "vitest";
import { FENCE_END_RE, isFenceClosed, createMarkdownRenderer, type MarkedLike } from "./markdown";

/**
 * markdown.test.ts — marked/DOMPurify/highlight 封装纯逻辑测试（Batch 1 remainder）。
 * 覆盖：围栏闭合判定（开/闭双态）、无 marked 降级转义、注入 fake marked 渲染 + DOMPurify 消毒、
 *       code renderer 转义（无 hljs）/高亮（有 hljs）、块投影流式（jsdom）。
 */

/** 最小 fake marked：code renderer 走 use() 注册，lexer 返回稳定块 + live 块 */
function makeFakeMarked(opts?: { hljs?: unknown; dompurify?: unknown }): {
  marked: MarkedLike;
  dompurify: { sanitize: (h: string) => string };
  hljs: { value: string };
} {
  let codeRenderer: ((code: string, infostring: string, escaped: boolean) => string) | null = null;
  const hljs = { value: "<span class=\"hljs-kw\">x</span>" };
  const dompurify = { sanitize: (h: string) => h.replace(/<script[\s\S]*?<\/script>/gi, "") };
  const marked: MarkedLike = {
    parse(text, _opts) {
      // 模拟 marked：```lang\nbody``` → codeRenderer(body, lang, false)；否则段落
      if (codeRenderer) {
        const m = /^```([\w-]*)[ \t]*\n?([\s\S]*?)(?:```)?$/.exec(text);
        if (m) return `<div>${codeRenderer(m[2], m[1] ?? "", false)}</div>`;
        return `<div>${codeRenderer(text, "", false)}</div>`;
      }
      return `<div><p>${text}</p></div>`;
    },
    lexer(text) {
      // 两个块：稳定段落 + 尾部 live 代码块
      return [
        { type: "paragraph", raw: "hello", text: "hello" },
        { type: "code", raw: "```js\nconst a=1;\n```", text: "const a=1;" },
      ];
    },
    parser(tokens) {
      return tokens
        .map((tok) => {
          if (tok.type === "code") {
            return codeRenderer
              ? codeRenderer(String(tok.text ?? ""), "js", false)
              : `<pre>${String(tok.text ?? "")}</pre>`;
          }
          return `<p>${String(tok.text ?? "")}</p>`;
        })
        .join("");
    },
    use(opts) {
      if (opts.renderer?.code) codeRenderer = opts.renderer.code;
      return undefined;
    },
  };
  return { marked, dompurify, hljs };
}

describe("FENCE_END_RE / isFenceClosed（双态验证）", () => {
  it("闭合围栏 → true（``` 与 ~~~）", () => {
    expect(FENCE_END_RE.test("```js\nconst a = 1;\n```")).toBe(true);
    expect(FENCE_END_RE.test("~~~\nline\n~~~")).toBe(true);
    expect(FENCE_END_RE.test("```")).toBe(true);
  });
  it("未闭合围栏 → false（纯文本不高亮，与 TUI fence_count%2 启发式一致）", () => {
    expect(FENCE_END_RE.test("```js\nconst a = 1;")).toBe(false);
    expect(isFenceClosed({ raw: "```js\nconst a = 1;" })).toBe(false);
    expect(isFenceClosed({ raw: "```js\nconst a = 1;\n```" })).toBe(true);
  });
  it("非代码 token（如 paragraph）不算围栏闭合", () => {
    expect(isFenceClosed({ raw: "hello world" })).toBe(false);
  });
});

describe("createMarkdownRenderer：无 marked → 转义降级", () => {
  it("renderMarkdown 返回转义后的原文（防注入）", async () => {
    const r = createMarkdownRenderer({});
    const out = await r.renderMarkdown("<script>alert(1)</script>");
    expect(out).toBe("&lt;script&gt;alert(1)&lt;/script&gt;");
  });
});

describe("createMarkdownRenderer：注入 marked + DOMPurify", () => {
  it("renderMarkdown 走 marked.parse + DOMPurify.sanitize", async () => {
    const fake = makeFakeMarked();
    const r = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify });
    const out = await r.renderMarkdown("hello <script>x</script>");
    // DOMPurify 移除 script；marked 包裹 div
    expect(out).not.toContain("<script>");
    expect(out).toContain("hello");
  });

  it("code renderer：无 hljs → 代码转义 + 复制按钮", async () => {
    const fake = makeFakeMarked();
    const r = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify });
    const out = await r.renderMarkdown("<b>bold</b>");
    expect(out).toContain("code-block");
    expect(out).toContain("code-copy");
    expect(out).toContain("&lt;b&gt;"); // 未高亮 → escapeHtml
  });

  it("code renderer：有 hljs → 高亮输出 + language 类", async () => {
    const fake = makeFakeMarked();
    const r = createMarkdownRenderer({
      marked: fake.marked,
      DOMPurify: fake.dompurify,
      hljs: {
        getLanguage: (lang: string) => lang === "js",
        highlight: () => fake.hljs,
        highlightAuto: () => fake.hljs,
      },
    });
    const out = await r.renderMarkdown("```js\nconst a = 1;\n```");
    expect(out).toContain("hljs-kw");
    expect(out).toContain("language-js");
  });

  it("t 注入：复制按钮文案走 i18n", async () => {
    const fake = makeFakeMarked();
    const r = createMarkdownRenderer({
      marked: fake.marked,
      DOMPurify: fake.dompurify,
      t: (key) => (key === "md.copyCode" ? "复制代码" : key),
    });
    const out = await r.renderMarkdown("x");
    expect(out).toContain("复制代码");
  });
});

describe("createMarkdownRenderer：块投影流式（jsdom）", () => {
  it("streamProject 构建 md-stream 结构：mark span + 稳定块 + live 块", () => {
    const fake = makeFakeMarked();
    const r = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify });
    const body = document.createElement("div");
    const state = { stableCount: 0, rawText: "" };
    const ok = r.streamProject(body, "hello", state);
    expect(ok).toBe(true);
    expect(body.querySelector(".msg-assistant-mark")?.textContent).toBe("✦ ");
    const stream = body.querySelector(".md-stream");
    expect(stream).not.toBeNull();
    // 1 稳定块 + 1 live 块
    expect(stream!.querySelectorAll(".md-block").length).toBe(2);
    expect(stream!.querySelector(".md-block.live")).not.toBeNull();
    // 稳定块已渲染，live 块为 live 状态
    expect(state.stableCount).toBe(1);
  });

  it("streamProject 无 marked → 返回 false（调用方回退纯文本）", () => {
    const r = createMarkdownRenderer({});
    const body = document.createElement("div");
    const state = { stableCount: 0, rawText: "" };
    expect(r.streamProject(body, "hello", state)).toBe(false);
  });

  it("streamFinalize：live 块转 full 渲染（一次性校正）", async () => {
    const fake = makeFakeMarked();
    const r = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify });
    const body = document.createElement("div");
    const state = { stableCount: 0, rawText: "" };
    r.streamProject(body, "hello", state);
    await r.streamFinalize(body, "final text");
    const stream = body.querySelector(".md-stream");
    expect(stream).not.toBeNull();
    expect(stream!.innerHTML).toContain("final text");
  });
});
