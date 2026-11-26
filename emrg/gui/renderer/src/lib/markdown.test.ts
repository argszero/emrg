import { afterEach, describe, expect, it, vi } from "vitest";
import {
  FENCE_END_RE,
  isFenceClosed,
  createMarkdownRenderer,
  installCodeCopy,
  normalizeImageSrc,
  isLocalImageSrc,
  type MarkedLike,
  type StreamState,
} from "./markdown";
import { EN_DICT, ZH_DICT } from "./i18n-dicts";

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

/** 文本驱动 fake marked：按 ``` 围栏切分 段落/code token；闭合状态由收尾围栏决定。
 * 提到模块级：代码块复制的用例也要多块与自定义代码文本，两个 describe 共用一份。 */
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
        codeBuf.length = 0; // 每块各自归零：不清空的话，下一块会把这一块的正文再抄一遍
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
    const state: StreamState = { stableCount: 0, rawText: "" };
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
    const state: StreamState = { stableCount: 0, rawText: "" };
    expect(r.streamProject(body, "hello", state)).toBe(false);
  });

  it("streamFinalize：live 块转 full 渲染（一次性校正）", async () => {
    const fake = makeFakeMarked();
    const r = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify });
    const body = document.createElement("div");
    const state: StreamState = { stableCount: 0, rawText: "" };
    r.streamProject(body, "hello", state);
    await r.streamFinalize(body, "final text");
    const stream = body.querySelector(".md-stream");
    expect(stream).not.toBeNull();
    expect(stream!.innerHTML).toContain("final text");
  });
});

describe("块投影：live 代码围栏 未闭合→纯文本 / 闭合→完整渲染（rant 2026-09-02T21:07:35 验收）", () => {
  it("未闭合围栏：live 保持纯文本 .stream-code（不高亮、无复制按钮）", () => {
    const fake = makeTextMarked();
    const r = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify });
    const body = document.createElement("div");
    const state: StreamState = { stableCount: 0, rawText: "" };
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
    const state: StreamState = { stableCount: 0, rawText: "" };
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

describe("normalizeImageSrc：本地图片两种写法归一（issue #2638）", () => {
  it("根绝对路径 → file:///…（origin 根不是宿主文件系统）", () => {
    expect(normalizeImageSrc("/Users/me/shot.png")).toBe("file:///Users/me/shot.png");
  });

  it("已是 file: 的原样返回（不重复加 scheme）", () => {
    expect(normalizeImageSrc("file:///Users/me/shot.png")).toBe("file:///Users/me/shot.png");
  });

  it("其它 scheme / 协议相对 / 相对路径原样返回", () => {
    expect(normalizeImageSrc("https://x.dev/a.png")).toBe("https://x.dev/a.png");
    expect(normalizeImageSrc("data:image/png;base64,AAA")).toBe("data:image/png;base64,AAA");
    expect(normalizeImageSrc("//cdn.example.com/a.png")).toBe("//cdn.example.com/a.png");
    expect(normalizeImageSrc("shots/a.png")).toBe("shots/a.png");
  });

  it("Windows 盘符路径 → file:///（单字母不是 scheme）", () => {
    expect(normalizeImageSrc("C:\\shots\\a.png")).toBe("file:///C:/shots/a.png");
    expect(normalizeImageSrc("C:/shots/a.png")).toBe("file:///C:/shots/a.png");
  });

  it("空值原样（不造出 file:// 空路径）", () => {
    expect(normalizeImageSrc("")).toBe("");
    expect(normalizeImageSrc(undefined)).toBe("");
  });

  it("名字里的 #、?、%、\\ 是数据，地址解码回来还是它（issue #3243）", () => {
    // 判据是往返：地址自己的 path 解出来，就是写进去的那条路径。这四者都是 URL 语法，
    // 在这条路径里却是文件名的一部分 —— 读成语法不报错，只指向另一条路径。
    for (const name of ["shot.png", "shot#1.png", "a?b.png", "100%.png", "a\\b.png", "中文.png"]) {
      const typed = `/Users/me/${name}`;
      expect(decodeURIComponent(new URL(normalizeImageSrc(typed)).pathname)).toBe(typed);
    }
    // 地址本身也钉住：往返之外，拼出来的就是这一种写法。
    expect(normalizeImageSrc("/Users/me/shot#1.png")).toBe("file:///Users/me/shot%231.png");
    expect(normalizeImageSrc("/Users/me/a?b.png")).toBe("file:///Users/me/a%3Fb.png");
    expect(normalizeImageSrc("/Users/me/100%.png")).toBe("file:///Users/me/100%25.png");
    expect(normalizeImageSrc("/Users/me/a\\b.png")).toBe("file:///Users/me/a%5Cb.png");
    // 盘符分支同一套转义，而已有写法一个字节都没动。
    expect(normalizeImageSrc("C:\\shots\\a#1.png")).toBe("file:///C:/shots/a%231.png");
    expect(normalizeImageSrc("C:\\shots\\a.png")).toBe("file:///C:/shots/a.png");
  });
});

describe("isLocalImageSrc：放行条件只认 img[src] 的 file:（issue #2638）", () => {
  it("img + src + file: → true（大小写不敏感）", () => {
    expect(isLocalImageSrc("img", "src", "file:///a.png")).toBe(true);
    expect(isLocalImageSrc("IMG", "SRC", "FILE:///a.png")).toBe(true);
  });

  it("同一 URL 挂在别的元素/属性上 → false（放行范围不扩）", () => {
    expect(isLocalImageSrc("a", "href", "file:///a.png")).toBe(false);
    expect(isLocalImageSrc("img", "onerror", "file:///a.png")).toBe(false);
    expect(isLocalImageSrc("iframe", "src", "file:///a.png")).toBe(false);
  });

  it("img[src] 但不是 file: → false（javascript:/data: 走默认策略）", () => {
    expect(isLocalImageSrc("img", "src", "javascript:alert(1)")).toBe(false);
  });
});

describe("块投影：正文里有选区 → 这一帧一个 DOM 都不写（issue #3034）", () => {
  /** 一个能回答「有没有选区」的判定，测试自己开关。 */
  const holds = (on: () => boolean) => ({ selectionHolds: on });

  it("有选区：streamProject 不碰 body/容器，只记下最新文本", () => {
    const fake = makeFakeMarked();
    let selected = true;
    const md = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify, ...holds(() => selected) });
    const body = document.createElement("div");
    const state: StreamState = { stableCount: 0, rawText: "" };

    expect(md.streamProject(body, "hello\n```js\nconst a=1;\n```", state)).toBe(true);
    // 什么都没写：body 仍是空的，没有 mark span、没有 md-stream
    expect(body.innerHTML).toBe("");
    expect(state.pendingText).toContain("const a=1;");

    // 再吃一帧，仍不写
    expect(md.streamProject(body, "hello\n```js\nconst a=2;\n```", state)).toBe(true);
    expect(body.innerHTML).toBe("");
    expect(state.pendingText).toContain("const a=2;");
  });

  it("选区清空后 resumeStream 一次补写成最新内容", async () => {
    const fake = makeFakeMarked();
    let selected = true;
    const md = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify, ...holds(() => selected) });
    const body = document.createElement("div");
    const state: StreamState = { stableCount: 0, rawText: "" };
    md.streamProject(body, "hello\n```js\nconst a=1;\n```", state);

    selected = false;
    await md.resumeStream(body, state);

    expect(body.querySelector(".md-stream")).not.toBeNull();
    expect(body.querySelector(".md-assistant-mark, .msg-assistant-mark")).not.toBeNull();
    expect(body.textContent).toContain("hello");
    expect(state.pendingText).toBeUndefined();
  });

  it("没有选区时照常写：这一条防止「有选区才渲染」被当成通用规则", () => {
    const fake = makeFakeMarked();
    const md = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify, ...holds(() => false) });
    const body = document.createElement("div");
    const state: StreamState = { stableCount: 0, rawText: "" };
    expect(md.streamProject(body, "hello\n```js\nconst a=1;\n```", state)).toBe(true);
    expect(body.querySelector(".md-stream")).not.toBeNull();
    expect(state.pendingText).toBeUndefined();
  });

  it("有选区时收尾校正也推迟，并在补做时落成最终内容", async () => {
    const fake = makeFakeMarked();
    let selected = true;
    const md = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify, ...holds(() => selected) });
    const body = document.createElement("div");
    const state: StreamState = { stableCount: 0, rawText: "" };
    // 先无选区地投影出一棵树，再开选区收尾
    const md2 = createMarkdownRenderer({ marked: fake.marked, DOMPurify: fake.dompurify, ...holds(() => false) });
    md2.streamProject(body, "hello\n```js\nconst a=1;\n```", state);
    const before = body.innerHTML;

    await md.streamFinalize(body, "hello\n```js\nconst a=1;\n```", state);
    expect(body.innerHTML).toBe(before); // 一个字符都没重写
    expect(state.pendingFinalize).toBeDefined();

    selected = false;
    await md.resumeStream(body, state);
    expect(body.innerHTML).not.toBe(before);
    expect(state.pendingFinalize).toBeUndefined();
  });
});

describe("代码块复制按钮：点击写剪贴板（issue #3037）", () => {
  /** 带缩进、空行与中文标点的代码：复制结果必须与屏幕上那一块逐字相等 */
  const CODE = 'def f():\n    # 中文标点，不动。\n\n    return "值"';
  const ONE = "先说一句\n\n```py\n" + CODE + "\n```";
  const TWO = "开头\n\n```js\nconst a = 1;\n```\n\n中间\n\n```py\n" + CODE + "\n```";

  const dictT = (dict: Record<string, string>) => (key: string) => dict[key] ?? key;
  const zh = dictT(ZH_DICT);
  const en = dictT(EN_DICT);

  afterEach(() => {
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  /** 真实 DOM：root(.transcript-view) > body。委托装在 root 上，与 TranscriptView 同形。 */
  function mount(
    opts: { t?: (key: string) => string; writeText?: (text: string) => Promise<void> } = {},
  ) {
    const fake = makeTextMarked();
    const t = opts.t;
    const md = createMarkdownRenderer({
      marked: fake.marked,
      DOMPurify: fake.dompurify,
      ...(t ? { t } : {}),
    });
    const root = document.createElement("div");
    root.className = "transcript-view";
    const body = document.createElement("div");
    root.appendChild(body);
    document.body.appendChild(root);
    const writes: string[] = [];
    installCodeCopy(root, {
      ...(t ? { t } : {}),
      writeText:
        opts.writeText ??
        ((text: string) => {
          writes.push(text);
          return Promise.resolve();
        }),
    });
    return { root, body, md, writes };
  }

  /** 给 Promise 链跑完的机会（fake timers 不会替你清微任务队列之外的链） */
  const settle = async (): Promise<void> => {
    for (let i = 0; i < 4; i++) await Promise.resolve();
  };

  /** 真派发点击事件，不直接调用处理函数 */
  const click = (node: Element): void => {
    node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  };

  const copyBtn = (body: HTMLElement, i = 0): HTMLButtonElement =>
    body.querySelectorAll<HTMLElement>(".code-block")[i].querySelector<HTMLButtonElement>(".code-copy")!;

  it("流式中已闭合的代码块：点按钮写入该块原文，1.5s 后复原", async () => {
    vi.useFakeTimers();
    const { md, body, writes } = mount({ t: zh });
    const state: StreamState = { stableCount: 0, rawText: "" };
    expect(md.streamProject(body, ONE, state)).toBe(true);
    const pre = body.querySelector(".code-block pre")!;
    const btn = copyBtn(body);
    expect(btn.textContent).toBe("复制代码");

    click(btn);
    await settle();

    expect(writes).toEqual([CODE]); // 缩进、空行、中文标点逐字保留
    expect(writes[0]).toBe(pre.textContent); // 写进去的就是屏幕上那一块
    expect(writes[0]).not.toContain("language-"); // 不夹带高亮留下的痕迹
    expect(writes[0]).not.toContain("```"); // 不夹带围栏与语言名
    expect(btn.textContent).toBe("已复制 ✓");

    await vi.advanceTimersByTimeAsync(1499);
    expect(btn.textContent).toBe("已复制 ✓");
    await vi.advanceTimersByTimeAsync(1);
    expect(btn.textContent).toBe("复制代码");
  });

  it("回答结束整块重写后按钮仍然可用（委托不在按钮上）", async () => {
    const { md, body, writes } = mount();
    const state: StreamState = { stableCount: 0, rawText: "" };
    md.streamProject(body, ONE, state);
    const before = copyBtn(body);
    await md.streamFinalize(body, ONE);
    const after = copyBtn(body);
    expect(after).not.toBe(before); // 节点确实被换掉了
    click(after);
    await settle();
    expect(writes).toEqual([CODE]);
  });

  it("一段回答里多个代码块：只复制被点的那一块", async () => {
    const { md, body, writes } = mount();
    const state: StreamState = { stableCount: 0, rawText: "" };
    md.streamProject(body, TWO, state);
    const btns = body.querySelectorAll(".code-copy");
    expect(btns.length).toBe(2);

    click(btns[1]);
    await settle();
    expect(writes).toEqual([CODE]);

    click(btns[0]);
    await settle();
    // writes 记的是点击顺序（先点的第二块），不是 DOM 顺序
    expect(writes).toEqual([CODE, "const a = 1;"]);
  });

  it("写入失败：文案切「复制失败」，3s 后才复原", async () => {
    vi.useFakeTimers();
    const { md, body } = mount({ t: zh, writeText: () => Promise.reject(new Error("denied")) });
    const state: StreamState = { stableCount: 0, rawText: "" };
    md.streamProject(body, ONE, state);
    const btn = copyBtn(body);

    click(btn);
    await settle();
    expect(btn.textContent).toBe("复制失败");

    await vi.advanceTimersByTimeAsync(1500);
    expect(btn.textContent).toBe("复制失败"); // 成功那一档的时长不适用于失败
    await vi.advanceTimersByTimeAsync(1500);
    expect(btn.textContent).toBe("复制代码");
  });

  it("按钮文案与成功反馈跟着界面语言（中/英）", async () => {
    vi.useFakeTimers();
    for (const [t, label, ok] of [
      [zh, "复制代码", "已复制 ✓"],
      [en, "Copy code", "Copied ✓"],
    ] as const) {
      document.body.innerHTML = "";
      const { md, body, writes } = mount({ t });
      const state: StreamState = { stableCount: 0, rawText: "" };
      md.streamProject(body, ONE, state);
      const btn = copyBtn(body);
      expect(btn.textContent).toBe(label);

      click(btn);
      await settle();
      expect(writes).toEqual([CODE]);
      expect(btn.textContent).toBe(ok);

      await vi.advanceTimersByTimeAsync(1500);
      expect(btn.textContent).toBe(label);
    }
  });

  it("失败反馈也是中英各一条", async () => {
    vi.useFakeTimers();
    for (const [t, fail] of [
      [zh, "复制失败"],
      [en, "Copy failed"],
    ] as const) {
      document.body.innerHTML = "";
      const { md, body } = mount({ t, writeText: () => Promise.reject(new Error("denied")) });
      const state: StreamState = { stableCount: 0, rawText: "" };
      md.streamProject(body, ONE, state);
      click(copyBtn(body));
      await settle();
      expect(copyBtn(body).textContent).toBe(fail);
    }
  });
});
