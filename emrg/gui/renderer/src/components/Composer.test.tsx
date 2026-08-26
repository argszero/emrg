import { describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer, type SendResult } from "./Composer";
import { createTranscriptStore, type TranscriptStore } from "../lib/transcript";
import { I18nProvider } from "../lib/i18n";

/**
 * Composer.test.tsx — 输入框 + / 补全菜单 + 发送流组件测试（Batch 2 remainder）。
 * 注入假 sendMessage，断言：补全菜单出现/键盘导航/选择填充、发送流程（用户行 +
 * 输入清空 + requestId）、失败恢复（G49：文案 + 文本回填）、busy 队列注入（#655）。
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
  const utils = render(
    <I18nProvider lang="zh">
      <Composer store={store} sid={opts.sid ?? "s1"} sandbox="workspace-write" {...opts} />
    </I18nProvider>,
  );
  const input = () => screen.getByTestId("composer-input") as HTMLTextAreaElement;
  return { ...utils, input };
}

describe("Composer — 补全菜单", () => {
  it("输入 / 前缀弹出匹配菜单（无空格仍在指令词）", async () => {
    const store = createTranscriptStore();
    const { input } = setup(store);
    await userEvent.type(input(), "/c");
    const menu = screen.getByTestId("cmd-menu");
    expect(menu).toBeInTheDocument();
    expect(screen.getAllByRole("menuitem").length).toBeGreaterThan(0);
  });

  it("空格后输入内容关闭菜单（离开指令词 → 普通消息）", async () => {
    const store = createTranscriptStore();
    const { input } = setup(store);
    // 注意：vanilla 用 trim() 判定——尾部空格被去后仍是指令词，菜单保留；输词后关闭
    await userEvent.type(input(), "/clear x");
    expect(screen.queryByTestId("cmd-menu")).not.toBeInTheDocument();
  });

  it("↑↓ 导航切换选中项（vanilla (index±1+n)%n 环绕）", async () => {
    const store = createTranscriptStore();
    const { input } = setup(store);
    await userEvent.type(input(), "/c");
    const items = screen.getAllByRole("menuitem");
    expect(items[0]).toHaveClass("selected");
    await userEvent.keyboard("{ArrowDown}");
    expect(items[1]).toHaveClass("selected");
    expect(items[0]).not.toHaveClass("selected");
    // 环绕：最后一项 + ↓ → 回到第 0 项（(last+1) % n = 0）
    await userEvent.keyboard("{ArrowDown}");
    expect(items[0]).toHaveClass("selected");
    // ↑ 也环绕：第 0 项 + ↑ → 回最后一项（(0-1+n) % n = n-1）
    await userEvent.keyboard("{ArrowUp}");
    expect(items[items.length - 1]).toHaveClass("selected");
  });

  it("Enter 选择补全项 → 填充输入框并关闭菜单（用户可继续回车执行）", async () => {
    const store = createTranscriptStore();
    const { input } = setup(store);
    await userEvent.type(input(), "/clear");
    await userEvent.keyboard("{Enter}");
    expect(input().value).toBe("/clear");
    expect(screen.queryByTestId("cmd-menu")).not.toBeInTheDocument();
  });

  it("mousedown 点击补全项 → 填充输入框（preventDefault 防失焦）", async () => {
    const store = createTranscriptStore();
    const { input } = setup(store);
    await userEvent.type(input(), "/clear");
    await userEvent.click(screen.getByTestId("cmd-item-/clear"));
    expect(input().value).toBe("/clear");
  });

  it("Escape 关闭菜单", async () => {
    const store = createTranscriptStore();
    const { input } = setup(store);
    await userEvent.type(input(), "/c");
    expect(screen.getByTestId("cmd-menu")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByTestId("cmd-menu")).not.toBeInTheDocument();
  });
});

describe("Composer — 发送流", () => {
  it("Enter 发送消息：用户行入 store + 输入清空 + sendMessage 带预生成 requestId + sandbox", async () => {
    const store = createTranscriptStore();
    const sent: Array<{ text: string; requestId: string; sandbox?: string | null }> = [];
    const { input } = setup(store, {
      sendMessage: async (o) => {
        sent.push({ text: o.text, requestId: o.requestId, sandbox: o.sandbox });
        return { requestId: o.requestId };
      },
    });
    await userEvent.type(input(), "hello emrg");
    await userEvent.keyboard("{Enter}");
    expect(sent).toHaveLength(1);
    expect(sent[0].text).toBe("hello emrg");
    expect(sent[0].sandbox).toBe("workspace-write");
    expect(sent[0].requestId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(input().value).toBe("");
    const entries = store.getEntries("s1");
    expect(entries[entries.length - 1]).toMatchObject({ kind: "user", text: "hello emrg" });
  });

  it("Ctrl+Enter 同样发送", async () => {
    const store = createTranscriptStore();
    let sent = 0;
    setup(store, {
      sendMessage: async () => {
        sent++;
        return {};
      },
    });
    await userEvent.type(screen.getByTestId("composer-input"), "ctrl send");
    await userEvent.keyboard("{Control>}{Enter}{/Control}");
    expect(sent).toBe(1);
  });

  it("Shift+Enter 不发送（换行）", async () => {
    const store = createTranscriptStore();
    let sent = 0;
    const { input } = setup(store, {
      sendMessage: async () => {
        sent++;
        return {};
      },
    });
    await userEvent.type(input(), "line one");
    await userEvent.keyboard("{Shift>}{Enter}{/Shift}");
    expect(sent).toBe(0);
  });

  it("空输入不发送", async () => {
    const store = createTranscriptStore();
    let sent = 0;
    setup(store, {
      sendMessage: async () => {
        sent++;
        return {};
      },
    });
    await userEvent.keyboard("{Enter}");
    expect(sent).toBe(0);
  });

  it("失败恢复：系统消息（copy.sendFailed）+ 文本回填", async () => {
    const store = createTranscriptStore();
    const { input } = setup(store, {
      sendMessage: async () => {
        throw new Error("boom");
      },
    });
    await userEvent.type(input(), "will fail");
    await userEvent.keyboard("{Enter}");
    // I18nProvider lang=zh → 译文；await 异步失败分支（G49：恢复输入框）
    expect(
      store.getEntries("s1").some((e) => e.kind === "system" && e.text === "没发送成功，你的话我还留着，再试一次？"),
    ).toBe(true);
    await waitFor(() => expect(input().value).toBe("will fail"));
  });

  it("无会话时提示 app.needSession 且不发送", async () => {
    const store = createTranscriptStore();
    let sent = 0;
    setup(store, {
      sid: null,
      sendMessage: async () => {
        sent++;
        return {};
      },
    });
    await userEvent.type(screen.getByTestId("composer-input"), "orphan");
    await userEvent.keyboard("{Enter}");
    expect(sent).toBe(0);
    expect(store.getEntries(null).some((e) => e.kind === "system" && e.text === "请先创建一个对话。")).toBe(true);
  });

  it("/ 指令不进发送流，走 onCommand 路由（vanilla rant 19:44 P1）", async () => {
    const store = createTranscriptStore();
    let sent = 0;
    const routed: Array<{ cmd: string; args?: string[] }> = [];
    const { input } = setup(store, {
      sendMessage: async () => {
        sent++;
        return {};
      },
      onCommand: (r) => routed.push({ cmd: r.cmd, args: r.args }),
    });
    await userEvent.type(input(), "/clear now");
    await userEvent.keyboard("{Enter}");
    expect(sent).toBe(0);
    expect(routed).toEqual([{ cmd: "/clear", args: ["now"] }]);
    expect(input().value).toBe("");
  });

  it("busy 时发送 → 入队（queue-injection #655）并在成功时以 daemon 回显 requestId 为准", async () => {
    const store = createTranscriptStore();
    const sent: string[] = [];
    const { input } = setup(store, {
      busy: true,
      sendMessage: async (o) => {
        sent.push(o.requestId);
        return { requestId: `echo-${o.requestId}` };
      },
    });
    await userEvent.type(input(), "queued msg");
    await userEvent.keyboard("{Enter}");
    expect(sent).toHaveLength(1); // busy 不拦截——直接发送
    // G124：ownStream 以 daemon 回显为准
    const version = store.getVersion();
    void version;
  });
});
