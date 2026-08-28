import "@testing-library/jest-dom/vitest";

/**
 * jsdom polyfills（tiptap/prosemirror 测试需要）。
 *
 * - prosemirror-view 的 coordsAtPos / scrollToSelection（EditorView 在
 *   focus/更新时滚动到选区）会对 Text/Range 调用 getClientRects()（及
 *   Range.getBoundingClientRect 兜底）——jsdom 只给 Element 实现了
 *   getClientRects，Text 与 Range 上均为 undefined，导致 tiptap 编辑器
 *   测试抛 "target.getClientRects is not a function"。
 *   这里补全：返回 1×1 非零矩形（singleRect 的 nonZero 检查短路，不再走
 *   getBoundingClientRect 兜底）。
 */
function fakeClientRects(this: Range | Text): DOMRectList {
  const rect: DOMRect = {
    left: 0,
    top: 0,
    right: 1,
    bottom: 1,
    width: 1,
    height: 1,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  };
  return [rect] as unknown as DOMRectList;
}
function fakeBoundingClientRect(this: Range): DOMRect {
  return {
    left: 0,
    top: 0,
    right: 1,
    bottom: 1,
    width: 1,
    height: 1,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect;
}

// lib.dom 的 Text 无 getClientRects（jsdom 运行时也没有）→ cast 后补全
type TextWithRects = Text & { getClientRects?: (this: Text) => DOMRectList };
const textProto = Text.prototype as TextWithRects;
if (typeof textProto.getClientRects === "undefined") {
  textProto.getClientRects = fakeClientRects;
}
// Range 在 lib.dom 有这两个方法，但 jsdom 运行时缺失 → 直接补
if (typeof Range.prototype.getClientRects === "undefined") {
  Range.prototype.getClientRects = fakeClientRects;
}
if (typeof Range.prototype.getBoundingClientRect === "undefined") {
  Range.prototype.getBoundingClientRect = fakeBoundingClientRect;
}

// jsdom 缺 document.elementFromPoint —— prosemirror 的 mousedown → posAtCoords
// 会调用它定位点击位置；返回 null 时 prosemirror 自带空值短路（posAtCoords → null
// → mousedown 直接 return），不崩溃。
if (typeof document.elementFromPoint !== "function") {
  document.elementFromPoint = () => null;
}
