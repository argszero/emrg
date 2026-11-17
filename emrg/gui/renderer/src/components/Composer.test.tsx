import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  Composer,
  type ComposerProps,
  type SaveFilePayload,
  type SaveImagePayload,
  type SendOptions,
} from "./Composer";
import { createTranscriptStore, type TranscriptStore } from "../lib/transcript";
import { I18nProvider } from "../lib/i18n";

/**
 * Composer.test.tsx — 输入框 + / 补全菜单 + 发送流 + 附件组件测试（纯文本框版，issue #2920）。
 *
 * 输入框是 `<textarea>`，不再是 tiptap 的 contenteditable：敲进去的字符就是发出去的字符，
 * 不做 markdown 解析、不做序列化、也不做发送前的反转义。所以这套测试驱动的是**真键盘事件
 * 与真 change 事件**，不再有 editor 命令。
 *
 * 断言四件事：补全菜单出现/键盘导航/选择填充；发送流（用户行 + 输入清空 + requestId +
 * 原文不加工）；失败恢复（G49）、busy 队列注入（#655）、停止回复；附件的三条入口
 * （粘贴/拖拽/选择文件）——图片按图片落盘、其它文件落盘后正文里只留一行路径。
 */

/** 渲染一棵组件树（所有可注入 prop 透传，`sandbox` 默认 workspace-write） */
function tree(store: TranscriptStore, opts: Partial<ComposerProps> = {}) {
  const { sandbox = "workspace-write", sid = "s1", ...rest } = opts;
  return (
    <I18nProvider lang="zh">
      <Composer store={store} sid={sid} sandbox={sandbox} {...rest} />
    </I18nProvider>
  );
}

function setup(store: TranscriptStore, opts: Partial<ComposerProps> = {}) {
  const utils = render(tree(store, opts));
  /** 输入框本体（真 textarea：值在 value 上） */
  const input = () => screen.getByTestId("composer-input") as HTMLTextAreaElement;
  /** 输入框当前内容 */
  const value = () => input().value;
  /** 一次性把内容设成 `text`（走 onChange，等价于整段粘贴） */
  const setText = (text: string) => {
    fireEvent.change(input(), { target: { value: text } });
  };
  /** 在输入框上按一个键（onKeyDown 挂在它身上） */
  const press = (key: string, mods: Record<string, boolean> = {}) => {
    fireEvent.keyDown(input(), { key, ...mods });
  };
  /** 在输入框上派发一次 paste（React 的 onPaste） */
  const paste = (clipboardData: unknown) => {
    const ev = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(ev, "clipboardData", { value: clipboardData });
    act(() => {
      input().dispatchEvent(ev);
    });
  };
  /** 在 composer 容器上派发一次 drop（wrapper 的 onDrop） */
  const drop = (dataTransfer: unknown) => {
    const ev = new Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(ev, "dataTransfer", { value: dataTransfer });
    act(() => {
      screen.getByTestId("composer").dispatchEvent(ev);
    });
  };
  /** 通过隐藏的 file input 选文件（第三条入口） */
  const pick = (files: File[]) => {
    const el = screen.getByTestId("composer-file-input") as HTMLInputElement;
    Object.defineProperty(el, "files", { value: files, configurable: true });
    act(() => {
      el.dispatchEvent(new Event("change", { bubbles: true }));
    });
    return el;
  };
  const rerenderWith = (over: Partial<ComposerProps>) => utils.rerender(tree(store, { ...opts, ...over }));
  return { ...utils, input, value, setText, press, paste, drop, pick, rerenderWith };
}

// ── 事件替身（只实现被测代码读的那几个面） ────────────────────────────────────

interface FakeCarrier {
  files?: File[];
  items?: { kind: string; type: string; getAsFile: () => File | null }[];
  types?: string[];
  text?: string;
}

/** clipboardData / dataTransfer 的替身 */
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

/** 造一个非图片文件（附件路径：落盘 + 正文一行路径） */
function textFile(name = "notes.txt", body = "hello"): File {
  return new File([body], name, { type: "text/plain" });
}

describe("Composer — 纯文本框（issue #2920 要求 1/2/5）", () => {
  it("输入框就是一个 textarea：没有 contenteditable，也没有格式栏", () => {
    setup(createTranscriptStore());
    const el = screen.getByTestId("composer-input");
    expect(el.tagName).toBe("TEXTAREA");
    // 要求 5：格式栏与格式快捷键随富文本一起删除
    expect(screen.queryByTestId("fmt-bar")).toBeNull();
    expect(document.querySelector("[contenteditable]")).toBeNull();
    expect(document.querySelector('[data-testid^="fmt-"]')).toBeNull();
  });

  it("markdown 字符原样待在框里：打 `**粗体**` 看到的是星号，不被渲染、不被转义", () => {
    const s = setup(createTranscriptStore());
    const typed = "**粗体** 与 `行内代码`，反斜杠 \\[x\\]";
    s.setText(typed);
    expect(s.value()).toBe(typed);
  });

  it("发送即原文：不做 markdown 序列化，也不做发送前的反转义", async () => {
    const store = createTranscriptStore();
    const sent: SendOptions[] = [];
    const s = setup(store, {
      sendMessage: async (o) => {
        sent.push(o);
        return { requestId: o.requestId };
      },
    });
    const typed = "**粗体**\n`code` 与 \\[x\\]";
    s.setText(typed);
    s.press("Enter");
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].text).toBe(typed);
    const entries = store.getEntries("s1");
    expect(entries[entries.length - 1]).toMatchObject({ kind: "user", text: typed });
  });
});

describe("Composer — 补全菜单", () => {
  it("输入 / 前缀弹出匹配菜单（无空格仍在指令词）", () => {
    const s = setup(createTranscriptStore());
    s.setText("/c");
    expect(screen.getByTestId("cmd-menu")).toBeInTheDocument();
    expect(screen.getAllByRole("menuitem").length).toBeGreaterThan(0);
  });

  it("空格后输入内容关闭菜单（离开指令词 → 普通消息）", () => {
    const s = setup(createTranscriptStore());
    // 注意：vanilla 用 trim() 判定——尾部空格被去后仍是指令词，菜单保留；输词后关闭
    s.setText("/clear x");
    expect(screen.queryByTestId("cmd-menu")).not.toBeInTheDocument();
  });

  it("↑↓ 导航切换选中项（vanilla (index±1+n)%n 环绕）", () => {
    const s = setup(createTranscriptStore());
    s.setText("/c");
    const items = screen.getAllByRole("menuitem");
    expect(items[0]).toHaveClass("selected");
    s.press("ArrowDown");
    expect(items[1]).toHaveClass("selected");
    expect(items[0]).not.toHaveClass("selected");
    // 环绕：最后一项 + ↓ → 回到第 0 项
    s.press("ArrowDown");
    expect(items[0]).toHaveClass("selected");
    // ↑ 也环绕：第 0 项 + ↑ → 回最后一项
    s.press("ArrowUp");
    expect(items[items.length - 1]).toHaveClass("selected");
  });

  it("Enter 选择补全项 → 填充输入框并关闭菜单（用户可继续回车执行）", () => {
    const s = setup(createTranscriptStore());
    s.setText("/clear");
    s.press("Enter");
    expect(s.value()).toBe("/clear");
    expect(screen.queryByTestId("cmd-menu")).not.toBeInTheDocument();
  });

  it("mousedown 点击补全项 → 填充输入框（preventDefault 防失焦）", async () => {
    const s = setup(createTranscriptStore());
    s.setText("/clear");
    await userEvent.click(screen.getByTestId("cmd-item-/clear"));
    expect(s.value()).toBe("/clear");
  });

  it("Escape 关闭菜单", () => {
    const s = setup(createTranscriptStore());
    s.setText("/c");
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
    s.setText("hello emrg");
    s.press("Enter");
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].text).toBe("hello emrg");
    expect(sent[0].sandbox).toBe("workspace-write");
    expect(sent[0].requestId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(s.value()).toBe("");
    const entries = store.getEntries("s1");
    expect(entries[entries.length - 1]).toMatchObject({ kind: "user", text: "hello emrg" });
  });

  it("Ctrl+Enter 同样发送", async () => {
    let sent = 0;
    const s = setup(createTranscriptStore(), {
      sendMessage: async () => {
        sent++;
        return {};
      },
    });
    s.setText("ctrl send");
    s.press("Enter", { ctrlKey: true });
    await waitFor(() => expect(sent).toBe(1));
  });

  it("Shift+Enter 不发送（换行交给 textarea 的原生行为）", async () => {
    let sent = 0;
    const s = setup(createTranscriptStore(), {
      sendMessage: async () => {
        sent++;
        return {};
      },
    });
    s.setText("line one");
    s.press("Enter", { shiftKey: true });
    expect(sent).toBe(0);
    // 按键被放行给浏览器（组件不拦），内容原样保留
    expect(s.value()).toBe("line one");
  });

  it("空输入不发送（只有空白也一样）", async () => {
    let sent = 0;
    const s = setup(createTranscriptStore(), {
      sendMessage: async () => {
        sent++;
        return {};
      },
    });
    s.press("Enter");
    s.setText("   ");
    s.press("Enter");
    expect(sent).toBe(0);
  });

  it("失败恢复：系统消息（copy.sendFailed）+ 原文回填", async () => {
    const store = createTranscriptStore();
    const s = setup(store, {
      sendMessage: async () => {
        throw new Error("boom");
      },
    });
    s.setText("will fail");
    s.press("Enter");
    // 异步失败分支（G49：恢复输入框）——await sendFn 的 rejection 在微任务里，
    // 系统消息断言必须 waitFor
    await waitFor(() =>
      expect(
        store.getEntries("s1").some((e) => e.kind === "system" && e.text === "没发送成功，你的话我还留着，再试一次？"),
      ).toBe(true),
    );
    await waitFor(() => expect(s.value()).toBe("will fail"));
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
    s.setText("orphan");
    s.press("Enter");
    await waitFor(() => expect(sent).toBe(0));
    expect(store.getEntries(null).some((e) => e.kind === "system" && e.text === "请先创建一个对话。")).toBe(true);
  });

  it("/ 指令不进发送流，走 onCommand 路由（vanilla rant 19:44 P1）", async () => {
    let sent = 0;
    const routed: Array<{ cmd: string; args?: string[] }> = [];
    const s = setup(createTranscriptStore(), {
      sendMessage: async () => {
        sent++;
        return {};
      },
      onCommand: (r) => routed.push({ cmd: r.cmd, args: r.args }),
    });
    s.setText("/clear now");
    s.press("Enter");
    await waitFor(() => expect(routed).toEqual([{ cmd: "/clear", args: ["now"] }]));
    expect(sent).toBe(0);
    expect(s.value()).toBe("");
  });

  it("busy 时发送 → 不拦截（queue-injection #655：daemon 排队注入）", async () => {
    const sent: string[] = [];
    const s = setup(createTranscriptStore(), {
      busy: true,
      sendMessage: async (o) => {
        sent.push(o.requestId);
        return { requestId: `echo-${o.requestId}` };
      },
    });
    s.setText("queued msg");
    s.press("Enter");
    await waitFor(() => expect(sent).toHaveLength(1));
  });
});

describe("Composer — 停止回复（rant 2026-09-02T20:30:05：React 迁移丢了 send↔stop 切换）", () => {
  it("busy 时发送按钮切换为停止按钮；点击 → 点名会话的 cancel，本地不打任何字", async () => {
    const store = createTranscriptStore();
    const cancelled: string[] = [];
    setup(store, {
      busy: true,
      cancel: async (sessionId) => {
        cancelled.push(sessionId);
      },
    });
    // busy → send 按钮隐藏、stop 按钮可见（■ / composer.stop 文案）
    expect(screen.queryByTestId("composer-send")).not.toBeInTheDocument();
    const stop = screen.getByTestId("composer-stop");
    expect(stop).toBeInTheDocument();
    expect(stop).toHaveAttribute("aria-label", "停止回复");
    expect(stop.textContent).toBe("■");
    await userEvent.click(stop);
    // Rant 2026-09-20T12:50:13：cancel 带的是会话 id，不是「我这个连接」。
    await waitFor(() => expect(cancelled).toEqual(["s1"]));
    // 结束这轮的唯一陈述是 daemon 的会话级 cancelled 回执——本地不乐观地打「已停止响应」。
    expect(store.getEntries("s1").some((e) => e.kind === "system")).toBe(false);
  });

  it("非 busy 时仍显示发送按钮（无 stop）", () => {
    setup(createTranscriptStore());
    expect(screen.getByTestId("composer-send")).toBeInTheDocument();
    expect(screen.queryByTestId("composer-stop")).not.toBeInTheDocument();
  });

  it("无会话可点名时不发 cancel（与 TUI request_cancel 同一规则）", async () => {
    const store = createTranscriptStore();
    const cancelled: string[] = [];
    setup(store, {
      sid: "",
      busy: true,
      cancel: async (sessionId) => {
        cancelled.push(sessionId);
      },
    });
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
    // 无菜单 → Esc → stop：点名会话的 cancel（文案/清 busy 由回执产生，见 daemonBridge）
    s.press("Escape");
    await waitFor(() => expect(cancelled).toEqual(["s1"]));
    expect(store.getEntries("s1").some((e) => e.kind === "system")).toBe(false);

    // / 菜单开着时 Esc 只关菜单——busy 也不触发第二次 stop
    s.setText("/c");
    expect(screen.getByTestId("cmd-menu")).toBeInTheDocument();
    s.press("Escape");
    expect(screen.queryByTestId("cmd-menu")).not.toBeInTheDocument();
    expect(cancelled).toEqual(["s1"]); // 未追加
  });
});

describe("Composer — 沙箱档位与额外可写根", () => {
  it("沙箱切换器：默认 workspace-write 激活 + 三档渲染（重构回归恢复，rant 2026-08-30T16:34:29）", () => {
    setup(createTranscriptStore());
    expect(screen.getByTestId("sandbox-switcher")).toBeTruthy();
    expect(screen.getByTestId("sandbox-workspace-write").className).toContain("active");
    expect(screen.getByTestId("sandbox-read-only").getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByTestId("sandbox-danger-full-access").getAttribute("aria-pressed")).toBe("false");
  });

  it("沙箱切换器：切到 read-only → 发送消息带 read-only sandbox", async () => {
    const sent: Array<{ sandbox?: string | null }> = [];
    const s = setup(createTranscriptStore(), {
      sendMessage: async (o) => {
        sent.push({ sandbox: o.sandbox });
        return { requestId: o.requestId };
      },
    });
    await userEvent.click(screen.getByTestId("sandbox-read-only"));
    expect(screen.getByTestId("sandbox-read-only").className).toContain("active");
    expect(screen.getByTestId("sandbox-workspace-write").className).not.toContain("active");
    s.setText("readonly mode");
    s.press("Enter");
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].sandbox).toBe("read-only");
  });

  it("沙箱切换器：切到 danger-full-access → 发送消息带 danger-full-access sandbox", async () => {
    const sent: Array<{ sandbox?: string | null }> = [];
    const s = setup(createTranscriptStore(), {
      sendMessage: async (o) => {
        sent.push({ sandbox: o.sandbox });
        return { requestId: o.requestId };
      },
    });
    await userEvent.click(screen.getByTestId("sandbox-danger-full-access"));
    expect(screen.getByTestId("sandbox-danger-full-access").className).toContain("active");
    s.setText("full access mode");
    s.press("Enter");
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].sandbox).toBe("danger-full-access");
  });

  it("沙箱切换器：父级接管时显示 daemon 报的档位，点击只上报、自己不动（rant 2026-09-30T09:30:16 GUI 半边）", async () => {
    const asked: string[] = [];
    setup(createTranscriptStore(), { sandbox: "read-only", onSandboxChange: (m) => asked.push(m) });
    // 显示的是 prop（daemon 说的），不是本组件默认的 workspace-write
    expect(screen.getByTestId("sandbox-read-only").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("sandbox-workspace-write").getAttribute("aria-pressed")).toBe("false");
    await userEvent.click(screen.getByTestId("sandbox-danger-full-access"));
    expect(asked).toEqual(["danger-full-access"]);
    // 点击不改显示：档位等 daemon 的 sandbox_set 回来（客户端不先改自己那份）
    expect(screen.getByTestId("sandbox-read-only").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("sandbox-danger-full-access").getAttribute("aria-pressed")).toBe("false");
  });

  it("沙箱切换器：父级接管时，随消息下发的档位也是 daemon 报的那个", async () => {
    const sent: Array<{ sandbox?: string | null }> = [];
    const s = setup(createTranscriptStore(), {
      sandbox: "danger-full-access",
      onSandboxChange: () => {},
      sendMessage: async (o) => {
        sent.push({ sandbox: o.sandbox });
        return { requestId: o.requestId };
      },
    });
    s.setText("full access please");
    s.press("Enter");
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].sandbox).toBe("danger-full-access");
  });

  it("额外可写根入口：不接线不渲染；接线后显示 daemon 报的条数，点击只上报（rant 2026-10-09T09:43:39 GUI 半边）", async () => {
    const store = createTranscriptStore();
    // 未接线（单挂载 / 单测）：入口不渲染——与 onSandboxChange 的约定一致。
    const first = setup(store);
    expect(screen.queryByTestId("sandbox-roots-open")).toBeNull();
    first.unmount();

    const opened: number[] = [];
    setup(store, { sandboxRoots: null, onManageSandboxRoots: () => opened.push(1) });
    expect(screen.getByTestId("sandbox-roots-open")).toBeTruthy();
    // 没人问过 → 只报入口，不报条数：`0` 会主张一个 daemon 没报过的状态。
    expect(screen.getByTestId("sandbox-roots-open")).toHaveTextContent("额外根");
    expect(screen.getByTestId("sandbox-roots-open")).not.toHaveTextContent("额外根 0");
    await userEvent.click(screen.getByTestId("sandbox-roots-open"));
    expect(opened).toHaveLength(1);
  });

  it("额外可写根入口：条数就是 daemon 那份列表的长度（空列表也照实报 0）", () => {
    const store = createTranscriptStore();
    setup(store, { sandboxRoots: ["/tmp/a", "/tmp/b"], onManageSandboxRoots: vi.fn() });
    expect(screen.getByTestId("sandbox-roots-open")).toHaveTextContent("额外根 2");
  });

  it("额外可写根入口：空列表报 0（这是 daemon 说过的状态，与「没人问过」不同）", () => {
    const store = createTranscriptStore();
    setup(store, { sandboxRoots: [], onManageSandboxRoots: vi.fn() });
    expect(screen.getByTestId("sandbox-roots-open")).toHaveTextContent("额外根 0");
  });
});

describe("Composer — 高度自适应（要求 6：涨到上限后内滚）", () => {
  /** jsdom 没有布局，`scrollHeight` 恒为 0——所以量到的「内容高度」由测试给定，不写常量进组件 */
  const withScrollHeight = (el: HTMLTextAreaElement, h: number) => {
    Object.defineProperty(el, "scrollHeight", { value: h, configurable: true });
  };

  it("内容变高 → 框跟着长；超过上限 → 停在 150px 并改成内滚", () => {
    const s = setup(createTranscriptStore());
    const el = s.input();

    withScrollHeight(el, 80);
    s.setText("两行字");
    expect(el.style.height).toBe("80px");
    expect(el.style.overflowY).toBe("hidden");

    withScrollHeight(el, 400);
    s.setText("高度超过上限的一整段");
    expect(el.style.height).toBe("150px");
    expect(el.style.overflowY).toBe("auto");
  });
});

describe("Composer — 草稿持久化（rant 2026-09-01T20:28:31）", () => {
  it("输入 → 草稿实时写入 store", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    s.setText("hello draft");
    await waitFor(() => expect(store.getComposerDraft("s1")).toBe("hello draft"));
  });

  it("卸载重挂载 → 草稿恢复（原文回填，不经任何序列化）", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    s.setText("**bold** draft");
    await waitFor(() => expect(store.getComposerDraft("s1")).toContain("bold"));
    s.unmount();
    const s2 = setup(store);
    await waitFor(() => expect(s2.value()).toBe("**bold** draft"));
  });

  it("发送成功 → 草稿清空", async () => {
    const store = createTranscriptStore();
    const sendMessage = vi.fn().mockResolvedValue({ requestId: "r1" });
    const s = setup(store, { sendMessage });
    s.setText("send me");
    await waitFor(() => expect(store.getComposerDraft("s1")).toBe("send me"));
    s.press("Enter");
    await waitFor(() => expect(sendMessage).toHaveBeenCalled());
    await waitFor(() => expect(store.getComposerDraft("s1")).toBe(""));
  });

  it("会话切换（同挂载 prop 变化）→ 旧会话草稿保留 + 新会话草稿载入", async () => {
    const store = createTranscriptStore();
    const s = setup(store);
    s.setText("draft for s1");
    await waitFor(() => expect(store.getComposerDraft("s1")).toBe("draft for s1"));
    // 预置 s2 草稿（模拟之前在该会话输入过）
    act(() => {
      store.setComposerDraft("draft for s2", "s2");
    });
    s.rerenderWith({ sid: "s2" });
    await waitFor(() => expect(s.value()).toBe("draft for s2"));
    // 旧会话草稿仍在 store（切回 s1 可恢复）
    expect(store.getComposerDraft("s1")).toBe("draft for s1");
  });
});

// ── 附件：粘贴/拖拽/选择文件的落盘与可见拒绝（rant 2026-09-30T09:35:04 · issue #2920） ──
//
// 宿主报障（2026-09-30T09:33）：「GUI 输入框里 Cmd+V， 现在没有任何效果」。自那以后每一条
// 拒绝都必须出声，且**两个面都取**（files + items，粘贴一路由 Chromium 放在 items 里）。
//
// issue #2920 把入口放大成「任何文件」：图片仍按图片落盘、正文里留 `[📷 label]` 标记（vision
// 内容），其它文件落盘后正文里只留**一行路径**——模型自己按需读，而不是把整个文件塞进上下文。

describe("Composer — 图片粘贴/拖拽（rant 2026-09-30T09:35:04）", () => {
  it("粘贴只带 items 的图片（Chromium 形态）会落盘并插入标记", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImagePayload) => ({ path: "/tmp/shot.png", mime: "image/png" }));
    const logLine = vi.fn();
    const s = setup(store, { saveImage, logLine });

    // files 为空 —— 这正是「只读 cd.files」那个写法会得到空数组的形态
    s.paste(makeCarrier({ files: [], items: [fileItem(pngFile())], types: ["Files"] }));

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    expect(saveImage.mock.calls[0][0]).toMatchObject({ sessionId: "s1", mime: "image/png", label: "shot" });
    await waitFor(() => expect(s.value()).toContain("[📷 shot]"));
    expect(logLine).toHaveBeenCalledWith("info", expect.stringContaining("[composer:attach] 1 image(s), 0 file(s)"));
    expect(screen.queryByTestId("composer-notice")).toBeNull();
  });

  it("粘贴既有文本又有图（items）时文本先落、图随后附上（图片插在文字中间，对齐 TUI）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImagePayload) => ({ path: "/tmp/shot.png", mime: "image/png" }));
    const s = setup(store, { saveImage });

    s.paste(makeCarrier({ files: [], items: [fileItem(pngFile())], types: ["Files", "text/plain"], text: "hello" }));

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(s.value()).toContain("hello"));
    // 文本在前、标记在后：一张图落在句子里它该在的位置
    expect(s.value().indexOf("hello")).toBeLessThan(s.value().indexOf("[📷 shot]"));
  });

  it("拖拽（dataTransfer.files）仍然工作", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImagePayload) => ({ path: "/tmp/drop.png", mime: "image/png" }));
    const s = setup(store, { saveImage });

    s.drop(makeCarrier({ files: [pngFile("drop.png")], types: ["Files"] }));

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    expect(saveImage.mock.calls[0][0]).toMatchObject({ mime: "image/png", label: "drop" });
  });

  it("声明了 Files 却一个文件都没取到 → 出声（把事件里到底有什么写出来）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const readClipboardImage = vi.fn(async () => null);
    const logLine = vi.fn();
    const s = setup(store, { saveImage, readClipboardImage, logLine });

    s.paste(makeCarrier({ files: [], items: [], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-notice");
    expect(notice.textContent).toContain("Files");
    expect(saveImage).not.toHaveBeenCalled();
    expect(logLine).toHaveBeenCalledWith("warn", expect.stringContaining("[composer:attach] clipboard fallback failed"));
  });

  it("白名单外的类型（macOS 菜单栏给的 TIFF）报出它的类型，而不是静默丢弃", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const readClipboardImage = vi.fn(async () => null);
    const s = setup(store, { saveImage, readClipboardImage });

    const tiff = new File([new Uint8Array([1, 2])], "shot.tiff", { type: "image/tiff" });
    s.paste(makeCarrier({ files: [tiff], items: [fileItem(tiff)], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-notice");
    expect(notice.textContent).toContain("image/tiff");
    expect(saveImage).not.toHaveBeenCalled();
  });

  // ── 系统给的图不受白名单所限 ────────────────────────────────────────────────
  //
  // 白名单只是「不必转换就能直接收」的快路径。macOS 把菜单栏/截图给的图声明成
  // `image/tiff`，renderer 解不了（Chromium 只出 png/jpeg/gif/webp/bmp/svg）——转换发生在
  // 有 NSImage 的一侧（main 的 `clipboard.readImage()` → `toPNG()`），这正是 TUI 向 macOS
  // 要 `«class PNGf»` 的同一个动作。

  /** main 归一后的 PNG（内容无关——这条路径不解析像素，只搬运字节）。 */
  const PNG_B64 = btoa("\x89PNG\r\n\x1a\n");

  it("事件里只有名单外的 TIFF 时，先请 main 把剪贴板里的图转成 PNG，再走同一条落盘路径", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImagePayload) => ({ path: "/tmp/shot.png", mime: "image/png" }));
    const readClipboardImage = vi.fn(async () => ({ data: PNG_B64, mime: "image/png", name: "clipboard.png" }));
    const logLine = vi.fn();
    const s = setup(store, { saveImage, readClipboardImage, logLine });

    const tiff = new File([new Uint8Array([1, 2])], "shot.tiff", { type: "image/tiff" });
    s.paste(makeCarrier({ files: [tiff], items: [fileItem(tiff)], types: ["Files"] }));

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    expect(readClipboardImage).toHaveBeenCalledTimes(1);
    // 落盘的必须是转换后的 PNG，不是那张 renderer 解不了的 TIFF
    expect(saveImage.mock.calls[0][0]).toMatchObject({ sessionId: "s1", mime: "image/png", label: "clipboard" });
    await waitFor(() => expect(s.value()).toContain("[📷 clipboard]"));
    expect(screen.queryByTestId("composer-notice")).toBeNull();
    expect(logLine).toHaveBeenCalledWith("info", expect.stringContaining("clipboard fallback"));
  });

  it("剪贴板里也没有可转换的图时，拒绝仍然可见并报出事件里给的类型", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const readClipboardImage = vi.fn(async () => null);
    const logLine = vi.fn();
    const s = setup(store, { saveImage, readClipboardImage, logLine });

    const tiff = new File([new Uint8Array([1, 2])], "shot.tiff", { type: "image/tiff" });
    s.paste(makeCarrier({ files: [tiff], items: [fileItem(tiff)], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-notice");
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

    const tiff = new File([new Uint8Array([1, 2])], "shot.tiff", { type: "image/tiff" });
    s.paste(makeCarrier({ files: [tiff], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-notice");
    expect(notice.textContent).toContain("image/tiff");
    expect(saveImage).not.toHaveBeenCalled();
    expect(logLine).toHaveBeenCalledWith(
      "warn",
      expect.stringContaining("window.emrg.readClipboardImage unavailable"),
    );
  });

  it("声明了 Files 却一个文件都没取到时，也先问一次剪贴板（宿主报障「按了没反应」的那个形态）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImagePayload) => ({ path: "/tmp/shot.png", mime: "image/png" }));
    const readClipboardImage = vi.fn(async () => ({ data: PNG_B64, mime: "image/png", name: "clipboard.png" }));
    const s = setup(store, { saveImage, readClipboardImage });

    s.paste(makeCarrier({ files: [], items: [], types: ["Files"] }));

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(s.value()).toContain("[📷 clipboard]"));
    expect(screen.queryByTestId("composer-notice")).toBeNull();
  });

  it("混合粘贴（png + tiff）不重复取剪贴板：能直接收的那张照常落盘，名单外的照常出声", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImagePayload) => ({ path: "/tmp/mix.png", mime: "image/png" }));
    const readClipboardImage = vi.fn(async () => ({ data: PNG_B64, mime: "image/png", name: "clipboard.png" }));
    const s = setup(store, { saveImage, readClipboardImage });

    const tiff = new File([new Uint8Array([1, 2])], "shot.tiff", { type: "image/tiff" });
    s.paste(makeCarrier({ files: [pngFile("mix.png"), tiff], types: ["Files"] }));

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    expect(saveImage.mock.calls[0][0]).toMatchObject({ mime: "image/png", label: "mix" });
    // 剪贴板里那张就是刚粘上来的图 —— 再取一次会把同一张图重复附加
    expect(readClipboardImage).not.toHaveBeenCalled();
    const notice = await screen.findByTestId("composer-notice");
    expect(notice.textContent).toContain("image/tiff");
  });

  it("纯文本粘贴不读剪贴板（没有图就没有这条路）", () => {
    const store = createTranscriptStore();
    const readClipboardImage = vi.fn(async () => ({ data: PNG_B64, mime: "image/png" }));
    const saveImage = vi.fn();
    const s = setup(store, { saveImage, readClipboardImage });

    s.paste(makeCarrier({ types: ["text/plain"], text: "just text" }));

    expect(readClipboardImage).not.toHaveBeenCalled();
    expect(saveImage).not.toHaveBeenCalled();
  });

  it("拖拽名单外的文件不走剪贴板：那本来就是磁盘上的一个文件", () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const readClipboardImage = vi.fn(async () => ({ data: PNG_B64, mime: "image/png" }));
    const s = setup(store, { saveImage, readClipboardImage });

    const tiff = new File([new Uint8Array([1, 2])], "dropped.tiff", { type: "image/tiff" });
    s.drop(makeCarrier({ files: [tiff], types: ["Files"] }));

    expect(readClipboardImage).not.toHaveBeenCalled();
    expect(saveImage).not.toHaveBeenCalled();
  });

  it("落盘被拒（IPC 报错）时把原因摆到输入框上", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImagePayload) => {
      throw new Error("invalid session_id");
    });
    const logLine = vi.fn();
    const s = setup(store, { saveImage, logLine });

    s.paste(makeCarrier({ files: [pngFile()], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-notice");
    expect(notice.textContent).toContain("invalid session_id");
    expect(s.value()).not.toContain("[📷");
    expect(logLine).toHaveBeenCalledWith("warn", expect.stringContaining("invalid session_id"));
  });

  it("没有会话时拒绝也要看得见（不再是静默 return）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const s = setup(store, { sid: null, saveImage });

    s.paste(makeCarrier({ files: [pngFile()], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-notice");
    expect(notice.textContent).toContain("会话");
    expect(saveImage).not.toHaveBeenCalled();
  });

  it("普通文本粘贴不报错（没有图就没有提示）", () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const s = setup(store, { saveImage });

    s.paste(makeCarrier({ types: ["text/plain"], text: "just text" }));

    expect(screen.queryByTestId("composer-notice")).toBeNull();
    expect(saveImage).not.toHaveBeenCalled();
  });
});

describe("Composer — 任意文件附件（issue #2920 要求 4）", () => {
  it("选择文件的入口不再只收图片：input 没有 accept，且 multiple", async () => {
    setup(createTranscriptStore());
    const input = screen.getByTestId("composer-file-input") as HTMLInputElement;
    expect(input.getAttribute("type")).toBe("file");
    // 要求 4：选择文件不再只收图片——accept 白名单已去掉
    expect(input.getAttribute("accept")).toBeNull();
    expect(input.multiple).toBe(true);
    expect(input.hidden).toBe(true);

    const clickSpy = vi.spyOn(input, "click");
    await userEvent.click(screen.getByTestId("composer-attach"));
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });

  it("粘贴一个非图片文件 → 落盘、正文里留一行路径，不当作图片", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const saveFile = vi.fn(async (_p: SaveFilePayload) => ({ path: "/tmp/notes.txt" }));
    const logLine = vi.fn();
    const s = setup(store, { saveImage, saveFile, logLine });

    s.paste(makeCarrier({ files: [textFile()], types: ["Files"] }));

    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(1));
    expect(saveFile.mock.calls[0][0]).toMatchObject({ sessionId: "s1", name: "notes.txt", mime: "text/plain" });
    // 模型拿到的是一个路径，不是整份内容——它自己按需读
    await waitFor(() => expect(s.value()).toBe("/tmp/notes.txt\n"));
    expect(saveImage).not.toHaveBeenCalled();
    expect(logLine).toHaveBeenCalledWith("info", expect.stringContaining("[composer:attach] file /tmp/notes.txt"));
  });

  it("拖入的非图片文件同样落盘给路径（含本端解不了的图：那是磁盘上的文件）", async () => {
    const store = createTranscriptStore();
    const saveFile = vi.fn(async (_p: SaveFilePayload) => ({ path: "/tmp/dropped.tiff" }));
    const s = setup(store, { saveFile });

    const tiff = new File([new Uint8Array([1, 2])], "dropped.tiff", { type: "image/tiff" });
    s.drop(makeCarrier({ files: [tiff], types: ["Files"] }));

    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(1));
    expect(saveFile.mock.calls[0][0]).toMatchObject({ name: "dropped.tiff" });
    await waitFor(() => expect(s.value()).toBe("/tmp/dropped.tiff\n"));
  });

  it("一次拖入多个文件 → 每个都落盘，路径按顺序插进正文", async () => {
    const store = createTranscriptStore();
    const paths = ["/tmp/a.txt", "/tmp/b.csv"];
    const saveFile = vi.fn(async (_p: SaveFilePayload) => ({ path: paths[saveFile.mock.calls.length - 1] }));
    const s = setup(store, { saveFile });

    s.drop(
      makeCarrier({
        files: [textFile("a.txt"), new File(["x"], "b.csv", { type: "text/csv" })],
        types: ["Files"],
      }),
    );

    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(2));
    // 两个路径必须是两个路径。没有分隔符时它们拼成 `…/a.txt…/b.csv` —— 一个谁也不是的
    // 字面量：模型拿它去读，两个文件一个都打不开（TUI 在插入处就补 `"\n"`，两端同形）。
    await waitFor(() => expect(s.value()).toBe("/tmp/a.txt\n/tmp/b.csv\n"));
  });

  it("文件落盘被拒也出声（fileReasonSave），正文里不留路径", async () => {
    const store = createTranscriptStore();
    const saveFile = vi.fn(async () => {
      throw new Error("disk full");
    });
    const s = setup(store, { saveFile });

    s.paste(makeCarrier({ files: [textFile()], types: ["Files"] }));

    const notice = await screen.findByTestId("composer-notice");
    expect(notice.textContent).toContain("disk full");
    expect(s.value()).not.toContain("/tmp/");
  });

  it("选中的图片走同一条图片路径：落盘 + 标记（与粘贴/拖拽一致）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImagePayload) => ({ path: "/tmp/pick.png", mime: "image/png" }));
    const saveFile = vi.fn();
    const logLine = vi.fn();
    const s = setup(store, { saveImage, saveFile, logLine });

    s.pick([pngFile("pick.png")]);

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    expect(saveImage.mock.calls[0][0]).toMatchObject({ sessionId: "s1", mime: "image/png", label: "pick" });
    await waitFor(() => expect(s.value()).toBe("[📷 pick]\n"));
    expect(saveFile).not.toHaveBeenCalled();
    expect(logLine).toHaveBeenCalledWith("info", expect.stringContaining("[composer:attach] image /tmp/pick.png"));
  });

  it("选中的非图片文件走文件路径（saveFile），正文里是落盘路径", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn();
    const saveFile = vi.fn(async (_p: SaveFilePayload) => ({ path: "/tmp/picked.csv" }));
    const s = setup(store, { saveImage, saveFile });

    s.pick([new File(["a,b"], "picked.csv", { type: "text/csv" })]);

    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(1));
    expect(saveFile.mock.calls[0][0]).toMatchObject({ name: "picked.csv", mime: "text/csv" });
    await waitFor(() => expect(s.value()).toBe("/tmp/picked.csv\n"));
    expect(saveImage).not.toHaveBeenCalled();
  });

  it("选完复位 input.value：同一个文件连选两次都触发 change（第二次不再静默）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImagePayload) => ({ path: "/tmp/again.png", mime: "image/png" }));
    const s = setup(store, { saveImage });

    for (let i = 0; i < 2; i += 1) {
      const el = s.pick([pngFile("again.png")]);
      await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(i + 1));
      expect(el.value).toBe("");
    }
  });

  it("一次选中图片与文件各一 → 两条路径各自到位（图片是标记、文件是路径）", async () => {
    const store = createTranscriptStore();
    const saveImage = vi.fn(async (_p: SaveImagePayload) => ({ path: "/tmp/two.png", mime: "image/png" }));
    const saveFile = vi.fn(async (_p: SaveFilePayload) => ({ path: "/tmp/two.txt" }));
    const s = setup(store, { saveImage, saveFile });

    s.pick([pngFile("two.png"), textFile("two.txt")]);

    await waitFor(() => expect(saveImage).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(s.value()).toBe("[📷 two]\n/tmp/two.txt\n"));
  });
});

describe("Composer — 附件随消息发出（issue #2920 要求 3/4）", () => {
  it("图片：随消息下发的 images 带 path/label/mime/position（daemon 按 position 切块）", async () => {
    const store = createTranscriptStore();
    const sent: SendOptions[] = [];
    const saveImage = vi.fn(async (_p: SaveImagePayload) => ({ path: "/tmp/shot.png", mime: "image/png" }));
    const s = setup(store, {
      saveImage,
      sendMessage: async (o) => {
        sent.push(o);
        return { requestId: o.requestId };
      },
    });

    s.paste(makeCarrier({ files: [pngFile()], types: ["Files"] }));
    await waitFor(() => expect(s.value()).toContain("[📷 shot]"));
    s.press("Enter");

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].text).toBe("[📷 shot]");
    expect(sent[0].images).toEqual([
      { path: "/tmp/shot.png", label: "[📷 shot]", mime: "image/png", position: 0 },
    ]);
  });

  it("标记被删掉 → 那张图不再随消息发出（占位符即图的生命周期，TUI 同语义）", async () => {
    const store = createTranscriptStore();
    const sent: SendOptions[] = [];
    const saveImage = vi.fn(async (_p: SaveImagePayload) => ({ path: "/tmp/shot.png", mime: "image/png" }));
    const s = setup(store, {
      saveImage,
      sendMessage: async (o) => {
        sent.push(o);
        return { requestId: o.requestId };
      },
    });

    s.paste(makeCarrier({ files: [pngFile()], types: ["Files"] }));
    await waitFor(() => expect(s.value()).toContain("[📷 shot]"));
    s.setText("图我不要了");
    s.press("Enter");

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].text).toBe("图我不要了");
    expect(sent[0].images).toBeNull();
  });

  it("文件：路径就是正文的一部分，没有 images 载荷（模型自己按路径去读）", async () => {
    const store = createTranscriptStore();
    const sent: SendOptions[] = [];
    const saveFile = vi.fn(async (_p: SaveFilePayload) => ({ path: "/tmp/notes.txt" }));
    const s = setup(store, {
      saveFile,
      sendMessage: async (o) => {
        sent.push(o);
        return { requestId: o.requestId };
      },
    });

    s.paste(makeCarrier({ files: [textFile()], types: ["Files"] }));
    await waitFor(() => expect(s.value()).toBe("/tmp/notes.txt\n"));
    s.press("Enter");

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].text).toBe("/tmp/notes.txt");
    expect(sent[0].images).toBeNull();
  });
});

describe("Composer — 输入历史召回（issue #2349 part 2/2）", () => {
  it("↑ 从最新一条往回走，↓ 走到最新一条再按一次把原草稿还回来", () => {
    const s = setup(createTranscriptStore(), { inputHistory: ["one", "two", "three"] });

    s.setText("draft");
    expect(s.value()).toBe("draft");

    s.press("ArrowUp");
    expect(s.value()).toBe("three");
    s.press("ArrowUp");
    expect(s.value()).toBe("two");
    s.press("ArrowUp");
    expect(s.value()).toBe("one");
    // 最旧一条就是尽头：再按 ↑ 停在原处，不会绕回最新。
    s.press("ArrowUp");
    expect(s.value()).toBe("one");

    s.press("ArrowDown");
    expect(s.value()).toBe("two");
    s.press("ArrowDown");
    expect(s.value()).toBe("three");
    // 走过最新一条 → 召回结束，宿主自己那份草稿回来（不是历史里的任何一条）。
    s.press("ArrowDown");
    expect(s.value()).toBe("draft");
  });

  it("历史为空或还没读到 → 方向键不拦，输入框原样不动", () => {
    for (const inputHistory of [[], null] as (string[] | null)[]) {
      const s = setup(createTranscriptStore(), { inputHistory });
      s.setText("keep me");
      s.press("ArrowUp");
      s.press("ArrowDown");
      expect(s.value()).toBe("keep me");
      s.unmount();
    }
  });

  it("未在召回中时 ↓ 不抢：没有草稿快照可还，内容不变", () => {
    const s = setup(createTranscriptStore(), { inputHistory: ["only"] });
    s.setText("typing");
    s.press("ArrowDown");
    expect(s.value()).toBe("typing");
  });

  it("发送之后指针归零：下一次 ↑ 又从最新一条开始", async () => {
    const sendMessage = vi.fn(async () => ({ requestId: "r1" }));
    const s = setup(createTranscriptStore(), { inputHistory: ["first", "second"], sendMessage });

    s.setText("hello");
    s.press("Enter");
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1));
    expect(s.value()).toBe("");

    s.press("ArrowUp");
    expect(s.value()).toBe("second");
  });

  it("发送成功之后上报这一条；发送失败则不上报（daemon 什么都没收下）", async () => {
    const onPromptSent = vi.fn();
    const sendMessage = vi.fn(async () => ({ requestId: "r1" }));
    const s = setup(createTranscriptStore(), { sid: "s1", inputHistory: ["old"], sendMessage, onPromptSent });

    s.setText("hello");
    s.press("Enter");
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1));
    // 会话 id 随文字一起走，且是**这次发送用的那个**（sendMessage 拿到的是同一个）。
    expect(onPromptSent).toHaveBeenCalledWith("s1", "hello");
    s.unmount();

    const failing = vi.fn(async () => {
      throw new Error("boom");
    });
    const onFailed = vi.fn();
    const s2 = setup(createTranscriptStore(), {
      sid: "s1",
      inputHistory: ["old"],
      sendMessage: failing,
      onPromptSent: onFailed,
    });
    s2.setText("nope");
    s2.press("Enter");
    await waitFor(() => expect(failing).toHaveBeenCalledTimes(1));
    expect(onFailed).not.toHaveBeenCalled();
  });

  it("会话切换之后指针归零：不会把上一个会话的草稿塞进新会话", () => {
    const s = setup(createTranscriptStore(), { sid: "s1", inputHistory: ["a1"] });

    s.setText("draft A");
    s.press("ArrowUp");
    expect(s.value()).toBe("a1");

    s.rerenderWith({ sid: "s2", inputHistory: ["b1"] });
    // 新会话按 ↓：未在召回中，所以什么都不还。
    s.press("ArrowDown");
    expect(s.value()).not.toBe("draft A");
  });

  it("召回中发送：这条离手，指针归零、草稿快照作废（↓ 不会把召回前的草稿塞回来）", async () => {
    const sendMessage = vi.fn(async () => ({ requestId: "r1" }));
    const s = setup(createTranscriptStore(), { inputHistory: ["first", "second"], sendMessage });

    // 先在召回里，然后发送：此刻指针指向 "second"、草稿快照里躺着 "draft"。
    s.setText("draft");
    s.press("ArrowUp");
    expect(s.value()).toBe("second");
    s.press("Enter");
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1));
    expect(s.value()).toBe("");

    // 指针已归零 ⇒ 下一次 ↑ 是最新一条，而不是从指针原地再退一格。
    s.press("ArrowUp");
    expect(s.value()).toBe("second");
    // 草稿快照已作废 ⇒ 走过最新一条是空输入框，不是召回前那份 "draft"。
    s.press("ArrowDown");
    expect(s.value()).toBe("");
  });

  it("光标不在首行时不抢 ↑（多行草稿里方向键照常移动光标）", () => {
    const s = setup(createTranscriptStore(), { inputHistory: ["old"] });
    s.setText("first line\nsecond line");
    // 光标落在第二行
    act(() => {
      s.input().setSelectionRange(15, 15);
    });
    s.press("ArrowUp");
    expect(s.value()).toBe("first line\nsecond line");
  });

  it("会话在发送途中被切走：上报的仍是**这次发送**的会话 id，不是切过去的那个", async () => {
    // issue #2349 part 2/2（veto cyc20261019-023629 的实测）：onPromptSent 若闭包住
    // 「渲染时 activeSid」，await 解析时 ref 已指向新渲染的闭包 ⇒ 这条 prompt 会被记进
    // 宿主**切过去**的会话，而那个会话的 daemon 文件里没有它。这里让 send 挂住、把组件
    // 重渲染成 s2，再放行 await：上报必须仍是 s1。
    const store = createTranscriptStore();
    let release: (v: { requestId: string }) => void = () => {};
    const sendMessage = vi.fn(() => new Promise<{ requestId: string }>((r) => {
      release = r;
    }));
    const onPromptSent = vi.fn();
    const s = setup(store, { sid: "s1", inputHistory: ["old"], sendMessage, onPromptSent });

    s.setText("typed in s1");
    s.press("Enter");
    expect(sendMessage).toHaveBeenCalledTimes(1);

    // 发送还没解析，宿主切到 s2（Shell 会因此重渲染、把新的 onPromptSent 挂进 ref）。
    s.rerenderWith({ sid: "s2", inputHistory: ["second"] });
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

  function mountWithInsert() {
    const store = createTranscriptStore();
    const sendMessage = vi.fn().mockResolvedValue({ requestId: "r1" });
    const utils = render(tree(store, { sid: "s1", sendMessage }));
    const input = () => screen.getByTestId("composer-input") as HTMLTextAreaElement;
    const rerenderWith = (insert: { id: number; text: string } | null) =>
      utils.rerender(tree(store, { sid: "s1", sendMessage, insertRequest: insert }));
    return { ...utils, store, sendMessage, input, rerenderWith };
  }

  it("整段原文进输入框（不是 preview 截断），光标在末尾，且不自动发送", async () => {
    const m = mountWithInsert();
    act(() => {
      m.rerenderWith({ id: 1, text: LONG });
    });

    // 逐字相等：纯文本框里就是这段字，没有任何序列化
    await waitFor(() => expect(m.input().value).toBe(LONG));
    // 光标在末尾：紧接着插入的字符落在最后，而不是开头。
    expect(m.input().selectionStart).toBe(LONG.length);
    // 不自动发送：回填是给宿主改的，发不发由宿主决定。
    expect(m.sendMessage).not.toHaveBeenCalled();
  });

  it("同一次请求只应用一次：宿主之后打的字不会被重渲染盖掉", async () => {
    const m = mountWithInsert();
    act(() => {
      m.rerenderWith({ id: 1, text: LONG });
    });
    await waitFor(() => expect(m.input().value).toBe(LONG));
    await userEvent.type(m.input(), "追加");
    // 同一个 id 再渲染一次（父级重渲染、会话无关的状态变化都会走到这里）。
    act(() => {
      m.rerenderWith({ id: 1, text: LONG });
    });
    expect(m.input().value).toContain("追加");
  });
});
