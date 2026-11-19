import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { StatusBand } from "./StatusBand";
import { I18nProvider } from "../lib/i18n";
import { STATUS_BAND_HEIGHT } from "../lib/cockpit";
import bandSource from "./StatusBand.tsx?raw";
import dictSource from "../lib/i18n-dicts.ts?raw";

/**
 * StatusBand.test.tsx — the one row that is always on screen (issue #2959 item 1).
 *
 * One of its cells is a reading of the daemon (the rolling 24-hour window) and one is
 * derived from a daemon timestamp (`startedAtMs`). Both are asserted with a number *and*
 * with `null`, because "the daemon has not reported" must render as `—` rather than as
 * zero tokens — an invented reading is the defect class this cockpit exists to remove.
 *
 * The turn counter's cell is gone (issue #3039), together with its per-session state, its
 * writer and its formatter: the row already says a turn is running and for how long, and
 * the tool-group summary underneath names what is happening.
 *
 * The window is three segments and never a total (rant 2026-11-19T09:31:00), and each
 * segment is `null` on its own: an endpoint that never reports cache hits must show `—`
 * there while input and output still read.
 */

function setup(props: Partial<Parameters<typeof StatusBand>[0]> = {}) {
  const handlers = {
    onToggleLeft: vi.fn(),
    onToggleRight: vi.fn(),
  };
  render(
    <I18nProvider lang="en">
      <StatusBand
        running={false}
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

  it("运行中 + 有窗口：状态与花费各报读数", () => {
    setup({
      running: true,
      usage: { input: 12_300_000, cache: 11_100_000, output: 245_000, windowStart: 0, windowEnd: 1 },
    });
    expect(screen.getByTestId("band-state")).toHaveTextContent("Running");
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
          usage={{ input: 1000, cache: 900, output: 10, windowStart: 0, windowEnd: 1 }}
          leftOpen
          rightOpen
          onToggleLeft={vi.fn()}
          onToggleRight={vi.fn()}
        />
      </I18nProvider>,
    );
    expect(screen.getByTestId("status-band").textContent).not.toContain("累计");
    expect(screen.getByTestId("band-usage")).toHaveTextContent("24h 输入 1.0k · 缓存 900 · 输出 10");
  });

  it("daemon 还没报 → 画 —，不画 0 token", () => {
    setup({ running: true, usage: null });
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

  it("只有三项读数与两个栏开关：会话标题、模型、消息条数、轮次都不在这行里（issue #2969 / #3039）", () => {
    setup({
      running: true,
      usage: { input: 3000, cache: 2000, output: 400, windowStart: 0, windowEnd: 1 },
      startedAtMs: Date.now() - 65_000,
    });
    const band = screen.getByTestId("status-band");
    // 三个变的读数：状态、计时、24 小时花费（轮次格子已删，issue #3039）
    expect(band.querySelectorAll(".band-state, .band-item")).toHaveLength(3);
    // ……两个按钮，一个不多：搜索与新建会话的入口在会话栏（issue #3028）
    expect(band.querySelectorAll("button")).toHaveLength(2);
    // 一个不动的值出现在这一行，就是把状态带变回了顶栏：这三样是宿主点名移出的
    for (const gone of ["session-info", "conn-status", "conn-vision"]) {
      expect(screen.queryByTestId(gone)).not.toBeInTheDocument();
    }
  });

  it("两个按钮：两个栏开关，各自回自己的回调；搜索与新建不在状态带上（issue #3028）", () => {
    const h = setup({ leftOpen: true, rightOpen: false });
    fireEvent.click(screen.getByTestId("band-toggle-left"));
    fireEvent.click(screen.getByTestId("band-toggle-right"));
    // 一个动作只留一个入口：这两个在会话栏表头，状态带上删掉而不是藏起来
    expect(screen.queryByTestId("band-search")).toBeNull();
    expect(screen.queryByTestId("band-new")).toBeNull();
    expect(h.onToggleLeft).toHaveBeenCalledTimes(1);
    expect(h.onToggleRight).toHaveBeenCalledTimes(1);
    // 开着的栏按钮带 .on，关着的不带——一眼能看出哪一栏在屏幕上
    expect(screen.getByTestId("band-toggle-left")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("band-toggle-right")).toHaveAttribute("aria-pressed", "false");
  });
});

describe("状态带不再有轮次读数（issue #3039）", () => {
  it("渲染结果里没有轮次：既没有那个测试 id，也没有 round N / 第 N 轮 的字样", () => {
    // 一个数字格子走了，不能只靠「类名还在不在」判——要读屏幕上真有的文字。
    setup({ running: true, usage: { input: 1, cache: 1, output: 1, windowStart: 0, windowEnd: 1 } });
    const band = screen.getByTestId("status-band");
    expect(screen.queryByTestId("band-round")).toBeNull();
    expect(band.textContent ?? "").not.toMatch(/round\s*\d/i);
    expect(band.textContent ?? "").not.toMatch(/第\s*\d+\s*轮/);
  });

  it("它的两层家都拆了：组件里没有它的 id，两个字典里都没有那两个键", () => {
    // 格子、状态、写者、格式器是一条链；只删格子会留下没人读的值与没人用的键，
    // 而下一次有人「顺手接上」时，第二个答案就回来了。
    expect(bandSource).not.toContain("band-round");
    expect(bandSource).not.toContain("band.round");
    expect(dictSource).not.toContain('"band.round"');
    expect(dictSource).not.toContain('"band.roundTitle"');
  });
});
