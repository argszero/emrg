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

describe("块投影：live 代码围栏 未闭合→纯文本 / 闭合→完整渲染（rant 2026-09-02T21:07:35 验收）", () => {
  /** 文本驱动 fake marked：按 ``` 围栏切分 段落/code token；闭合状态由收尾围栏决定 */
  function makeTextMarked(): { marked: MarkedLike; dompurify: { sanitize: (h: string) => string } } {
    let codeRenderer: ((code: string, infostring: string, escaped: boolean) => string) | null = null;
    const dompurify = { sanitize: (h: string) => h.replace(/<script[\s\S]*?<\/script>/gi, "") };

    function tokenize(text: string): Array<Record<string, unknown>> {
      const lines = text.split("\n");
      const tokens: Array<Record<string, unknown>> = [];
      let fence: string | null = null;
      let lang = "";
      const codeBuf: string[] = [];
      const paraBuf: string[] = [];
      const flushPara = () => {
        const t = paraBuf.join("\n").trim();
        if (t) tokens.push({ type: "paragraph", raw: t, text: t });
        paraBuf.length = 0;
      };
      for (const line of lines) {
        const m = /^(`{3,}|~{3,})[ \t]*([A-Za-z0-9_+-]*)[ \t]*$/.exec(line);
        if (!fence && m) {
          flushPara();
          fence = m[1];
          lang = m[2] || "";
          continue;
        }
        if (fence && m && m[1][0] === fence[0] && m[1].length >= fence.length) {
          tokens.push({ type: "code", raw: fence + "\n" + codeBuf.join("\n") + "\n" + fence, text: codeBuf.join("\n"), lang });
          fence = null;
          continue;
        }
        if (fence) {
          codeBuf.push(line);
          continue;
        }
        paraBuf.push(line);
      }
      if (fence) {
        // 未闭合：raw 无收尾围栏 → engine isFenceClosed false → live 纯文本路径
        tokens.push({ type: "code", raw: fence + "\n" + codeBuf.join("\n"), text: codeBuf.join("\n"), lang });
      } else {
        flushPara();
      }
      return tokens;
    }

    const renderToken = (tok: Record<string, unknown>): string => {
      if (tok.type === "code") {
        const code = String(tok.text ?? "");
        const lg = String(tok.lang ?? "");
        return codeRenderer ? codeRenderer(code, lg, false) : `<pre>${code}</pre>`;
      }
      return `<p>${String(tok.text ?? "")}</p>`;
    };

    return {
      dompurify,
      marked: {
        parse(text: string) {
          return tokenize(text).map(renderToken).join("");
        },
        lexer(text: string) {
          return tokenize(text);
        },
        parser(tokens: Array<Record<string, unknown>>) {
          return tokens.map(renderToken).join("");
        },
        use(opts) {
          if (opts.renderer?.code) codeRenderer = opts.renderer.code;
          return undefined;
        },
      },
    };
  }

  it("未闭合围栏：live 保持纯文本 .stream-code（不高亮、无复制按钮）", () => {
    const fake = makeTextMarked();
    const r = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify });
    const body = document.createElement("div");
    const state = { stableCount: 0, rawText: "" };
    const ok = r.streamProject(body, "intro\n\n```py\nstill open", state);
    expect(ok).toBe(true);
    // intro 段落已稳定渲染（stable），未闭合围栏在 live 块
    expect(state.stableCount).toBe(1);
    const live = body.querySelector(".md-block.live")!;
    expect(live.querySelector(".stream-code")?.textContent).toContain("still open");
    expect(live.querySelector(".code-block")).toBeNull();
  });

  it("围栏追加闭合行 → live 由纯文本转为完整 code-block（增量收敛）", () => {
    const fake = makeTextMarked();
    const r = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify });
    const body = document.createElement("div");
    const state = { stableCount: 0, rawText: "" };
    r.streamProject(body, "intro\n\n```py\nstill open", state);
    expect(body.querySelector(".md-block.live .stream-code")).not.toBeNull();
    // 追加收尾围栏后再次投影：live 块内容替换为完整渲染（引擎 parser 路径）
    r.streamProject(body, "intro\n\n```py\nstill open\n```", state);
    const live = body.querySelector(".md-block.live")!;
    expect(live.querySelector(".stream-code")).toBeNull();
    expect(live.querySelector(".code-block")).not.toBeNull();
    expect(live.textContent).toContain("still open");
    // 稳定块未重投影（intro 段落缓存不动）
    expect(state.stableCount).toBe(1);
    expect(body.querySelectorAll(".md-block:not(.live)")).toHaveLength(1);
  });
});
