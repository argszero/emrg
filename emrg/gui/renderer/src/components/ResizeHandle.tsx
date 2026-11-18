import { useEffect, useRef, useState } from "react";
import { HANDLE_WIDTH, resizedWidth, type ColumnBounds } from "../lib/cockpit";

/**
 * ResizeHandle — the 6px drag surface between two columns (issue #2959 item 4).
 *
 * Drag widens the adjacent column with no upper bound but the window; double-click
 * restores that column's default. While a drag is in flight the cursor is `col-resize`
 * **everywhere** and text selection is suppressed — set on `<body>`, because a pointer
 * that leaves the 6px strip would otherwise hand the cursor back to whatever it is over
 * and start selecting the transcript mid-drag.
 *
 * The width lives in the shell, not here: a column's width must survive the column
 * being closed and reopened, and a component that unmounts cannot remember it.
 */
export interface ResizeHandleProps {
  side: "left" | "right";
  /** The column's width when the drag starts. */
  width: number;
  bounds: ColumnBounds;
  onResize: (px: number) => void;
  onReset: () => void;
}

export function ResizeHandle({ side, width, bounds, onResize, onReset }: ResizeHandleProps) {
  const [dragging, setDragging] = useState(false);
  const start = useRef({ x: 0, w: 0 });
  // The shell's callback is a fresh lambda each render; subscribing to it directly
  // would tear down and rebuild the window listeners on every mouse move.
  const onResizeRef = useRef(onResize);
  onResizeRef.current = onResize;
  const boundsRef = useRef(bounds);
  boundsRef.current = bounds;

  useEffect(() => {
    if (!dragging) return;
    document.body.classList.add("col-resizing");
    const move = (e: MouseEvent) => {
      // Dragging right grows the left column and shrinks the right one, so the same
      // pointer movement means opposite signs on the two sides.
      const dx = side === "left" ? e.clientX - start.current.x : start.current.x - e.clientX;
      onResizeRef.current(resizedWidth(start.current.w, dx, boundsRef.current));
    };
    const up = () => setDragging(false);
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.classList.remove("col-resizing");
    };
  }, [dragging, side]);

  return (
    <div
      className={`col-handle col-handle-${side}${dragging ? " dragging" : ""}`}
      data-testid={`col-handle-${side}`}
      role="separator"
      aria-orientation="vertical"
      style={{ width: HANDLE_WIDTH }}
      title={/* the same promise the double-click keeps */ "Drag to resize · double-click to reset"}
      onMouseDown={(e) => {
        e.preventDefault();
        start.current = { x: e.clientX, w: width };
        setDragging(true);
      }}
      onDoubleClick={(e) => {
        e.preventDefault();
        onReset();
      }}
    />
  );
}
