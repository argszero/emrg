import { describe, expect, it } from "vitest";
import { MENU_MARGIN, placeMenu } from "./contextMenu";

/**
 * contextMenu.test.ts — the edge rule behind the trees' right-click menu (issue #3041).
 *
 * Each case is one position the issue names: open at the pointer inside the window, and
 * flip inward when the pointer sits near the right edge, the bottom edge, or both. The
 * numbers asserted are the rendered ones — the far edge of the menu against the viewport
 * — which is what makes "不出窗口" measurable rather than eyeballed.
 */

const VIEW = { width: 1024, height: 768 };
const SIZE = { width: 180, height: 72 };

describe("placeMenu", () => {
  it("室内：菜单左上角就在锚点上，不翻转", () => {
    expect(placeMenu({ x: 100, y: 120 }, SIZE, VIEW)).toEqual({
      left: 100,
      top: 120,
      flippedX: false,
      flippedY: false,
    });
  });

  it("贴右边缘：翻到锚点左侧，右边缘仍在窗口内", () => {
    const p = placeMenu({ x: VIEW.width - 10, y: 100 }, SIZE, VIEW);
    expect(p.flippedX).toBe(true);
    expect(p.left).toBe(VIEW.width - 10 - SIZE.width);
    expect(p.left + SIZE.width).toBeLessThanOrEqual(VIEW.width);
  });

  it("贴下边缘：翻到锚点上方，下边缘仍在窗口内", () => {
    const p = placeMenu({ x: 100, y: VIEW.height - 6 }, SIZE, VIEW);
    expect(p.flippedY).toBe(true);
    expect(p.top).toBe(VIEW.height - 6 - SIZE.height);
    expect(p.top + SIZE.height).toBeLessThanOrEqual(VIEW.height);
  });

  it("右下角：两个方向同时翻，两个边缘都不越界", () => {
    const p = placeMenu({ x: VIEW.width - 2, y: VIEW.height - 2 }, SIZE, VIEW);
    expect(p.flippedX).toBe(true);
    expect(p.flippedY).toBe(true);
    expect(p.left + SIZE.width).toBeLessThanOrEqual(VIEW.width);
    expect(p.top + SIZE.height).toBeLessThanOrEqual(VIEW.height);
    expect(p.left).toBeGreaterThanOrEqual(0);
    expect(p.top).toBeGreaterThanOrEqual(0);
  });

  it("菜单比窗口还大：贴住左上边距，坐标不为负", () => {
    const p = placeMenu({ x: 900, y: 700 }, { width: 1200, height: 900 }, VIEW);
    expect(p.left).toBe(MENU_MARGIN);
    expect(p.top).toBe(MENU_MARGIN);
  });
});
