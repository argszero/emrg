import { describe, expect, it } from "vitest";
import {
  baseName,
  extensionOf,
  iconKind,
  neighbourRow,
  rowsFor,
  sortEntries,
  type DirMap,
  type TreeEntry,
} from "./fileTree";

/**
 * fileTree.test.ts — the rules the two trees are drawn by (issue #3026).
 *
 * Each assertion is a requirement the issue names, held where it can be tested without
 * a window: the order entries are listed in, what an open folder contributes, which
 * icon kinds exist, and where a keyboard move lands.
 */

const e = (name: string, type: "dir" | "file" = "file"): TreeEntry => ({
  name,
  path: `/r/${name}`,
  type,
});

const loaded = (entries: TreeEntry[]): DirMap => ({
  "/r": { status: "loaded", entries, error: null, truncated: false },
});

describe("sortEntries", () => {
  it("目录在前、文件在后，各自按名字排", () => {
    const input = [e("b.md"), e("z", "dir"), e("a.md"), e("a", "dir")];
    expect(sortEntries(input).map((x) => x.name)).toEqual(["a", "z", "a.md", "b.md"]);
  });

  it("点开头的条目照常参与排序，不被藏起来", () => {
    const input = [e(".emrg", "dir"), e("src", "dir"), e(".gitignore")];
    expect(sortEntries(input).map((x) => x.name)).toEqual([".emrg", "src", ".gitignore"]);
  });
});

describe("rowsFor", () => {
  it("根没给就不出行", () => {
    expect(rowsFor(null, loaded([e("a")]), new Set())).toEqual([]);
  });

  it("只有展开且已读的目录才给出子项", () => {
    const dirs: DirMap = {
      "/r": {
        status: "loaded",
        entries: [e("sub", "dir"), e("a.md")],
        error: null,
        truncated: false,
      },
    };
    // 末级已读 → 一行
    expect(rowsFor("/r", dirs, new Set()).map((r) => r.name)).toEqual(["sub", "a.md"]);
    // 展开但子级还没读到 → 不画空子项（那是把「还没读」说成「是空的」）
    expect(rowsFor("/r", dirs, new Set(["/r/sub"])).map((r) => r.name)).toEqual(["sub", "a.md"]);
    // 读到之后才出现，层级加一
    dirs["/r/sub"] = { status: "loaded", entries: [e("deep.md")], error: null, truncated: false };
    const rows = rowsFor("/r", dirs, new Set(["/r/sub"]));
    expect(rows.map((r) => r.name)).toEqual(["sub", "deep.md", "a.md"]);
    expect(rows.map((r) => r.depth)).toEqual([0, 1, 0]);
    expect(rows[0].expanded).toBe(true);
  });
});

describe("extensionOf / iconKind", () => {
  it("扩展名取最后一个点之后，点开头或没有点的不算扩展名", () => {
    expect(extensionOf("a.md")).toBe("md");
    expect(extensionOf("a.b.JSON")).toBe("json");
    expect(extensionOf(".gitignore")).toBe("");
    expect(extensionOf("trailing.")).toBe("");
    expect(extensionOf("plain")).toBe("");
  });

  it("目录给闭合/展开两态，文件按扩展名分，未知扩展名落到通用字形", () => {
    expect(iconKind({ name: "src", type: "dir", expanded: false })).toBe("dir");
    expect(iconKind({ name: "src", type: "dir", expanded: true })).toBe("dir-open");
    expect(iconKind({ name: "a.md", type: "file", expanded: false })).toBe("markdown");
    expect(iconKind({ name: "a.log", type: "file", expanded: false })).toBe("text");
    expect(iconKind({ name: "a.json", type: "file", expanded: false })).toBe("json");
    expect(iconKind({ name: "a.tsx", type: "file", expanded: false })).toBe("code");
    expect(iconKind({ name: "a.png", type: "file", expanded: false })).toBe("image");
    expect(iconKind({ name: "a.xyz", type: "file", expanded: false })).toBe("file");
  });
});

describe("neighbourRow", () => {
  const dirs: DirMap = loaded([e("a"), e("b")]);
  const rows = rowsFor("/r", dirs, new Set());

  it("没有选中时，下移落到第一行、上移落到最后一行", () => {
    expect(neighbourRow(rows, null, 1)).toBe("/r/a");
    expect(neighbourRow(rows, null, -1)).toBe("/r/b");
  });

  it("到头后绕回另一端", () => {
    expect(neighbourRow(rows, "/r/b", 1)).toBe("/r/a");
    expect(neighbourRow(rows, "/r/a", -1)).toBe("/r/b");
  });
});

describe("baseName", () => {
  it("取最后一段，尾部分隔符不算", () => {
    expect(baseName("/a/b/c")).toBe("c");
    expect(baseName("/a/b/")).toBe("b");
    expect(baseName("c")).toBe("c");
  });
});
