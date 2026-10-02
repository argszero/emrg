import { describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach } from "vitest";
import { ContextMenu } from "./ContextMenu";

/**
 * ContextMenu.test.tsx — 会话右键菜单（vanilla `showOpenSessionsMenu` 的行为）。
 *
 * 这个组件存在是因为 `Sidebar` 的 `onContextMenu` **一直没有调用方**：右键点会话时
 * `e.preventDefault()` 吃掉了原生菜单、替代菜单又不出现。所以这里的断言不只是
 * 「菜单能画出来」，而是「每一项真的会做那件事」与「菜单不会成为陷阱」
 * （↑↓/Enter/Escape/点外面）。
 */

afterEach(() => cleanup());

function menu(over: Partial<Parameters<typeof ContextMenu>[0]> = {}) {
  const onDismiss = vi.fn();
  const actions = [vi.fn(), vi.fn(), vi.fn()];
  render(
    <ContextMenu
      x={100}
      y={120}
      onDismiss={onDismiss}
      items={[
        { label: "Close session", onSelect: actions[0] },
        { label: "Rename", onSelect: actions[1] },
        { label: "Delete", danger: true, onSelect: actions[2] },
      ]}
      {...over}
    />,
  );
  return { onDismiss, actions };
}

describe("ContextMenu", () => {
  it("画出每一项，文本是传进来的标签（调用方负责取词典）", () => {
    menu();
    const items = screen.getAllByTestId("ctx-item");
    expect(items.map((b) => b.textContent)).toEqual(["Close session", "Rename", "Delete"]);
  });

  it("第三方是 danger（vanilla 删除项 danger=true）", () => {
    menu();
    const items = screen.getAllByTestId("ctx-item");
    expect(items[2].className).toContain("danger");
    expect(items[0].className).not.toContain("danger");
  });

  it("第一项默认高亮（vanilla setActive(0)）", () => {
    menu();
    expect(screen.getAllByTestId("ctx-item")[0].className).toContain("active");
  });

  it("点选项 → 先关菜单再执行那件事", () => {
    const { onDismiss, actions } = menu();
    fireEvent.click(screen.getAllByTestId("ctx-item")[1]);
    expect(actions[1]).toHaveBeenCalledTimes(1);
    expect(actions[0]).not.toHaveBeenCalled();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("↓ 移动高亮，Enter 触发当前项（不是第一项）", () => {
    const { onDismiss, actions } = menu();
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(screen.getAllByTestId("ctx-item")[1].className).toContain("active");
    fireEvent.keyDown(document, { key: "Enter" });
    expect(actions[1]).toHaveBeenCalledTimes(1);
    expect(actions[0]).not.toHaveBeenCalled();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("↑ 从第一项回绕到最后一项（vanilla 的取模）", () => {
    const { actions } = menu();
    fireEvent.keyDown(document, { key: "ArrowUp" });
    expect(screen.getAllByTestId("ctx-item")[2].className).toContain("active");
    fireEvent.keyDown(document, { key: "Enter" });
    expect(actions[2]).toHaveBeenCalledTimes(1);
  });

  it("Escape 关闭且不执行任何一项", () => {
    const { onDismiss, actions } = menu();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onDismiss).toHaveBeenCalledTimes(1);
    actions.forEach((a) => expect(a).not.toHaveBeenCalled());
  });

  it("点菜单以外 → 关闭；点菜单以内 → 不关（vanilla 的 closest('#ctx-menu')）", () => {
    const { onDismiss } = menu();
    fireEvent.click(screen.getAllByTestId("ctx-item")[0]);
    const afterItem = onDismiss.mock.calls.length; // 选项点击自己会关一次
    fireEvent.click(document.body);
    expect(onDismiss.mock.calls.length).toBe(afterItem + 1);
  });

  it("菜单不出屏幕：x/y 夹进视口（vanilla 的 160/80 余量）", () => {
    menu({ x: window.innerWidth - 5, y: window.innerHeight - 5 });
    const el = screen.getByTestId("ctx-menu");
    expect(el.style.left).toBe(`${window.innerWidth - 160}px`);
    expect(el.style.top).toBe(`${window.innerHeight - 80}px`);
  });
});
