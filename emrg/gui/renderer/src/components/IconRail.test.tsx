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

describe("图标轨的结构（issue #2986）", () => {
  function railEl(): Element {
    const rail = document.querySelector(".icon-rail");
    expect(rail).toBeTruthy();
    return rail as Element;
  }

  it("顶部标识是仓库的 SVG，不是任何文字符号", () => {
    setup();
    const mark = railEl().querySelector(".rail-mark");
    expect(mark).toBeTruthy();
    expect(mark!.querySelector("svg")).toBeTruthy();
    // 文字符号会随字体变形、换字号就走样，而且要它命名一件东西它也命名不了
    expect((mark!.textContent || "").trim()).toBe("");
  });

  it("轨宽就是契约的 60px（常量经 style 落到元素上）", () => {
    setup();
    expect((railEl() as HTMLElement).style.width).toBe("60px");
  });

  it("五个页面按钮连续排列，弹性占位在它们之后（设置不被推到底部）", () => {
    setup();
    const rail = railEl();
    const items = Array.from(rail.querySelectorAll(".rail-item"));
    // 五个页面 + 主题切换
    expect(items.length).toBe(6);
    const children = Array.from(rail.children);
    const fifthAt = children.indexOf(items[4]);
    const spacerAt = children.indexOf(rail.querySelector(".rail-spacer") as Element);
    expect(fifthAt).toBeGreaterThan(-1);
    expect(spacerAt).toBeGreaterThan(-1);
    expect(fifthAt).toBeLessThan(spacerAt);
  });

  it("轨上没有任何旧侧栏的类名（旧规则的 flex: 1 会把按钮拉长）", () => {
    setup();
    const rail = railEl();
    expect(rail.className).toBe("icon-rail");
    const pages = Array.from(rail.querySelectorAll<HTMLElement>("[data-view]"));
    expect(pages).toHaveLength(5);
    for (const page of pages) {
      // 允许的只有自己的两个：盒子类与选中态。多出来的一个类就是别人的规则能进来的门，
      // 而那条规则不需要写在这里，它已经在这个仓库里了。
      expect([...page.classList].filter((c) => c !== "rail-item" && c !== "active")).toEqual([]);
    }
    expect(document.querySelector(".side-nav")).toBeNull();
    expect(document.querySelector(".side-nav-item")).toBeNull();
  });

  it("底部有主题切换按钮与 daemon 连接点，且用自绘气泡而不是浏览器原生 title", () => {
    setup();
    const rail = railEl();
    expect(rail.querySelector('[data-testid="rail-theme"]')).toBeTruthy();
    expect(rail.querySelector('[data-testid="rail-conn"]')).toBeTruthy();
    // 原生 title 由窗口管理器绘制，偏移、淡入、层级都做不到，所以轨里一个都不许有
    expect(rail.querySelectorAll("[title]").length).toBe(0);
    expect(rail.querySelectorAll('[role="tooltip"]').length).toBeGreaterThan(0);
  });
});
