/**
 * Where a menu anchored at a pointer is drawn, and whether it had to flip (issue #3041).
 *
 * Pure, so the edge rule is decided without a layout engine: the component measures
 * itself and hands the numbers here, and the tests hand in the numbers a browser would
 * give. Keeping it out of the component is what makes "贴着窗口右下边时向内翻转" an
 * assertion instead of a screenshot.
 */

/** Gap kept between the menu and the window edge. */
export const MENU_MARGIN = 4;

export interface MenuPlacement {
  left: number;
  top: number;
  /** The menu sits before the anchor rather than after it. */
  flippedX: boolean;
  flippedY: boolean;
}

/**
 * One axis: prefer to open at the anchor, else sit before it, else — for a menu larger
 * than the space either way — cling to the near edge so the coordinate never goes
 * negative and the far edge never passes the viewport.
 */
function axis(
  anchor: number,
  size: number,
  viewport: number,
  margin: number,
): { at: number; flipped: boolean } {
  const preferred = anchor + size + margin <= viewport ? anchor : anchor - size;
  const last = Math.max(margin, viewport - size - margin);
  const at = Math.min(Math.max(preferred, margin), last);
  return { at, flipped: at !== anchor };
}

export function placeMenu(
  anchor: { x: number; y: number },
  size: { width: number; height: number },
  viewport: { width: number; height: number },
  margin: number = MENU_MARGIN,
): MenuPlacement {
  const x = axis(anchor.x, size.width, viewport.width, margin);
  const y = axis(anchor.y, size.height, viewport.height, margin);
  return { left: x.at, top: y.at, flippedX: x.flipped, flippedY: y.flipped };
}
