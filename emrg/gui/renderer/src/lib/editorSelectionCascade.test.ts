import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { EditorState } from "@codemirror/state";
import { EditorView, drawSelection, highlightActiveLine } from "@codemirror/view";
// 样式表要真的进文档：下面的断言读的是 `document.styleSheets` 里那些**发出的**规则，
// 不是源文件文本——上一版读文本，于是全绿地放过了这个毛病。
import "../cockpit.css";

/**
 * 选区画不出来的两个机制（issue #3208）。
 *
 * 一、级联。CodeMirror 的基础主题里，有焦点时那条选区规则是五个类
 * （`&light.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground`，
 * `&light` 在 mount 时展开成 `.cm-editor` 上那个生成的类名），我们原来是四个类，
 * 于是真正画出来的是它自己的淡紫 `#d7d4f0`，不是强调色。
 * 二、层叠。选区画在正文下面一层（marker 的 `z-index` 是 -1），当前行原来是不透明实色，
 * 而拖选总是收在光标那一行——最该看见的一段正好被盖住。
 *
 * jsdom 不算布局，也解不出 `color-mix()`（实测：带 `var()` 的声明读 `.background` 拿到空串，
 * `.cssText` 才留着原文），所以这里不问「算出来是什么颜色」，改问级联自己回答的那个问题：
 * **这个元素身上，最后选中哪一条声明**。选择器从挂起来的编辑器里读，`&light` 已展开，
 * 生成类名也在真实的 `.cm-editor` 上，比的因此是浏览器真看到的那两条。
 */

interface Rule {
  readonly selector: string;
  readonly css: string;
  /** 文档顺序：特异性相同时，靠后的那条赢 */
  readonly order: number;
}

/** 类选择器的个数。相关选择器都是纯类链，没有 id 和标签选择器，类数就是特异性。 */
function classCount(selector: string): number {
  // `\w` 只认 ASCII，而生成的主题类名是 `ͼ2` 这种非 ASCII 字符；按分隔符排除才数得准
  return (selector.match(/\.[^ >+~,.#:[]+/gu) ?? []).length;
}

/** 命中这个元素的每条声明，连同它的选择器和文档顺序。 */
function rulesMatching(element: Element): Rule[] {
  const found: Rule[] = [];
  let order = 0;
  for (const sheet of Array.from(document.styleSheets) as CSSStyleSheet[]) {
    let rules: CSSRuleList;
    try {
      rules = sheet.cssRules;
    } catch {
      // 读不到的样式表不是「干净」，是没读到：跳过，不静默当成空
      continue;
    }
    for (const rule of Array.from(rules) as CSSStyleRule[]) {
      const selectorText = rule.selectorText;
      if (!selectorText || !rule.style) continue;
      for (const part of selectorText.split(",")) {
        const selector = part.trim();
        const position = order++;
        if (!selector) continue;
        let hit = false;
        try {
          hit = element.matches(selector);
        } catch {
          // 解析不了的选择器按未命中处理，但不代表其它选择器没命中
          hit = false;
        }
        if (hit) found.push({ selector, css: rule.style.cssText, order: position });
      }
    }
  }
  return found;
}

/** 级联：特异性最高的赢；一样高时文档里靠后的赢。 */
function winner(element: Element): Rule | null {
  const matched = rulesMatching(element);
  if (matched.length === 0) return null;
  return matched.reduce((best, rule) => {
    const mine = classCount(rule.selector);
    const theirs = classCount(best.selector);
    if (mine !== theirs) return mine > theirs ? rule : best;
    return rule.order > best.order ? rule : best;
  });
}

const isOurs = (rule: Rule): boolean => rule.selector.includes(".editor-body");

/** `color-mix()` 里的百分比：两个强度就是两个数。 */
function strength(css: string): number {
  return Number.parseInt(/(\d+)%/u.exec(css)?.[1] ?? "0", 10);
}

let host: HTMLElement;
let view: EditorView;

beforeAll(() => {
  // `.editor-body` 是面板给编辑器套的那一层，样式表里每条规则都挂在它下面
  host = document.createElement("div");
  host.className = "editor-body";
  document.body.appendChild(host);
  view = new EditorView({
    state: EditorState.create({
      doc: "const a = 1;\nconst b = 2;\n",
      extensions: [drawSelection(), highlightActiveLine()],
    }),
    parent: host,
  });
});

afterAll(() => {
  view.destroy();
  host.remove();
});

/** 在真实的 `.cm-selectionLayer` 里放一个同类元素：祖先链是真的，生成类名也在真的 `.cm-editor` 上。 */
function selectionMarker(): HTMLElement {
  const layer = view.dom.querySelector(".cm-selectionLayer");
  expect(layer, "编辑器没有画出选区层：`drawSelection()` 没挂上").not.toBeNull();
  const marker = document.createElement("div");
  marker.className = "cm-selectionBackground";
  (layer as HTMLElement).appendChild(marker);
  return marker;
}

function activeLine(): HTMLElement {
  const content = view.dom.querySelector(".cm-content");
  expect(content, "编辑器没有内容层").not.toBeNull();
  const line = document.createElement("div");
  line.className = "cm-line cm-activeLine";
  (content as HTMLElement).appendChild(line);
  return line;
}

describe("选区画出来的是强调色，当前行盖不住它（issue #3208）", () => {
  it("有焦点时，级联选中强调色那条，不是基础主题的淡紫", () => {
    const marker = selectionMarker();
    view.dom.classList.add("cm-focused");
    const won = winner(marker);
    expect(won, "没有任何规则命中选区元素").not.toBeNull();
    expect(won?.css).toContain("var(--accent)");
    // 基础主题的淡紫 / 灰 / 深色一旦赢，画出来就是这三个值之一
    expect(won?.css).not.toMatch(/rgb\(215, 212, 240\)|rgb\(217, 217, 217\)|rgb\(34, 34, 34\)/u);
  });

  it("没有焦点时也是强调色，且比有焦点时淡", () => {
    const marker = selectionMarker();
    view.dom.classList.remove("cm-focused");
    const idle = winner(marker);
    view.dom.classList.add("cm-focused");
    const focused = winner(marker);
    expect(idle?.css).toContain("var(--accent)");
    expect(focused?.css).toContain("var(--accent)");
    expect(strength(focused?.css ?? "")).toBeGreaterThan(strength(idle?.css ?? ""));
  });

  it("两边各比一次：我们的选择器在有无焦点时都压过基础主题那条", () => {
    const marker = selectionMarker();
    for (const focused of [false, true]) {
      view.dom.classList.toggle("cm-focused", focused);
      const matched = rulesMatching(marker);
      const ours = matched.filter(isOurs);
      const base = matched.filter((rule) => !isOurs(rule));
      // 两边都得有命中，否则「压过」是拿空集比出来的
      expect(ours.length, `焦点=${String(focused)} 时没有命中我们的规则`).toBeGreaterThan(0);
      expect(base.length, `焦点=${String(focused)} 时没有命中基础主题的规则`).toBeGreaterThan(0);
      const max = (rules: Rule[]): number => Math.max(...rules.map((r) => classCount(r.selector)));
      expect(max(ours)).toBeGreaterThan(max(base));
    }
  });

  it("当前行是半透明的，而且仍然只声明 background-color", () => {
    const won = winner(activeLine());
    expect(won, "没有任何规则命中当前行").not.toBeNull();
    // 赢的必须是我们的那条：不透明的当前行会把下面那层选区盖死
    expect(won?.selector).toContain(".editor-body");
    expect(won?.css).toContain("var(--bg-hover)");
    expect(won?.css).toMatch(/transparent/u);
    // 简写 `background` 会把缩进参考线的 background-image 一起清掉
    expect(won?.css).toContain("background-color");
    expect(won?.css).not.toMatch(/(^|;)\s*background\s*:/u);
  });
});
