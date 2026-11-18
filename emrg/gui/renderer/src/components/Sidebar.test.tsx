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

  it("条目格式：有标题 → agent/title；无标题 → agent/sid", () => {
    setup();
    const items = screen.getAllByTestId("open-session-item");
    expect(items[0].querySelector(".conv-title")).toHaveTextContent("emrg/聊天 A");
    // s2 无 entry.title → resolveEntryTitle 回退本地 known 标题
    expect(items[1].querySelector(".conv-title")).toHaveTextContent("aitokenpool/本地会话 B");
  });

  it("无标题且本地未知 → agent/sid 降级格式", () => {
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

  // ── The rail and the two chat buttons moved out of this component (issue #2959) ──
  //
  // What used to be asserted here — a nav rail inside the sidebar, and the new-chat /
  // open-chat buttons at its foot — is now `IconRail.test.tsx` and the status band's
  // own test. What this file keeps asserting is what this component still does: the
  // list of open sessions, and the stopwatch on a running one.

  it("运行中的会话带呼吸点与秒表，读的是 daemon 的轮开始时刻", () => {
    const t0 = Date.now() - 65_000;
    setup({ turnStartBySid: { s1: t0 } });
    const items = screen.getAllByTestId("open-session-item");
    expect(items[0].querySelector("[data-testid=open-session-run-dot]")).not.toBeNull();
    expect(items[0].querySelector("[data-testid=open-session-timer]")).toHaveTextContent("1:05");
    // 没有轮在跑的会话不显示计时（不是 0:00）
    expect(items[1].querySelector("[data-testid=open-session-timer]")).toBeNull();
  });

  it("列表只放打开的会话：daemon 列出但未打开的历史不进这一栏", () => {
    setup({
      knownSessions: [
        { session_id: "s1", title: "聊天 A" },
        { session_id: "s9", title: "很久以前" },
      ],
    });
    const items = screen.getAllByTestId("open-session-item");
    expect(items).toHaveLength(2);
    expect(items.map((el) => el.dataset.sid)).toEqual(["s1", "s2"]);
  });
});
