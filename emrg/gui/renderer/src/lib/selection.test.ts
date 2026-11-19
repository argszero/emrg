import { describe, expect, it, vi } from "vitest";
import {
  caretFromPoint,
  nearestOffset,
  nearestTextNode,
  seedSelection,
  selectionIn,
  type CharPoint,
} from "./selection";

/**
 * selection.test.ts — 选区起点与「选区是否拦住流式」的判定（issue #3034）。
 *
 * 每条都按它要防的那次改坏来命名：起点退回段落开头、等距时取后一个字、把折叠选区当成有选区、
 * 量不到 caret 却假装放下选区。jest-dom 没有排版，所以矩形由测试给、caret 由桩给——两侧走同一段判定。
 */

const rect = (offset: number, left: number, top: number, w = 8, h = 16): CharPoint => ({
  offset,
  left,
  top,
  right: left + w,
  bottom: top + h,
});

describe("nearestOffset：起点是离指针最近的那个字", () => {
  const row = [rect(0, 0, 0), rect(1, 8, 0), rect(2, 16, 0), rect(3, 24, 0)];

  it("点在某个字上 → 那个字的偏移", () => {
    expect(nearestOffset(row, 20, 8)).toBe(2);
  });

  it("点在正文下方几像素的留白 → 仍是最近那个字，不是这一段的开头", () => {
    // 变异「从段落开头起算」会让这条回答 0。
    expect(nearestOffset(row, 27, 40)).toBe(3);
  });

  it("点在正文上方的留白 → 最近的那个字", () => {
    expect(nearestOffset(row, 9, -30)).toBe(1);
  });

  it("等距时取更小的偏移：缝两侧各有一个字，起点落在前一个字之后", () => {
    const two = [rect(5, 0, 0, 10), rect(6, 10, 0, 10)];
    expect(nearestOffset(two, 10, 8)).toBe(5);
  });

  it("没有可量的字符 → null（量不到不是一个读数，不能假装从 0 开始）", () => {
    expect(nearestOffset([], 10, 10)).toBeNull();
  });
});

describe("selectionIn：折叠选区不算有选区", () => {
  const build = () => {
    const root = document.createElement("div");
    root.innerHTML = "<p>hello world</p>";
    document.body.appendChild(root);
    return root;
  };

  it("有未折叠选区落在内部 → true", () => {
    const root = build();
    const text = root.querySelector("p")!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, 5);
    const sel = document.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
    expect(selectionIn(root)).toBe(true);
  });

  it("折叠选区（只是一个 caret）→ false：否则光标一在回复里，回复就再也输出不动", () => {
    const root = build();
    const text = root.querySelector("p")!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 3);
    range.collapse(true);
    const sel = document.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
    expect(selectionIn(root)).toBe(false);
  });

  it("选区在节点之外 → false", () => {
    const root = build();
    const outside = document.createElement("p");
    outside.textContent = "other";
    document.body.appendChild(outside);
    const range = document.createRange();
    range.setStart(outside.firstChild!, 0);
    range.setEnd(outside.firstChild!, 3);
    const sel = document.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
    expect(selectionIn(root)).toBe(false);
  });

  it("没有选区 / 没有节点 → false，不抛", () => {
    document.getSelection()!.removeAllRanges();
    expect(selectionIn(document.body)).toBe(false);
    expect(selectionIn(null)).toBe(false);
  });
});

describe("caretFromPoint：两种浏览器写法各试一次，都没有就回答 null", () => {
  it("caretPositionFromPoint（标准）被优先使用", () => {
    const node = document.createTextNode("x");
    const doc = { caretPositionFromPoint: vi.fn(() => ({ offsetNode: node, offset: 1 })) } as unknown as Document;
    expect(caretFromPoint(doc, 5, 5)).toEqual({ node, offset: 1 });
  });

  it("只有 caretRangeFromPoint（WebKit 写法）时从 range 起止点取", () => {
    const node = document.createTextNode("abc");
    const range = document.createRange();
    range.setStart(node, 2);
    const doc = { caretRangeFromPoint: vi.fn(() => range) } as unknown as Document;
    expect(caretFromPoint(doc, 5, 5)).toEqual({ node, offset: 2 });
  });

  it("两个都没有（jsdom）→ null，不猜一个起点", () => {
    expect(caretFromPoint(document, 5, 5)).toBeNull();
  });
});

describe("nearestTextNode：从元素走到它里面的字", () => {
  it("元素 → 它内部的第一个文本节点", () => {
    const span = document.createElement("span");
    span.textContent = "✦ ";
    expect(nearestTextNode(span)).toBe(span.firstChild);
  });

  it("自己是文本节点 → 原样返回", () => {
    const text = document.createTextNode("x");
    expect(nearestTextNode(text)).toBe(text);
  });
});

describe("seedSelection：按在正文以外，就从最近的字起手", () => {
  const seed = () => {
    document.body.innerHTML = "<div id='body'>hello world</div>";
    const node = document.getElementById("body")!.firstChild!;
    document.getSelection()!.removeAllRanges();
    return node;
  };

  it("caret 量得到 → 放下一个折叠选区在最近的字符位置", () => {
    const node = seed();
    const ok = seedSelection(document, { x: 40, y: 8 }, () => ({ node, offset: 7 }));
    expect(ok).toBe(true);
    const sel = document.getSelection()!;
    expect(sel.rangeCount).toBe(1);
    expect(sel.isCollapsed).toBe(true);
    expect(sel.anchorNode).toBe(node);
    expect(sel.anchorOffset).toBe(7);
  });

  it("caret 落在元素上 → 落到它内部的文本节点，而不是整段作废", () => {
    const node = seed();
    const element = document.body;
    const ok = seedSelection(document, { x: 40, y: 8 }, () => ({ node: element, offset: 0 }));
    expect(ok).toBe(true);
    expect(document.getSelection()!.anchorNode).toBe(node);
  });

  it("量不到 caret → false，且不改动既有选区（不假装成功）", () => {
    seed();
    expect(seedSelection(document, { x: 40, y: 8 }, () => null)).toBe(false);
    expect(document.getSelection()!.rangeCount).toBe(0);
  });

  it("偏移越过文本长度 → 夹到末尾，不抛", () => {
    const node = seed();
    const ok = seedSelection(document, { x: 999, y: 8 }, () => ({ node, offset: 999 }));
    expect(ok).toBe(true);
    expect(document.getSelection()!.anchorOffset).toBe("hello world".length);
  });

  it("没有 document → false", () => {
    expect(seedSelection(null, { x: 0, y: 0 })).toBe(false);
  });
});
