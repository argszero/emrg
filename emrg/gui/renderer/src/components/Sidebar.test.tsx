import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Sidebar } from "./Sidebar";
import { I18nProvider } from "../lib/i18n";
import type { OpenSessionEntry, SessionInfo } from "../lib/sidebar";

/**
 * Sidebar.test.tsx — Sidebar React 组件测试（Batch 3）。
 * 镜像 vanilla sidebar.js renderOpenSessions 行为：label 显隐、条目格式、
 * 点击切换、右键菜单、激活高亮。类名与 vanilla CSS 一致（Batch 5 复用）。
 */

const sessions: OpenSessionEntry[] = [
  { sid: "s1", title: "聊天 A", projectName: "emrg", lastActive: "2026-08-26T09:00:00Z" },
  { sid: "s2", projectName: "aitokenpool", lastActive: "2026-08-26T08:00:00Z" },
];

const known: SessionInfo[] = [
  { session_id: "s1", title: "聊天 A" },
  { session_id: "s2", title: "本地会话 B" },
];

function setup(props: Partial<Parameters<typeof Sidebar>[0]> = {}) {
  const utils = render(
    <I18nProvider lang="zh">
      <Sidebar openSessions={sessions} knownSessions={known} {...props} />
    </I18nProvider>,
  );
  return { ...utils };
}

describe("Sidebar", () => {
  it("有打开会话 → 显示分组 label（sidebar.openSessions）与条目", () => {
    setup();
    expect(screen.getByTestId("open-sessions-label")).toHaveTextContent("打开的会话");
    expect(screen.getByTestId("open-sessions-label")).not.toHaveAttribute("hidden");
    expect(screen.getAllByTestId("open-session-item")).toHaveLength(2);
  });

  it("无打开会话 → label hidden + 空 nav（vanilla 行为）", () => {
    setup({ openSessions: [] });
    expect(screen.getByTestId("open-sessions-label")).toHaveAttribute("hidden");
    expect(screen.queryAllByTestId("open-session-item")).toHaveLength(0);
  });

  it("条目格式：有标题 → project/title；无标题 → project/sid", () => {
    setup();
    const items = screen.getAllByTestId("open-session-item");
    expect(items[0].querySelector(".conv-title")).toHaveTextContent("emrg/聊天 A");
    // s2 无 entry.title → resolveEntryTitle 回退本地 known 标题
    expect(items[1].querySelector(".conv-title")).toHaveTextContent("aitokenpool/本地会话 B");
  });

  it("无标题且本地未知 → project/sid 降级格式", () => {
    setup({ knownSessions: [] });
    const items = screen.getAllByTestId("open-session-item");
    expect(items[0].querySelector(".conv-title")).toHaveTextContent("emrg/聊天 A");
    expect(items[1].querySelector(".conv-title")).toHaveTextContent("aitokenpool/s2");
  });

  it("点击条目 → onSelect(sid)", async () => {
    const onSelect = vi.fn();
    setup({ onSelect });
    await userEvent.click(screen.getAllByTestId("open-session-item")[0]);
    expect(onSelect).toHaveBeenCalledWith("s1");
  });

  it("右键条目 → onContextMenu(entry, event) + preventDefault", async () => {
    const onContextMenu = vi.fn();
    setup({ onContextMenu });
    const item = screen.getAllByTestId("open-session-item")[0];
    await userEvent.pointer({ keys: "[MouseRight]", target: item });
    expect(onContextMenu).toHaveBeenCalledTimes(1);
    expect(onContextMenu.mock.calls[0][0]).toMatchObject({ sid: "s1" });
    expect(onContextMenu.mock.calls[0][1].defaultPrevented).toBe(true);
  });

  it("activeSid 匹配条目带 .active 高亮", () => {
    setup({ activeSid: "s1" });
    const items = screen.getAllByTestId("open-session-item");
    expect(items[0]).toHaveClass("active");
    expect(items[1]).not.toHaveClass("active");
  });

  it("条目 data-sid 属性（vanilla dataset.sid 对应）", () => {
    setup();
    expect(screen.getAllByTestId("open-session-item")[1]).toHaveAttribute("data-sid", "s2");
  });

  it("排序：lastActive 倒序（新在前）", () => {
    setup();
    const items = screen.getAllByTestId("open-session-item");
    expect(items[0]).toHaveAttribute("data-sid", "s1"); // 09:00 > 08:00
    expect(items[1]).toHaveAttribute("data-sid", "s2");
  });

  it("注入 labelFn 可覆盖默认格式（测试隔离依赖）", () => {
    setup({ labelFn: (_p, _t, sid) => `L:${sid}` });
    const items = screen.getAllByTestId("open-session-item");
    expect(items[0].querySelector(".conv-title")).toHaveTextContent("L:s1");
  });

  // ── Batch 5 slice 4：新对话/打开会话按钮 ──

  it("渲染新对话/打开会话按钮（vanilla new-chat-btn / open-chat-btn）", () => {
    setup();
    expect(screen.getByTestId("new-chat-btn")).toHaveTextContent("＋ 新对话");
    expect(screen.getByTestId("open-chat-btn")).toHaveTextContent("打开会话");
  });

  it("无打开会话时按钮仍渲染（空态入口）", () => {
    setup({ openSessions: [] });
    expect(screen.getByTestId("new-chat-btn")).toBeInTheDocument();
    expect(screen.getByTestId("open-chat-btn")).toBeInTheDocument();
    expect(screen.queryByTestId("open-session-item")).not.toBeInTheDocument();
  });

  it("点击新对话 → onNewChat；点击打开会话 → onOpenChat", async () => {
    const onNewChat = vi.fn();
    const onOpenChat = vi.fn();
    setup({ onNewChat, onOpenChat });
    await userEvent.click(screen.getByTestId("new-chat-btn"));
    expect(onNewChat).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByTestId("open-chat-btn"));
    expect(onOpenChat).toHaveBeenCalledTimes(1);
  });
});
