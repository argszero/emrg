import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { StatusBand } from "./StatusBand";
import { I18nProvider } from "../lib/i18n";
import { STATUS_BAND_HEIGHT } from "../lib/cockpit";

/**
 * StatusBand.test.tsx — the one row that is always on screen (issue #2959 item 1).
 *
 * Two of its cells are readings of the daemon (`round` from `turn_progress`, `tokens`
 * from the same frame) and one is derived from a daemon timestamp (`startedAtMs`).
 * All three are asserted with a number *and* with `null`, because "the daemon has not
 * reported" must render as `—` rather than as round 1 or as zero tokens — an invented
 * reading is the defect class this cockpit exists to remove.
 */

function setup(props: Partial<Parameters<typeof StatusBand>[0]> = {}) {
  const handlers = {
    onToggleLeft: vi.fn(),
    onToggleRight: vi.fn(),
    onSearch: vi.fn(),
    onNew: vi.fn(),
  };
  render(
    <I18nProvider lang="en">
      <StatusBand
        running={false}
        round={null}
        tokens={null}
        connected
        model="deepseek-v3"
        leftOpen
        rightOpen
        {...handlers}
        {...props}
      />
    </I18nProvider>,
  );
  return handlers;
}

describe("StatusBand", () => {
  it("44px 高（契约里的固定行高）", () => {
    setup();
    expect(screen.getByTestId("status-band").style.height).toBe(`${STATUS_BAND_HEIGHT}px`);
  });

  it("空闲/运行两态用 daemon 的 busy 说话，而不是本端的请求", () => {
    setup({ running: false });
    expect(screen.getByTestId("band-state")).toHaveTextContent("Idle");
    expect(screen.getByTestId("band-state")).toHaveClass("idle");
  });

  it("运行中 + 有轮数 + 有 token：三个格子都报读数", () => {
    setup({ running: true, round: 3, tokens: 1500 });
    expect(screen.getByTestId("band-state")).toHaveTextContent("Running");
    expect(screen.getByTestId("band-round")).toHaveTextContent("round 3");
    expect(screen.getByTestId("band-spend")).toHaveTextContent("1.5k tokens");
  });

  it("daemon 还没报 → 画 —，不画第 1 轮、也不画 0 token", () => {
    setup({ running: true, round: null, tokens: null });
    expect(screen.getByTestId("band-round")).toHaveTextContent("round —");
    expect(screen.getByTestId("band-spend")).toHaveTextContent("— tokens");
  });

  it("计时来自 daemon 的轮开始时刻：中途打开也显示已过去的时间", () => {
    const startedAt = Date.now() - 305_000; // 5 分 5 秒前
    setup({ running: true, startedAtMs: startedAt });
    expect(screen.getByTestId("band-elapsed").textContent).toMatch(/\[5:0\d\]/);
  });

  it("没有轮在跑 → 计时格子是 —（不留上一轮的时刻）", () => {
    setup({ running: false, startedAtMs: undefined });
    expect(screen.getByTestId("band-elapsed")).toHaveTextContent("—");
  });

  it("会话信息与连接状态作为插槽渲染（会话名/模型仍在状态带里）", () => {
    setup({
      sessionInfo: <span data-testid="slot-session">Alpha (s1)</span>,
      conn: <span data-testid="slot-conn">deepseek-v3</span>,
    });
    expect(screen.getByTestId("slot-session")).toBeInTheDocument();
    expect(screen.getByTestId("slot-conn")).toBeInTheDocument();
  });

  it("四个按钮：搜索 / 新对话 / 两个栏开关，各自回自己的回调", () => {
    const h = setup({ leftOpen: true, rightOpen: false });
    fireEvent.click(screen.getByTestId("band-search"));
    fireEvent.click(screen.getByTestId("band-new"));
    fireEvent.click(screen.getByTestId("band-toggle-left"));
    fireEvent.click(screen.getByTestId("band-toggle-right"));
    expect(h.onSearch).toHaveBeenCalledTimes(1);
    expect(h.onNew).toHaveBeenCalledTimes(1);
    expect(h.onToggleLeft).toHaveBeenCalledTimes(1);
    expect(h.onToggleRight).toHaveBeenCalledTimes(1);
    // 开着的栏按钮带 .on，关着的不带——一眼能看出哪一栏在屏幕上
    expect(screen.getByTestId("band-toggle-left")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("band-toggle-right")).toHaveAttribute("aria-pressed", "false");
  });
});
