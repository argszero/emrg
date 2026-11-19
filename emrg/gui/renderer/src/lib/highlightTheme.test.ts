import { describe, expect, it } from "vitest";

/**
 * highlightTheme.test.ts — 代码块高亮的两套皮肤（issue #3013）。
 *
 * 这个文件读的是**真实样式表**，不是组件树：主题颜色住在 `css/tokens.css` 的四个块里，
 * 规则表在 `css/highlight.css`。断言的判据是 WCAG 对比度，算出来的数写进失败信息，
 * 于是「哪个 token 掉到线下」一眼可见，而不是笼统的一句不达标。
 *
 * 读取方式沿用 conversationType.test.ts 的约定：走 Vite 的 `?raw`，不用 `node:fs`——
 * 渲染层没有 `@types/node`，一个 `node:` 导入会让干净环境下的 `npm run typecheck` 变红
 * （tests/test_renderer_node_types.py 守着这条）。
 */
const RAW = import.meta.glob(["../../css/*.css"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

function sheet(key: string): string {
  const css = RAW[key];
  if (typeof css !== "string" || css.trim() === "") {
    // 空字符串是「没读到」，不是「没有样式」：底下每条断言都会在空文本上真空通过。
    throw new Error(`${key} was not read — the glob found: ${Object.keys(RAW).sort().join(", ")}`);
  }
  return css;
}

const TOKENS = sheet("../../css/tokens.css");
const HIGHLIGHT = sheet("../../css/highlight.css");

/** 剔除注释，免得注释里的色值被当成声明。 */
function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

interface Block {
  selector: string;
  body: string;
}

/** 展平一层 `@media`：返回每个选择器自己的声明块。 */
function blocks(css: string): Block[] {
  const text = stripComments(css);
  const out: Block[] = [];
  let i = 0;
  let selector = "";
  while (i < text.length) {
    const open = text.indexOf("{", i);
    if (open < 0) break;
    const head = text.slice(i, open).trim();
    // 找到与这个 `{` 配对的 `}`
    let depth = 0;
    let j = open;
    for (; j < text.length; j += 1) {
      if (text[j] === "{") depth += 1;
      else if (text[j] === "}") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    const body = text.slice(open + 1, j);
    if (head.startsWith("@media")) {
      out.push(...blocks(body));
    } else if (head) {
      out.push({ selector: head, body });
    }
    i = j + 1;
    selector = "";
  }
  void selector;
  return out;
}

/** 一个块里的 `--name: value` 对。 */
function vars(block: Block): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of block.body.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    out[m[1]] = m[2].trim();
  }
  return out;
}

const ALL = blocks(TOKENS);
function blockFor(selector: string): Record<string, string> {
  const found = ALL.filter((b) => b.selector === selector);
  expect(found.length, `tokens.css 里应恰好有一个 "${selector}" 块`).toBe(1);
  return vars(found[0]);
}

//: 浅色默认（:root）、系统深色、手动深色、手动浅色 —— 与 tokens.css 现有的四块同构。
const LIGHT = blockFor(":root");
const DARK_SYSTEM = blockFor(':root:not([data-theme="light"])');
const DARK_FORCED = blockFor(':root[data-theme="dark"]');
const LIGHT_FORCED = blockFor(':root[data-theme="light"]');

/** 高亮变量：所有 --hl-* 的名字。 */
const HL_NAMES = Object.keys(LIGHT)
  .filter((n) => n.startsWith("--hl-"))
  .sort();

const HEX = /^#[0-9a-fA-F]{6}$/;

function rgb(hex: string): [number, number, number] {
  expect(HEX.test(hex), `${hex} 不是 6 位十六进制色值`).toBe(true);
  return [
    parseInt(hex.slice(1, 3), 16),
    parseInt(hex.slice(3, 5), 16),
    parseInt(hex.slice(5, 7), 16),
  ];
}

function channel(c: number): number {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const [r, g, b] = rgb(hex);
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/** WCAG 2.x 对比度，1–21，与顺序无关。 */
function contrast(a: string, b: string): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** 正文类高亮阈值；注释按 WCAG 的辅助文本一档。 */
const AA_BODY = 4.5;
const AA_COMMENT = 3;

describe("issue #3013：代码块高亮的变量层", () => {
  it("四个皮肤块定义同一套 --hl-*，一个都不缺", () => {
    expect(HL_NAMES.length, "tokens.css 里没有 --hl-* 变量").toBeGreaterThan(0);
    for (const [label, v] of [
      ["dark system", DARK_SYSTEM],
      ["dark forced", DARK_FORCED],
      ["light forced", LIGHT_FORCED],
    ] as const) {
      expect(Object.keys(v).filter((n) => n.startsWith("--hl-")).sort(), `${label} 块`).toEqual(
        HL_NAMES,
      );
    }
  });

  it("手动浅色 = 默认浅色，手动深色 = 系统深色（同一个选择只有一种结果）", () => {
    for (const name of HL_NAMES) {
      expect(LIGHT_FORCED[name], `${name}：手动浅色应与默认浅色逐字相同`).toBe(LIGHT[name]);
      expect(DARK_FORCED[name], `${name}：手动深色应与系统深色逐字相同`).toBe(DARK_SYSTEM[name]);
    }
  });

  it("浅色与深色逐项不同：换皮肤真的换颜色", () => {
    for (const name of HL_NAMES) {
      expect(LIGHT[name], `${name} 在两套皮肤下是同一个值，皮肤没有生效`).not.toBe(
        DARK_SYSTEM[name],
      );
    }
  });

  it("深色的每一项都还是改动前的 GitHub Dark 取值（观感不变）", () => {
    // 逐项写进断言：删掉的 vendor/highlight.github-dark.css 里原本就是这些值。
    const expected: Record<string, string> = {
      "--hl-bg": "#0d1117",
      "--hl-text": "#c9d1d9",
      "--hl-keyword": "#ff7b72",
      "--hl-entity": "#d2a8ff",
      "--hl-constant": "#79c0ff",
      "--hl-string": "#a5d6ff",
      "--hl-variable": "#ffa657",
      "--hl-comment": "#8b949e",
      "--hl-tag": "#7ee787",
      "--hl-section": "#1f6feb",
      "--hl-bullet": "#f2cc60",
      "--hl-add-fg": "#aff5b4",
      "--hl-add-bg": "#033a16",
      "--hl-del-fg": "#ffdcd7",
      "--hl-del-bg": "#67060c",
    };
    expect(DARK_SYSTEM, "深色变量的清单本身变了").toMatchObject(expected);
  });

  it("浅色下每种高亮文字都读得清：逐个 token 对其实际底色算对比度", () => {
    const board = LIGHT["--hl-bg"];
    const table: [string, string, number][] = [
      ["text", LIGHT["--hl-text"], AA_BODY],
      ["string", LIGHT["--hl-string"], AA_BODY],
      ["constant", LIGHT["--hl-constant"], AA_BODY],
      ["keyword", LIGHT["--hl-keyword"], AA_BODY],
      ["variable", LIGHT["--hl-variable"], AA_BODY],
      ["entity", LIGHT["--hl-entity"], AA_BODY],
      ["tag", LIGHT["--hl-tag"], AA_BODY],
      ["section", LIGHT["--hl-section"], AA_BODY],
      ["bullet", LIGHT["--hl-bullet"], AA_BODY],
      ["comment", LIGHT["--hl-comment"], AA_COMMENT],
    ];
    const failures = table
      .map(([name, fg, min]) => ({ name, fg, min, ratio: contrast(fg, board) }))
      .filter((r) => r.ratio < r.min)
      .map((r) => `${r.name} ${r.fg} on ${board} = ${r.ratio.toFixed(2)}:1 < ${r.min}:1`);
    expect(failures, failures.join("\n")).toEqual([]);
  });

  it("浅色下 diff 是浅底深字，且两行都过 AA", () => {
    for (const [label, fgName, bgName] of [
      ["新增", "--hl-add-fg", "--hl-add-bg"],
      ["删除", "--hl-del-fg", "--hl-del-bg"],
    ] as const) {
      const fg = LIGHT[fgName];
      const bg = LIGHT[bgName];
      expect(luminance(bg), `${label}行的底色 ${bg} 不是浅色（浅色的亮度应在 0.5 以上）`)
        .toBeGreaterThan(0.5);
      expect(luminance(fg), `${label}行的文字 ${fg} 不比底色深`).toBeLessThan(luminance(bg));
      expect(contrast(fg, bg), `${label}行 ${fg} on ${bg}`).toBeGreaterThanOrEqual(AA_BODY);
    }
  });
});

describe("issue #3013：规则表只引用变量，不写死颜色", () => {
  const body = stripComments(HIGHLIGHT);

  it("highlight.css 里没有任何字面色值", () => {
    const literals = [
      ...body.matchAll(/#[0-9a-fA-F]{3,8}\b/g),
      ...body.matchAll(/\brgba?\(/g),
      ...body.matchAll(/\bhsla?\(/g),
    ].map((m) => m[0]);
    expect(literals, `这些字面色值应改为 tokens.css 的 --hl-* 变量: ${literals.join(", ")}`).toEqual(
      [],
    );
  });

  it("用到的每个 --hl-* 都有定义，且都在 tokens.css 里", () => {
    const used = new Set([...body.matchAll(/var\((--hl-[a-z-]+)\)/g)].map((m) => m[1]));
    expect(used.size, "一条 var(--hl-*) 都没有：规则表没接上变量层").toBeGreaterThan(0);
    const undefinedNames = [...used].filter((n) => !(n in LIGHT)).sort();
    expect(undefinedNames, `未定义的变量: ${undefinedNames.join(", ")}`).toEqual([]);
  });

  it("vendor 主题里每一类 token 都还在规则表里被染色（没有掉规则）", () => {
    // 取自被删除的 vendor/highlight.github-dark.css：漏掉任何一类，那类 token 就会退回正文色，
    // 屏幕上表现为「某种语言突然没高亮了」。
    const required = [
      ".hljs-doctag",
      ".hljs-keyword",
      ".hljs-template-tag",
      ".hljs-template-variable",
      ".hljs-type",
      ".hljs-title",
      ".hljs-title.class_",
      ".hljs-title.function_",
      ".hljs-attr",
      ".hljs-attribute",
      ".hljs-literal",
      ".hljs-meta",
      ".hljs-number",
      ".hljs-operator",
      ".hljs-variable",
      ".hljs-selector-attr",
      ".hljs-selector-class",
      ".hljs-selector-id",
      ".hljs-regexp",
      ".hljs-string",
      ".hljs-built_in",
      ".hljs-symbol",
      ".hljs-comment",
      ".hljs-code",
      ".hljs-formula",
      ".hljs-name",
      ".hljs-quote",
      ".hljs-selector-tag",
      ".hljs-selector-pseudo",
      ".hljs-subst",
      ".hljs-section",
      ".hljs-bullet",
      ".hljs-emphasis",
      ".hljs-strong",
      ".hljs-addition",
      ".hljs-deletion",
    ];
    const selectors = blocks(HIGHLIGHT).flatMap((b) => b.selector.split(",").map((s) => s.trim()));
    const missing = required.filter((cls) => !selectors.includes(cls));
    expect(missing, `这些 token 类不再有颜色规则: ${missing.join(", ")}`).toEqual([]);
  });

  it("diff 两行的颜色来自变量而非 vendor 主题", () => {
    const addition = blocks(HIGHLIGHT).find((b) => b.selector === ".hljs-addition");
    const deletion = blocks(HIGHLIGHT).find((b) => b.selector === ".hljs-deletion");
    expect(addition, ".hljs-addition 规则不存在").toBeDefined();
    expect(deletion, ".hljs-deletion 规则不存在").toBeDefined();
    expect(addition!.body).toContain("var(--hl-add-bg)");
    expect(addition!.body).toContain("var(--hl-add-fg)");
    expect(deletion!.body).toContain("var(--hl-del-bg)");
    expect(deletion!.body).toContain("var(--hl-del-fg)");
  });
});
