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

/** `node` 子树里最后一段字；没有文字回答 `null`。 */
function lastTextNodeIn(node: Node | null): Node | null {
  if (!node) return null;
  if (node.nodeType === 3) return node;
  const walker = node.ownerDocument?.createTreeWalker?.(node, 4 /* NodeFilter.SHOW_TEXT */);
  if (!walker) return null;
  let last: Node | null = null;
  for (let n = walker.nextNode(); n; n = walker.nextNode()) last = n;
  return last;
}

/**
 * 把 `(node, offset)` 这个 caret 换成「字节点 + 字偏移」。
 *
 * 分开写是因为两者不是同一种偏移：`caretPositionFromPoint` 落在**元素**上时，`offset` 是
 * 「第几个孩子」的缝（0..childCount），不是字偏移。以前 `seedSelection` 把元素换成它里面
 * 第一个文本节点、却继续用那个孩子序号当字偏移——点在行尾留白、两块行内盒之间这类地方时，
 * 缝的序号与字的位置没有关系，锚点就落到**这一段开头**，拖动于是从段首选到当前
 * （宿主报障 2026-11-23「想选中一句话，变成了选择从开头到当前」）。
 *
 * 所以元素上的缝按它的字面意思解析：缝后面那段字的开头；后面再没有字就落在元素里最后一段
 * 字的末尾。文本节点照旧，越界夹进长度。
 */
export function textPositionAt(node: Node, offset: number): { node: Node; offset: number } | null {
  if (!node) return null;
  if (node.nodeType === 3) {
    const length = (node.textContent ?? "").length;
    return { node, offset: Math.max(0, Math.min(offset, length)) };
  }
  if (node.nodeType !== 1) return null;
  const children = node.childNodes;
  const index = Math.max(0, Math.min(offset, children.length));
  // 缝之后的第一段字：`nearestTextNode` 会沿兄弟链找，正是「这之后」的意思
  const after = index < children.length ? nearestTextNode(children[index]) : null;
  if (after) return { node: after, offset: 0 };
  // 缝在末尾（或其后已无文字）：元素里最后一段字的末尾
  for (let i = Math.min(index, children.length) - 1; i >= 0; i -= 1) {
    const before = lastTextNodeIn(children[i]);
    if (before) return { node: before, offset: (before.textContent ?? "").length };
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
  // 元素上的 offset 是孩子序号，换节点时必须一起换算——否则锚点会落到这一段开头
  const at = textPositionAt(caret.node, caret.offset);
  if (!at) return false;
  const range = doc.createRange();
  try {
    range.setStart(at.node, at.offset);
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
