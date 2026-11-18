import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ResizeHandle } from "./ResizeHandle";
import type { ColumnBounds } from "../lib/cockpit";

/**
 * ResizeHandle.test.tsx — the 6px drag surface (issue #2959 item 4).
 *
 * Both sides are exercised even though the shell mounts only the left one today:
 * the sign of the movement is the whole content of this component, and a right
 * handle wired with the left's sign widens the column when the pointer moves the
 * other way — a defect that reads as "the drag feels inverted" and nothing else.
 */

const bounds: ColumnBounds = { min: 180, max: 900 };

function setup(side: "left" | "right" = "left", width = 264) {
  const onResize = vi.fn();
  const onReset = vi.fn();
  render(<ResizeHandle side={side} width={width} bounds={bounds} onResize={onResize} onReset={onReset} />);
  return { onResize, onReset, handle: screen.getByTestId(`col-handle-${side}`) };
}

function drag(handle: HTMLElement, fromX: number, toX: number) {
  fireEvent.mouseDown(handle, { clientX: fromX });
  fireEvent.mouseMove(window, { clientX: toX });
  fireEvent.mouseUp(window);
}

describe("ResizeHandle", () => {
  it("左栏：指针向右 → 变宽；向右拖出边界时被 max 挡住", () => {
    const { onResize, handle } = setup("left", 264);
    drag(handle, 300, 340);
    expect(onResize).toHaveBeenLastCalledWith(304);
    drag(handle, 300, 5000);
    expect(onResize).toHaveBeenLastCalledWith(bounds.max);
  });

  it("右栏：指针向右 → 变窄（同一段位移在两栏上是相反符号）", () => {
    const { onResize, handle } = setup("right", 280);
    drag(handle, 300, 340);
    expect(onResize).toHaveBeenLastCalledWith(240);
  });

  it("往左拖到最小宽度以下 → 停在 min，不出现负数宽", () => {
    const { onResize, handle } = setup("left", 200);
    drag(handle, 300, 0);
    expect(onResize).toHaveBeenLastCalledWith(bounds.min);
  });

  it("双击 → 恢复默认（onReset）", () => {
    const { onReset, handle } = setup();
    fireEvent.doubleClick(handle);
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("拖动期间 body 带 col-resizing（指针离开 6px 也不开始选字），松手后清掉", () => {
    const { handle } = setup();
    fireEvent.mouseDown(handle, { clientX: 300 });
    expect(document.body.classList.contains("col-resizing")).toBe(true);
    fireEvent.mouseUp(window);
    expect(document.body.classList.contains("col-resizing")).toBe(false);
  });
});
