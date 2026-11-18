import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { IconRail } from "./IconRail";
import { I18nProvider } from "../lib/i18n";
import { RAIL_WIDTH } from "../lib/cockpit";

/**
 * IconRail.test.tsx — the 60px column (issue #2959 item 2).
 *
 * The rail took over the navigation that used to sit inside the sidebar, so these
 * assertions are the ones the sidebar's old nav tests made, moved to the component
 * that now carries the behaviour — plus the one new fact that matters: settings keeps
 * a door, because a page nobody can open is a page lost in a layout change.
 */

const VIEWS = ["sessions", "agents", "tasks", "rants", "settings"] as const;

function setup(props: Partial<Parameters<typeof IconRail>[0]> = {}) {
  const onSwitchView = vi.fn();
  render(
    <I18nProvider lang="en">
      <IconRail activeView="sessions" onSwitchView={onSwitchView} {...props} />
    </I18nProvider>,
  );
  return { onSwitchView };
}

describe("IconRail", () => {
  it("五个页面各一个按钮，宽度是契约写的 60px", () => {
    setup();
    for (const view of VIEWS) {
      const btn = screen.getByTestId(`nav-${view}`);
      expect(btn).toHaveAttribute("data-view", view);
    }
    // 页面集合是这五个，不是「至少这五个」：多出来的一个同样是这次重建要拿掉的东西
    // （一个没人要的页面留在栏里，正是把导航搬出来时要清理的那一半）。下一轮的循环
    // 只会点它认识的五个，所以计数必须自己站在这里。
    expect(screen.getAllByTestId(/^nav-/)).toHaveLength(VIEWS.length);
    expect(screen.getByTestId("side-nav").style.width).toBe(`${RAIL_WIDTH}px`);
  });

  it("当前页高亮，其余不亮", () => {
    setup({ activeView: "tasks" });
    expect(screen.getByTestId("nav-tasks")).toHaveClass("active");
    expect(screen.getByTestId("nav-sessions")).not.toHaveClass("active");
  });

  it("点按钮 → onSwitchView(view)，设置页也在其中", () => {
    const { onSwitchView } = setup();
    for (const view of VIEWS) {
      fireEvent.click(screen.getByTestId(`nav-${view}`));
      expect(onSwitchView).toHaveBeenCalledWith(view);
    }
    expect(onSwitchView).toHaveBeenCalledTimes(VIEWS.length);
  });
});
