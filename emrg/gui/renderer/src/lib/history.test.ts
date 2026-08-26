import { describe, expect, it } from "vitest";
import {
  HISTORY_PAGE,
  applyHistoryPage,
  createHistoryPages,
  historyPageState,
  scrollCompensation,
  shouldLoadOlder,
} from "./history";

/**
 * history.test.ts — 历史分页状态机测试（Batch 2 remainder）。
 * 镜像 vanilla app.js historyPages（816-876）行为：offset 前进 / hasMore 关闭 /
 * 空页停 / 双守卫 / 滚差补偿。
 */

describe("historyPageState", () => {
  it("creates a fresh state on first access (offset 0, no more, not loading)", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    expect(st).toEqual({ offset: 0, hasMore: false, loading: false });
  });

  it("returns the same instance for repeated access (vanilla Map semantics)", () => {
    const pages = createHistoryPages();
    const a = historyPageState(pages, "s1");
    a.offset = 50;
    expect(historyPageState(pages, "s1")).toBe(a);
  });

  it("keeps sessions isolated (P3 per-sid)", () => {
    const pages = createHistoryPages();
    historyPageState(pages, "s1").offset = 50;
    const s2 = historyPageState(pages, "s2");
    expect(s2.offset).toBe(0);
  });
});

describe("applyHistoryPage", () => {
  it("advances offset and records hasMore", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    applyHistoryPage(st, 50, true);
    expect(st.offset).toBe(50);
    expect(st.hasMore).toBe(true);
  });

  it("accumulates offsets across pages (50 + 50 → 100)", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    applyHistoryPage(st, HISTORY_PAGE, true);
    applyHistoryPage(st, HISTORY_PAGE, true);
    expect(st.offset).toBe(100);
    expect(st.hasMore).toBe(true);
  });

  it("closes hasMore when server says no more", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    applyHistoryPage(st, 50, false);
    expect(st.hasMore).toBe(false);
  });

  it("empty page forces hasMore=false (vanilla: msgs.length===0 → hasMore=false)", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    applyHistoryPage(st, 0, true);
    expect(st.hasMore).toBe(false);
    expect(st.offset).toBe(0);
  });

  it("returns the message count of the page", () => {
    const pages = createHistoryPages();
    const st = historyPageState(pages, "s1");
    expect(applyHistoryPage(st, 17, true)).toBe(17);
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
