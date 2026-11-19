import { beforeEach, describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { EmrgMark } from "./EmrgMark";
import { setTheme } from "../lib/theme";

/**
 * EmrgMark.test.tsx — the rail's mark reads the theme (issue #3030).
 *
 * The two palettes are pinned from both sides, because either one alone can go wrong
 * quietly: "the light variant exists" without "the dark one is unchanged" lets the
 * icon's own artwork drift, and the reverse leaves a near-black square on a white rail.
 *
 * The theme is driven through the real store (`lib/theme.ts`) rather than a prop: the
 * defect this file exists for is a mark that reads the theme **once**, at first render,
 * so a test that hands the palette in could not see it. A switch has to repaint the
 * mounted element.
 */

beforeEach(() => {
  // The store is module-level and every test would otherwise inherit the last one's theme.
  act(() => setTheme("dark"));
});

function swatchOf(el: Element): string[] {
  const out: string[] = [];
  for (const node of [el, ...Array.from(el.querySelectorAll("*"))]) {
    for (const attr of ["fill", "stroke", "stop-color", "stopColor"]) {
      const v = node.getAttribute(attr);
      if (v) out.push(v.toLowerCase());
    }
  }
  return out;
}

describe("EmrgMark", () => {
  it("深色：图标原始的渐变、枝叶色与高光点一字不动", () => {
    render(<EmrgMark />);
    const svg = screen.getByTestId("rail-mark-svg");
    expect(svg).toHaveAttribute("data-mark-variant", "dark");

    const colors = swatchOf(svg);
    // 底色渐变的两端、枝的亮青、茎与节点的渐变、高光点
    for (const kept of ["#111c16", "#070b08", "#00ff87", "#00c97f", "#047857", "#e4fff2"]) {
      expect(colors).toContain(kept);
    }
    // 底色仍是那条渐变，不是实心色
    const body = svg.querySelector("rect");
    expect(body).toHaveAttribute("fill", "url(#rail-bgGrad)");
    // 五个高光点（毂心一个 + 四个节点各一个）
    expect(colors.filter((c) => c === "#e4fff2")).toHaveLength(5);
  });

  it("浅色：实心主题绿底、浅绿枝叶，而且整棵树里没有一处亮青", () => {
    act(() => setTheme("light"));
    render(<EmrgMark />);
    const svg = screen.getByTestId("rail-mark-svg");
    expect(svg).toHaveAttribute("data-mark-variant", "light");

    // #00FF87 在白底上只有 1.34:1：浅色变体一个色标都不许带进来
    expect(svg.outerHTML.toLowerCase()).not.toContain("#00ff87");
    expect(svg.querySelector("rect")).toHaveAttribute("fill", "#067A52");

    const colors = swatchOf(svg);
    expect(colors).toContain("#d9fbe8"); // 枝叶
    expect(colors).toContain("#067a52"); // 实心底与高光点
    expect(colors).not.toContain("#111c16"); // 深色底不得残留
    expect(colors).not.toContain("#e4fff2");
  });

  it("切主题时已挂载的标识立刻换，不靠重挂载或刷新", () => {
    render(<EmrgMark />);
    const svg = screen.getByTestId("rail-mark-svg");
    expect(svg).toHaveAttribute("data-mark-variant", "dark");
    const darkMarkup = svg.outerHTML;

    act(() => setTheme("light"));
    // 同一个 DOM 节点，不是重新渲染出来的另一个
    expect(screen.getByTestId("rail-mark-svg")).toBe(svg);
    expect(svg).toHaveAttribute("data-mark-variant", "light");
    expect(svg.outerHTML).not.toBe(darkMarkup);
    expect(svg.querySelector("rect")).toHaveAttribute("fill", "#067A52");

    act(() => setTheme("dark"));
    expect(svg).toHaveAttribute("data-mark-variant", "dark");
    expect(svg.querySelector("rect")).toHaveAttribute("fill", "url(#rail-bgGrad)");
  });

  it("尺寸由 props 决定，body 的 viewBox 与圆角仍是图标自己的", () => {
    render(<EmrgMark size={48} />);
    const svg = screen.getByTestId("rail-mark-svg");
    expect(svg).toHaveAttribute("width", "48");
    expect(svg).toHaveAttribute("height", "48");
    // 裁剪盒与圆角 206（912 的图标自身）不随 size 变，改它就是把图画歪
    expect(svg).toHaveAttribute("viewBox", "40 40 944 944");
    expect(svg.querySelector("rect")).toHaveAttribute("rx", "206");

    render(<EmrgMark />);
    const defaults = screen.getAllByTestId("rail-mark-svg")[1];
    expect(defaults).toHaveAttribute("width", "32");
    expect(defaults).toHaveAttribute("height", "32");
  });

  it("是内联 <svg>，内部没有文本字符（不退回文字符号）", () => {
    render(<EmrgMark />);
    const svg = screen.getByTestId("rail-mark-svg");
    expect(svg.tagName.toLowerCase()).toBe("svg");
    expect(svg.textContent).toBe("");
    expect(svg.querySelectorAll("text")).toHaveLength(0);
    expect(svg).toHaveAttribute("role", "img");
    expect(svg).toHaveAttribute("aria-label", "EMRG");
  });
});
