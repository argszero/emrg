import { describe, expect, it } from "vitest";
import { act } from "react";
import { render, screen } from "@testing-library/react";
import { TranscriptView } from "./TranscriptView";
import { createTranscriptStore, type TranscriptStore } from "../lib/transcript";
import { I18nProvider } from "../lib/i18n";
import type { MarkdownRenderer } from "../lib/markdown";

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

function setup(store: TranscriptStore, sid?: string | null) {
  return render(
    <I18nProvider lang="zh">
      <TranscriptView store={store} sid={sid} renderer={fakeMd} />
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

  it("其他客户端的广播流带 remote 标签；own stream 不带", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.handleDelta([{ request_id: "r1", content: "broadcast" }], "s1");
    const { container } = setup(store, "s1");
    expect(screen.getByText("（来自其他客户端）")).toBeInTheDocument();
    // own stream → 无标签
    const store2 = createTranscriptStore({ t: (k) => k });
    store2.setOwnStream("r2");
    store2.handleDelta([{ request_id: "r2", content: "own" }], "s1");
    const { container: c2 } = setup(store2, "s1");
    expect(c2.querySelector(".remote-label")).not.toBeInTheDocument();
  });

  it("sid=null 缺省桶：无 sid 事件渲染到默认视图", () => {
    const store = createTranscriptStore({ t: (k) => k });
    store.addUserMessage("legacy", null);
    const { container } = setup(store);
    const div = container.querySelector(".msg.user");
    expect(div).not.toBeNull();
    expect(div!.textContent).toBe("legacy");
  });
});
