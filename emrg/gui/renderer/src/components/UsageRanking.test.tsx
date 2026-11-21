import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { UsageRanking, placePanel } from "./UsageRanking";
import { I18nProvider } from "../lib/i18n";
import { STATUS_BAND_HEIGHT } from "../lib/cockpit";
import type { UsageWindowReading } from "../lib/daemonBridge";

/**
 * UsageRanking.test.tsx — one segment at a time, hanging under the reading that opened it.
 *
 * Two subjects, and the second is the one this file exists for twice.
 *
 * **The segment.** One table can be sorted by one scale: the session that spent the most
 * input is not the one that spent the most output. So everything the host reads has to
 * follow the clicked segment — title, fourth column, note, footer — and the order has to
 * be that segment's, not a cached one from the last time the panel opened. The unnamed
 * volume is asserted as an absence from the table: it names no conversation, so it has
 * nothing to open and nothing to rank.
 *
 * **The shape (issue #3123).** It used to be a modal: a dimmed, blurred backdrop behind a
 * centred card with a 关闭 button, and an unbounded row list that grew the card past the
 * window edge. So what is asserted below is what replaced each part of that — the panel is
 * placed from the clicked reading rather than from the window centre, the rows live inside
 * a bounded scroller, the unnamed block and the reconciliation line sit **outside** that
 * scroller, the footer is the last thing in the panel rather than a button, and the two
 * dismissals are Escape and a click outside. A regression to any of these puts the
 * transcript back under a dimmed backdrop without failing a single arithmetic test.
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

/** A real element to anchor to, with a laid-out rectangle jsdom would otherwise report as 0. */
function makeAnchor(rect: { left: number; bottom: number }): HTMLElement {
  const anchor = document.createElement("button");
  anchor.type = "button";
  anchor.textContent = "in";
  document.body.appendChild(anchor);
  vi.spyOn(anchor, "getBoundingClientRect").mockReturnValue({
    ...rect,
    right: rect.left + 40,
    top: rect.bottom - 20,
    width: 40,
    height: 20,
    x: rect.left,
    y: rect.bottom - 20,
    toJSON: () => ({}),
  } as DOMRect);
  return anchor;
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
});

describe("UsageRanking — 挂在读数下面的浮层，不是模态框（issue #3123）", () => {
  it("位置由被点的那个读数算出来，不是从窗口中间算", () => {
    const anchor = makeAnchor({ left: 300, bottom: 44 });
    show({ anchor, segment: "input" });
    const panel = screen.getByTestId("usage-ranking");
    // 顶部正好在读数下沿之下 8px，左沿与它同列
    expect(panel.style.top).toBe("52px");
    expect(panel.style.left).toBe("300px");
  });

  it("左右两边都夹住：贴左窗沿时不越界，靠右太远时左沿收到窗口内", () => {
    // 夹紧的规则本身，两个方向都量——只量一侧时把 Math.min 写成 Math.max 也通得过
    expect(placePanel(null, 1024).left).toBe(10);
    expect(placePanel({ getBoundingClientRect: () => ({ left: 0, bottom: 44 }) } as unknown as HTMLElement, 1024).left).toBe(10);
    expect(placePanel({ getBoundingClientRect: () => ({ left: 980, bottom: 44 }) } as unknown as HTMLElement, 1024).left).toBe(330);
    // 没有锚点时落在带子下面的内缩角上，不编一个按钮位置
    expect(placePanel(null, 1024).top).toBe(STATUS_BAND_HEIGHT + 8);
  });

  it("窗口一改大小就重新量：量一次的坐标在拖动窗口之后是错的", () => {
    const anchor = makeAnchor({ left: 300, bottom: 44 });
    show({ anchor, segment: "input" });
    expect(screen.getByTestId("usage-ranking").style.left).toBe("300px");
    // 锚点在窗口变窄后仍在原处，左沿被窗口右边夹住
    const original = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 800 });
    try {
      fireEvent(window, new Event("resize"));
      expect(screen.getByTestId("usage-ranking").style.left).toBe("106px");
    } finally {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: original });
    }
  });

  it("行在一个有界的滚动盒里，页脚和没有名字的那条在盒子外面", () => {
    show({ segment: "input" });
    const panel = screen.getByTestId("usage-ranking");
    const list = screen.getByTestId("usage-list");
    // 滚动只发生在这一层：表格整个在它里面
    expect(list).toContainElement(screen.getAllByTestId("usage-row")[0]);
    expect(list.querySelector("table")).not.toBeNull();
    // 盒子外面的三条都不在它里面，否则滚到哪就把对账行带出视野
    expect(list).not.toContainElement(screen.getByTestId("usage-footer"));
    expect(list).not.toContainElement(screen.getByTestId("usage-unnamed"));
    expect(list).not.toContainElement(screen.getByTestId("usage-title"));
    // 最后一件是那句等式，不是一个按钮
    expect(panel.lastElementChild).toBe(screen.getByTestId("usage-footer"));
  });

  it("没有关闭按钮，也没有任何遮罩：既不 dim 也不 blur，也不拦窗口", () => {
    show({ segment: "input" });
    expect(screen.queryByTestId("usage-close")).toBeNull();
    expect(document.querySelector(".usage-backdrop, .dialog-backdrop")).toBeNull();
    // 不 modal：它不是 aria-modal，窗口里的东西照常可点可读
    expect(screen.getByTestId("usage-ranking")).not.toHaveAttribute("aria-modal");
  });

  it("Esc 关掉，点面板外面关掉，点面板里面不关", () => {
    const h = show({ segment: "input" });
    fireEvent.click(screen.getByTestId("usage-list"));
    expect(h.onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(h.onClose).toHaveBeenCalledTimes(1);
  });

  it("点外面关闭；但点状态带上的读数不关——那是那个按钮自己的决定", () => {
    const out = show({ segment: "input" });
    fireEvent.click(document.body);
    expect(out.onClose).toHaveBeenCalledTimes(1);

    // 重开一次，点同一个读数：面板必须让路，否则「再点一次就是关掉」变成重新打开
    const again = show({ segment: "input" });
    const bandReading = document.createElement("button");
    bandReading.setAttribute("data-band-usage", "input");
    document.body.appendChild(bandReading);
    fireEvent.click(bandReading);
    expect(again.onClose).not.toHaveBeenCalled();
  });

  it("收起时什么都不画", () => {
    show({ open: false });
    expect(screen.queryByTestId("usage-ranking")).toBeNull();
  });
});
