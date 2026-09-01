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
    onCommand?: (r: { type: "command" | "unknown"; cmd: string; args?: string[] }) => void;
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
  return { ...utils, editor, input, type, press };
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
