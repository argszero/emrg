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

  it("只给运行中的会话画 [m:ss]（rant 2026-09-30T09:47:11 requirement 5）", () => {
    // 侧栏计时是 `turnStartBySid` 的纯展示：有基准才画，没有就不画。
    // 基准只来自 daemon（`turn_start` 的 `started_at`，或打开会话时 `meta.turn` 的快照）
    // —— 这一支钉住「有基准 → 画」与「无基准 → 不画」两侧，格式与 TUI 同源。
    const { unmount } = setup({ turnStartBySid: { s1: Date.now() - 305_000 } });
    const items = screen.getAllByTestId("open-session-item");
    // 运行中的 s1：5 分 5 秒前开始 → [5:05]（分钟不补零，与 TUI divmod 一致；
    // 秒位在断言与渲染之间会走一两秒，故只钉分钟）
    const timer = items[0].querySelector(".open-session-timer");
    expect(timer).not.toBeNull();
    expect(timer!.textContent).toMatch(/^\[5:\d\d\]$/);
    // 没有基准的 s2：一行都不画（不得从 0 起算，也不得留空壳）
    expect(items[1].querySelector(".open-session-timer")).toBeNull();
    unmount();

    // 基准清空（轮结束）→ 计时消失，不残留
    setup({ turnStartBySid: {} });
    expect(screen.queryAllByTestId("open-session-timer")).toHaveLength(0);
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

  // ── Batch 5 slice 5：侧边导航 rail（vanilla #side-nav） ──

  it("渲染五个导航按钮（data-view + i18n title，vanilla side-nav-item）", () => {
    setup();
    for (const view of ["sessions", "projects", "tasks", "rants", "settings"]) {
      const btn = screen.getByTestId(`nav-${view}`);
      expect(btn).toHaveClass("side-nav-item");
      expect(btn).toHaveAttribute("data-view", view);
    }
    expect(screen.getAllByTestId(/^nav-/).length).toBe(5);
  });

  it("activeView 匹配按钮带 .active 高亮", () => {
    setup({ activeView: "tasks" });
    expect(screen.getByTestId("nav-tasks")).toHaveClass("active");
    expect(screen.getByTestId("nav-sessions")).not.toHaveClass("active");
    expect(screen.getByTestId("nav-projects")).not.toHaveClass("active");
  });

  it("无打开会话时 nav rail 仍渲染（空态入口）", () => {
    setup({ openSessions: [] });
    expect(screen.getByTestId("side-nav")).toBeInTheDocument();
    expect(screen.getByTestId("nav-projects")).toBeInTheDocument();
    expect(screen.queryByTestId("open-session-item")).not.toBeInTheDocument();
  });

  it("点击 nav 按钮 → onSwitchView(view)", async () => {
    const onSwitchView = vi.fn();
    setup({ onSwitchView });
    await userEvent.click(screen.getByTestId("nav-projects"));
    expect(onSwitchView).toHaveBeenCalledWith("projects");
    await userEvent.click(screen.getByTestId("nav-settings"));
    expect(onSwitchView).toHaveBeenCalledWith("settings");
  });
});

describe("Sidebar 成长卡（#growth-card）", () => {
  it("顶上就有成长卡，写着计数与那句说明（vanilla #growth-card）", () => {
    // vanilla `index.html:24-27` 的 `#growth-card`：`🌱 已自我进化 <b>N</b> 次` + 一行
    // `growthNote`，WorkBuddy P3 的「自进化可见化」。#1024 删 vanilla 渲染层时，
    // `layout.css:137-155` 的 CSS 与词典四条串都留下了，**元素与读者一起没了**
    // （实测 2026-10-02：`growth-card` 在 `src/` 下零命中）—— 于是 React 版里这个
    // 数只出现在**设置 → 关于**，主界面上一句都看不到。
    setup({ evolutionCount: 53 });
    const card = screen.getByTestId("growth-card");
    expect(card).toHaveTextContent("已自我进化");
    expect(card).toHaveTextContent("53");
    expect(screen.getByTestId("growth-count")).toHaveTextContent("53");
    expect(screen.getByTestId("growth-note")).toHaveTextContent("边工作边学习，越用越懂你");
    // 标题（悬停）来自词典，不是硬编码 —— vanilla 的 data-i18n-title 同源
    expect(card).toHaveAttribute("title", "EMRG 每次自我改进都会自动汇报");
  });

  it("计数没报过（null/缺省）→ 落成 0，而不是空白（vanilla 的 `?? 0`）", () => {
    setup({});
    expect(screen.getByTestId("growth-count")).toHaveTextContent("0");
    setup({ evolutionCount: null });
    expect(screen.getAllByTestId("growth-count").at(-1)).toHaveTextContent("0");
  });

  it("空态（没有打开会话）同样有成长卡 —— 它是这一栏里与工作内容无关的那件东西", () => {
    setup({ openSessions: [], evolutionCount: 7 });
    expect(screen.getByTestId("growth-card")).toBeInTheDocument();
    expect(screen.getByTestId("growth-count")).toHaveTextContent("7");
  });
});
