import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MARKDOWN_HTML_CLASS, MARKDOWN_PLAIN_CLASS, MarkdownText } from "./MarkdownText";
import { BODY_TEXT_SELECTOR } from "./TranscriptView";
import type { MarkdownRenderer } from "../lib/markdown";

/**
 * MarkdownText.test.tsx — 已完成正文的盒模型，以及它和「按下守卫」用的是同一份类名。
 *
 * 已完成的回复一直选不中，流式那半边反而好选。两条路径的差别在容器盒：块级 markdown 落进
 * 行内 `<span>` 会被浏览器拆成匿名块，跨匿名块的选区是它最不可靠的地方。jsdom 没有排版，
 * 所以这里钉的是**盒子形态**与**类名的一致性**；页面上的实际选区行为只能由真实浏览器证明。
 */

/** 只回答一次块级渲染：`<p>` 正是触发 block-in-inline 的那种孩子。 */
const blockMd: MarkdownRenderer = {
  renderMarkdown: async (text) => `<p>${text}</p>`,
  streamProject: () => false,
  streamFinalize: async () => {},
  resumeStream: async () => {},
};

describe("MarkdownText：块级 markdown 落进块级容器", () => {
  it("已完成的正文是块级盒子，不是行内 span", async () => {
    const { container } = render(<MarkdownText text="hello" md={blockMd} />);
    await screen.findByText("hello");
    const el = container.querySelector(`.${MARKDOWN_HTML_CLASS}`) as HTMLElement;
    expect(el).not.toBeNull();
    expect(el.querySelector("p")).not.toBeNull();
    expect(el.tagName).toBe("DIV");
  });
});

describe("一个规则一个产出者：正文的两个类名都进按下守卫", () => {
  it("两个默认类名都命中 BODY_TEXT_SELECTOR", () => {
    const probe = document.createElement("div");
    for (const cls of [MARKDOWN_HTML_CLASS, MARKDOWN_PLAIN_CLASS]) {
      probe.className = cls;
      expect(probe.matches(BODY_TEXT_SELECTOR), cls).toBe(true);
    }
  });

  it("守卫仍然认流式容器（流式那半边一直是对的，不能被这次改动挪走）", () => {
    const probe = document.createElement("div");
    probe.className = "md-stream";
    expect(probe.matches(BODY_TEXT_SELECTOR)).toBe(true);
  });

  it("非正文的件（✦、时间、工具行）不在守卫里——按在它们身上起手才是种 caret 的场合", () => {
    const probe = document.createElement("div");
    for (const cls of ["msg-assistant-mark", "msg-time", "tool-row", "tool-group-bar"]) {
      probe.className = cls;
      expect(probe.matches(BODY_TEXT_SELECTOR), cls).toBe(false);
    }
  });
});
