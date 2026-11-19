import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SessionColumn } from "./SessionColumn";
import { I18nProvider } from "../lib/i18n";

/**
 * SessionColumn.test.tsx — the open-session list's order (issue #3022).
 *
 * The order is decided in the main process; this column draws the array it is given. So
 * the tests here cover the two things this layer can get wrong on its own: re-sorting what
 * it was handed, and reporting a drag as something other than "one row moved to one place".
 *
 * They read the **rendered row order** (`data-sid` on each row), never the input array:
 * an assertion on the array would pass just as well with a sort in the middle, because the
 * array is what the sort is handed, not what the user sees.
 */

const entry = (sid: string, title: string, lastActive: string, openedAt = "2026-11-19T09:00:00Z") => ({
  sid,
  title,
  projectName: "emrg",
  openedAt,
  lastActive,
});

function setup(props: Partial<Parameters<typeof SessionColumn>[0]> = {}) {
  const onSelect = vi.fn();
  const onReorder = vi.fn();
  const view = render(
    <I18nProvider lang="en">
      <SessionColumn openSessions={[]} onSelect={onSelect} onReorder={onReorder} {...props} />
    </I18nProvider>,
  );
  return { onSelect, onReorder, view };
}

const rowSids = () => screen.getAllByTestId("open-session-item").map((el) => el.getAttribute("data-sid"));
const rowWraps = () => screen.getAllByTestId("open-session-item").map((el) => el.parentElement as HTMLElement);

describe("SessionColumn 的顺序（issue #3022）", () => {
  it("按入参数组的顺序画，不看 lastActive：最近活动的那条不许自己挪上来", () => {
    // C 最后打开所以在最上面，B 最久没有活动；A 的活动时间最新，但位置仍是第三条。
    // 只要这里加回一次按 lastActive 的排序，读到的行序就会变成 a、c、b。
    setup({
      openSessions: [
        entry("c", "C", "2026-11-19T09:05:00Z"),
        entry("b", "B", "2026-11-19T09:01:00Z"),
        entry("a", "A", "2026-11-19T09:09:00Z"),
      ],
    });
    expect(rowSids()).toEqual(["c", "b", "a"]);
  });

  it("同一条会话来一条消息（只有 lastActive 变新）也不换位置", () => {
    const list = [
      entry("c", "C", "2026-11-19T09:05:00Z"),
      entry("a", "A", "2026-11-19T09:01:00Z"),
      entry("b", "B", "2026-11-19T09:02:00Z"),
    ];
    const { view } = setup({ openSessions: list });
    expect(rowSids()).toEqual(["c", "a", "b"]);
    view.rerender(
      <I18nProvider lang="en">
        <SessionColumn
          openSessions={[
            entry("c", "C", "2026-11-19T09:05:00Z"),
            entry("a", "A", "2026-11-19T09:59:00Z"),
            entry("b", "B", "2026-11-19T09:02:00Z"),
          ]}
          onSelect={vi.fn()}
          onReorder={vi.fn()}
        />
      </I18nProvider>,
    );
    expect(rowSids()).toEqual(["c", "a", "b"]);
  });

  it("把最后一行拖到第一行：交出去的顺序是 a、c、b", () => {
    const { onReorder } = setup({
      openSessions: [entry("c", "C", "t3"), entry("b", "B", "t2"), entry("a", "A", "t1")],
    });
    const wraps = rowWraps();
    fireEvent.dragStart(wraps[2]);
    fireEvent.dragOver(wraps[0]);
    fireEvent.drop(wraps[0]);
    expect(onReorder).toHaveBeenCalledTimes(1);
    expect(onReorder).toHaveBeenCalledWith(["a", "c", "b"]);
  });

  it("落在自己身上不回报任何顺序（一次没有移动的拖动）", () => {
    const { onReorder } = setup({
      openSessions: [entry("c", "C", "t3"), entry("b", "B", "t2")],
    });
    const wraps = rowWraps();
    fireEvent.dragStart(wraps[1]);
    fireEvent.dragOver(wraps[1]);
    fireEvent.drop(wraps[1]);
    expect(onReorder).not.toHaveBeenCalled();
  });

  it("不传 onReorder 就不开放拖拽（这一层不自己猜主进程收不收）", () => {
    setup({ openSessions: [entry("a", "A", "t1")], onReorder: undefined });
    expect(rowWraps()[0]).toHaveAttribute("draggable", "false");
  });

  it("运行点与计时仍只表示「这条在跑」，与顺序无关", () => {
    const now = Date.now();
    setup({
      openSessions: [entry("a", "A", "t1"), entry("b", "B", "t2")],
      turnStartBySid: { b: now - 65_000 },
    });
    expect(screen.getAllByTestId("open-session-run-dot")).toHaveLength(1);
    expect(screen.getByTestId("open-session-timer")).toHaveTextContent("[1:05]");
    expect(rowSids()).toEqual(["a", "b"]);
  });
});
