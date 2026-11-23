import { afterEach, describe, expect, it, vi } from "vitest";
// 样式表走 Vite 的 `?raw`，不用 `node:fs`：渲染层的 tsconfig 只声明
// `vite/client` 与 `@testing-library/jest-dom`，`node:fs` 在本机因为 `emrg/gui/node_modules`
// 提升了 `@types/node` 才编得过，而 CI 在 `emrg/gui/renderer` 里 `npm ci` 没有这个邻居——
// 同一份文件这里绿、那里红（`tests/test_renderer_node_types.py` 钉的就是这条）。
import shellCss from "../shell.css?raw";
import { act } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TOOL_GROUP_TICK_MS, TranscriptView, summaryLine } from "./TranscriptView";
import { createTranscriptStore, type ToolGroup, type TranscriptStore } from "../lib/transcript";
import { I18nProvider, t as translate } from "../lib/i18n";
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
    resumeStream: async () => {},
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

function setup(
  store: TranscriptStore,
  sid?: string | null,
  renderer?: MarkdownRenderer,
  paging?: { hasMore?: boolean; loading?: boolean; onLoadOlder?: () => void },
) {
  return render(
    <I18nProvider lang="zh">
      <TranscriptView
        store={store}
        sid={sid}
        renderer={renderer ?? fakeMd}
        hasMore={paging?.hasMore}
        loading={paging?.loading}
        onLoadOlder={paging?.onLoadOlder}
      />
    </I18nProvider>,
  );
}

describe("TranscriptView", () => {
  it("渲染用户/系统消息与历史加载条", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("hello", "s1");
    store.addSystemMessage("system note", "s1");
    store.setLoadBar("加载历史中…", "s1");
    const { container } = setup(store, "s1");
    // Stage 1：用户消息走 markdown 渲染 → 文本在 .msg.user 内的渲染 span 里
    expect(screen.getByText("system note")).toHaveClass("msg", "system");
    const userDiv = container.querySelector(".msg.user");
    expect(userDiv).not.toBeNull();
    expect(userDiv!.textContent).toBe("hello");
    expect(container.querySelector(".history-load-bar")).toHaveTextContent("加载历史中…");
  });

  it("顶部历史条可加载更早历史时是一个真按钮，点击触发上翻回调（宿主报障 2026-10-08）", () => {
    // 宿主原话：「另外点击加载更早消息，没有任何效果」。CSS 一直有 `cursor: pointer`、文案一直
    // 是祈使句，实体却是一个没有 onClick 的 div —— 可点的样子、不可点的实体。
    const store = createTranscriptStore({ t: (k) => k });
    store.setLoadBar("↑ 加载更早消息", "s1");
    const onLoadOlder = vi.fn();
    setup(store, "s1", undefined, { hasMore: true, onLoadOlder });
    const bar = screen.getByTestId("history-load-bar");
    expect(bar.tagName).toBe("BUTTON");
    fireEvent.click(bar);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it("没有更早历史时顶部条退化为状态文字，不再是控件（点击无处可去）", () => {
    // 「没有更多历史」/ 正在加载 —— 此时它只是状态行：没有 button 角色，点它也不触发回调。
    const store = createTranscriptStore({ t: (k) => k });
    store.setLoadBar("没有更多历史", "s1");
    const onLoadOlder = vi.fn();
    const { container } = setup(store, "s1", undefined, { hasMore: false, onLoadOlder });
    const bar = container.querySelector(".history-load-bar")!;
    expect(bar.tagName).toBe("DIV");
    expect(screen.queryByRole("button")).toBeNull();
    fireEvent.click(bar);
    expect(onLoadOlder).not.toHaveBeenCalled();
  });

  it("正在取更早的一页（loading）时顶部条也不再是控件：加载中不可重复触发", () => {
    // rant 2026-10-09T09:25:00 的验收「加载中不可重复触发」的控件那一半：in-flight 期间
    // 顶条既不该被滚到顶再次触发（上面那条），也不该仍长成可点的按钮（这条）。
    const store = createTranscriptStore({ t: (k) => k });
    store.setLoadBar("↑ 加载更早消息", "s1");
    const onLoadOlder = vi.fn();
    const { container } = setup(store, "s1", undefined, { hasMore: true, loading: true, onLoadOlder });
    const bar = container.querySelector(".history-load-bar")!;
    expect(bar.tagName).toBe("DIV");
    fireEvent.click(bar);
    expect(onLoadOlder).not.toHaveBeenCalled();
  });

  /**
   * 滚到顶的触发判定 —— 断言落在**容器接缝**上（派发真实 scroll 事件，看回调有没有被叫），
   * 而不是去 grep 源码里有没有出现 `shouldLoadOlder`。这组测试就是 issue #1979 的判别器：
   * 判定的生产读取点收敛到 `lib/history.ts` 之后，改库里的阈值（`<= 2`）或拆掉 `!loading`
   * 那一半，这里必须变红；收敛之前生产路径读的是 `TranscriptView` 自己那份副本，改库
   * **毫无效果**，这组断言在那时是绿的 —— 那正是缺陷本身。
   */
  function scrollViewportTo(container: HTMLElement, scrollTop: number) {
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    Object.defineProperty(viewport, "scrollTop", { value: scrollTop, configurable: true, writable: true });
    Object.defineProperty(viewport, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(viewport, "clientHeight", { value: 400, configurable: true });
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll", { bubbles: false }));
    });
  }

  it("滚到顶（scrollTop=0）且还有更早的一页 → 触发上翻回调", () => {
    const store = createTranscriptStore({ t: (k) => k });
    const onLoadOlder = vi.fn();
    const { container } = setup(store, "s1", undefined, { hasMore: true, onLoadOlder });
    scrollViewportTo(container, 0);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    // 阈值是闭区间（vanilla `scrollTop <= 2`）：2 也算到顶，3 不算
    scrollViewportTo(container, 2);
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it("没滚到顶（scrollTop=3）不触发上翻", () => {
    const store = createTranscriptStore({ t: (k) => k });
    const onLoadOlder = vi.fn();
    const { container } = setup(store, "s1", undefined, { hasMore: true, onLoadOlder });
    scrollViewportTo(container, 3);
    expect(onLoadOlder).not.toHaveBeenCalled();
  });

  it("正在取更早的一页（loading）时滚到顶不再触发：in-flight 锁在判定里，不靠调用方自觉", () => {
    // `loading` 那一半一旦丢，连滚会把同一页请求重复发出去（游标没动），这是收敛后的
    // 判定式里唯一防重入的地方，所以它必须由接缝测试钉住。
    const store = createTranscriptStore({ t: (k) => k });
    const onLoadOlder = vi.fn();
    const { container } = setup(store, "s1", undefined, { hasMore: true, loading: true, onLoadOlder });
    scrollViewportTo(container, 0);
    expect(onLoadOlder).not.toHaveBeenCalled();
  });

  it("没有更早的一页（hasMore=false）时滚到顶不触发", () => {
    const store = createTranscriptStore({ t: (k) => k });
    const onLoadOlder = vi.fn();
    const { container } = setup(store, "s1", undefined, { hasMore: false, onLoadOlder });
    scrollViewportTo(container, 0);
    expect(onLoadOlder).not.toHaveBeenCalled();
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

  it("折叠行 = 工具名称 + intent；展开 = 名称/输入/输出三段（rant 2026-09-30T09:17:54 → #1787）", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart(
      {
        tool_call_id: "t1",
        tool_name: "bash",
        request_id: "r1",
        intent: "check config",
        arguments: { command: "ls -la", intent: "check config" },
      },
      "s1",
    );
    store.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 0.4, content: "out" }, "s1");
    const { container } = setup(store, "s1");
    const row = container.querySelector(".tool-row")!;

    // 折叠行本身就该看得见名称与 intent（宿主裁定：两端一致）
    expect(row.querySelector(".tool-name")).toHaveTextContent("bash");
    expect(row.querySelector(".tool-intent")).toHaveTextContent("check config");

    // 面板默认隐藏，但三段都在（标题文案来自字典，与 TUI 同语）
    const output = container.querySelector(".tool-output")!;
    expect(output).toHaveClass("hidden");
    const titles = [...output.querySelectorAll(".tool-detail-title")].map((e) => e.textContent);
    expect(titles).toEqual(["名称", "输入", "输出"]);
    const bodies = [...output.querySelectorAll(".tool-detail-body")].map((e) => e.textContent);
    expect(bodies[0]).toBe("bash");
    expect(bodies[1]).toContain('"command": "ls -la"');
    expect(bodies[2]).toContain("out");

    // 点击行 → 展开（同时挂上 expanded，chevron 规则不再是死代码）
    act(() => {
      (row as HTMLElement).click();
    });
    expect(output).not.toHaveClass("hidden");
    expect(row).toHaveClass("expanded");
  });

  it("无入参的工具行不渲染「输入」段（daemon 对空载荷发 {}，画成一段噪音不如不画）", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart(
      { tool_call_id: "t1", tool_name: "read", request_id: "r1", arguments: {} },
      "s1",
    );
    store.handleToolEnd({ tool_call_id: "t1", tool_name: "read", elapsed: 0.1, content: "body" }, "s1");
    const { container } = setup(store, "s1");
    const titles = [...container.querySelectorAll(".tool-detail-title")].map((e) => e.textContent);
    expect(titles).toEqual(["名称", "输出"]);
  });

  it("入参超 2000 字沿用同一套截断，展开全文按钮同时服务输入与输出", () => {
    const long = `{"command":"x${"y".repeat(2500)}"}`;
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart(
      { tool_call_id: "t1", tool_name: "bash", request_id: "r1", arguments: long },
      "s1",
    );
    store.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", content: "out" }, "s1");
    const { container } = setup(store, "s1");
    const body = container.querySelectorAll(".tool-detail-body")[1]!;
    expect(body.textContent!.length).toBeLessThan(2100);
    expect(body.textContent!.endsWith("…")).toBe(true);
    // 展开全文后完整（输入自身超限，输出没超——按钮不能只认输出）
    const btn = container.querySelector(".tool-expand-btn")!;
    act(() => {
      (btn as HTMLElement).click();
    });
    const full = container.querySelectorAll(".tool-detail-body")[1]!;
    expect(full.textContent!.length).toBeGreaterThan(2500);
    expect(full.textContent!.endsWith("…")).toBe(false);
  });

  it("同批第几个显示在工具名之前，daemon 给什么就打印什么（rant 2026-10-20T12:22:21）", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart(
      { tool_call_id: "t1", tool_name: "bash", request_id: "r1", intent: "check config", call_label: "(2/3) " },
      "s1",
    );
    const { container } = setup(store, "s1");
    const row = container.querySelector(".tool-row")!;
    const label = row.querySelector(".tool-call-label");
    // 逐字比较：toHaveTextContent 会归一空白，正好抹掉那个尾随空格——而那个空格是
    // 协议的一部分（label 自带尾随空格，客户端不再自己拼）。
    expect(label!.textContent).toBe("(2/3) ");
    // 位置：label 紧贴在工具名之前，不是名字后面、也不是 intent 后面
    const name = row.querySelector(".tool-name")!;
    expect(label!.nextElementSibling).toBe(name);
    expect(name).toHaveTextContent("bash");
    expect(row.textContent!.indexOf("(2/3) ")).toBeLessThan(row.textContent!.indexOf("bash"));
  });

  it("没有 call_label 时行与改动前逐字相同：不出现 (1/1)、不出现空括号", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1", intent: "check config" }, "s1");
    const { container } = setup(store, "s1");
    const row = container.querySelector(".tool-row")!;
    // 缺字段 = 连节点都没有（空 span 也会让「逐字相同」不成立）
    expect(row.querySelector(".tool-call-label")).toBeNull();
    expect(row.querySelector(".tool-name")).toHaveTextContent(/^bash$/);
    expect(row.textContent).not.toContain("(1/1)");
    expect(row.textContent).not.toContain("(");
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

  it("连续工具合并组：新组默认收起，摘要带最后一行，收起只由点击决定（#2980 / rant 2026-10-17T19:23:08）", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
    store.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 0.4, content: "" }, "s1");
    store.handleToolStart({ tool_call_id: "t2", tool_name: "read", request_id: "r1" }, "s1");
    store.handleToolEnd({ tool_call_id: "t2", tool_name: "read", elapsed: 1.2, content: "" }, "s1");
    const { container } = setup(store, "s1");
    const group = container.querySelector(".tool-group")!;
    // #2980：宿主还没点过 bar，新组就是**收起**的——跑过十几个工具的一轮不再先把十几行
    // 铺开。旧版本在这里断言 `not.toHaveClass("collapsed")`，那正是「新组一律展开」的来源。
    expect(group).toHaveClass("collapsed");
    expect(container.querySelectorAll(".tool-row")).toHaveLength(2);
    // 摘要是「几次 + 多久 + 最后一行」（#2980 验收 3）。
    expect(container.querySelector(".tool-group-summary")).toHaveTextContent(
      "2个工具执行 1.6s（read 已读取文件）",
    );
    // 摘要里的最后一行就是点开后那一行**折叠形态**的文字：两处各写一套措辞就会是同一个
    // 事实的两个说法。这里从句子的渲染产物上取，不从函数上取。
    const latestRow = container.querySelectorAll(".tool-row")[1]!;
    const renderedLatest = [".tool-call-label", ".tool-name", ".tool-label", ".tool-intent"]
      .map((sel) => latestRow.querySelector(sel)?.textContent ?? "")
      .filter(Boolean)
      .join(" ");
    expect(renderedLatest).toBe("read 已读取文件");
    expect(container.querySelector(".tool-group-summary")!.textContent).toContain(
      `（${renderedLatest}）`,
    );
    // 新工具并入组：行数增长，摘要随 tool_start 就刷新（不等 tool_end），状态不动
    act(() => {
      store.handleToolStart(
        { tool_call_id: "t3", tool_name: "edit", request_id: "r1", intent: "改一处排版" },
        "s1",
      );
    });
    expect(container.querySelectorAll(".tool-row")).toHaveLength(3);
    expect(group).toHaveClass("collapsed");
    expect(container.querySelector(".tool-group-summary")).toHaveTextContent(
      "3个工具执行 1.6s（edit 正在修改文件… 改一处排版）",
    );
    act(() => {
      store.handleToolEnd({ tool_call_id: "t3", tool_name: "edit", elapsed: 0.3, content: "" }, "s1");
    });
    expect(container.querySelectorAll(".tool-row")).toHaveLength(3);
    expect(group).toHaveClass("collapsed");
    expect(container.querySelector(".tool-group-summary")).toHaveTextContent(
      "3个工具执行 1.9s（edit 已修改文件 改一处排版）",
    );
    // 点击 bar → 展开（唯一入口）
    act(() => {
      (container.querySelector(".tool-group-bar") as HTMLElement).click();
    });
    expect(group).not.toHaveClass("collapsed");
    // 展开之后再来一个工具：仍然展开，不因为 tool_start 就收起
    act(() => {
      store.handleToolStart({ tool_call_id: "t4", tool_name: "bash", request_id: "r1" }, "s1");
    });
    expect(group).not.toHaveClass("collapsed");
  });

  it("表头耗时：还在跑的工具也计入，每 0.1 秒走一格，全部结束即停表（rant 2026-11-19T09:31:00）", () => {
    vi.useFakeTimers();
    try {
      // 「现在」钉在整秒上，起点就落在这一刻：表头第一眼是已量过的 0.4s + 在跑行的 0.0s
      vi.setSystemTime(new Date("2026-11-19T00:00:00Z"));
      const startedAtSec = Math.floor(Date.now() / 1000);
      const store = createTranscriptStore({ t: (k) => k });
      store.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
      store.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 0.4, content: "" }, "s1");
      store.handleToolStart(
        { tool_call_id: "t2", tool_name: "read", request_id: "r1", started_at: startedAtSec },
        "s1",
      );
      const { container } = setup(store, "s1");
      const header = () => container.querySelector(".tool-group-summary")!.textContent ?? "";

      // 在跑的那一行不再是「贡献 0」：0.4s 之外时间照样出现，正因为它有了起点
      expect(header()).toBe("2个工具执行 0.4s（read 正在读取文件…）");
      expect(vi.getTimerCount(), "在跑的组应当挂着表头刷新定时器").toBe(1);

      // 一格一格推进：每次只推一个间隔、每次都要求表头正好加 0.1。
      // 一次推 1.2 秒只会证明「推得够远就会重渲染」——间隔被错手改成 1 秒，那样推法照样
      // 通过（第一次推就跨过了唯一的定时器），而宿主看到的是一秒跳一下。
      for (let k = 1; k <= 12; k += 1) {
        act(() => {
          vi.advanceTimersByTime(TOOL_GROUP_TICK_MS);
        });
        expect(header(), `第 ${k} 格`).toBe(
          `2个工具执行 ${(0.4 + k / 10).toFixed(1)}s（read 正在读取文件…）`,
        );
      }

      // 结束：daemon 量出来的 elapsed 顶上来，本地推算消失，定时器跟着清掉
      act(() => {
        store.handleToolEnd({ tool_call_id: "t2", tool_name: "read", elapsed: 1.5, content: "" }, "s1");
      });
      expect(header()).toBe("2个工具执行 1.9s（read 已读取文件）");
      expect(vi.getTimerCount(), "组内全部结束后不应留常驻定时器").toBe(0);
      // 停表之后再推时钟：数字一动不动（定时器真的没了，不是「还在跑但这一次没触发」）
      act(() => {
        vi.advanceTimersByTime(2000);
      });
      expect(header()).toBe("2个工具执行 1.9s（read 已读取文件）");
    } finally {
      vi.useRealTimers();
    }
  });

  it("老帧没有起点：在跑的组不显示时间、也不挂定时器（rant 2026-11-19T09:31:00）", () => {
    vi.useFakeTimers();
    try {
      const store = createTranscriptStore({ t: (k) => k });
      store.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
      // 前一个工具必须先结束才会成组（两个同时在跑的行各自独立成行）
      store.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", content: "" }, "s1");
      store.handleToolStart({ tool_call_id: "t2", tool_name: "read", request_id: "r1" }, "s1");
      const { container } = setup(store, "s1");
      // 没有起点就没有可算的区间：宁可整段时间不出现，也不拿 0 充数（#2980 同一口径）
      expect(container.querySelector(".tool-group-summary")!.textContent).toBe(
        "2个工具执行（read 正在读取文件…）",
      );
      expect(vi.getTimerCount(), "没有起点就不该有定时器在空转").toBe(0);
    } finally {
      vi.useRealTimers();
    }
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
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
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
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll", { bubbles: false }));
    });
    // 底部 → autoScroll=true；新消息到达 → 自动滚到底
    store.addUserMessage("new", "s1");
    expect(viewport.scrollTop).toBe(1000); // autoScroll 触发 scrollTop=scrollHeight
  });

  // ── 前插滚差补偿（宿主报障「加载更早消息」的编排层；`Shell.tsx:462` 的注释一直写着
  //    「整块前插 + 滚差补偿由渲染层处理」，而 `lib/history.ts` 的 `scrollCompensation`
  //    此前只有单测、零生产调用者）。两条互为反面：前插必须补偿，追加必须不动。 ──

  /** jsdom 无布局：用**已渲染的消息数**折算文档高度，于是高度随 DOM 一起在提交中变化——
   *  渲染期读到旧值、layout effect 读到新值，与浏览器里插入 DOM 的时序一致（写死一个
   *  数字只能测「高度差」这一半，测不到「插入前读还是插入后读」）。 */
  function mockHeightByMessages(viewport: HTMLElement) {
    Object.defineProperty(viewport, "scrollHeight", {
      configurable: true,
      get: () => (viewport.querySelectorAll(".msg").length + 1) * 400,
    });
    Object.defineProperty(viewport, "clientHeight", { value: 400, configurable: true });
    Object.defineProperty(viewport, "scrollTop", { value: 0, configurable: true, writable: true });
  }

  it("更早一页前插后按高度差补偿 scrollTop（上翻一页不再跳一屏）", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("newest", "s1");
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    mockHeightByMessages(viewport); // 1 条 → 800
    // 上翻到顶：scrollTop=0（派发 scroll 让 atBottom 翻转，走一次真实提交）
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll", { bubbles: false }));
    });
    // 更早一页插到最前 → DOM 变高 400（2 条 → 1200）
    act(() => {
      store.prependEntries([{ kind: "user", id: "t-prepended-older", text: "older" }], "s1");
    });
    expect(viewport.scrollTop).toBe(400); // 0 + (1200 - 800)
  });

  it("追加（非前插）不补偿 scrollTop：文档同样变高，但内容在后面，视口不动", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("newest", "s1");
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    mockHeightByMessages(viewport);
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll", { bubbles: false }));
    });
    // 同样长高 400——若补偿只看「高度变了」而不看「是前插」，这里会错误地跳到 400
    act(() => {
      store.addUserMessage("newer", "s1");
    });
    expect(viewport.scrollTop).toBe(0);
  });
});

/**
 * 消息行上的时刻（rant 2026-10-17T10:03:08 → issue #2340）。断言读的是**渲染产物**：
 * 类型上有没有字段、store 里有没有值，都不算 —— 宿主看的是屏幕上那一行。
 */
describe("消息时刻", () => {
  it("用户行与助手行各显示 daemon 给的那个 HH:MM", async () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("hi", "s1", {
      requestId: "req-1",
      timestamp: "2026-10-17T10:27:55.123456+08:00",
    });
    store.handleDelta([{ request_id: "req-1", content: "hello" }], "s1");
    store.handleDone({ request_id: "req-1", timestamp: "2026-10-17T10:28:09+08:00" }, "s1");
    const { container } = setup(store, "s1");

    const times = Array.from(container.querySelectorAll(".msg-time")).map((el) => el.textContent);
    expect(times).toEqual(["10:27", "10:28"]);
    // 完整日期挂在悬停上，行上只留时钟
    expect(container.querySelector(".msg.user .msg-time")!.getAttribute("title")).toBe(
      "2026-10-17 10:27",
    );
    // 助手正文照旧渲染（时间元素没把消息顶掉）
    await waitFor(() => expect(screen.getByText("hello")).toBeInTheDocument());
  });

  it("没有时刻的行不渲染任何时间元素：不是空括号，不是 Invalid Date，不是 NaN", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("no clock", "s1"); // 帧还没来（或本就没有时刻的老记录）
    store.handleDelta([{ request_id: "r1", content: "reply" }], "s1");
    store.handleDone({ request_id: "r1", timeout: true }, "s1"); // 取消：没有回复落盘
    const { container } = setup(store, "s1");

    expect(container.querySelector(".msg-time")).toBeNull();
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/Invalid|NaN|\(\)|（）|\[\]/);
  });

  it("坏时刻同样什么都不画（记录可读性由格式器裁决，视图不做第二次猜测）", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("bad clock", "s1", { requestId: "req-1", timestamp: "not a date" });
    const { container } = setup(store, "s1");
    expect(container.querySelector(".msg-time")).toBeNull();
  });
});

/**
 * 流式隔离：一个 token 只重渲染正在变化的那一条。
 *
 * 断言数的是**每条条目各自的重渲染次数**。`useSyncExternalStore` 的 getSnapshot 只在该
 * 条目的组件渲染时被调用，所以「某个 id 的 getEntryVersion 被调了几次」就是它被重渲染了
 * 几次 —— 这正是宿主看见的成本：旧实现每来一个 token 就把整条会话的条目全部渲染一遍，
 * 会话越长越慢。
 *
 * 判别力（两向都量过）：把 `TranscriptView` 的订阅改回 `store.getVersion`（父组件每帧
 * 重渲染、条目元素整批重建），下面第一条与第三条立刻变红 —— 那时每个条目的 getSnapshot
 * 都会被各调一次。只加 `React.memo` 挡不住它：那挡的是 props 未变的子组件，而这里父组件
 * 的每帧重渲染会把新元素交给每一个条目。
 */
describe("流式只重渲染正在变化的那一条", () => {
  /** 把 getEntryVersion 包成计数器：每条条目被渲染几次，就记几次 */
  function withRenderCounter(store: TranscriptStore): {
    store: TranscriptStore;
    rendersOf: (id: string) => number;
    reset: () => void;
  } {
    const renders = new Map<string, number>();
    const wrapped: TranscriptStore = {
      ...store,
      getEntryVersion: (id) => {
        renders.set(id, (renders.get(id) ?? 0) + 1);
        return store.getEntryVersion(id);
      },
    };
    return {
      store: wrapped,
      rendersOf: (id) => renders.get(id) ?? 0,
      reset: () => renders.clear(),
    };
  }

  /** 若干条已结束的历史 + 一条刚开流的助手条目 */
  function historyThenStream(history = 2): { base: TranscriptStore; liveId: string; otherIds: string[] } {
    const base = createTranscriptStore({ t: (k) => k });
    for (let n = 0; n < history; n += 1) {
      base.addUserMessage(`q${n}`, "s1");
      base.handleDelta([{ request_id: `r${n}`, content: `a${n}` }], "s1");
      base.handleDone({ request_id: `r${n}` }, "s1");
    }
    base.handleDelta([{ request_id: "live", content: "hello" }], "s1");
    const entries = base.getEntries("s1");
    return {
      base,
      liveId: entries[entries.length - 1].id,
      otherIds: entries.slice(0, -1).map((e) => e.id),
    };
  }

  it("一个 token 只重渲染正在写的那一条；其余历史条目一次都不重渲染", () => {
    const { base, liveId, otherIds } = historyThenStream();
    const { store, rendersOf, reset } = withRenderCounter(base);
    setup(store, "s1");
    // 非空主体：没有历史条目时「其余条目零渲染」什么都证明不了
    expect(otherIds.length).toBeGreaterThan(1);
    const structure = base.getStructureVersion();
    reset();

    act(() => {
      base.handleDelta([{ request_id: "live", content: " world" }], "s1");
    });

    expect(rendersOf(liveId)).toBeGreaterThan(0);
    for (const id of otherIds) expect(rendersOf(id)).toBe(0);
    // 另一半：文本增长不算结构变化，列表不必重读
    expect(base.getStructureVersion()).toBe(structure);
  });

  it("会话有多长都不影响每帧：50 条历史里一个 token 仍然只渲染那一条", () => {
    const { base, liveId, otherIds } = historyThenStream(50);
    const { store, rendersOf, reset } = withRenderCounter(base);
    setup(store, "s1");
    expect(otherIds.length).toBe(100); // 50 轮 → 每条提问一条回复，加一条在流式里
    reset();

    act(() => {
      base.handleDelta([{ request_id: "live", content: "!" }], "s1");
    });

    expect(rendersOf(liveId)).toBeGreaterThan(0);
    // 长会话的每帧成本 = 1 条，不是 101 条
    expect(otherIds.reduce((acc, id) => acc + rendersOf(id), 0)).toBe(0);
  });

  it("条目集合变化时列表照旧更新：新条目出现在 DOM 里（结构通道没被焊死）", async () => {
    const { base } = historyThenStream();
    const { store } = withRenderCounter(base);
    setup(store, "s1");

    await act(async () => {
      base.addUserMessage("brand new", "s1");
    });

    await waitFor(() => expect(screen.getByText("brand new")).toBeInTheDocument());
  });

  it("两次 delta 的文本都进了渲染产物（订阅下沉之后流式没有冻住）", () => {
    const base = createTranscriptStore({ t: (k) => k });
    const { store } = withRenderCounter(base);
    setup(store, "s1");

    act(() => {
      base.handleDelta([{ request_id: "live", content: "hello" }], "s1");
    });
    expect(screen.getByText("hello")).toBeInTheDocument();

    act(() => {
      base.handleDelta([{ request_id: "live", content: " world" }], "s1");
    });
    // 第二个 token 的文本必须出现在屏幕上：订阅下沉后若某一帧没重渲染，流式就冻在那里了
    expect(screen.getByText("hello world")).toBeInTheDocument();
  });
});

/**
 * #2980 验收 3 的四种缺省，一句一句钉住。
 *
 * 有两种缺省在屏幕上都表现为「少一点东西」：没有量过耗时的时候，消失的是时间；没有最后
 * 一行的时候，消失的是整个括号。只有分开断言，才说得清消失的不是数量、也不是一对空括号。
 * 用的 t 是真词典（`translate(k, p, "zh")`）——断言的字符串就是宿主看到的那一行。
 */
describe("#2980：组摘要的四种缺省", () => {
  const zh = (k: string, p?: Record<string, unknown>) => translate(k, p, "zh");

  /** 组由真 store 生成：`count` 与 `timed` 是 `updateToolGroup` 算出来的，不是这里拼的。 */
  function groupOf(first: number | undefined, second: number | undefined): ToolGroup {
    const store = createTranscriptStore({ t: (k) => k });
    const calls: Array<[string, string, number | undefined]> = [
      ["a", "bash", first],
      ["b", "read", second],
    ];
    for (const [id, name, elapsed] of calls) {
      store.handleToolStart({ tool_call_id: id, tool_name: name, request_id: "r1" }, "s1");
      store.handleToolEnd(
        { tool_call_id: id, tool_name: name, content: "", ...(elapsed === undefined ? {} : { elapsed }) },
        "s1",
      );
    }
    const entry = store.getEntries("s1").find((e) => e.kind === "tool-group");
    expect(entry, "两个连续的工具没有并成一组").toBeDefined();
    return (entry as { group: ToolGroup }).group;
  }

  it("有时间、有最后一行 → 数量 + 时间 + 折起来的那一行", () => {
    expect(summaryLine(groupOf(0.4, 1.2), "read 已读取文件", zh)).toBe(
      "2个工具执行 1.6s（read 已读取文件）",
    );
  });

  it("有时间、没有最后一行 → 括号整组不出现，不是一对空括号", () => {
    expect(summaryLine(groupOf(0.4, 1.2), "", zh)).toBe("2个工具执行 1.6s");
  });

  it("没有时间、有最后一行 → 不出现 0.0s（重放的老记录一行都没量过）", () => {
    expect(summaryLine(groupOf(undefined, undefined), "read 已读取文件", zh)).toBe(
      "2个工具执行（read 已读取文件）",
    );
  });

  it("既没有时间也没有最后一行 → 只剩数量", () => {
    expect(summaryLine(groupOf(undefined, undefined), "", zh)).toBe("2个工具执行");
  });

  /** 一行已量过 0.5s，另一行还在跑、带起点：`nowMs` 一传，在跑那行就按「现在 − 起点」入账。 */
  function liveGroup(startedAtMs: number): ToolGroup {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart({ tool_call_id: "a", tool_name: "bash", request_id: "r1" }, "s1");
    store.handleToolEnd({ tool_call_id: "a", tool_name: "bash", content: "", elapsed: 0.5 }, "s1");
    store.handleToolStart(
      { tool_call_id: "b", tool_name: "read", request_id: "r1", started_at: startedAtMs / 1000 },
      "s1",
    );
    const entry = store.getEntries("s1").find((e) => e.kind === "tool-group");
    expect(entry, "两个连续的工具没有并成一组").toBeDefined();
    return (entry as { group: ToolGroup }).group;
  }

  it("在跑的那一行按起点入账：已量 0.5s + 在跑 2.0s = 2.5s", () => {
    expect(summaryLine(liveGroup(1_000_000), "read 正在读取文件…", zh, 1_002_000)).toBe(
      "2个工具执行 2.5s（read 正在读取文件…）",
    );
  });

  it("起点在未来（墙钟往回跳）：夹到 0，表头不出现负数", () => {
    expect(summaryLine(liveGroup(1_000_000), "read 正在读取文件…", zh, 999_000)).toBe(
      "2个工具执行 0.5s（read 正在读取文件…）",
    );
  });

  it("结束的行带着起点也只算 daemon 的 elapsed：本地推算不叠上去", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart(
      { tool_call_id: "a", tool_name: "bash", request_id: "r1", started_at: 1_000 },
      "s1",
    );
    store.handleToolEnd({ tool_call_id: "a", tool_name: "bash", content: "", elapsed: 0.5 }, "s1");
    store.handleToolStart(
      { tool_call_id: "b", tool_name: "read", request_id: "r1", started_at: 1_002 },
      "s1",
    );
    const entry = store.getEntries("s1").find((e) => e.kind === "tool-group");
    expect(entry, "两个连续的工具没有并成一组").toBeDefined();
    // 「现在」= 起点后 3.0s：在跑那行入账 3.0s；已结束的 a 起点在 5s 之前，但它只算 0.5s
    // ——两个数都算就是 8.5s，屏幕上那个数就既不是 daemon 量的、也不是宿主等的。
    expect(
      summaryLine((entry as { group: ToolGroup }).group, "read 正在读取文件…", zh, 1_005_000),
    ).toBe("2个工具执行 3.5s（read 正在读取文件…）");
  });

  it("不传 nowMs：与改动前逐字一致，在跑的行不入账", () => {
    expect(summaryLine(liveGroup(1_000_000), "read 正在读取文件…", zh)).toBe(
      "2个工具执行 0.5s（read 正在读取文件…）",
    );
  });

  it("已量过的行为零、在跑的行有起点 → 时间照样出现（旧版把它算作「一行都没量过」）", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleToolStart({ tool_call_id: "a", tool_name: "bash", request_id: "r1" }, "s1");
    // 没带 elapsed 的结束帧（失败/取消）：这一行什么都没量到，但它让下一行能并进组
    store.handleToolEnd({ tool_call_id: "a", tool_name: "bash", content: "" }, "s1");
    store.handleToolStart(
      { tool_call_id: "b", tool_name: "read", request_id: "r1", started_at: 1_000 },
      "s1",
    );
    const entry = store.getEntries("s1").find((e) => e.kind === "tool-group");
    expect(entry, "两个连续的工具没有并成一组").toBeDefined();
    expect(
      summaryLine((entry as { group: ToolGroup }).group, "read 正在读取文件…", zh, 1_001_500),
    ).toBe("2个工具执行 1.5s（read 正在读取文件…）");
  });
});

/**
 * 聊天区滚动（issue #3024）：按钮的落点、贴底判定、高度变化的重算，以及每条会话自己的
 * 位置。
 *
 * jsdom 既没有布局也没有真正的滚动。三条度量挂在 `HTMLElement.prototype` 上、读一个可变
 * 对象，于是**重新挂载出来的新元素也戴同一副尺子**（切页回来那条用例正需要这一点）；
 * 「按钮在不在可视区内」读的是结构与真实样式表的定位基准 —— 一次假造的屏幕坐标证明不了
 * 任何事。
 */
describe("聊天区滚动（issue #3024）", () => {
  const metrics = { scrollTop: 0, scrollHeight: 0, clientHeight: 400 };

  function installMetrics(): void {
    Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
      configurable: true,
      get: () => metrics.scrollHeight,
    });
    Object.defineProperty(HTMLElement.prototype, "clientHeight", {
      configurable: true,
      get: () => metrics.clientHeight,
    });
    Object.defineProperty(HTMLElement.prototype, "scrollTop", {
      configurable: true,
      get: () => metrics.scrollTop,
      set: (v: number) => {
        metrics.scrollTop = v;
      },
    });
  }

  afterEach(() => {
    // jsdom 把这三条挂在 Element.prototype 上：删掉自造的那份就还原了
    delete (HTMLElement.prototype as { scrollHeight?: unknown }).scrollHeight;
    delete (HTMLElement.prototype as { clientHeight?: unknown }).clientHeight;
    delete (HTMLElement.prototype as { scrollTop?: unknown }).scrollTop;
    metrics.scrollTop = 0;
    metrics.scrollHeight = 0;
    metrics.clientHeight = 400;
    vi.unstubAllGlobals();
  });

  type Memory = { current: Map<string, { scrollTop: number; following: boolean }> };

  function view(store: TranscriptStore, sid: string | null, memory: Memory) {
    return (
      <I18nProvider lang="zh">
        <TranscriptView store={store} sid={sid} renderer={fakeMd} scrollMemory={memory} />
      </I18nProvider>
    );
  }

  function metricsTo(scrollHeight: number, scrollTop: number, clientHeight = 400): void {
    metrics.scrollHeight = scrollHeight;
    metrics.scrollTop = scrollTop;
    metrics.clientHeight = clientHeight;
  }

  it("按钮是滚动容器的兄弟而不是后代（清单第 1 条）", () => {
    installMetrics();
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("bottom", "s1");
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    metricsTo(1000, 0); // 上翻离底
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    const button = container.querySelector(".transcript-back-to-bottom");
    expect(button).not.toBeNull();
    // 挂在滚动容器内部 ⇒ 布局落在**滚动内容**的坐标系 ⇒ 随内容一起滚出可视区，
    // 判定说要显示而屏幕上什么也没有。
    expect(viewport.contains(button)).toBe(false);
    const shell = container.querySelector(".transcript-shell");
    expect(button!.parentElement).toBe(shell);
    expect(shell!.contains(viewport)).toBe(true);
  });

  it("壳是定位基准、按钮绝对定位于其上，滚动容器不再是（清单第 1 条）", () => {
    expect(shellCss.length).toBeGreaterThan(0); // 读成空串会让下面三条空转通过
    const ruleOf = (selector: string): string =>
      new RegExp(`${selector}\\s*\\{[^}]*\\}`).exec(shellCss)?.[0] ?? "";
    expect(ruleOf("\\.transcript-shell")).toMatch(/position:\s*relative/);
    expect(ruleOf("\\.transcript-back-to-bottom")).toMatch(/position:\s*absolute/);
    // 滚动容器若仍带 position:relative，按钮的定位基准就又回到滚动内容上
    expect(ruleOf("\\.transcript-view")).not.toMatch(/position:\s*relative/);
  });

  it("离底 1px 即显示按钮，贴底才隐藏（清单第 2 条：没有容差带）", () => {
    installMetrics();
    const store = createTranscriptStore({ t: (k) => k });
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;

    metricsTo(1000, 599); // 差 1px
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(container.querySelector(".transcript-back-to-bottom")).not.toBeNull();

    metricsTo(1000, 561); // 差 39px：旧的 40px 容差会把这里判成「在底部」
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(container.querySelector(".transcript-back-to-bottom")).not.toBeNull();

    metricsTo(1000, 600); // 贴底
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(container.querySelector(".transcript-back-to-bottom")).toBeNull();
  });

  it("内容变高却没有 scroll 事件时，判定与跟随同步更新（清单第 3、4 条）", () => {
    installMetrics();
    const callbacks: Array<() => void> = [];
    class FakeResizeObserver {
      constructor(cb: () => void) {
        callbacks.push(cb);
      }
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);

    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("x", "s1");
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    metricsTo(1000, 600); // 在底部
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(container.querySelector(".transcript-back-to-bottom")).toBeNull();
    expect(callbacks.length).toBeGreaterThan(0);

    // 图片解码完 / 文字重新折行：高度变了，**一个 scroll 事件都没有**
    metricsTo(1400, 600);
    act(() => {
      callbacks.forEach((cb) => cb());
    });
    // 本来就在底部 → 补滚到新的底部，而不是停在旧高度上
    expect(metrics.scrollTop).toBe(1400);
    expect(container.querySelector(".transcript-back-to-bottom")).toBeNull();

    // 不在底部时同一次高度变化只重算判定，不替读者滚动（读者自己滚上去，走真实的 scroll）
    metricsTo(1400, 100);
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    metricsTo(1800, 100); // 又长高 400，仍然没有 scroll 事件
    act(() => {
      callbacks.forEach((cb) => cb());
    });
    expect(metrics.scrollTop).toBe(100);
    expect(container.querySelector(".transcript-back-to-bottom")).not.toBeNull();
  });

  it("流式文字到达时照旧跟随：它是版本号而不是结构化消息（清单第 4 条）", () => {
    installMetrics();
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("a", "s1");
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    metricsTo(1000, 600); // 贴底
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    // 流式：第一条 delta 建出这条助手消息（结构确实变了），之后同一 request_id 的追加
    // 只动内容版本 —— 跟随必须对**后者**也生效，否则每来一个 token 就冻住一次。
    act(() => {
      store.handleDelta([{ request_id: "r1", content: "a" }], "s1");
    });
    metrics.scrollHeight = 1200;
    act(() => {
      store.handleDelta([{ request_id: "r1", content: "more" }], "s1");
    });
    expect(metrics.scrollTop).toBe(1200);
  });

  it("每条会话各记自己的位置，互切各回各位；没打开过的贴底（清单第 6、8 条）", () => {
    installMetrics();
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("a", "sa");
    const memory: Memory = { current: new Map() };
    const { container, rerender } = render(view(store, "sa", memory));
    const viewport = container.querySelector(".transcript-view") as HTMLElement;

    metricsTo(3000, 0);
    metrics.scrollTop = 1200;
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(memory.current.get("sa")).toEqual({ scrollTop: 1200, following: false });

    // 切到从没打开过的 sb（内容短得多）：贴底并跟随，而不是沿用 sa 的 1200
    metricsTo(800, 0);
    rerender(view(store, "sb", memory));
    expect(metrics.scrollTop).toBe(800);
    expect(memory.current.get("sb")?.following).toBe(true);

    // sb 停在 400，再切回 sa
    metricsTo(800, 400);
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    metricsTo(3000, 400);
    rerender(view(store, "sa", memory));
    expect(metrics.scrollTop).toBe(1200); // 自己那一处，不是 sb 的 400
    // 离开时不在跟随，回来仍不跟：新内容不把读者甩走
    store.addUserMessage("while away", "sa");
    expect(metrics.scrollTop).toBe(1200);
  });

  it("切到别的页面再回来（卸载后重新挂载）位置与跟随状态一并恢复（清单第 7 条）", () => {
    installMetrics();
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("a", "s1");
    const memory: Memory = { current: new Map() };

    const first = render(view(store, "s1", memory));
    const viewport = first.container.querySelector(".transcript-view") as HTMLElement;
    metricsTo(2000, 0);
    metrics.scrollTop = 700;
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(memory.current.get("s1")).toEqual({ scrollTop: 700, following: false });

    first.unmount(); // 切到 agent 页：聊天区整块被卸载
    store.addUserMessage("while away", "s1"); // 期间到了新内容，文档变高
    metricsTo(2400, 2400); // 重新挂载时浏览器给出的初值就是贴底

    const second = render(view(store, "s1", memory));
    const vp2 = second.container.querySelector(".transcript-view") as HTMLElement;
    // 回来落在离开时那一处，不是底部
    expect(metrics.scrollTop).toBe(700);
    expect(second.container.querySelector(".transcript-back-to-bottom")).not.toBeNull();
    // 跟随状态也一并恢复：不在底部，新内容不动视口
    act(() => {
      store.addUserMessage("after return", "s1");
    });
    expect(metrics.scrollTop).toBe(700);
    void vp2;
  });

  /**
   * 跟随只由读者自己的输入关掉（issue #3046）。
   *
   * 这一组的判别器是**帧的推进顺序**：先让内容变高，再让布局留下一个不在底部的位置，最后才
   * 派发 scroll 事件。只伪造一个数值再手动派发事件的写法，在缺陷还在的时候也会绿——那种写法
   * 证明不了任何事。
   */
  it("内容变高、布局留下一个不在底部的位置、随后才来 scroll 事件：跟随不断且补回底部（清单第 2、4 条）", () => {
    installMetrics();
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("x", "s1");
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    metricsTo(1000, 600); // 打开即贴底（max = 1000 - 400）
    // 一帧：工具组表头刷新让内容变高，布局把位置留在 600（离底 400）
    metricsTo(1400, 600);
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    // 读者没滚过 → 视口自己补回真正的底部，按钮不出现
    expect(metrics.scrollTop).toBe(1400);
    expect(container.querySelector(".transcript-back-to-bottom")).toBeNull();
  });

  it("补底写入的回声不算读者滚动：内容再长高时跟随仍在（清单第 3 条）", () => {
    installMetrics();
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("a", "s1");
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    metricsTo(1000, 600); // 贴底
    act(() => {
      store.handleDelta([{ request_id: "r1", content: "a" }], "s1");
    });
    // 浏览器对那次写入派发的 scroll 事件：位置正是写进去的值，且就在刚才
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(container.querySelector(".transcript-back-to-bottom")).toBeNull();
    // 再长高 400：若上一步被误读成读者滚动，这里就停在 1000 不动了
    metricsTo(1400, 1000);
    act(() => {
      store.handleDelta([{ request_id: "r1", content: "b" }], "s1");
    });
    expect(metrics.scrollTop).toBe(1400);
    expect(container.querySelector(".transcript-back-to-bottom")).toBeNull();
  });

  it("只有读者真的滚了才停止跟随：没有输入的 scroll 事件不停，滚轮之后的停（清单第 1 条反向）", () => {
    installMetrics();
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("a", "s1");
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    metricsTo(1000, 600); // 贴底跟随
    // 重排造成的位置变化，没有输入：不算读者
    metricsTo(1000, 560);
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(container.querySelector(".transcript-back-to-bottom")).toBeNull();
    // 滚轮真的来了，位置离开底部：这才是读者，按钮出现
    metricsTo(1000, 300);
    act(() => {
      viewport.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(container.querySelector(".transcript-back-to-bottom")).not.toBeNull();
  });

  it("按在正文上不算读者滚动，按在滚动条上才算（清单第 1 条）", () => {
    installMetrics();
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("a", "s1");
    const { container } = setup(store, "s1");
    const viewport = container.querySelector(".transcript-view") as HTMLElement;
    // 视口有 15px 纵向滚动条：clientWidth 785、offsetWidth 800，右边缘在 800
    Object.defineProperty(viewport, "clientWidth", { configurable: true, get: () => 785 });
    Object.defineProperty(viewport, "offsetWidth", { configurable: true, get: () => 800 });
    viewport.getBoundingClientRect = () =>
      ({ left: 0, top: 0, right: 800, bottom: 400, width: 800, height: 400, x: 0, y: 0 }) as DOMRect;
    metricsTo(1000, 600); // 贴底跟随
    // 按在正文里（x=100）：位置因重排离开底部，但这不是读者在滚动 → 跟随不关
    metricsTo(1000, 560);
    act(() => {
      viewport.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, clientX: 100 }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(container.querySelector(".transcript-back-to-bottom")).toBeNull();
    // 按在滚动条上（x=795）：这才是读者在拖 → 跟随关，按钮出现
    metricsTo(1000, 300);
    act(() => {
      viewport.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, clientX: 795 }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(container.querySelector(".transcript-back-to-bottom")).not.toBeNull();
  });

  it("滚动容器显式关掉滚动锚定（清单第 7 条）", () => {
    expect(shellCss.length).toBeGreaterThan(0); // 读成空串会让这条空转通过
    const rule = new RegExp(`\\.transcript-view\\s*\\{[^}]*\\}`).exec(shellCss)?.[0] ?? "";
    expect(rule).not.toBe("");
    expect(rule).toMatch(/overflow-anchor:\s*none/);
  });
});

/**
 * 宿主报障 2026-11-23T14:33:25（第三次；前两次 2026-11-19T15:31:14、2026-11-20T18:08:13）：
 * 「chat 页面还是无法选中」。录屏逐帧量到的形状是：蓝色选区一次成形、0.64–0.89 秒后一次消失。
 * 本组件里唯一能这样清掉选区的地方是按下时的 `seedSelection` —— 它先 `removeAllRanges()`
 * 再放一个折叠 caret。它只该用在不带正文的件上（✦、时间、工具行）：按在正文里时，浏览器
 * 自己那一下就已经给了起点，我们再换一次只会把宿主手上的选区换掉。
 */
describe("按下不动既有选区：正文自己按下的地方不种 caret", () => {
  afterEach(() => {
    document.getSelection()?.removeAllRanges();
    delete (document as unknown as Record<string, unknown>).caretRangeFromPoint;
  });

  it("已完成的回复里按下 → 选区还在", async () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleDelta([{ request_id: "r1", content: "**bold** text" }], "s1");
    const { container } = setup(store, "s1");
    act(() => {
      store.handleDone({ request_id: "r1" }, "s1");
    });
    const mdDiv = await screen.findByText("**bold** text", { selector: ".md-test" });
    const textNode = mdDiv.firstChild!;
    // jsdom 没有 caret API：给它一个。否则 seedSelection 在「量不到 caret」那一步就返回，
    // 这条用例会因为什么都没发生而通过 —— 空比较不是测量。
    (document as unknown as Record<string, unknown>).caretRangeFromPoint = () => {
      const r = document.createRange();
      r.setStart(textNode, 0);
      r.collapse(true);
      return r;
    };
    const sel = document.getSelection()!;
    const range = document.createRange();
    range.selectNodeContents(textNode);
    sel.removeAllRanges();
    sel.addRange(range);
    expect(sel.isCollapsed).toBe(false);

    const body = container.querySelector(".assistant-html") as HTMLElement;
    expect(body).not.toBeNull();
    fireEvent.pointerDown(body, { clientX: 10, clientY: 10 });

    expect(document.getSelection()!.isCollapsed).toBe(false);
  });
});
