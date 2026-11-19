import { useLayoutEffect, useState, type RefObject } from "react";
import { placeMenu, type MenuPlacement } from "../lib/contextMenu";

/**
 * ContextMenu — the right-click menu the two directory trees share (issue #3041).
 *
 * The look is the repository's existing `.ctx-menu` / `.ctx-item` block
 * (`css/components.css`). Its `.ctx-item.active` highlight was written for arrow-key
 * navigation and had no consumer until this file; this is that consumer, so no second
 * menu style is introduced.
 *
 * Placement is `placeMenu`, a pure function — this component only measures itself and
 * hands the numbers over. It is drawn `position: fixed` at the pointer, so the caller
 * owns closing it on anything that would leave the anchor behind: a scroll, a click
 * elsewhere, Escape, or a new root.
 */
export type ContextMenuId = "copy" | "open";

export interface ContextMenuItem {
  id: ContextMenuId;
  /** Already translated: the menu renders what it is given. */
  label: string;
  testId: string;
}

export interface ContextMenuProps {
  /** Pointer position, in viewport coordinates. */
  anchor: { x: number; y: number };
  items: ContextMenuItem[];
  /** Index the arrow keys light up. */
  active: number;
  testId: string;
  /** The caller's ref, so it can tell a click inside the menu from a click outside it. */
  menuRef: RefObject<HTMLDivElement | null>;
  onPick: (id: ContextMenuId) => void;
  onHover: (index: number) => void;
}

export function ContextMenu({ anchor, items, active, testId, menuRef, onPick, onHover }: ContextMenuProps) {
  // The first render knows only the pointer; the size is the browser's answer, read
  // before the frame is painted so the menu never appears off-window first.
  const [placement, setPlacement] = useState<MenuPlacement>(() =>
    placeMenu(
      anchor,
      { width: 0, height: 0 },
      { width: window.innerWidth, height: window.innerHeight },
    ),
  );

  useLayoutEffect(() => {
    const rect = menuRef.current?.getBoundingClientRect();
    setPlacement(
      placeMenu(
        anchor,
        { width: rect?.width ?? 0, height: rect?.height ?? 0 },
        { width: window.innerWidth, height: window.innerHeight },
      ),
    );
  }, [anchor.x, anchor.y, items.length, menuRef]);

  return (
    <div
      ref={menuRef}
      className="ctx-menu"
      role="menu"
      data-testid={testId}
      style={{ left: placement.left, top: placement.top }}
      data-flipped-x={placement.flippedX ? "1" : "0"}
      data-flipped-y={placement.flippedY ? "1" : "0"}
    >
      {items.map((item, i) => (
        <button
          key={item.id}
          type="button"
          className={`ctx-item${i === active ? " active" : ""}`}
          role="menuitem"
          data-testid={item.testId}
          onMouseEnter={() => onHover(i)}
          onMouseDown={(e) => {
            // mousedown rather than click: the tree keeps the focus its ↑↓ / Esc need,
            // and the document-level outside-click listener must not read this as one.
            e.preventDefault();
            e.stopPropagation();
            onPick(item.id);
          }}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
