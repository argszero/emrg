import { describe, expect, it } from "vitest";
import {
  DEFAULT_FONT_SIZE,
  DEFAULT_PREFS,
  EditorPrefsStore,
  LARGE_FILE_BYTES,
  MINIMAP_LINES,
  PLAIN_TEXT,
  catalogueNames,
  cursorPositionAt,
  diskChange,
  formatBytes,
  indentUnitFor,
  indentWidthFor,
  languageFor,
  languageSupportFor,
  isHtmlPath,
  lineEndingFor,
  lineForRatio,
  minimapLines,
  readFailureKey,
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

  it("从中间开始的那一页总是只读，哪怕它已经是最后一页", () => {
    // 最后一页的 truncated 是 false——确实没有下一页了——可它仍然只是文件的一段：
    // 放它可编辑，存回去就把十万行写成一千行（issue #3210）
    expect(readOnlyReason({ binary: false, truncated: false, size: 10, startLine: 2 })).toBe(
      "truncated",
    );
    expect(readOnlyReason({ binary: false, truncated: false, size: 10, startLine: 99001 })).toBe(
      "truncated",
    );
    // 第一行开始、也读完整了，才是可编辑的那一个
    expect(readOnlyReason({ binary: false, truncated: false, size: 10, startLine: 1 })).toBeNull();
    expect(readOnlyReason({ binary: false, truncated: false, size: 10 })).toBeNull();
    // 二进制还是最根本的那个理由
    expect(readOnlyReason({ binary: true, truncated: false, size: 10, startLine: 5 })).toBe(
      "binary",
    );
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
    expect(DEFAULT_PREFS).toEqual({ wrap: false, minimap: false, fontSize: DEFAULT_FONT_SIZE });
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
    expect(store.get("s1")).toEqual({
      wrap: false,
      minimap: true,
      fontSize: DEFAULT_FONT_SIZE,
    });
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

describe("minimapLines (issue #3200)", () => {
  it("画的是文件自己的行，不是每行长度的条形", () => {
    expect(minimapLines("const a = 1;\n\n  return a;")).toEqual([
      "const a = 1;",
      "",
      "  return a;",
    ]);
  });

  it("行尾空白不进图：带二十个尾随空格的一行与不带的那一行画出来一样", () => {
    expect(minimapLines("a   \nb\t\n")).toEqual(["a", "b", ""]);
  });

  it("长文件抽样而不是截断：文件末尾的行一样出现在图上", () => {
    const text = Array.from({ length: 4000 }, (_, i) => `line ${i}`).join("\n");
    const lines = minimapLines(text);
    expect(lines).toHaveLength(MINIMAP_LINES);
    // 抽样到的是文件末尾附近，不是第 600 行：截断会让每个大文件都长得像自己的第一页
    const last = Number(lines[lines.length - 1].split(" ")[1]);
    expect(last).toBeGreaterThan(3600);
    // 抽到的行仍然按文件顺序排列
    const first = Number(lines[0].split(" ")[1]);
    const second = Number(lines[1].split(" ")[1]);
    expect(second).toBeGreaterThan(first);
  });

  it("短文件一行一条，不凑数也不留空", () => {
    expect(minimapLines("a\nb\nc")).toHaveLength(3);
  });
});

describe("indentUnitFor / lineEndingFor (issue #3200)", () => {
  it("四空格的文件报 4，八空格是它的倍数，不算第二种缩进", () => {
    expect(indentUnitFor("def a():\n    if x:\n        return 1\n").width).toBe(4);
  });

  it("两空格的文件报 2，不因为有一行缩进更深就跳到 4", () => {
    expect(indentUnitFor("a\n  b\n    c\n").width).toBe(2);
  });

  it("制表符就是制表符：这类文件不问宽度，宽度是读者的制表位", () => {
    expect(indentUnitFor("a\n\tb\n\t\tc\n").kind).toBe("tabs");
  });

  it("没有缩进的文件给默认宽度，不是 0", () => {
    expect(indentUnitFor("a\nb\n")).toEqual({ kind: "spaces", width: 4 });
  });

  it("全是视觉对齐（5 / 9 / 13）时回落到默认宽度，不报 1", () => {
    expect(indentWidthFor([5, 9, 13])).toBe(4);
  });

  it("CRLF 必须先判：CRLF 里也含 CR，先问 CR 会把每个 Windows 文件都报成裸 CR", () => {
    expect(lineEndingFor("a\r\nb\r\n")).toBe("crlf");
    expect(lineEndingFor("a\nb\n")).toBe("lf");
    expect(lineEndingFor("a\rb\r")).toBe("cr");
    // 混着的时候说的是各行的行尾，不是文中那一个孤立的 CR
    expect(lineEndingFor("a\r\nb\rc\r\n")).toBe("crlf");
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

describe("readFailureKey", () => {
  it("每个失败码有自己的那一句，而且互不相同：两码同句等于没分", () => {
    const codes = ["not_found", "is_directory", "not_regular", "unreadable", "window_invalid"];
    const keys = codes.map((code) => readFailureKey(code));
    expect(keys).toEqual([
      "editor.failed.notFound",
      "editor.failed.isDirectory",
      "editor.failed.notRegular",
      "editor.failed.unreadable",
      "editor.failed.window",
    ]);
    expect(new Set(keys).size).toBe(keys.length);
    // 通用那一句不是其中任何一码的答案：否则分码就是分了个寂寞
    expect(keys).not.toContain("editor.failed");
  });

  it("认不出的码与没有码都回落成通用那一句：新版本的服务端不该让这里读不出话", () => {
    expect(readFailureKey("something_new")).toBe("editor.failed");
    expect(readFailureKey(null)).toBe("editor.failed");
    expect(readFailureKey(undefined)).toBe("editor.failed");
    expect(readFailureKey("")).toBe("editor.failed");
  });
});

describe("formatBytes", () => {
  it("按读取者会扫的单位报，不是一长串数字", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(999)).toBe("999 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(4 * 1024 * 1024)).toBe("4.0 MB");
    expect(formatBytes(54840525)).toBe("52.3 MB");
    expect(formatBytes(3 * 1024 ** 4)).toBe("3.0 TB");
    // 到顶之后不往上造单位：PB 也好，再大也好，都还在 TB 这一档里
    expect(formatBytes(2048 * 1024 ** 4)).toBe("2048.0 TB");
  });

  it("量不出来的时候不给数字，也不给负号", () => {
    expect(formatBytes(Number.NaN)).toBe("");
    expect(formatBytes(Number.POSITIVE_INFINITY)).toBe("");
    expect(formatBytes(-1)).toBe("");
  });
});

describe("isHtmlPath", () => {
  it("只把 .html / .htm 当页面，长得像的不算", () => {
    expect(isHtmlPath("/p/index.html")).toBe(true);
    expect(isHtmlPath("/p/a.htm")).toBe(true);
    // 大小写不改变它是哪一种文件
    expect(isHtmlPath("/p/INDEX.HTML")).toBe(true);
    // 逐条都是「看起来像」的那个邻居：xhtml 是另一种格式，名字里带 html 的是纯文本，
    // 备份文件是 html 的上一版，以点结尾的没有扩展名。
    expect(isHtmlPath("/p/a.xhtml")).toBe(false);
    expect(isHtmlPath("/p/html-notes.txt")).toBe(false);
    expect(isHtmlPath("/p/a.html.bak")).toBe(false);
    expect(isHtmlPath("/p/a.")).toBe(false);
    expect(isHtmlPath("/p/notes")).toBe(false);
  });
});
