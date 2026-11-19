/**
 * 选区从哪里起算，以及它会不会拦住流式重写（issue #3034）。
 *
 * 三件事留给纯函数，浏览器 API 只做最后一层适配，因为 jsdom 没有排版：
 *
 * - `nearestOffset` 在只有字符矩形、没有 caret API 的地方决定起点——「离指针最近的那个字」，
 *   而不是「这一段的开头」。空集回答 `null`：量不到就不是一个读数，不能假装从 0 开始。
 * - `selectionIn` 回答「活选区是否落在本节点内」。这是流式必须暂停的唯一条件。
 * - `seedSelection` 在按下的地方不是可选的正文时，先放一个**折叠**选区在最近的 caret 上，
 *   于是随后的拖动从那里延伸，而不是从段落开头延伸。
 *
 * `caretFromPoint` 是唯一碰 `caretPositionFromPoint` / `caretRangeFromPoint` 的地方，并且作为
 * 参数进出：浏览器侧用默认实现，测试侧给一个桩，两侧走同一段判定。
 */

/** 一个字符的矩形（视口坐标系）与它在自己文本节点里的偏移。 */
export interface CharPoint {
  offset: number;
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** 指针位置。只取判定要用的两个分量，避免把整个事件类型拖进纯函数。 */
export interface PointerPoint {
  x: number;
  y: number;
}

/** 一个 caret 位置。`node` 是文本节点时 `offset` 是字符偏移。 */
export interface Caret {
  node: Node;
  offset: number;
}

const EPS = 1e-9;

/**
 * 离 `(x, y)` 最近的那个字符偏移；`points` 为空回答 `null`。
 *
 * 距离按矩形外的缺口算：点在矩形内两个缺口都是 0，于是与「点在字上」得分相同。等距时取更小的
 * 偏移——同一条缝两侧各有一个字时，起点落在前一个字之后，读起来才是「从我按的地方开始」。
 */
export function nearestOffset(points: readonly CharPoint[], x: number, y: number): number | null {
  if (!points.length) return null;
  let best: CharPoint | null = null;
  let bestDist = Infinity;
  for (const p of points) {
    const dx = x < p.left ? p.left - x : x > p.right ? x - p.right : 0;
    const dy = y < p.top ? p.top - y : y > p.bottom ? y - p.bottom : 0;
    const dist = dx * dx + dy * dy;
    if (best && dist > bestDist + EPS) continue;
    if (best && Math.abs(dist - bestDist) <= EPS && p.offset >= best.offset) continue;
    best = p;
    bestDist = dist;
  }
  return best ? best.offset : null;
}

/**
 * `root` 内是否有活选区。
 *
 * 折叠选区（一个 caret）不算：它不持有任何字，流式重写不会伤到它，而把它当成「有选区」会让
 * 每一条正在输出的回复在光标存在时停止更新。没有 selection API 的环境回答 `false`。
 */
export function selectionIn(root: Node | null, doc?: Document | null): boolean {
  const d = doc ?? (typeof document === "undefined" ? null : document);
  if (!root || !d || typeof d.getSelection !== "function") return false;
  const sel = d.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return false;
  for (let i = 0; i < sel.rangeCount; i += 1) {
    const range = sel.getRangeAt(i);
    if (root.contains(range.startContainer) || root.contains(range.endContainer)) return true;
  }
  return false;
}

type CaretApi = {
  caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
  caretRangeFromPoint?: (x: number, y: number) => Range | null;
};

/**
 * 指针下的 caret，两种浏览器写法各试一次；都没有就回答 `null`。
 *
 * 先 `caretPositionFromPoint`（标准），再 `caretRangeFromPoint`（WebKit 的历史写法）。两者都缺席
 * 的环境（jsdom）回答 `null`，调用方据此不做任何事——不猜一个起点。
 */
export function caretFromPoint(doc: Document | null | undefined, x: number, y: number): Caret | null {
  if (!doc) return null;
  const api = doc as unknown as CaretApi;
  if (typeof api.caretPositionFromPoint === "function") {
    const pos = api.caretPositionFromPoint(x, y);
    if (pos && pos.offsetNode) return { node: pos.offsetNode, offset: pos.offset };
    return null;
  }
  if (typeof api.caretRangeFromPoint === "function") {
    const range = api.caretRangeFromPoint(x, y);
    if (range) return { node: range.startContainer, offset: range.startOffset };
  }
  return null;
}

/** 从 `node` 出发，沿兄弟链找最近的文本节点；找不到回答 `null`。 */
export function nearestTextNode(node: Node | null): Node | null {
  if (!node) return null;
  if (node.nodeType === 3) return node;
  const walker = node.ownerDocument?.createTreeWalker?.(node, 4 /* NodeFilter.SHOW_TEXT */);
  if (!walker) return null;
  const inner = walker.nextNode();
  if (inner) return inner;
  let sibling = node.nextSibling;
  while (sibling) {
    if (sibling.nodeType === 3) return sibling;
    const innerSibling = sibling.ownerDocument?.createTreeWalker?.(sibling, 4)?.nextNode();
    if (innerSibling) return innerSibling;
    sibling = sibling.nextSibling;
  }
  return null;
}

/**
 * 在按下处放一个折叠选区，让随后的拖动从最近的正文延伸。
 *
 * 返回 `true` 表示选区已经放下；`false` 表示这一下什么都没做（量不到 caret、没有 selection API），
 * 调用方于是保留浏览器自己的行为，而不是假装成功。
 *
 * 只放折叠选区：拖动由浏览器继续，不需要自己实现拖动、自动滚动或命中测试。
 */
export function seedSelection(
  doc: Document | null | undefined,
  point: PointerPoint,
  caretAt: (doc: Document, x: number, y: number) => Caret | null = caretFromPoint,
): boolean {
  if (!doc || typeof doc.getSelection !== "function" || typeof doc.createRange !== "function") return false;
  const caret = caretAt(doc, point.x, point.y);
  if (!caret) return false;
  const node = caret.node.nodeType === 3 ? caret.node : nearestTextNode(caret.node);
  if (!node || node.nodeType !== 3) return false;
  const length = (node.textContent ?? "").length;
  const offset = Math.max(0, Math.min(caret.offset, length));
  const range = doc.createRange();
  try {
    range.setStart(node, offset);
  } catch {
    return false;
  }
  range.collapse(true);
  const sel = doc.getSelection();
  if (!sel) return false;
  sel.removeAllRanges();
  sel.addRange(range);
  return true;
}
