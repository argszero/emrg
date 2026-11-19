import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { StatusBand } from "./StatusBand";
import { I18nProvider } from "../lib/i18n";
import { STATUS_BAND_HEIGHT } from "../lib/cockpit";

/**
 * StatusBand.test.tsx — the one row that is always on screen (issue #2959 item 1).
 *
 * Two of its cells are readings of the daemon (`round` from `turn_progress`, and the
 * rolling 24-hour window) and one is derived from a daemon timestamp (`startedAtMs`).
 * All three are asserted with a number *and* with `null`, because "the daemon has not
 * reported" must render as `—` rather than as round 1 or as zero tokens — an invented
 * reading is the defect class this cockpit exists to remove.
 *
 * The window is three segments and never a total (rant 2026-11-19T09:31:00), and each
 * segment is `null` on its own: an endpoint that never reports cache hits must show `—`
 * there while input and output still read.
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
        usage={null}
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

  it("运行中 + 有轮数 + 有窗口：三个格子都报读数", () => {
    setup({
      running: true,
      round: 3,
      usage: { input: 12_300_000, cache: 11_100_000, output: 245_000, windowStart: 0, windowEnd: 1 },
    });
    expect(screen.getByTestId("band-state")).toHaveTextContent("Running");
    expect(screen.getByTestId("band-round")).toHaveTextContent("round 3");
    expect(screen.getByTestId("band-usage")).toHaveTextContent(
      "24h in 12.3M · cache 11.1M · out 245.0k",
    );
  });

  it("一段没测定就画 —，不画 0：三段各自独立", () => {
    setup({
      running: true,
      usage: { input: 1000, cache: null, output: 5, windowStart: 0, windowEnd: 1 },
    });
    expect(screen.getByTestId("band-usage")).toHaveTextContent("24h in 1.0k · cache — · out 5");
  });

  it("状态带不再说「累计」：窗口是三个独立段，不是一个当轮总数", () => {
    // rant 2026-11-19T09:31:00 的中文口径：宿主看到的必须是最近 24 小时，而不是「本轮累计」。
    render(
      <I18nProvider lang="zh">
        <StatusBand
          running
          round={1}
          usage={{ input: 1000, cache: 900, output: 10, windowStart: 0, windowEnd: 1 }}
          leftOpen
          rightOpen
          onToggleLeft={vi.fn()}
          onToggleRight={vi.fn()}
          onSearch={vi.fn()}
          onNew={vi.fn()}
        />
      </I18nProvider>,
    );
    expect(screen.getByTestId("status-band").textContent).not.toContain("累计");
    expect(screen.getByTestId("band-usage")).toHaveTextContent("24h 输入 1.0k · 缓存 900 · 输出 10");
  });

  it("daemon 还没报 → 画 —，不画第 1 轮、也不画 0 token", () => {
    setup({ running: true, round: null, usage: null });
    expect(screen.getByTestId("band-round")).toHaveTextContent("round —");
    expect(screen.getByTestId("band-usage")).toHaveTextContent("24h in — · cache — · out —");
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

  it("只有四项读数与四个按钮：会话标题、模型、消息条数都不在这行里（issue #2969）", () => {
    setup({
      running: true,
      round: 2,
      usage: { input: 3000, cache: 2000, output: 400, windowStart: 0, windowEnd: 1 },
      startedAtMs: Date.now() - 65_000,
    });
    const band = screen.getByTestId("status-band");
    // 四个变的读数……
    expect(band.querySelectorAll(".band-state, .band-item")).toHaveLength(4);
    // ……四个按钮，一个不多
    expect(band.querySelectorAll("button")).toHaveLength(4);
    // 一个不动的值出现在这一行，就是把状态带变回了顶栏：这三样是宿主点名移出的
    for (const gone of ["session-info", "conn-status", "conn-vision"]) {
      expect(screen.queryByTestId(gone)).not.toBeInTheDocument();
    }
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
