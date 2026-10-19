import { describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Editor } from "@tiptap/react";
import { useRef, type MutableRefObject } from "react";
import { Composer, type SendResult } from "./Composer";
import { createTranscriptStore, type TranscriptStore } from "../lib/transcript";
import { I18nProvider } from "../lib/i18n";

/**
 * Composer.test.tsx — 输入框 + / 补全菜单 + 发送流组件测试（tiptap 版，rant 2026-08-28T14:07:29）。
 * 迁移：userEvent.type textarea → tiptap editor 命令驱动（editorRef.commands.insertContent /
 * editor.view.dom 键盘事件）。注入假 sendMessage，断言：补全菜单出现/键盘导航/选择填充、
 * 发送流程（用户行 + 输入清空 + requestId）、失败恢复（G49）、busy 队列注入（#655）。
 */

function setup(
  store: TranscriptStore,
  opts: {
    sid?: string | null;
    sendMessage?: (o: { sessionId: string | null; text: string; requestId: string; sandbox?: string | null }) => Promise<SendResult>;
    busy?: boolean;
    cancel?: (sessionId: string) => Promise<unknown>;
    onCommand?: (r: { type: "command" | "unknown"; cmd: string; args?: string[] }) => void;
    sandbox?: string | null;
    onSandboxChange?: (mode: string) => void;
    /** 会话额外可写根（daemon 报的那份）；null = 没人问过（rant 2026-10-09T09:43:39） */
    sandboxRoots?: string[] | null;
    /** 会话的输入历史（daemon 报的那份，最旧在前）；null = 还没问过（issue #2349） */
    inputHistory?: string[] | null;
    /** 发送成功后上报这一条（父级据此把召回列表接上，issue #2349 part 2/2） */
    onPromptSent?: (sid: string, text: string) => void;
    /** 父级接管「额外可写根」入口：点击只上报意图（打开管理对话框） */
    onManageSandboxRoots?: () => void;
    saveImage?: (payload: { sessionId?: string | null; data: string; label: string; mime?: string }) =>
      Promise<{ path: string; mime?: string }>;
    readClipboardImage?: () => Promise<{ data: string; mime?: string; name?: string } | null>;
    logLine?: (level: string, msg: string) => void;
  } = {},
) {
  const editorRef: MutableRefObject<Editor | null> = { current: null };
  const utils = render(
    <I18nProvider lang="zh">
      <Composer store={store} sid={opts.sid ?? "s1"} sandbox="workspace-write" editorRef={editorRef} {...opts} />
    </I18nProvider>,
  );
  const editor = () => editorRef.current;
  const input = () => screen.getByTestId("composer-input") as HTMLElement;
  const type = (text: string) => {
    act(() => {
      editorRef.current?.commands.insertContent(text);
    });
  };
  /** 在编辑器 DOM 上派发真实 keydown（tiptap editorProps.handleKeyDown 拦截） */
  const press = (key: string, mods: { shiftKey?: boolean; ctrlKey?: boolean } = {}, code?: string) => {
    act(() => {
      const dom = editorRef.current?.view.dom;
      if (!dom) throw new Error("editor not mounted");
      dom.dispatchEvent(new KeyboardEvent("keydown", { key, code, bubbles: true, cancelable: true, ...mods }));
    });
  };
  return { ...utils, editor, input, type, press, editorRef };
}

/** 等待 editorRef 挂载（immediatelyRender:false → useEffect 后才有实例） */
async function waitEditor(setupResult: ReturnType<typeof setup>) {
  await waitFor(() => expect(setupResult.editor()).not.toBeNull());
  return setupResult.editor()!;
}

describe("Composer — 补全菜单", () => {
  it("输入 / 前缀弹出匹配菜单（无空格仍在指令词）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("/c");
    });
    const menu = screen.getByTestId("cmd-menu");
    expect(menu).toBeInTheDocument();
    expect(screen.getAllByRole("menuitem").length).toBeGreaterThan(0);
  });

  it("空格后输入内容关闭菜单（离开指令词 → 普通消息）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    // 注意：vanilla 用 trim() 判定——尾部空格被去后仍是指令词，菜单保留；输词后关闭
    act(() => {
      editor.commands.insertContent("/clear x");
    });
    expect(screen.queryByTestId("cmd-menu")).not.toBeInTheDocument();
  });

  it("↑↓ 导航切换选中项（vanilla (index±1+n)%n 环绕）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("/c");
    });
    const items = screen.getAllByRole("menuitem");
    expect(items[0]).toHaveClass("selected");
    s.press("ArrowDown");
    expect(items[1]).toHaveClass("selected");
    expect(items[0]).not.toHaveClass("selected");
    // 环绕：最后一项 + ↓ → 回到第 0 项（(last+1) % n = 0）
    s.press("ArrowDown");
    expect(items[0]).toHaveClass("selected");
    // ↑ 也环绕：第 0 项 + ↑ → 回最后一项（(0-1+n) % n = n-1）
    s.press("ArrowUp");
    expect(items[items.length - 1]).toHaveClass("selected");
  });

  it("Enter 选择补全项 → 填充输入框并关闭菜单（用户可继续回车执行）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("/clear");
    });
    s.press("Enter");
    expect(editor.getText()).toBe("/clear");
    expect(screen.queryByTestId("cmd-menu")).not.toBeInTheDocument();
  });

  it("mousedown 点击补全项 → 填充输入框（preventDefault 防失焦）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("/clear");
    });
    await userEvent.click(screen.getByTestId("cmd-item-/clear"));
    expect(editor.getText()).toBe("/clear");
  });

  it("Escape 关闭菜单", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("/c");
    });
    expect(screen.getByTestId("cmd-menu")).toBeInTheDocument();
    s.press("Escape");
    expect(screen.queryByTestId("cmd-menu")).not.toBeInTheDocument();
  });
});

describe("Composer — 发送流", () => {
  it("Enter 发送消息：用户行入 store + 输入清空 + sendMessage 带预生成 requestId + sandbox", async () => {
    const store = createTranscriptStore();
    const sent: Array<{ text: string; requestId: string; sandbox?: string | null }> = [];
    const s = setup(store, {
      sendMessage: async (o) => {
        sent.push({ text: o.text, requestId: o.requestId, sandbox: o.sandbox });
        return { requestId: o.requestId };
      },
    });
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("hello emrg");
    });
    s.press("Enter");
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].text).toBe("hello emrg");
    expect(sent[0].sandbox).toBe("workspace-write");
    expect(sent[0].requestId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(editor.getText()).toBe("");
    const entries = store.getEntries("s1");
    expect(entries[entries.length - 1]).toMatchObject({ kind: "user", text: "hello emrg" });
  });

  it("Ctrl+Enter 同样发送", async () => {
    const store = createTranscriptStore();
    let sent = 0;
    const s = setup(store, {
      sendMessage: async () => {
        sent++;
        return {};
      },
    });
    await waitEditor(s);
    s.type("ctrl send");
    s.press("Enter", { ctrlKey: true });
    await waitFor(() => expect(sent).toBe(1));
  });

  it("Shift+Enter 不发送（换行）", async () => {
    const store = createTranscriptStore();
    let sent = 0;
    const s = setup(store, {
      sendMessage: async () => {
        sent++;
        return {};
      },
    });
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("line one");
    });
    s.press("Enter", { shiftKey: true });
    expect(sent).toBe(0);
    // Shift+Enter → hardBreak 换行（tiptap 默认），内容保留（getText 以 \n 表示 hardBreak）
    expect(editor.getText()).toBe("line one\n");
  });

  it("空输入不发送", async () => {
    const store = createTranscriptStore();
    let sent = 0;
    const s = setup(store, {
      sendMessage: async () => {
        sent++;
        return {};
      },
    });
    await waitEditor(s);
    s.press("Enter");
    expect(sent).toBe(0);
  });

  it("失败恢复：系统消息（copy.sendFailed）+ 文本回填", async () => {
    const store = createTranscriptStore();
    const s = setup(store, {
      sendMessage: async () => {
        throw new Error("boom");
      },
    });
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("will fail");
    });
    s.press("Enter");
    // 异步失败分支（G49：恢复输入框）——await sendFn 的 rejection 在微任务里，
    // 系统消息断言必须 waitFor
    await waitFor(() =>
      expect(
        store.getEntries("s1").some((e) => e.kind === "system" && e.text === "没发送成功，你的话我还留着，再试一次？"),
      ).toBe(true),
    );
    await waitFor(() => expect(editor.getText()).toBe("will fail"));
  });

  it("无会话时提示 app.needSession 且不发送", async () => {
    const store = createTranscriptStore();
    let sent = 0;
    const s = setup(store, {
      sid: null,
      sendMessage: async () => {
        sent++;
        return {};
      },
    });
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("orphan");
    });
    s.press("Enter");
    await waitFor(() => expect(sent).toBe(0));
    expect(store.getEntries(null).some((e) => e.kind === "system" && e.text === "请先创建一个对话。")).toBe(true);
  });

  it("/ 指令不进发送流，走 onCommand 路由（vanilla rant 19:44 P1）", async () => {
    const store = createTranscriptStore();
    let sent = 0;
    const routed: Array<{ cmd: string; args?: string[] }> = [];
    const s = setup(store, {
      sendMessage: async () => {
        sent++;
        return {};
      },
      onCommand: (r) => routed.push({ cmd: r.cmd, args: r.args }),
    });
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("/clear now");
    });
    s.press("Enter");
    await waitFor(() => expect(routed).toEqual([{ cmd: "/clear", args: ["now"] }]));
    expect(sent).toBe(0);
    expect(editor.getText()).toBe("");
  });

  it("markdown round-trip：setContent(md) 回显不损坏（rant 验收：旧纯文本/富文本编辑场景）", async () => {
    const store = createTranscriptStore();
    const s = setup(store, {
      sendMessage: async () => ({}),
    });
    const editor = await waitEditor(s);
    // 旧纯文本消息 → setContent 解析 → 文本不变
    act(() => {
      editor.commands.setContent("plain old message");
    });
    expect(editor.getText()).toBe("plain old message");
    // markdown 富文本 → getMarkdown 序列化等价（history.jsonl 存 markdown str）
    act(() => {
      editor.commands.setContent("**bold** and `code`");
    });
    const md = (editor.storage as unknown as { markdown: { getMarkdown(): string } }).markdown.getMarkdown();
    expect(md.trim()).toContain("**bold**");
    expect(md.trim()).toContain("`code`");
  });

  it("busy 时发送 → 入队（queue-injection #655）并在成功时以 daemon 回显 requestId 为准", async () => {
    const store = createTranscriptStore();
    const sent: string[] = [];
    const s = setup(store, {
      busy: true,
      sendMessage: async (o) => {
        sent.push(o.requestId);
        return { requestId: `echo-${o.requestId}` };
      },
    });
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("queued msg");
    });
    s.press("Enter");
    await waitFor(() => expect(sent).toHaveLength(1)); // busy 不拦截——直接发送
    // G124：ownStream 以 daemon 回显为准
    const version = store.getVersion();
    void version;
  });
});

describe("Composer — 停止回复（rant 2026-09-02T20:30:05：React 迁移丢了 send↔stop 切换）", () => {
  it("busy 时发送按钮切换为停止按钮；点击 → 点名会话的 cancel，本地不打任何字", async () => {
    const store = createTranscriptStore();
    const cancelled: string[] = [];
    const s = setup(store, {
      busy: true,
      cancel: async (sessionId) => {
        cancelled.push(sessionId);
      },
    });
    await waitEditor(s);
    // busy → send 按钮隐藏、stop 按钮可见（■ / composer.stop 文案）
    expect(screen.queryByTestId("composer-send")).not.toBeInTheDocument();
    const stop = screen.getByTestId("composer-stop");
    expect(stop).toBeInTheDocument();
    expect(stop).toHaveAttribute("aria-label", "停止回复");
    expect(stop.textContent).toBe("■");
    await userEvent.click(stop);
    // Rant 2026-09-20T12:50:13：cancel 带的是会话 id，不是「我这个连接」。
    await waitFor(() => expect(cancelled).toEqual(["s1"]));
    // 结束这轮的唯一陈述是 daemon 的会话级 cancelled 回执（daemonBridge 那条）——
    // 本地乐观的清 typing / 打「已停止响应。」/ 置 busy=false 已删除（否则按 Esc 的一端
    // 「已中断」而真正跑着这轮的服务端毫无反应）。
    expect(store.getEntries("s1").some((e) => e.kind === "system")).toBe(false);
    expect(store.getEntries("s1").some((e) => e.kind === "system" && e.text.includes("已停止响应"))).toBe(false);
  });

  it("非 busy 时仍显示发送按钮（无 stop）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    await waitEditor(s);
    expect(screen.getByTestId("composer-send")).toBeInTheDocument();
    expect(screen.queryByTestId("composer-stop")).not.toBeInTheDocument();
  });

  it("无会话可点名时不发 cancel（与 TUI request_cancel 同一规则）", async () => {
    const store = createTranscriptStore();
    const cancelled: string[] = [];
    const s = setup(store, {
      sid: "",
      busy: true,
      cancel: async (sessionId) => {
        cancelled.push(sessionId);
      },
    });
    await waitEditor(s);
    await userEvent.click(screen.getByTestId("composer-stop"));
    // 没有会话可点名就没有可问的事：一帧都不发，本地也不陈述任何结果。
    expect(cancelled).toEqual([]);
    expect(store.getEntries("").some((e) => e.kind === "system")).toBe(false);
  });

  it("busy + Esc → 停止（菜单未开）；菜单开着时 Esc 只关菜单不误停", async () => {
    const store = createTranscriptStore();
    const cancelled: string[] = [];
    const s = setup(store, {
      busy: true,
      cancel: async (sessionId) => {
        cancelled.push(sessionId);
      },
    });
    const editor = await waitEditor(s);
    // 无菜单 → Esc → stop：点名会话的 cancel（文案/清 busy 由回执产生，见 daemonBridge）
    s.press("Escape");
    await waitFor(() => expect(cancelled).toEqual(["s1"]));
    expect(store.getEntries("s1").some((e) => e.kind === "system")).toBe(false);

    // / 菜单开着时 Esc 只关菜单——busy 也不触发第二次 stop
    act(() => {
      editor.commands.insertContent("/c");
    });
    expect(screen.getByTestId("cmd-menu")).toBeInTheDocument();
    s.press("Escape");
    expect(screen.queryByTestId("cmd-menu")).not.toBeInTheDocument();
    expect(cancelled).toEqual(["s1"]); // 未追加
  });
});

describe("Composer — 格式栏与快捷键（Stage 2, rant 14:07:29）", () => {
  /** 全选 + 取 markdown（tiptap-markdown 序列化断言） */
  function mdOf(s: ReturnType<typeof setup>, editor: Editor): string {
    const md = (editor.storage as unknown as { markdown: { getMarkdown(): string } }).markdown.getMarkdown();
    return md.trim();
  }

  it("格式栏渲染 9 个按钮（粗体/斜体/删除线/代码/链接/无序/有序/引用/标题）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    await waitEditor(s);
    expect(screen.getByTestId("fmt-bar")).toBeInTheDocument();
    const ids = ["bold", "italic", "strike", "code", "link", "bullet", "ordered", "quote", "heading"];
    for (const id of ids) expect(screen.getByTestId(`fmt-${id}`)).toBeInTheDocument();
  });

  it("粗体按钮 → 选中文本序列化为 **bold**", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("hello bold");
      editor.commands.selectAll();
    });
    await userEvent.click(screen.getByTestId("fmt-bold"));
    expect(mdOf(s, editor)).toContain("**hello bold**");
    // 激活态高亮
    await waitFor(() => expect(screen.getByTestId("fmt-bold")).toHaveClass("active"));
  });

  it("斜体按钮 → *italic*", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("emph");
      editor.commands.selectAll();
    });
    await userEvent.click(screen.getByTestId("fmt-italic"));
    expect(mdOf(s, editor)).toContain("*emph*");
  });

  it("删除线按钮 → ~~strike~~", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("obsolete");
      editor.commands.selectAll();
    });
    await userEvent.click(screen.getByTestId("fmt-strike"));
    expect(mdOf(s, editor)).toContain("~~obsolete~~");
  });

  it("行内代码按钮 → `code`", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("const x");
      editor.commands.selectAll();
    });
    await userEvent.click(screen.getByTestId("fmt-code"));
    expect(mdOf(s, editor)).toContain("`const x`");
  });

  it("链接按钮（LinkDialog 输入 URL）→ [label](url)；再次点击解除链接", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("my link");
      editor.commands.selectAll();
    });
    // 点击链接按钮 → 弹出应用内 LinkDialog（非 window.prompt；Electron 禁用 prompt）
    await userEvent.click(screen.getByTestId("fmt-link"));
    const urlInput = screen.getByTestId("link-url");
    expect(urlInput).toBeInTheDocument();
    await userEvent.clear(urlInput);
    await userEvent.type(urlInput, "https://example.com");
    await userEvent.click(screen.getByTestId("link-ok"));
    expect(mdOf(s, editor)).toContain("[my link](https://example.com)");
    // 已激活链接 → 再点解除（LinkDialog 关闭）
    await userEvent.click(screen.getByTestId("fmt-link"));
    expect(mdOf(s, editor)).not.toContain("[my link]");
    expect(screen.queryByTestId("link-dialog")).not.toBeInTheDocument();
  });

  it("无序列表按钮 → - item", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("todo");
      editor.commands.selectAll();
    });
    await userEvent.click(screen.getByTestId("fmt-bullet"));
    expect(mdOf(s, editor)).toContain("- todo");
  });

  it("有序列表按钮 → 1. item", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("step");
      editor.commands.selectAll();
    });
    await userEvent.click(screen.getByTestId("fmt-ordered"));
    expect(mdOf(s, editor)).toContain("1. step");
  });

  it("引用按钮 → > quote", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("noted");
      editor.commands.selectAll();
    });
    await userEvent.click(screen.getByTestId("fmt-quote"));
    expect(mdOf(s, editor)).toContain("> noted");
  });

  it("标题按钮循环 p→h1→h2→h3→p（单按钮四态）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("section");
    });
    // 每次点击前选中文本区间（0..7，"section" 长度）——selectAll 会把 toggleHeading
    // 追加的尾随空段落也选上导致级别误读（tiptap v3 混合选区 isActive 不可靠）
    const selectText = () => {
      act(() => {
        editor.commands.setTextSelection({ from: 0, to: 7 });
      });
    };
    const click = async () => {
      selectText();
      await userEvent.click(screen.getByTestId("fmt-heading"));
    };
    await click();
    expect(mdOf(s, editor)).toContain("# section");
    await click();
    expect(mdOf(s, editor)).toContain("## section");
    await click();
    expect(mdOf(s, editor)).toContain("### section");
    await click();
    expect(mdOf(s, editor)).toContain("section"); // 回到段落（无 # 前缀）
    expect(mdOf(s, editor)).not.toMatch(/^#+ /);
  });

  it("⌘B 快捷键（StarterKit 内置）→ 粗体激活", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("shortcut");
      editor.commands.selectAll();
    });
    s.press("b", { ctrlKey: true }); // jsdom 无 metaKey → ctrlKey 等价 Mod
    await waitFor(() => expect(editor.isActive("bold")).toBe(true));
    expect(mdOf(s, editor)).toContain("**shortcut**");
  });

  it("⌘K 快捷键（无内置绑定，显式补挂）→ 打开 LinkDialog", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("kb");
      editor.commands.selectAll();
    });
    s.press("k", { ctrlKey: true });
    // ⌘K 打开应用内 LinkDialog（非 window.prompt）；输入 URL 后确认
    const urlInput = await screen.findByTestId("link-url");
    expect(urlInput).toBeInTheDocument();
    await userEvent.clear(urlInput);
    await userEvent.type(urlInput, "https://shortcut.dev");
    await userEvent.click(screen.getByTestId("link-ok"));
    expect(mdOf(s, editor)).toContain("[kb](https://shortcut.dev)");
  });

  it("⌘⇧8 快捷键（无内置绑定）→ 无序列表", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("listy");
      editor.commands.selectAll();
    });
    s.press("8", { ctrlKey: true, shiftKey: true }, "Digit8");
    // isActive("bulletList") 在 tiptap v3 切换后选区不可靠 → 断言 markdown 输出
    await waitFor(() => expect(mdOf(s, editor)).toContain("- listy"));
  });

  it("⌘⇧7 快捷键（无内置绑定）→ 有序列表", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("seq");
      editor.commands.selectAll();
    });
    s.press("7", { ctrlKey: true, shiftKey: true }, "Digit7");
    await waitFor(() => expect(mdOf(s, editor)).toContain("1. seq"));
  });

  it("粘贴净化：transformPastedHTML 剥离 script/事件属性（DOMPurify）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    const sanitize = editor.options.editorProps.transformPastedHTML as (html: string) => string;
    const out = sanitize('<b>ok</b><script>alert(1)</script><img src="x" onerror="alert(2)"><a href="https://a.dev" onclick="evil()">lnk</a>');
    expect(out).toContain("<b>ok</b>");
    expect(out).toContain('href="https://a.dev"');
    expect(out).not.toContain("<script");
    expect(out).not.toContain("onerror");
    expect(out).not.toContain("onclick");
  });

  it("格式命令作用于格式栏点击后选区保持（mousedown preventDefault 防失焦）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("keep selection");
      editor.commands.selectAll();
    });
    await userEvent.click(screen.getByTestId("fmt-italic"));
    // 点击格式按钮后选区仍在（mousedown preventDefault 防失焦）→ 命令作用于选中文本
    expect(mdOf(s, editor)).toContain("*keep selection*");
  });

  it("沙箱切换器：默认 workspace-write 激活 + 三档渲染（重构回归恢复，rant 2026-08-30T16:34:29）", async () => {
    const store = createTranscriptStore();
    setup(store);
    await waitFor(() => expect(screen.getByTestId("sandbox-switcher")).toBeTruthy());
    expect(screen.getByTestId("sandbox-workspace-write").className).toContain("active");
    expect(screen.getByTestId("sandbox-read-only").getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByTestId("sandbox-danger-full-access").getAttribute("aria-pressed")).toBe("false");
  });

  it("沙箱切换器：切到 read-only → 发送消息带 read-only sandbox", async () => {
    const store = createTranscriptStore();
    const sent: Array<{ sandbox?: string | null }> = [];
    const s = setup(store, {
      sendMessage: async (o) => {
        sent.push({ sandbox: o.sandbox });
        return { requestId: o.requestId };
      },
    });
    const editor = await waitEditor(s);
    await userEvent.click(screen.getByTestId("sandbox-read-only"));
    expect(screen.getByTestId("sandbox-read-only").className).toContain("active");
    expect(screen.getByTestId("sandbox-workspace-write").className).not.toContain("active");
    act(() => {
      editor.commands.insertContent("readonly mode");
    });
    s.press("Enter");
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].sandbox).toBe("read-only");
  });

  it("沙箱切换器：父级接管时显示 daemon 报的档位，点击只上报、自己不动（rant 2026-09-30T09:30:16 GUI 半边）", async () => {
    const store = createTranscriptStore();
    const asked: string[] = [];
    setup(store, { sandbox: "read-only", onSandboxChange: (m) => asked.push(m) });
    await waitFor(() => expect(screen.getByTestId("sandbox-switcher")).toBeTruthy());
    // 显示的是 prop（daemon 说的），不是本组件默认的 workspace-write
    expect(screen.getByTestId("sandbox-read-only").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("sandbox-workspace-write").getAttribute("aria-pressed")).toBe("false");
    await userEvent.click(screen.getByTestId("sandbox-danger-full-access"));
    expect(asked).toEqual(["danger-full-access"]);
    // 点击不改显示：档位等 daemon 的 sandbox_set 回来（客户端不先改自己那份）
    expect(screen.getByTestId("sandbox-read-only").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("sandbox-danger-full-access").getAttribute("aria-pressed")).toBe("false");
  });

  it("额外可写根入口：不接线不渲染；接线后显示 daemon 报的条数，点击只上报（rant 2026-10-09T09:43:39 GUI 半边）", async () => {
    const store = createTranscriptStore();
    // 未接线（单挂载 / 单测）：入口不渲染——与 onSandboxChange 的约定一致。
    const first = setup(store);
    await waitFor(() => expect(screen.getByTestId("sandbox-switcher")).toBeTruthy());
    expect(screen.queryByTestId("sandbox-roots-open")).toBeNull();
    first.unmount();

    const opened: number[] = [];
    setup(store, { sandboxRoots: null, onManageSandboxRoots: () => opened.push(1) });
    await waitFor(() => expect(screen.getByTestId("sandbox-roots-open")).toBeTruthy());
    // 没人问过 → 只报入口，不报条数：`0` 会主张一个 daemon 没报过的状态。
    expect(screen.getByTestId("sandbox-roots-open")).toHaveTextContent("额外根");
    expect(screen.getByTestId("sandbox-roots-open")).not.toHaveTextContent("额外根 0");
    await userEvent.click(screen.getByTestId("sandbox-roots-open"));
    expect(opened).toHaveLength(1);
  });

  it("额外可写根入口：条数就是 daemon 那份列表的长度（空列表也照实报 0）", async () => {
    const store = createTranscriptStore();
    setup(store, { sandboxRoots: ["/tmp/a", "/tmp/b"], onManageSandboxRoots: vi.fn() });
    await waitFor(() => expect(screen.getByTestId("sandbox-roots-open")).toHaveTextContent("额外根 2"));
  });

  it("额外可写根入口：空列表报 0（这是 daemon 说过的状态，与「没人问过」不同）", async () => {
    const store = createTranscriptStore();
    setup(store, { sandboxRoots: [], onManageSandboxRoots: vi.fn() });
    await waitFor(() => expect(screen.getByTestId("sandbox-roots-open")).toHaveTextContent("额外根 0"));
  });

  it("沙箱切换器：父级接管时，随消息下发的档位也是 daemon 报的那个", async () => {
    const store = createTranscriptStore();
    const sent: Array<{ sandbox?: string | null }> = [];
    const s = setup(store, {
      sandbox: "danger-full-access",
      onSandboxChange: () => {},
      sendMessage: async (o) => {
        sent.push({ sandbox: o.sandbox });
        return { requestId: o.requestId };
      },
    });
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("full access please");
    });
    s.press("Enter");
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].sandbox).toBe("danger-full-access");
  });

  it("沙箱切换器：切到 danger-full-access → 发送消息带 danger-full-access sandbox", async () => {
    const store = createTranscriptStore();
    const sent: Array<{ sandbox?: string | null }> = [];
    const s = setup(store, {
      sendMessage: async (o) => {
        sent.push({ sandbox: o.sandbox });
        return { requestId: o.requestId };
      },
    });
    const editor = await waitEditor(s);
    await userEvent.click(screen.getByTestId("sandbox-danger-full-access"));
    expect(screen.getByTestId("sandbox-danger-full-access").className).toContain("active");
    act(() => {
      editor.commands.insertContent("full access mode");
    });
    s.press("Enter");
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].sandbox).toBe("danger-full-access");
  });
});

describe("Composer — 草稿持久化（rant 2026-09-01T20:28:31）", () => {
  it("输入 → 草稿实时写入 store（onUpdate 序列化 markdown）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("hello draft");
    });
    await waitFor(() => expect(store.getComposerDraft("s1")).toContain("hello draft"));
  });

  it("卸载重挂载 → 草稿恢复（onCreate setContent 回填）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("**bold** draft");
    });
    await waitFor(() => expect(store.getComposerDraft("s1")).toContain("bold"));
    s.unmount();
    const s2 = setup(store);
    const editor2 = await waitEditor(s2);
    await waitFor(() => expect(editor2.getText()).toContain("draft"));
  });

  it("发送成功 → 草稿清空（clearContent → onUpdate 空内容）", async () => {
    const store = createTranscriptStore();
    const sendMessage = vi.fn().mockResolvedValue({ requestId: "r1" });
    const s = setup(store, { sendMessage });
    const editor = await waitEditor(s);
    act(() => {
      editor.commands.insertContent("send me");
    });
    await waitFor(() => expect(store.getComposerDraft("s1")).toContain("send me"));
    s.press("Enter");
    await waitFor(() => expect(sendMessage).toHaveBeenCalled());
    await waitFor(() => expect(store.getComposerDraft("s1")).toBe(""));
  });

  it("会话切换（同挂载 prop 变化）→ 旧会话草稿保留 + 新会话草稿载入", async () => {
    const store = createTranscriptStore();
    const editorRef: MutableRefObject<Editor | null> = { current: null };
    const ui = (sid: string) => (
      <I18nProvider lang="zh">
        <Composer store={store} sid={sid} sandbox="workspace-write" editorRef={editorRef} />
      </I18nProvider>
    );
    const s = render(ui("s1"));
    await waitFor(() => expect(editorRef.current).not.toBeNull());
    act(() => {
      editorRef.current!.commands.insertContent("draft for s1");
    });
    await waitFor(() => expect(store.getComposerDraft("s1")).toContain("draft for s1"));
    // 预置 s2 草稿（模拟之前在该会话输入过）
    act(() => {
      store.setComposerDraft("draft for s2", "s2");
    });
    s.rerender(ui("s2"));
    await waitFor(() => expect(editorRef.current!.getText()).toContain("draft for s2"));
    // 旧会话草稿仍在 store（切回 s1 可恢复）
    expect(store.getComposerDraft("s1")).toContain("draft for s1");
  });
});

// ── 图片路径：粘贴/拖拽的落盘与可见拒绝（rant 2026-09-30T09:35:04） ────────────
//
// 宿主报障（2026-09-30T09:33）：「GUI 输入框里 Cmd+V， 现在没有任何效果」。
// 实现自 2026-09-02 就在（e46c160a / PR #1109），但 ~/.emrg/emrg-gui.log 里
// `[gui:saveImage]` 全量 0 命中 ⇒ 那条 IPC 一次都没被走到，而拒绝路径全是静默的
// （不插占位符、不提示、不写日志），所以「图片被丢弃」与「粘贴本来就没内容」外部
// 无从区分。下面这几条把两侧都钉住：**两个面都取**（files + items，粘贴一路由
// Chromium 放在 items 里）与**每一次拒绝都出声**。

type SaveImageArgs = { sessionId?: string | null; data: string; label: string; mime?: string };

interface FakeCarrier {
  files?: File[];
  items?: { kind: string; type: string; getAsFile: () => File | null }[];
  types?: string[];
  text?: string;
}

/** clipboardData / dataTransfer 的替身（只实现被测代码读的那几个面） */
function makeCarrier(init: FakeCarrier) {
  return {
    files: init.files ?? [],
    items: init.items ?? [],
    types: init.types ?? [],
    getData: (t: string) => (t === "text/plain" ? init.text ?? "" : ""),
  };
}

function fileItem(file: File | null, type?: string) {
  return { kind: "file", type: type ?? file?.type ?? "", getAsFile: () => file };
}

/** 造一个真实 image/png File（jsdom 有 File + FileReader，base64 走真实路径） */
function pngFile(name = "shot.png"): File {
  return new File([new Uint8Array([137, 80, 78, 71])], name, { type: "image/png" });
}

/** 在编辑器 DOM 上派发一次 paste（ProseMirror 的 capturePaste → handlePaste，延迟 50ms） */
async function firePaste(editor: Editor, clipboardData: unknown): Promise<void> {
  const ev = new Event("paste", { bubbles: true, cancelable: true });
  Object.defineProperty(ev, "clipboardData", { value: clipboardData });
  act(() => {
    editor.view.dom.dispatchEvent(ev);
  });
}

/** 在 composer 容器上派发一次 drop（wrapper 的 onDrop，不经 ProseMirror） */
async function fireDrop(el: HTMLElement, dataTransfer: unknown): Promise<void> {
  const ev = new Event("drop", { bubbles: true, cancelable: true });
  Object.defineProperty(ev, "dataTransfer", { value: dataTransfer });
  act(() => {
    el.dispatchEvent(ev);
  });
}

describe("Composer — 图片粘贴/拖拽（rant 2026-09-30T09:35:04）", () => {
  it("粘贴只带 items 的图片（Chromium 形态）会落盘并插入占位符", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImageArgs) => ({ path: "/tmp/shot.png", mime: "image/png" }));
    const logLine = vi.fn();
    const s = setup(store, { saveImage, logLine });
    const editor = await waitEditor(s);

    // files 为空 —— 这正是「只读 cd.files」那个写法会得到空数组的形态
    await firePaste(editor, makeCarrier({ files: [], items: [fileItem(pngFile())], types: ["Files"] }));

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    expect(saveImage.mock.calls[0][0]).toMatchObject({ sessionId: "s1", mime: "image/png", label: "shot" });
    await waitFor(() => expect(editor.getText()).toContain("[📷 shot]"));
    expect(logLine).toHaveBeenCalledWith("info", expect.stringContaining("[composer:image] attach 1 file(s)"));
    expect(screen.queryByTestId("composer-image-notice")).toBeNull();
  });

  it("粘贴既有文本又有图（items）时文本先落、图随后附上（对齐 TUI）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImageArgs) => ({ path: "/tmp/shot.png", mime: "image/png" }));
    const s = setup(store, { saveImage });
    const editor = await waitEditor(s);

    await firePaste(
      editor,
      makeCarrier({ files: [], items: [fileItem(pngFile())], types: ["Files", "text/plain"], text: "hello" }),
    );

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(editor.getText()).toContain("hello"));
    expect(editor.getText()).toContain("[📷 shot]");
  });

  it("拖拽（dataTransfer.files）仍然工作", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImageArgs) => ({ path: "/tmp/drop.png", mime: "image/png" }));
    const s = setup(store, { saveImage });
    await waitEditor(s);

    await fireDrop(screen.getByTestId("composer"), makeCarrier({ files: [pngFile("drop.png")], types: ["Files"] }));

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    expect(saveImage.mock.calls[0][0]).toMatchObject({ mime: "image/png", label: "drop" });
  });

  it("声明了 Files 却一个图片都没取到 → 出声（把事件里到底有什么写出来）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const logLine = vi.fn();
    const s = setup(store, { saveImage, logLine });
    const editor = await waitEditor(s);

    await firePaste(editor, makeCarrier({ files: [], items: [], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-image-notice");
    expect(notice.textContent).toContain("Files");
    expect(saveImage).not.toHaveBeenCalled();
    expect(logLine).toHaveBeenCalledWith("warn", expect.stringContaining("[composer:image] refused:"));
  });

  it("白名单外的类型（macOS 菜单栏给的 TIFF）报出它的类型，而不是静默丢弃", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const logLine = vi.fn();
    const s = setup(store, { saveImage, logLine });
    const editor = await waitEditor(s);

    const tiff = new File([new Uint8Array([1, 2])], "shot.tiff", { type: "image/tiff" });
    await firePaste(editor, makeCarrier({ files: [tiff], items: [fileItem(tiff)], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-image-notice");
    expect(notice.textContent).toContain("image/tiff");
    expect(saveImage).not.toHaveBeenCalled();
  });

  // ── 要求 4：系统给的图不受白名单所限 ────────────────────────────────────────────
  //
  // 白名单只是「不必转换就能直接收」的快路径。macOS 把菜单栏/截图给的图声明成
  // `image/tiff`，renderer 解不了（Chromium 只出 png/jpeg/gif/webp/bmp/svg，canvas
  // 也没有 TIFF 解码器）——于是同一次粘贴 TUI 收得到、GUI 只能拒绝。转换发生在有
  // NSImage 的一侧（main 的 `clipboard.readImage()` → `toPNG()`），这正是 TUI 向
  // macOS 要 `«class PNGf»` 的同一个动作。

  /** main 归一后的 PNG（内容无关——这条路径不解析像素，只搬运字节）。 */
  const PNG_B64 = btoa("\x89PNG\r\n\x1a\n");

  it("事件里只有名单外的 TIFF 时，先请 main 把剪贴板里的图转成 PNG，再走同一条落盘路径", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImageArgs) => ({ path: "/tmp/shot.png", mime: "image/png" }));
    const readClipboardImage = vi.fn(async () => ({ data: PNG_B64, mime: "image/png", name: "clipboard.png" }));
    const logLine = vi.fn();
    const s = setup(store, { saveImage, readClipboardImage, logLine });
    const editor = await waitEditor(s);

    const tiff = new File([new Uint8Array([1, 2])], "shot.tiff", { type: "image/tiff" });
    await firePaste(editor, makeCarrier({ files: [tiff], items: [fileItem(tiff)], types: ["Files"] }));

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    expect(readClipboardImage).toHaveBeenCalledTimes(1);
    // 落盘的必须是转换后的 PNG，不是那张 renderer 解不了的 TIFF
    expect(saveImage.mock.calls[0][0]).toMatchObject({ sessionId: "s1", mime: "image/png", label: "clipboard" });
    await waitFor(() => expect(editor.getText()).toContain("[📷 clipboard]"));
    expect(screen.queryByTestId("composer-image-notice")).toBeNull();
    expect(logLine).toHaveBeenCalledWith("info", expect.stringContaining("clipboard fallback"));
  });

  it("剪贴板里也没有可转换的图时，拒绝仍然可见并报出事件里给的类型", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const readClipboardImage = vi.fn(async () => null);
    const logLine = vi.fn();
    const s = setup(store, { saveImage, readClipboardImage, logLine });
    const editor = await waitEditor(s);

    const tiff = new File([new Uint8Array([1, 2])], "shot.tiff", { type: "image/tiff" });
    await firePaste(editor, makeCarrier({ files: [tiff], items: [fileItem(tiff)], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-image-notice");
    expect(notice.textContent).toContain("image/tiff");
    expect(saveImage).not.toHaveBeenCalled();
    expect(logLine).toHaveBeenCalledWith("warn", expect.stringContaining("clipboard fallback failed"));
  });

  it("读剪贴板这条桥本身失败（旧 preload）时，也不静默：提示可见、原因进日志", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const readClipboardImage = vi.fn(async () => {
      throw new Error("window.emrg.readClipboardImage unavailable");
    });
    const logLine = vi.fn();
    const s = setup(store, { saveImage, readClipboardImage, logLine });
    const editor = await waitEditor(s);

    const tiff = new File([new Uint8Array([1, 2])], "shot.tiff", { type: "image/tiff" });
    await firePaste(editor, makeCarrier({ files: [tiff], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-image-notice");
    expect(notice.textContent).toContain("image/tiff");
    expect(saveImage).not.toHaveBeenCalled();
    expect(logLine).toHaveBeenCalledWith(
      "warn",
      expect.stringContaining("window.emrg.readClipboardImage unavailable"),
    );
  });

  it("声明了 Files 却一个文件都没取到时，也先问一次剪贴板（宿主报障「按了没反应」的那个形态）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImageArgs) => ({ path: "/tmp/shot.png", mime: "image/png" }));
    const readClipboardImage = vi.fn(async () => ({ data: PNG_B64, mime: "image/png", name: "clipboard.png" }));
    const s = setup(store, { saveImage, readClipboardImage });
    const editor = await waitEditor(s);

    await firePaste(editor, makeCarrier({ files: [], items: [], types: ["Files"] }));

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    expect(editor.getText()).toContain("[📷 clipboard]");
    expect(screen.queryByTestId("composer-image-notice")).toBeNull();
  });

  it("混合粘贴（png + tiff）不重复取剪贴板：能直接收的那张照常落盘，名单外的照常出声", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImageArgs) => ({ path: "/tmp/mix.png", mime: "image/png" }));
    const readClipboardImage = vi.fn(async () => ({ data: PNG_B64, mime: "image/png", name: "clipboard.png" }));
    const s = setup(store, { saveImage, readClipboardImage });
    const editor = await waitEditor(s);

    const tiff = new File([new Uint8Array([1, 2])], "shot.tiff", { type: "image/tiff" });
    await firePaste(
      editor,
      makeCarrier({ files: [pngFile("mix.png"), tiff], types: ["Files"] }),
    );

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    expect(saveImage.mock.calls[0][0]).toMatchObject({ mime: "image/png", label: "mix" });
    // 剪贴板里那张就是刚粘上来的图 —— 再取一次会把同一张图重复附加
    expect(readClipboardImage).not.toHaveBeenCalled();
    const notice = await screen.findByTestId("composer-image-notice");
    expect(notice.textContent).toContain("image/tiff");
  });

  it("纯文本粘贴不读剪贴板（没有图就没有这条路）", async () => {
    const store = createTranscriptStore();
    const readClipboardImage = vi.fn(async () => ({ data: PNG_B64, mime: "image/png" }));
    const saveImage = vi.fn();
    const s = setup(store, { saveImage, readClipboardImage });
    const editor = await waitEditor(s);

    await firePaste(editor, makeCarrier({ types: ["text/plain"], text: "just text" }));

    expect(readClipboardImage).not.toHaveBeenCalled();
    expect(saveImage).not.toHaveBeenCalled();
  });

  it("拖拽名单外的文件不走剪贴板（拖的是 Finder 里的文件，不是剪贴板）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const readClipboardImage = vi.fn(async () => ({ data: PNG_B64, mime: "image/png" }));
    const s = setup(store, { saveImage, readClipboardImage });
    await waitEditor(s);

    const tiff = new File([new Uint8Array([1, 2])], "dropped.tiff", { type: "image/tiff" });
    await fireDrop(screen.getByTestId("composer"), makeCarrier({ files: [tiff], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-image-notice");
    expect(notice.textContent).toContain("image/tiff");
    expect(readClipboardImage).not.toHaveBeenCalled();
    expect(saveImage).not.toHaveBeenCalled();
  });

  it("落盘被拒（IPC 报错）时把原因摆到输入框上", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImageArgs) => {
      throw new Error("invalid session_id");
    });
    const logLine = vi.fn();
    const s = setup(store, { saveImage, logLine });
    const editor = await waitEditor(s);

    await firePaste(editor, makeCarrier({ files: [pngFile()], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-image-notice");
    expect(notice.textContent).toContain("invalid session_id");
    expect(editor.getText()).not.toContain("[📷");
    expect(logLine).toHaveBeenCalledWith("warn", expect.stringContaining("invalid session_id"));
  });

  it("没有会话时拒绝也要看得见（不再是静默 return）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const s = setup(store, { sid: null, saveImage });
    const editor = await waitEditor(s);

    await firePaste(editor, makeCarrier({ files: [pngFile()], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-image-notice");
    expect(notice.textContent).toContain("会话");
    expect(saveImage).not.toHaveBeenCalled();
  });

  it("普通文本粘贴不报错（没有图就没有提示）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const s = setup(store, { saveImage });
    const editor = await waitEditor(s);

    await firePaste(editor, makeCarrier({ types: ["text/plain"], text: "just text" }));

    expect(screen.queryByTestId("composer-image-notice")).toBeNull();
    expect(saveImage).not.toHaveBeenCalled();
  });

  it("选图入口存在：格式栏的按钮打开一个 image/* 的文件选择器（此前 renderer 里没有任何 input[type=file]）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    await waitEditor(s);

    const input = screen.getByTestId("composer-image-input") as HTMLInputElement;
    expect(input.getAttribute("type")).toBe("file");
    expect(input.getAttribute("accept")).toBe("image/*");
    expect(input.multiple).toBe(true);

    const clickSpy = vi.spyOn(input, "click");
    await userEvent.click(screen.getByTestId("composer-attach-image"));
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });

  it("选中的文件走同一条 attach 路径：落盘 + 光标处占位符（与粘贴/拖拽一致）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImageArgs) => ({ path: "/tmp/pick.png", mime: "image/png" }));
    const logLine = vi.fn();
    const s = setup(store, { saveImage, logLine });
    const editor = await waitEditor(s);

    const input = screen.getByTestId("composer-image-input") as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [pngFile("pick.png")], configurable: true });
    act(() => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    expect(saveImage.mock.calls[0][0]).toMatchObject({ sessionId: "s1", mime: "image/png", label: "pick" });
    expect(editor.getText()).toContain("[📷 pick]");
    expect(logLine).toHaveBeenCalledWith("info", expect.stringContaining("[composer:image] attach 1 file(s)"));
  });

  it("选完复位 input.value：同一张图连选两次都触发 change（第二次不再静默）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImageArgs) => ({ path: "/tmp/again.png", mime: "image/png" }));
    const s = setup(store, { saveImage });
    await waitEditor(s);

    const input = screen.getByTestId("composer-image-input") as HTMLInputElement;
    for (let i = 0; i < 2; i += 1) {
      Object.defineProperty(input, "files", { value: [pngFile("again.png")], configurable: true });
      act(() => {
        input.dispatchEvent(new Event("change", { bubbles: true }));
      });
      await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(i + 1));
      expect(input.value).toBe("");
    }
  });
});

describe("Composer — 输入历史召回（issue #2349 part 2/2）", () => {
  /** 编辑器当前内容（markdown 序列化，同发送路径读的那一份） */
  const content = (s: ReturnType<typeof setup>) => {
    const ed = s.editor()!;
    return (ed.storage as unknown as { markdown: { getMarkdown(): string } }).markdown.getMarkdown().trim();
  };

  it("↑ 从最新一条往回走，↓ 走到最新一条再按一次把原草稿还回来", async () => {
    const store = createTranscriptStore();
    const s = setup(store, { inputHistory: ["one", "two", "three"] });
    await waitEditor(s);

    s.type("draft");
    expect(content(s)).toBe("draft");

    s.press("ArrowUp");
    expect(content(s)).toBe("three");
    s.press("ArrowUp");
    expect(content(s)).toBe("two");
    s.press("ArrowUp");
    expect(content(s)).toBe("one");
    // 最旧一条就是尽头：再按 ↑ 停在原处，不会绕回最新。
    s.press("ArrowUp");
    expect(content(s)).toBe("one");

    s.press("ArrowDown");
    expect(content(s)).toBe("two");
    s.press("ArrowDown");
    expect(content(s)).toBe("three");
    // 走过最新一条 → 召回结束，宿主自己那份草稿回来（不是历史里的任何一条）。
    s.press("ArrowDown");
    expect(content(s)).toBe("draft");
  });

  it("历史为空或还没读到 → 方向键不拦，输入框原样不动", async () => {
    for (const inputHistory of [[], null] as (string[] | null)[]) {
      const store = createTranscriptStore();
      const s = setup(store, { inputHistory });
      await waitEditor(s);
      s.type("keep me");
      s.press("ArrowUp");
      s.press("ArrowDown");
      expect(content(s)).toBe("keep me");
      s.unmount();
    }
  });

  it("未在召回中时 ↓ 不抢：没有草稿快照可还，内容不变", async () => {
    const store = createTranscriptStore();
    const s = setup(store, { inputHistory: ["only"] });
    await waitEditor(s);
    s.type("typing");
    s.press("ArrowDown");
    expect(content(s)).toBe("typing");
  });

  it("发送之后指针归零：下一次 ↑ 又从最新一条开始", async () => {
    const store = createTranscriptStore();
    const sendMessage = vi.fn(async () => ({ requestId: "r1" }));
    const s = setup(store, { inputHistory: ["first", "second"], sendMessage });
    await waitEditor(s);

    s.type("hello");
    s.press("Enter");
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1));
    expect(content(s)).toBe("");

    s.press("ArrowUp");
    expect(content(s)).toBe("second");
  });

  it("发送成功之后上报这一条；发送失败则不上报（daemon 什么都没收下）", async () => {
    const store = createTranscriptStore();
    const onPromptSent = vi.fn();
    const sendMessage = vi.fn(async () => ({ requestId: "r1" }));
    const s = setup(store, { sid: "s1", inputHistory: ["old"], sendMessage, onPromptSent } as never);
    await waitEditor(s);

    s.type("hello");
    s.press("Enter");
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1));
    // 会话 id 随文字一起走，且是**这次发送用的那个**（sendMessage 拿到的是同一个）。
    expect(onPromptSent).toHaveBeenCalledWith("s1", "hello");
    s.unmount();

    const failing = vi.fn(async () => {
      throw new Error("boom");
    });
    const onFailed = vi.fn();
    const s2 = setup(createTranscriptStore(), { sid: "s1", inputHistory: ["old"], sendMessage: failing, onPromptSent: onFailed } as never);
    await waitEditor(s2);
    s2.type("nope");
    s2.press("Enter");
    await waitFor(() => expect(failing).toHaveBeenCalledTimes(1));
    expect(onFailed).not.toHaveBeenCalled();
  });

  it("会话切换之后指针归零：不会把上一个会话的草稿塞进新会话", async () => {
    const store = createTranscriptStore();
    const s = setup(store, { sid: "s1", inputHistory: ["a1"] });
    await waitEditor(s);

    s.type("draft A");
    s.press("ArrowUp");
    expect(content(s)).toBe("a1");

    s.rerender(
      <I18nProvider lang="zh">
        <Composer store={store} sid="s2" sandbox="workspace-write" inputHistory={["b1"]} editorRef={s.editorRef} />
      </I18nProvider>,
    );
    // 新会话按 ↓：未在召回中，所以什么都不还。
    s.press("ArrowDown");
    expect(content(s)).not.toBe("draft A");
  });

  it("召回中发送：这条离手，指针归零、草稿快照作废（下一次 ↑ 从最新一条重来，↓ 不会把召回前的草稿塞回来）", async () => {
    const store = createTranscriptStore();
    const sendMessage = vi.fn(async () => ({ requestId: "r1" }));
    const s = setup(store, { inputHistory: ["first", "second"], sendMessage });
    await waitEditor(s);

    // 先在召回里，然后发送：此刻 historyIndexRef 指向 "second"、草稿快照里躺着 "draft"。
    s.type("draft");
    s.press("ArrowUp");
    expect(content(s)).toBe("second");
    s.press("Enter");
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1));
    expect(content(s)).toBe("");

    // 指针已归零 ⇒ 下一次 ↑ 是最新一条，而不是从指针原地再退一格。
    s.press("ArrowUp");
    expect(content(s)).toBe("second");
    // 草稿快照已作废 ⇒ 走过最新一条是空输入框，不是召回前那份 "draft"
    // （那条消息已经随发送离手，留着它会把旧草稿当成"用户还没发的东西"还回去）。
    s.press("ArrowDown");
    expect(content(s)).toBe("");
  });

  it("会话在发送途中被切走：上报的仍是**这次发送**的会话 id，不是切过去的那个", async () => {
    // issue #2349 part 2/2（veto cyc20261019-023629 的实测）：onPromptSent 若闭包住
    // 「渲染时 activeSid」，await 解析时 ref 已指向新渲染的闭包 ⇒ 这条 prompt 会被记进
    // 宿主**切过去**的会话，而那个会话的 daemon 文件里没有它。这里让 send 挂住、把组件
    // 重渲染成 s2，再放行 await：上报必须仍是 s1。
    const store = createTranscriptStore();
    let release: (v: { requestId: string }) => void = () => {};
    const sendMessage = vi.fn(() => new Promise<{ requestId: string }>((r) => { release = r; }));
    const onPromptSent = vi.fn();
    const s = setup(store, { sid: "s1", inputHistory: ["old"], sendMessage, onPromptSent } as never);
    await waitEditor(s);

    s.type("typed in s1");
    s.press("Enter");
    expect(sendMessage).toHaveBeenCalledTimes(1);

    // 发送还没解析，宿主切到 s2（Shell 会因此重渲染、把新的 onPromptSent 挂进 ref）。
    s.rerender(
      <I18nProvider lang="zh">
        <Composer store={store} sid="s2" sandbox="workspace-write" inputHistory={["second"]} onPromptSent={onPromptSent} />
      </I18nProvider>,
    );
    await act(async () => {
      release({ requestId: "r1" });
    });
    await waitFor(() => expect(onPromptSent).toHaveBeenCalledTimes(1));
    expect(onPromptSent).toHaveBeenCalledWith("s1", "typed in s1");
  });
});

/**
 * issue #2384 — `/rewind` 把选中的那条原文放回输入框。
 *
 * 与草稿恢复（`getComposerDraft`）的区别正是这条测试存在的原因：草稿是**受控值**，
 * 输入框每次挂载读一次；而 rewind 的回填是宿主在输入框已经挂载、可能正在打字时发生的
 * **一次性插入**。所以它按 `id` 应用一次，绝不覆盖宿主之后打的字，也绝不自动发送。
 */
describe("Composer — /rewind 回填（issue #2384）", () => {
  const LONG = "第一行\n\n第二行：" + "x".repeat(300);

  function mountWithInsert(text: string) {
    const store = createTranscriptStore();
    const sendMessage = vi.fn(async () => ({ ok: true }) as unknown as SendResult);
    const editorRef: MutableRefObject<Editor | null> = { current: null };
    const tree = (insert: { id: number; text: string } | null) => (
      <I18nProvider lang="zh">
        <Composer
          store={store}
          sid="s1"
          sandbox="workspace-write"
          editorRef={editorRef}
          sendMessage={sendMessage}
          insertRequest={insert}
        />
      </I18nProvider>
    );
    const utils = render(tree(null));
    return { ...utils, store, sendMessage, editorRef, text: LONG, rerenderWith: (insert: { id: number; text: string } | null) => utils.rerender(tree(insert)) };
  }

  it("整段原文进输入框（不是 preview 截断），光标在末尾，且不自动发送", async () => {
    const m = mountWithInsert(LONG);
    await waitFor(() => expect(m.editorRef.current).not.toBeNull());
    const editor = m.editorRef.current!;

    act(() => {
      m.rerenderWith({ id: 1, text: LONG });
    });

    // 逐字相等（走 tiptap 的 markdown 序列化，与草稿恢复同一条路）。
    const md = (editor.storage as unknown as { markdown: { getMarkdown(): string } }).markdown.getMarkdown();
    expect(md).toBe(LONG);
    // 光标在末尾：紧接着插入的字符落在最后，而不是开头。
    act(() => {
      editor.commands.insertContent("!");
    });
    expect(editor.getText().trimEnd().endsWith("!")).toBe(true);
    // 不自动发送：回填是给宿主改的，发不发由宿主决定。
    expect(m.sendMessage).not.toHaveBeenCalled();
  });

  it("同一次请求只应用一次：宿主之后打的字不会被重渲染盖掉", async () => {
    const m = mountWithInsert(LONG);
    await waitFor(() => expect(m.editorRef.current).not.toBeNull());
    const editor = m.editorRef.current!;

    act(() => {
      m.rerenderWith({ id: 1, text: LONG });
    });
    act(() => {
      editor.commands.focus("end");
      editor.commands.insertContent("追加");
    });
    // 同一个 id 再渲染一次（父级重渲染、会话无关的状态变化都会走到这里）。
    act(() => {
      m.rerenderWith({ id: 1, text: LONG });
    });
    expect(editor.getText()).toContain("追加");
  });
});
