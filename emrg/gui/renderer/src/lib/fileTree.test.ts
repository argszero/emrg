import { describe, expect, it } from "vitest";
import { chevronFor, iconFor, ICONS, rootNameFrom, sortEntries, type FileEntry } from "./fileTree";

/**
 * fileTree.test.ts — 文件树纯逻辑测试（Batch 3，镜像 vanilla file-tree.js）。
 * 断言：图标映射（含未知扩展名回退）、目录开合图标、根名解析、排序（目录在前）。
 */

describe("iconFor", () => {
  it("目录 → 开/合文件夹图标", () => {
    expect(iconFor({ type: "dir", name: "src" }, true)).toBe(ICONS.dirOpen);
    expect(iconFor({ type: "dir", name: "src" }, false)).toBe(ICONS.dirClosed);
  });

  it("扩展名映射：js/ts → fileCode，md → fileMd，json → fileJson", () => {
    expect(iconFor({ type: "file", name: "app.js" }, false)).toBe(ICONS.fileCode);
    expect(iconFor({ type: "file", name: "main.ts" }, false)).toBe(ICONS.fileCode);
    expect(iconFor({ type: "file", name: "README.md" }, false)).toBe(ICONS.fileMd);
    expect(iconFor({ type: "file", name: "pkg.json" }, false)).toBe(ICONS.fileJson);
  });

  it("图片/文本/yml 扩展名映射", () => {
    expect(iconFor({ type: "file", name: "a.png" }, false)).toBe(ICONS.fileImg);
    expect(iconFor({ type: "file", name: "b.log" }, false)).toBe(ICONS.fileTxt);
    expect(iconFor({ type: "file", name: "c.yml" }, false)).toBe(ICONS.fileYml);
  });

  it("未知扩展名 → fileDefault（大写扩展名也命中，split().pop() 前已 toLowerCase）", () => {
    expect(iconFor({ type: "file", name: "mystery.xyz" }, false)).toBe(ICONS.fileDefault);
    expect(iconFor({ type: "file", name: "NoExt" }, false)).toBe(ICONS.fileDefault);
    expect(iconFor({ type: "file", name: "README.MD" }, false)).toBe(ICONS.fileMd);
  });
});

describe("chevronFor", () => {
  it("展开 ▾ / 折叠 ▸", () => {
    expect(chevronFor(true)).toBe(ICONS.chevronDown);
    expect(chevronFor(false)).toBe(ICONS.chevronRight);
  });
});

describe("rootNameFrom", () => {
  it("取路径末段（POSIX + Windows 分隔符）", () => {
    expect(rootNameFrom("/home/user/proj/src")).toBe("src");
    expect(rootNameFrom("C:\\Users\\me\\proj")).toBe("proj");
    expect(rootNameFrom("/")).toBe("/"); // 根路径无末段 → 原样返回（vanilla: pop()||path）
  });

  it("无分隔符 → 原样返回", () => {
    expect(rootNameFrom("proj")).toBe("proj");
  });
});

describe("sortEntries", () => {
  const mk = (name: string, type: "dir" | "file" = "file"): FileEntry => ({ name, path: `/x/${name}`, type });

  it("目录在前、按名排序", () => {
    const list = [mk("b.txt"), mk("a_dir", "dir"), mk("a.txt"), mk("z_dir", "dir")];
    const sorted = sortEntries(list);
    expect(sorted.map((e) => e.name)).toEqual(["a_dir", "z_dir", "a.txt", "b.txt"]);
  });

  it("不修改入参数组", () => {
    const list = [mk("b"), mk("a")];
    sortEntries(list);
    expect(list.map((e) => e.name)).toEqual(["b", "a"]);
  });
});
