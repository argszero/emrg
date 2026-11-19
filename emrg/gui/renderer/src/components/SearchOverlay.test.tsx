import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SearchOverlay } from "./SearchOverlay";
import { I18nProvider } from "../lib/i18n";
import type { SessionHit } from "../lib/cockpit";

/**
 * SearchOverlay.test.tsx — the history browser (issue #2959 item 6).
 *
 * The sidebar used to list the whole history beside the open sessions; the history
 * moved here, and the three questions the overlay has to answer are: does it reach a
 * session by title, by id **and** by agent name; does it keep the open ones apart from
 * the rest; and does an empty result say so instead of showing an empty card.
 */

const HOUR = 3_600_000;

/**
 * The middle of one local day.
 *
 * The overlay's "today" bucket is the **local calendar day** (`sameLocalDay` compares
 * `toDateString()`), so a fixture written as `Date.now() - HOUR` is "today" only while
 * the suite runs two hours past local midnight or later. Measured on CI: the `test` leg
 * started at 00:03 UTC on 2026-11-19 and this file's "今天的" hit fell on 11-18, so
 * `search-group-today` was never rendered. Anchoring the fixture and `nowMs` to one
 * instant inside the day keeps the assertion about grouping.
 */
function localNoon(daysAgo = 0): number {
  const d = new Date();
  d.setDate(d.getDate() - daysAgo);
  d.setHours(12, 0, 0, 0);
  return d.getTime();
}

function hit(over: Partial<SessionHit> = {}): SessionHit {
  return { sid: "s1", title: "Alpha", agent: "emrg", at: Date.now(), open: false, ...over };
}

function setup(props: Partial<Parameters<typeof SearchOverlay>[0]> = {}) {
  const onClose = vi.fn();
  const onPick = vi.fn();
  render(
    <I18nProvider lang="en">
      <SearchOverlay open hits={[hit()]} onClose={onClose} onPick={onPick} nowMs={Date.now()} {...props} />
    </I18nProvider>,
  );
  return { onClose, onPick };
}

describe("SearchOverlay", () => {
  it("open=false → 什么都不渲染", () => {
    setup({ open: false });
    expect(screen.queryByTestId("search-overlay")).not.toBeInTheDocument();
  });

  it("三组：打开中 / 今天 / 更早，各归各位", () => {
    const now = localNoon();
    setup({
      nowMs: now,
      hits: [
        hit({ sid: "open", title: "打开的那个", open: true }),
        hit({ sid: "today", title: "今天的", at: now - HOUR }),
        hit({ sid: "old", title: "很久以前", at: now - 40 * 24 * HOUR }),
      ],
    });
    expect(screen.getByTestId("search-group-open")).toHaveTextContent("打开的那个");
    expect(screen.getByTestId("search-group-today")).toHaveTextContent("今天的");
    expect(screen.getByTestId("search-group-earlier")).toHaveTextContent("很久以前");
    expect(screen.getByTestId("search-count")).toHaveTextContent("3");
  });

  it("查询按标题、会话 id 和 agent 名都能命中（记得 agent 就该找得到）", () => {
    setup({
      hits: [
        hit({ sid: "s1", title: "Alpha", agent: "emrg" }),
        hit({ sid: "s2", title: "Beta", agent: "aitokenpool" }),
      ],
    });
    fireEvent.change(screen.getByTestId("search-input"), { target: { value: "aitoken" } });
    const hits = screen.getAllByTestId("search-hit");
    expect(hits).toHaveLength(1);
    expect(hits[0]).toHaveAttribute("data-sid", "s2");

    fireEvent.change(screen.getByTestId("search-input"), { target: { value: "s1" } });
    expect(screen.getAllByTestId("search-hit")).toHaveLength(1);
    expect(screen.getAllByTestId("search-hit")[0]).toHaveAttribute("data-sid", "s1");
  });

  it("没有命中 → 明说，而不是一张空卡片", () => {
    setup();
    fireEvent.change(screen.getByTestId("search-input"), { target: { value: "zzz" } });
    expect(screen.queryAllByTestId("search-hit")).toHaveLength(0);
    expect(screen.getByTestId("search-empty")).toHaveTextContent("No matching session");
  });

  it("点一条 → onPick(sid)；Esc → onClose", () => {
    const { onPick, onClose } = setup();
    fireEvent.click(screen.getByTestId("search-hit"));
    expect(onPick).toHaveBeenCalledWith("s1");
    fireEvent.keyDown(screen.getByTestId("search-input"), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("再打开时清空上一次的查询（上次的过滤条件会藏起这次要找的会话）", () => {
    const { rerender } = render(
      <I18nProvider lang="en">
        <SearchOverlay open hits={[hit({ sid: "s1" })]} onClose={vi.fn()} onPick={vi.fn()} />
      </I18nProvider>,
    );
    fireEvent.change(screen.getByTestId("search-input"), { target: { value: "zzz" } });
    expect(screen.getByTestId("search-empty")).toBeInTheDocument();
    rerender(
      <I18nProvider lang="en">
        <SearchOverlay open={false} hits={[hit({ sid: "s1" })]} onClose={vi.fn()} onPick={vi.fn()} />
      </I18nProvider>,
    );
    rerender(
      <I18nProvider lang="en">
        <SearchOverlay open hits={[hit({ sid: "s1" })]} onClose={vi.fn()} onPick={vi.fn()} />
      </I18nProvider>,
    );
    expect(screen.getByTestId("search-input")).toHaveValue("");
    expect(screen.getAllByTestId("search-hit")).toHaveLength(1);
  });
});
