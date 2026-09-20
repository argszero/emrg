import { describe, expect, it } from "vitest";
import {
  HISTORY_PAGE,
  applyHistoryPage,
  createHistoryPages,
  historyPageState,
  scrollCompensation,
  shouldLoadOlder,
  unloadedRecords,
} from "./history";

/**
 * history.test.ts — 历史分页状态机测试（Batch 2 remainder）。
 * 游标语义自 rant 2026-09-20T18:58:44 起是 **record_index**（绝对位置）：更早一页 =
 * `record_index < 最早已加载`，不再是从最新往回数的 offset 计数。
 */

/** 造一页假记录（daemon include_records 每条都带 record_index） */
function pageOf(indexes: number[]) {
  return indexes.map((i) => ({ record_index: i, kind: "message", content: `m${i}` }));
}

describe("historyPageState", () => {
  it("creates a fresh state on first access (no cursor, no more, not loading, nothing loaded)", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    expect(st).toEqual({ oldestIndex: null, hasMore: false, loading: false, loaded: new Set() });
  });

  it("returns the same instance for repeated access (vanilla Map semantics)", () => {
    const pages = createHistoryPages();
    const a = historyPageState(pages, "s1");
    a.oldestIndex = 50;
    expect(historyPageState(pages, "s1")).toBe(a);
  });

  it("keeps sessions isolated (P3 per-sid)", () => {
    const pages = createHistoryPages();
    const s1 = historyPageState(pages, "s1");
    s1.oldestIndex = 50;
    s1.loaded.add(50);
    const s2 = historyPageState(pages, "s2");
    expect(s2.oldestIndex).toBeNull();
    expect(s2.loaded.size).toBe(0);
  });
});

describe("applyHistoryPage", () => {
  it("sets the cursor to the oldest record of the page and records hasMore", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    applyHistoryPage(st, pageOf([50, 51, 52, 99]), true);
    expect(st.oldestIndex).toBe(50);
    expect(st.hasMore).toBe(true);
    expect(st.loaded.size).toBe(4);
  });

  it("carries the cursor back across pages (page 2's cursor is its own oldest)", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    applyHistoryPage(st, pageOf(Array.from({ length: HISTORY_PAGE }, (_, i) => 50 + i)), true);
    applyHistoryPage(st, pageOf(Array.from({ length: HISTORY_PAGE }, (_, i) => i)), true);
    expect(st.oldestIndex).toBe(0);
    expect(st.loaded.size).toBe(100);
  });

  it("closes hasMore when server says no more", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    applyHistoryPage(st, pageOf([1, 2]), false);
    expect(st.hasMore).toBe(false);
  });

  it("empty page forces hasMore=false and leaves the cursor untouched", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    st.oldestIndex = 7;
    applyHistoryPage(st, [], true);
    expect(st.hasMore).toBe(false);
    expect(st.oldestIndex).toBe(7);
  });

  it("returns the record count of the page", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    expect(applyHistoryPage(st, pageOf([0, 1, 2, 3, 4]), true)).toBe(5);
  });
});

describe("cursor stability (rant 2026-09-20T18:58:44: an offset slid, a record_index does not)", () => {
  it("a record appended between pages does not move an already-loaded page's window", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    // Page 1 = the newest 3 records of a 10-record history.
    applyHistoryPage(st, pageOf([7, 8, 9]), true);
    expect(st.oldestIndex).toBe(7);
    // Paging between requests: one more record lands at the end of the history.
    // The next window is asked for by absolute index, so it is the 3 records
    // *below* 7 — not the 3 below the (now shifted) newest.
    const older = pageOf([4, 5, 6]).filter((r) => r.record_index < (st.oldestIndex as number));
    expect(older.map((r) => r.record_index)).toEqual([4, 5, 6]);
    applyHistoryPage(st, older, true);
    expect(st.oldestIndex).toBe(4);
  });

  it("drops records a clamped cursor made overlap (compaction shrank the history)", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    applyHistoryPage(st, pageOf([10, 11, 12]), true);
    // The history shrank, so the daemon clamps the request and the same records
    // come back once more — they must not be replayed a second time.
    expect(unloadedRecords(st, pageOf([11, 12]))).toEqual([]);
    expect(unloadedRecords(st, pageOf([8, 9, 11]))).toEqual([{ record_index: 8, kind: "message", content: "m8" }, { record_index: 9, kind: "message", content: "m9" }]);
  });

  it("passes a whole page through the first time", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    expect(unloadedRecords(st, pageOf([0, 1, 2]))).toHaveLength(3);
  });
});

describe("scrollCompensation", () => {
  it("keeps visual position after prepend (prevScrollTop + height delta)", () => {
    // 用户在 500px 处，新历史插入顶部后容器高度 +600 → 需滚到 1100 保持视觉不变
    expect(scrollCompensation(500, 2000, 2600)).toBe(1100);
  });

  it("no height change → no compensation (empty page)", () => {
    expect(scrollCompensation(42, 2000, 2000)).toBe(42);
  });

  it("never goes below prevScrollTop on shrink (Math.max 0 clamp)", () => {
    // 理论上 prepend 只会增高度；即便异常收缩也保持原位置不回落
    expect(scrollCompensation(100, 2000, 1800)).toBe(100);
  });
});

describe("shouldLoadOlder", () => {
  it("triggers at scrollTop <= 2 with more pages and not loading", () => {
    expect(shouldLoadOlder(0, true, false)).toBe(true);
    expect(shouldLoadOlder(2, true, false)).toBe(true);
  });

  it("does not trigger below threshold", () => {
    expect(shouldLoadOlder(3, true, false)).toBe(false);
    expect(shouldLoadOlder(100, true, false)).toBe(false);
  });

  it("does not trigger without more pages", () => {
    expect(shouldLoadOlder(0, false, false)).toBe(false);
  });

  it("does not trigger while loading (in-flight lock)", () => {
    expect(shouldLoadOlder(0, true, true)).toBe(false);
  });
});
