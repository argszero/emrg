import { describe, expect, it } from "vitest";
import { act } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { TranscriptView } from "./TranscriptView";
import { createTranscriptStore, type TranscriptStore } from "../lib/transcript";
import { I18nProvider } from "../lib/i18n";
import {
  createMarkdownRenderer,
  type MarkdownRenderer,
  type MarkedLike,
} from "../lib/markdown";

/**
 * TranscriptView.test.tsx — 聊天区 React 组件测试（Batch 2，设计 §5 Batch 2 项 6 验收）。
 * 注入假 markdown 渲染器（无 marked vendor 依赖），断言消息渲染/工具流转/合并组交互。
 */

// 假渲染器：renderMarkdown 包一层 .md-test 便于断言 done 后的 HTML 注入
const fakeMd: MarkdownRenderer = {
  renderMarkdown: async (text) => `<div class="md-test">${text}</div>`,
  streamProject: () => false,
  streamFinalize: async () => {},
};

/**
 * 文本驱动 fake marked（rant 2026-09-02T21:07:35 验收）：按 ``` 围栏切分 段落/code token，
 * 闭合状态由是否存在收尾围栏决定——让块投影引擎在「闭合围栏 → 渲染 / 未闭合围栏 →
 * live 纯文本」之间真实切换（与真实 marked lexer 语义对齐：code token 的 raw 含未闭合
 * 围栏时 isFenceClosed 为 false）。
 */
function makeTextDrivenMarked(): { marked: MarkedLike; dompurify: { sanitize(h: string): string } } {
  let codeRenderer: ((code: string, infostring: string, escaped: boolean) => string) | null = null;
  const dompurify = { sanitize: (h: string) => h.replace(/<script[\s\S]*?<\/script>/gi, "") };

  function tokenize(text: string): Array<Record<string, unknown>> {
    const lines = text.split("\n");
    const tokens: Array<Record<string, unknown>> = [];
    let fence: string | null = null; // 当前围栏起始标记（``` 或 ~~~）
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
        tokens.push({
          type: "code",
          raw: fence + "\n" + codeBuf.join("\n") + "\n" + fence,
          text: codeBuf.join("\n"),
          lang,
        });
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
      // 未闭合：raw 无收尾围栏 → engine isFenceClosed 为 false → live 纯文本路径
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
        // 全量渲染：EOF 未闭合围栏也按 code 块收尾（与真实 marked 语义一致）
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

function setup(store: TranscriptStore, sid?: string | null, renderer?: MarkdownRenderer) {
  return render(
    <I18nProvider lang="zh">
      <TranscriptView store={store} sid={sid} renderer={renderer ?? fakeMd} />
    </I18nProvider>,
  );
}

describe("TranscriptView", () => {
  it("渲染用户/系统/历史消息与历史加载条", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("hello", "s1");
    store.addSystemMessage("system note", "s1");
    store.addHistoryMessage("old message", "s1");
    store.setLoadBar("加载历史中…", "s1");
    const { container } = setup(store, "s1");
    // Stage 1：用户/历史消息走 markdown 渲染 → 文本在 .msg.user 内的渲染 span 里
    expect(screen.getByText("system note")).toHaveClass("msg", "system");
    const userDiv = container.querySelector(".msg.user");
    expect(userDiv).not.toBeNull();
    expect(userDiv!.textContent).toBe("hello");
    const historyDiv = container.querySelector(".msg.user.history");
    expect(historyDiv).not.toBeNull();
    expect(historyDiv!.textContent).toBe("old message");
    expect(container.querySelector(".history-load-bar")).toHaveTextContent("加载历史中…");
  });

  it("用户消息 markdown 渲染：富文本不字面显示（rant 2026-08-28T14:07:29 验收）", async () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("**bold** `code` [link](https://x)", "s1");
    const { container } = setup(store, "s1");
    // 假渲染器把 markdown 包进 .md-test → 证明走了 markdown 路径而非纯文本直出
    const mdDiv = await screen.findByText("**bold** `code` [link](https://x)", { selector: ".md-test" });
    expect(mdDiv).toBeInTheDocument();
  });

  it("流式显示纯文本，done 后渲染 markdown（✦ 前缀不破坏块语法）", async () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleDelta([{ request_id: "r1", content: "**bold** text" }], "s1");
    const { container } = setup(store, "s1");
    const body = container.querySelector(".msg-body");
    expect(body).toHaveClass("typing");
    expect(body!.textContent).toContain("**bold** text");
    act(() => {
      store.handleDone({ request_id: "r1" }, "s1");
    });
    // done → typing 移除 + 假渲染器输出 .md-test（整体 markdown；await 覆盖异步渲染与重渲染）
    expect(container.querySelector(".msg-body")).not.toHaveClass("typing");
    const mdDiv = await screen.findByText("**bold** text", { selector: ".md-test" });
    expect(mdDiv).toBeInTheDocument();
    // ✦ 标记独立于渲染文本
    expect(container.querySelector(".msg-assistant-mark")).toHaveTextContent("✦");
  });

  it("流式期间块投影：闭合代码围栏即时渲染、未闭合围栏保持纯文本，done 全量校正（rant 2026-09-02T21:07:35 方案 B）", async () => {
    const { marked, dompurify } = makeTextDrivenMarked();
    const streamMd = createMarkdownRenderer({ marked, DOMPurify: dompurify });
    const store = createTranscriptStore({ t: (k) => k });
    store.handleDelta([{ request_id: "r1", content: "intro\n\n```js\nconst a = 1;\n```" }], "s1");
    const { container } = setup(store, "s1", streamMd);
    // typing 中：引擎已在 host 里投影「✦ + .md-stream」——不再纯文本等 done
    const body = container.querySelector(".msg-body")!;
    expect(body).toHaveClass("typing");
    expect(container.querySelector(".md-stream")).not.toBeNull();
    expect(container.querySelector(".md-stream-host .msg-assistant-mark")).toHaveTextContent("✦");
    // 闭合 js 围栏在流式期间已渲染成 code-block（含复制按钮）
    expect(container.querySelector(".md-block .code-block")).not.toBeNull();
    expect(body.textContent).not.toContain("```js");
    // 追加未闭合 py 围栏 → live 块保持纯文本（.stream-code，无 code-block）
    act(() => {
      store.handleDelta([{ request_id: "r1", content: "\n\n```py\nstill typing..." }], "s1");
    });
    expect(container.querySelector(".msg-body")).toHaveClass("typing");
    const live = container.querySelector(".md-block.live") as HTMLElement;
    expect(live).not.toBeNull();
    expect(live.querySelector(".code-block")).toBeNull();
    expect(live.querySelector(".stream-code")?.textContent).toContain("still typing...");
    // 之前的稳定块被缓存：intro 段 + 闭合 js 块为稳定 .md-block，live 块独立
    expect(container.querySelectorAll(".md-block:not(.live)")).toHaveLength(2);
    expect(container.querySelectorAll(".code-block")).toHaveLength(1); // 未重复渲染
    // done → typing 移除 + streamFinalize 全量校正（live/.stream-code 消失，整段全量 markdown）
    act(() => {
      store.handleDone({ request_id: "r1" }, "s1");
    });
    expect(container.querySelector(".msg-body")).not.toHaveClass("typing");
    await waitFor(() => {
      expect(container.querySelector(".stream-code")).toBeNull();
    });
    expect(container.querySelectorAll(".md-block")).toHaveLength(0); // finalize 整段替换
    expect(container.querySelectorAll(".code-block")).toHaveLength(2); // js + py（EOF 收尾）
    expect(container.querySelector(".msg-body")?.textContent).toContain("still typing...");
  });

  it("工具行 running → done：label 切换 + ✓ + 耗时 + 输出默认隐藏、点击展开", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart(
      { tool_call_id: "t1", tool_name: "bash", request_id: "r1", intent: "check config" },
      "s1",
    );
    const { container } = setup(store, "s1");
    const row = container.querySelector(".tool-row")!;
    expect(row).toHaveClass("running");
    expect(screen.getByText("正在运行命令…")).toBeInTheDocument();
    expect(screen.getByText("check config")).toHaveClass("tool-intent");
    act(() => {
      store.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 0.4, content: "out" }, "s1");
    });
    expect(row).toHaveClass("done");
    expect(screen.getByText("已运行命令")).toBeInTheDocument();
    expect(screen.getByText("· 0.4s")).toHaveClass("tool-time");
    const output = container.querySelector(".tool-output")!;
    expect(output).toHaveClass("hidden");
    expect(output).toHaveTextContent("out");
    // 点击行 → 展开输出
    act(() => {
      (row as HTMLElement).click();
    });
    expect(output).not.toHaveClass("hidden");
  });

  it("失败工具行：failed label（tool.failText）且无耗时", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
    store.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 9.9, error: "boom" }, "s1");
    const { container } = setup(store, "s1");
    const row = container.querySelector(".tool-row")!;
    expect(row).toHaveClass("failed");
    expect(screen.getByText("这一步没成功，我换个方法试试")).toBeInTheDocument();
    expect(container.querySelector(".tool-time")).not.toBeInTheDocument();
  });

  it("连续工具合并组：摘要 + 收起，bar 点击展开后不再自动收起", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
    store.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 0.4, content: "" }, "s1");
    store.handleToolStart({ tool_call_id: "t2", tool_name: "read", request_id: "r1" }, "s1");
    store.handleToolEnd({ tool_call_id: "t2", tool_name: "read", elapsed: 1.2, content: "" }, "s1");
    const { container } = setup(store, "s1");
    const group = container.querySelector(".tool-group")!;
    expect(group).toHaveClass("collapsed");
    expect(container.querySelectorAll(".tool-row")).toHaveLength(2);
    expect(container.querySelector(".tool-group-summary")).toHaveTextContent("2 个工具执行 · 1.6s");
    // bar 点击 → 展开 + user-expanded
    act(() => {
      (container.querySelector(".tool-group-bar") as HTMLElement).click();
    });
    expect(group).not.toHaveClass("collapsed");
    expect((group as HTMLElement).dataset.userExpanded).toBe("1");
    // 新工具并入组且不自动收起
    act(() => {
      store.handleToolStart({ tool_call_id: "t3", tool_name: "edit", request_id: "r1" }, "s1");
      store.handleToolEnd({ tool_call_id: "t3", tool_name: "edit", elapsed: 0.3, content: "" }, "s1");
    });
    expect(container.querySelectorAll(".tool-row")).toHaveLength(3);
    expect(group).not.toHaveClass("collapsed");
    expect(container.querySelector(".tool-group-summary")).toHaveTextContent("3 个工具执行 · 1.9s");
  });

  it("广播流与 own stream 均不显示 remote 标签（rant 2026-08-28T22:16:36 去掉）", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleDelta([{ request_id: "r1", content: "broadcast" }], "s1");
    const { container } = setup(store, "s1");
    expect(screen.queryByText("（来自其他客户端）")).not.toBeInTheDocument();
    expect(container.querySelector(".remote-label")).not.toBeInTheDocument();
  });

  it("sid=null 缺省桶：无 sid 事件渲染到默认视图", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("legacy", null);
    const { container } = setup(store);
    const div = container.querySelector(".msg.user");
    expect(div).not.toBeNull();
    expect(div!.textContent).toBe("legacy");
  });

  it("上翻离底显示「回到底部」按钮，点击回底 + 设 autoScroll（rant 2026-08-28T22:36:18）", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("bottom message", "s1");
    // 手动把滚动属性置为「已在底部」→ 按钮隐藏
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    // 模拟用户上翻：scrollTop=0（远小于 scrollHeight）
    Object.defineProperty(viewport, "scrollTop", { value: 0, configurable: true, writable: true });
    Object.defineProperty(viewport, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(viewport, "clientHeight", { value: 400, configurable: true });
    // 触发 scroll（capture listener 会拾取）
    act(() => {
      viewport.dispatchEvent(new Event("scroll", { bubbles: false }));
    });
    expect(container.querySelector(".transcript-back-to-bottom")).not.toBeNull();
    // 点击回底
    act(() => {
      container.querySelector(".transcript-back-to-bottom")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(viewport.scrollTop).toBe(1000); // 滚到底
  });

  it("新消息到达时若在底部则自动滚底（autoScroll 标志）", () => {
    const store = createTranscriptStore({ t: (k) => k });
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    Object.defineProperty(viewport, "scrollTop", { value: 1000, configurable: true, writable: true });
    Object.defineProperty(viewport, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(viewport, "clientHeight", { value: 400, configurable: true });
    act(() => {
      viewport.dispatchEvent(new Event("scroll", { bubbles: false }));
    });
    // 底部 → autoScroll=true；新消息到达 → 自动滚到底
    store.addUserMessage("new", "s1");
    expect(viewport.scrollTop).toBe(1000); // autoScroll 触发 scrollTop=scrollHeight
  });
});
