import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { UsageRanking } from "./UsageRanking";
import { I18nProvider } from "../lib/i18n";
import type { UsageWindowReading } from "../lib/daemonBridge";

/**
 * UsageRanking.test.tsx — one segment at a time (rant 2026-11-20T17:30:50).
 *
 * The panel exists because one table can be sorted by one scale: the session that spent
 * the most input is not the one that spent the most output. So what is asserted here is
 * that **everything the host reads follows the clicked segment** — the title, the fourth
 * column's name, the note under the table, the footer's reconciliation — and that the
 * order really is that segment's, not a cached one from the last time the panel opened.
 *
 * The unnamed volume is asserted as an absence from the table: it names no conversation,
 * so it has nothing to open and nothing to rank. Item 5 is that refusal.
 *
 * The arithmetic itself is `lib/usageRanking.test.ts`; this file drives the DOM it lands
 * in, so a bar measured against the wrong denominator still shows up as a wrong width.
 */

const reading: UsageWindowReading = {
  input: 1000,
  cache: 500,
  output: 100,
  sessionInput: 400,
  sessionCache: 200,
  sessionOutput: 40,
  groups: [
    { session: "s_a", calls: 2, input: 600, cache: 300, output: 10 },
    { session: "s_b", calls: 8, input: 300, cache: 150, output: 80 },
    { session: null, calls: 1, input: 100, cache: 50, output: 10 },
  ],
  windowStart: 0,
  windowEnd: 1,
};

function show(
  props: Partial<Parameters<typeof UsageRanking>[0]> = {},
): { onOpenSession: ReturnType<typeof vi.fn>; onClose: ReturnType<typeof vi.fn> } {
  const handlers = { onOpenSession: vi.fn(), onClose: vi.fn() };
  render(
    <I18nProvider lang="en">
      <UsageRanking
        open
        segment="input"
        reading={reading}
        onClose={handlers.onClose}
        onOpenSession={handlers.onOpenSession}
        {...props}
      />
    </I18nProvider>,
  );
  return handlers;
}

function rowSids(): string[] {
  return screen.getAllByTestId("usage-row").map((row) => row.getAttribute("data-sid") ?? "");
}

describe("UsageRanking — 一段一张表", () => {
  it("标题、第四列的名字、表下的说明都跟着当前这一段变", () => {
    const input = render(
      <I18nProvider lang="en">
        <UsageRanking open segment="input" reading={reading} onClose={vi.fn()} onOpenSession={vi.fn()} />
      </I18nProvider>,
    );
    expect(screen.getByTestId("usage-ranking")).toHaveTextContent(
      "Last 24 hours · ranked by in",
    );
    expect(screen.getByTestId("usage-fourth-name")).toHaveTextContent("Cache hit rate");
    // 输入段没有额外的说明：那两段各自的注解只属于它们自己
    expect(screen.queryByTestId("usage-note-cache")).toBeNull();
    expect(screen.queryByTestId("usage-note-output")).toBeNull();
    input.unmount();

    render(
      <I18nProvider lang="en">
        <UsageRanking open segment="output" reading={reading} onClose={vi.fn()} onOpenSession={vi.fn()} />
      </I18nProvider>,
    );
    expect(screen.getByTestId("usage-ranking")).toHaveTextContent(
      "Last 24 hours · ranked by out",
    );
    expect(screen.getByTestId("usage-fourth-name")).toHaveTextContent("Output per call");
    expect(screen.getByTestId("usage-note-output")).toBeInTheDocument();
    expect(screen.queryByTestId("usage-note-cache")).toBeNull();
  });

  it("排的顺序是当前这一段的：按输入是两个会话一个顺序，按输出是另一个", () => {
    const input = render(
      <I18nProvider lang="en">
        <UsageRanking open segment="input" reading={reading} onClose={vi.fn()} onOpenSession={vi.fn()} />
      </I18nProvider>,
    );
    expect(rowSids()).toEqual(["s_a", "s_b"]);
    input.unmount();

    render(
      <I18nProvider lang="en">
        <UsageRanking open segment="output" reading={reading} onClose={vi.fn()} onOpenSession={vi.fn()} />
      </I18nProvider>,
    );
    // 输出花得多的那个会话，输入花得少：同一个窗口，两张表不可能同序
    expect(rowSids()).toEqual(["s_b", "s_a"]);
  });

  it("占比条的分母是这一段的全部量：第一行 60% 的宽度，不是满格", () => {
    show({ segment: "input" });
    const bars = screen.getAllByTestId("usage-bar").map((bar) => bar.querySelector("i"));
    expect(bars[0]?.getAttribute("style")).toContain("width: 60%");
    expect(bars[1]?.getAttribute("style")).toContain("width: 30%");
    // 拿最大行当分母时会画成 100% 和 50%——这一行的读法是「它占了今天多少」
    expect(screen.getAllByTestId("usage-share").map((el) => el.textContent)).toEqual([
      "60%",
      "30%",
    ]);
  });

  it("五列各读各的：这一段 token、第四列、调用次数", () => {
    show({ segment: "output" });
    const first = screen.getAllByTestId("usage-row")[0];
    expect(first.querySelector("[data-testid='usage-tokens']")?.textContent).toBe("80");
    expect(first.querySelector("[data-testid='usage-fourth']")?.textContent).toBe("10"); // 80 / 8 次
    expect(first.querySelector("[data-testid='usage-calls']")?.textContent).toBe("8");
  });

  it("没有会话归属的那一坨不是一行：不进表，只在表下单独说一条", () => {
    show({ segment: "input" });
    expect(rowSids()).toEqual(["s_a", "s_b"]);
    const unnamed = screen.getByTestId("usage-unnamed");
    expect(unnamed).toHaveTextContent("Unnamed 100 (1 calls)");
    // 为什么它排不进来，也写在旁边：没有名字就没有会话可开
    expect(unnamed).toHaveTextContent("only started recording session names partway back");
    // 一条，不是三条：面板一次只显一段，所以每一段有且只有这一条琥珀提示
    expect(screen.getAllByTestId("usage-unnamed")).toHaveLength(1);
    // 它点不开：它自己不是控件，里面也没有一个控件或可聚焦的东西。两个方向都要问——
    // 只问后代时，把 role="button" 挂在这条提示自己身上就从断言底下走过去了（实测）。
    const control = "button, a, [role='button'], [tabindex]";
    expect(unnamed.matches(control)).toBe(false);
    expect(unnamed.querySelectorAll(control)).toHaveLength(0);
  });

  it("页脚的等式：列出的 + 没有名字的 = 这一段总量", () => {
    show({ segment: "input" });
    expect(screen.getByTestId("usage-footer")).toHaveTextContent(
      "Listed sessions 900 + unnamed 100 = this segment's total 1.0k",
    );
  });

  it("点一行开这个会话，并把面板让开", () => {
    const h = show({ segment: "input" });
    fireEvent.click(screen.getAllByTestId("usage-row")[1]);
    expect(h.onOpenSession).toHaveBeenCalledWith("s_b");
  });

  it("知道会话名字就印名字，不知道就印号——不编一个名字", () => {
    show({ segment: "input", nameOf: (sid: string) => (sid === "s_a" ? "Alpha" : undefined) });
    expect(screen.getAllByTestId("usage-row-name").map((el) => el.textContent)).toEqual([
      "Alpha",
      "s_b",
    ]);
  });

  it("daemon 还没报 → 空表文案，一行都不编", () => {
    show({ reading: null });
    expect(screen.getByTestId("usage-empty")).toBeInTheDocument();
    expect(screen.queryAllByTestId("usage-row")).toHaveLength(0);
    // 没测到就是没测到：页脚的三个数都是 —，不是 0
    expect(screen.getByTestId("usage-footer")).toHaveTextContent(
      "Listed sessions — + unnamed — = this segment's total —",
    );
  });

  it("关闭按钮交给 onClose", () => {
    const h = show();
    fireEvent.click(screen.getByTestId("usage-close"));
    expect(h.onClose).toHaveBeenCalledTimes(1);
  });
});
