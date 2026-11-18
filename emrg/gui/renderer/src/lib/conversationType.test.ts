import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * conversationType.test.ts — the conversation area's type, resolved rather than declared.
 *
 * Issue #2980: the host measured the running app and found the area a step too large —
 * the user bubble rendering at 15px/1.7 (it carried no declaration of its own and
 * inherited the document body) while the assistant's reply to it rendered at 14px/1.55,
 * and each heading a further step above the body it leads.
 *
 * Why this file resolves a cascade instead of grepping for a declaration: "13.5px is
 * written somewhere" is satisfied by a rule that loses, and the mutation the issue asks
 * for — put an old value back — leaves such a rule in place and still green. So the
 * subjects here are:
 *
 * - **the winning value**: for each element, every declaration whose subject is that
 *   element is collected from the sheets `src/main.tsx` loads, in load order, and the
 *   one a browser would apply (highest specificity, then latest) is asserted. An `em`
 *   value is resolved against the body it inherits from, because `1em` and `13.5px` are
 *   the same answer to the host's question only when the body is 13.5px.
 * - **the absence of the old steps**: none of the values the issue lists as "before" may
 *   survive on a conversation element in any sheet. That is the half that makes
 *   restoring one old number turn this red, and it is the issue's own requirement that
 *   not one old size be left in the area.
 *
 * Approximation, stated rather than hidden: a selector applies when its subject (its
 * last compound) is the element. Every selector in this tree that styles these elements
 * is prefixed by `.react-shell` or `.msg-body`, and the element really sits under both,
 * so the prefix never decides. A stylesheet that introduced a prefix for another shell
 * would need this matcher revisited.
 */

const RENDERER = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

/** The sheets `src/main.tsx` loads, in its order (a later sheet wins a tie). */
const SHEETS = [
  "css/tokens.css",
  "css/base.css",
  "css/components.css",
  "css/layout.css",
  "css/animations.css",
  "src/shell.css",
  "src/cockpit.css",
].map((rel) => ({ rel, css: readFileSync(resolve(RENDERER, rel), "utf8") }));

interface Declaration {
  selector: string;
  prop: string;
  value: string;
  order: number;
  sheet: string;
}

function declarations(): Declaration[] {
  const out: Declaration[] = [];
  let order = 0;
  for (const { rel, css } of SHEETS) {
    const stripped = css.replace(/\/\*[\s\S]*?\*\//g, "");
    const block = /([^{}]+)\{([^{}]*)\}/g;
    let m: RegExpExecArray | null;
    while ((m = block.exec(stripped)) !== null) {
      const head = m[1].trim();
      if (!head || head.startsWith("@")) continue;
      for (const selector of head.split(",").map((s) => s.trim().replace(/\s+/g, " "))) {
        for (const piece of m[2].split(";")) {
          const at = piece.indexOf(":");
          if (at < 0) continue;
          const prop = piece.slice(0, at).trim();
          const value = piece.slice(at + 1).trim();
          if (!prop || !value) continue;
          out.push({ selector, prop, value, order: order++, sheet: rel });
        }
      }
    }
  }
  return out;
}

const DECLS = declarations();

/** ids=100, classes/attributes=10, elements=1 — enough to order every rule in this tree. */
function specificity(selector: string): number {
  const ids = (selector.match(/#[\w-]+/g) ?? []).length;
  const classes =
    (selector.match(/\.[\w-]+/g) ?? []).length + (selector.match(/\[[^\]]*\]/g) ?? []).length;
  const elements = selector
    .replace(/[.#][\w-]+(\([^)]*\))?/g, " ")
    .replace(/\[[^\]]*\]/g, " ")
    .split(/[\s>+~]+/)
    .filter((part) => /^[a-zA-Z]/.test(part)).length;
  return ids * 100 + classes * 10 + elements;
}

function applicable(subject: RegExp, prop: string): Declaration[] {
  return DECLS.filter((d) => d.prop === prop && subject.test(d.selector));
}

function winner(subject: RegExp, prop: string): Declaration | undefined {
  return applicable(subject, prop).sort(
    (a, b) => specificity(a.selector) - specificity(b.selector) || a.order - b.order,
  ).pop();
}

/** `1em` is an answer only once the size it inherits is known. */
function resolved(subject: RegExp, prop: string, parentPx: number | null): string | undefined {
  const won = winner(subject, prop);
  if (!won) return undefined;
  const em = /^([\d.]+)em$/.exec(won.value);
  if (!em || parentPx === null) return won.value;
  return `${Number((Number(em[1]) * parentPx).toFixed(2))}px`;
}

const USER_BUBBLE = /(^|\s)\.msg\.user$/;
const BODY = /(^|\s)\.msg-body$/;
const HEADING = /\.msg-body h[123]$/;
const TABLE = /\.msg-body table$/;
const CODE = /\.msg-body code$/;
const PARAGRAPH = /\.msg-body p$/;
const LIST_ITEM = /\.msg-body li$/;

/** The conversation area is 13.5px; the heading step and the fixed ones read against it. */
const BODY_PX = 13.5;

describe("#2980：对话区的字号，按层叠解析出来的值", () => {
  it("用户气泡 13.5px / 1.62 —— 有自己的声明，不再继承文档 body 的 15px", () => {
    expect(resolved(USER_BUBBLE, "font-size", null)).toBe("13.5px");
    expect(resolved(USER_BUBBLE, "line-height", null)).toBe("1.62");
    // 它现在有自己的声明：这正是「比助手正文还大一号」消失的原因。
    expect(applicable(USER_BUBBLE, "font-size").length).toBeGreaterThan(0);
  });

  it("助手正文 13.5px / 1.72", () => {
    expect(resolved(BODY, "font-size", null)).toBe("13.5px");
    expect(resolved(BODY, "line-height", null)).toBe("1.72");
  });

  it("h1 / h2 / h3 与正文同字号，只靠字重区分", () => {
    expect(resolved(HEADING, "font-size", BODY_PX)).toBe("13.5px");
    expect(winner(HEADING, "font-weight")?.value).toBe("600");
  });

  it("表格 12px、行内 code 12px、段落间距 11px、列表项 4px", () => {
    expect(resolved(TABLE, "font-size", BODY_PX)).toBe("12px");
    expect(resolved(CODE, "font-size", BODY_PX)).toBe("12px");
    expect(winner(PARAGRAPH, "margin")?.value).toBe("0 0 11px");
    expect(winner(LIST_ITEM, "margin")?.value).toBe("4px 0");
  });
});

describe("#2980：对话区不留一处旧档字号", () => {
  // 宿主给的那张表的「现在」一列：任何一个留在这里，都还是屏幕上量得到的旧档。
  const OLD = ["1.35em", "1.2em", "1.1em", "0.88em", "0.92em", "0.6em", "0.2em"];

  /**
   * 七条用例共用这一段断言是必要的，但**不能**写成一个循环：静态计数器只数行首的
   * `it(`/`test(`，循环里的一条会执行七次却只被数到一次，`Agent.md` 的总数会少于
   * vitest 实际跑的（`tests/test_doc_counts.py` 的 loop tripwire 就是钉这一点的）。
   */
  function assertNoOldValue(name: string, subject: RegExp): void {
    const leftovers = DECLS.filter(
      (d) => subject.test(d.selector) && OLD.includes(d.value),
    ).map((d) => `${d.sheet}: ${d.selector} { ${d.prop}: ${d.value} }`);
    expect(leftovers, `${name}的旧档字号还留在对话区：\n${leftovers.join("\n")}`).toEqual([]);
  }

  it("用户气泡没有留下旧值", () => {
    assertNoOldValue("用户气泡", USER_BUBBLE);
  });

  it("助手正文没有留下旧值", () => {
    assertNoOldValue("助手正文", BODY);
  });

  it("标题没有留下旧值", () => {
    assertNoOldValue("标题", HEADING);
  });

  it("表格没有留下旧值", () => {
    assertNoOldValue("表格", TABLE);
  });

  it("行内 code 没有留下旧值", () => {
    assertNoOldValue("行内 code", CODE);
  });

  it("段落没有留下旧值", () => {
    assertNoOldValue("段落", PARAGRAPH);
  });

  it("列表项没有留下旧值", () => {
    assertNoOldValue("列表项", LIST_ITEM);
  });
});
