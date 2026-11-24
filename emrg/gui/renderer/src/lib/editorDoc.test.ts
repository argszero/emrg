import { describe, expect, it } from "vitest";
import {
  DEFAULT_PREFS,
  EditorPrefsStore,
  LARGE_FILE_BYTES,
  MINIMAP_BARS,
  PLAIN_TEXT,
  catalogueNames,
  cursorPositionAt,
  diskChange,
  languageFor,
  languageSupportFor,
  lineForRatio,
  minimapBars,
  readOnlyReason,
} from "./editorDoc";
import { EditorState } from "@codemirror/state";
import { syntaxTree } from "@codemirror/language";

/**
 * The editor's rules, one test per rule that has a wrong-but-plausible neighbour
 * (rant 2026-11-20T18:01:52).
 */

describe("languageFor (item 2)", () => {
  it("按扩展名认出语言：py / tsx / yml / rs 各归各的", () => {
    expect(languageFor("/p/a.py").name).toBe("Python");
    expect(languageFor("/p/a.tsx").name).toBe("TSX");
    expect(languageFor("/p/a.yml").name).toBe("YAML");
    expect(languageFor("/p/a.rs").name).toBe("Rust");
  });

  it("大写扩展名一样认：A.PY 与 a.py 是同一门语言", () => {
    expect(languageFor("/p/A.PY").name).toBe("Python");
  });

  it("认不出的扩展名是纯文本，不报错也不猜一门语言", () => {
    expect(languageFor("/p/a.zzz")).toEqual(PLAIN_TEXT);
    expect(languageFor("/p/notes")).toEqual(PLAIN_TEXT);
    // 隐藏文件没有扩展名，不能把「.bashrc」的 bash 当后缀
    expect(languageFor("/p/.bashrc")).toEqual(PLAIN_TEXT);
    expect(languageFor("/p/a.")).toEqual(PLAIN_TEXT);
  });

  it("表里每个名字都是装着的目录里真有的语言", () => {
    const installed = new Set(catalogueNames());
    const extensions = [
      "py", "js", "mjs", "cjs", "jsx", "ts", "tsx", "json", "md", "markdown", "html",
      "htm", "css", "sh", "bash", "zsh", "yml", "yaml", "toml", "rs", "go", "java", "sql",
      "xml",
    ];
    for (const ext of extensions) {
      const choice = languageFor(`/p/a.${ext}`);
      expect(choice.name, ext).not.toBeNull();
      expect(installed.has(choice.name as string), `${ext} → ${choice.name}`).toBe(true);
    }
  });
});

describe("languageSupportFor", () => {
  it("纯文本没有着色扩展，也不去加载任何语法", async () => {
    await expect(languageSupportFor(PLAIN_TEXT)).resolves.toBeNull();
  });

  it("目录里没有的名字也只是没有着色，不抛错", async () => {
    await expect(languageSupportFor({ name: "NoSuchLanguage" })).resolves.toBeNull();
  });

  it("认识的语言能取到扩展", async () => {
    await expect(languageSupportFor({ name: "Python" })).resolves.not.toBeNull();
  });

  it("拿到的扩展真的装上语法：文档被解析，注释与缩进都在", async () => {
    // 非空不足以说明什么 —— `LanguageSupport.support` 也是非空的对象，装上去却什么也不解析。
    // 所以这里把扩展真装进一个 state，问的是「这份文档解析了吗」，不是「拿到了东西吗」。
    const support = await languageSupportFor({ name: "Python" });
    const state = EditorState.create({
      doc: "if True:\n    pass",
      extensions: support ? [support] : [],
    });
    expect(state.languageDataAt("commentTokens", 0).length).toBeGreaterThan(0);
    expect(syntaxTree(state).toString()).toContain("IfStatement");
  });
});

describe("readOnlyReason (item 12)", () => {
  it("二进制、只读到开头、超过阈值，各有各的理由", () => {
    expect(readOnlyReason({ binary: true, truncated: false, size: 10 })).toBe("binary");
    expect(readOnlyReason({ binary: false, truncated: true, size: 10 })).toBe("truncated");
    expect(
      readOnlyReason({ binary: false, truncated: false, size: LARGE_FILE_BYTES + 1 }),
    ).toBe("large");
  });

  it("阈值之内、阈值本身、尺寸没量到，都不是只读", () => {
    expect(readOnlyReason({ binary: false, truncated: false, size: 10 })).toBeNull();
    expect(
      readOnlyReason({ binary: false, truncated: false, size: LARGE_FILE_BYTES }),
    ).toBeNull();
    // 没量到尺寸是「不知道」，不是「太大」：拿不到 stat 不该让文件变成只读
    expect(readOnlyReason({ binary: false, truncated: false, size: null })).toBeNull();
  });

  it("二进制优先于其它理由：三个都对时说的是最根本的那个", () => {
    expect(
      readOnlyReason({ binary: true, truncated: true, size: LARGE_FILE_BYTES * 2 }),
    ).toBe("binary");
  });
});

describe("diskChange (item 5)", () => {
  const stamp = { mtime: 100, size: 10 };

  it("mtime 变了就是变了，size 变了也是", () => {
    expect(diskChange(stamp, { mtime: 101, size: 10 })).toBe("changed");
    expect(diskChange(stamp, { mtime: 100, size: 11 })).toBe("changed");
  });

  it("文件没了是 deleted，与「变了」是两句话", () => {
    expect(diskChange(stamp, { exists: false, mtime: null, size: null })).toBe("deleted");
  });

  it("一模一样就是没变", () => {
    expect(diskChange(stamp, { mtime: 100, size: 10 })).toBeNull();
  });

  it("没量到的一侧不算改动：探测失败不该弹「磁盘版本已变」", () => {
    expect(diskChange(stamp, { mtime: null, size: null })).toBeNull();
    expect(diskChange(null, { mtime: 200, size: 20 })).toBeNull();
    expect(diskChange(stamp, null)).toBeNull();
    // 一侧量到、另一侧没量到：只有量到的那对比得了才算数
    expect(diskChange({ mtime: null, size: 10 }, { mtime: 5, size: 10 })).toBeNull();
  });
});

describe("EditorPrefsStore (item 8)", () => {
  it("默认不折行、缩略图关", () => {
    const store = new EditorPrefsStore();
    expect(store.get("s1")).toEqual(DEFAULT_PREFS);
    expect(DEFAULT_PREFS).toEqual({ wrap: false, minimap: false });
  });

  it("按会话各记各的：s1 打开折行不影响 s2", () => {
    const store = new EditorPrefsStore();
    store.set("s1", { wrap: true });
    expect(store.get("s1").wrap).toBe(true);
    expect(store.get("s2").wrap).toBe(false);
  });

  it("改一个开关不动另一个", () => {
    const store = new EditorPrefsStore();
    store.set("s1", { minimap: true });
    expect(store.get("s1")).toEqual({ wrap: false, minimap: true });
  });

  it("两个面板不共享：一个 store 的设置不会出现在另一个 store 里", () => {
    const a = new EditorPrefsStore();
    const b = new EditorPrefsStore();
    a.set("s1", { minimap: true });
    expect(b.get("s1").minimap).toBe(false);
  });
});

describe("cursorPositionAt (item 10)", () => {
  const doc = {
    lineAt(offset: number) {
      const starts = [0, 6, 11];
      let i = 2;
      while (i > 0 && starts[i] > offset) i -= 1;
      return { number: i + 1, from: starts[i] };
    },
  };

  it("行列都从 1 开始数", () => {
    expect(cursorPositionAt(doc, 0)).toEqual({ line: 1, column: 1 });
    expect(cursorPositionAt(doc, 3)).toEqual({ line: 1, column: 4 });
    expect(cursorPositionAt(doc, 6)).toEqual({ line: 2, column: 1 });
    expect(cursorPositionAt(doc, 8)).toEqual({ line: 2, column: 3 });
  });
});

describe("minimapBars (item 8)", () => {
  it("长文件也画得下：条数被条带上限压住，文档再长也不涨", () => {
    const fourThousand = minimapBars(
      Array.from({ length: 4000 }, (_, i) => `line ${i}`).join("\n"),
    );
    const fortyThousand = minimapBars(
      Array.from({ length: 40000 }, (_, i) => `line ${i}`).join("\n"),
    );
    // 上限是「不超过」：按 ceil 分组时装不满整条带，所以钉死等于 120 反而是错的口径
    expect(fourThousand.length).toBeLessThanOrEqual(MINIMAP_BARS);
    expect(fortyThousand.length).toBeLessThanOrEqual(MINIMAP_BARS);
    // 十倍的文档，条数不涨
    expect(fortyThousand.length).toBeLessThanOrEqual(fourThousand.length + 2);
  });

  it("聚合而不是截断：文件末尾的长行在图上一样看得见", () => {
    const text = `${"x\n".repeat(119)}${"y".repeat(400)}`;
    const bars = minimapBars(text);
    // 最后一条代表文件末尾，宽度满格
    expect(bars[bars.length - 1].width).toBeCloseTo(1, 5);
  });

  it("宽度按文档自己最长的一行归一：短文件也用满整列", () => {
    const bars = minimapBars("ab\nabcd");
    expect(Math.max(...bars.map((b) => b.width))).toBeCloseTo(1, 5);
  });

  it("空行是空行：没内容的位置不画，只留空白的行也算空", () => {
    const bars = minimapBars("abc\n\n   \nxyz");
    expect(bars.map((b) => b.filled)).toEqual([true, false, false, true]);
  });

  it("没有内容的文档给空图，不给 120 条零宽", () => {
    expect(minimapBars("")).toEqual([]);
    expect(minimapBars("\n\n  \n")).toEqual([]);
  });

  it("短文件一行一条，不硬凑 120 条", () => {
    expect(minimapBars("a\nb\nc")).toHaveLength(3);
  });
});

describe("lineForRatio", () => {
  it("从 1 数起，两头都夹住：点最底下不掉到文件外", () => {
    expect(lineForRatio(0, 100)).toBe(1);
    expect(lineForRatio(0.5, 100)).toBe(51);
    expect(lineForRatio(1, 100)).toBe(100);
    expect(lineForRatio(-3, 100)).toBe(1);
    expect(lineForRatio(9, 100)).toBe(100);
  });

  it("空文档落在第一行", () => {
    expect(lineForRatio(0.5, 0)).toBe(1);
  });
});
