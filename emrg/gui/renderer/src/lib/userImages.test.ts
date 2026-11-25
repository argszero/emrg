import { describe, expect, it } from "vitest";
import { splitUserText } from "./userImages";
import type { ImageAttach } from "./composer";

/**
 * userImages.test.ts — 「正文切成文字与图」这一个判断。
 *
 * 宿主 2026-11-25T18:17:18 的缺陷是**没有一处把占位符换成图**，所以这里的断言分两类：
 * 图真的出现在它自己那个位置（不是被拼到末尾、不是整段替换），以及**找不到标记的图不画**
 * —— 后者是半边：一个错位的缩略图比缺一张更难看出是错的。
 */

const img = (path: string, label: string, position?: number): ImageAttach => ({ path, label, position });

describe("splitUserText", () => {
  it("把标记换成图，前后文字各自成段", () => {
    const text = "看这张 [📷 shot] 就明白了";
    const parts = splitUserText(text, [img("/tmp/a.png", "[📷 shot]", text.indexOf("[📷 shot]"))]);
    expect(parts).toEqual([
      { kind: "text", text: "看这张 " },
      { kind: "image", image: img("/tmp/a.png", "[📷 shot]", text.indexOf("[📷 shot]")) },
      { kind: "text", text: " 就明白了" },
    ]);
  });

  it("标记在开头时不留空文字段", () => {
    const parts = splitUserText("[📷 a] 正文", [img("/tmp/a.png", "[📷 a]", 0)]);
    expect(parts.map((p) => p.kind)).toEqual(["image", "text"]);
    expect(parts[0].kind === "image" && parts[0].image.path).toBe("/tmp/a.png");
  });

  it("标记在结尾时同样不留空文字段", () => {
    const parts = splitUserText("正文 [📷 a]", [img("/tmp/a.png", "[📷 a]", 3)]);
    expect(parts.map((p) => p.kind)).toEqual(["text", "image"]);
    expect(parts[0].kind === "text" && parts[0].text).toBe("正文 ");
  });

  it("同名标记按位置分开：两张 `[📷 image]` 各归各的图", () => {
    const text = "[📷 image] 第一张 [📷 image] 第二张";
    const second = text.lastIndexOf("[📷 image]");
    const parts = splitUserText(text, [
      img("/tmp/1.png", "[📷 image]", 0),
      img("/tmp/2.png", "[📷 image]", second),
    ]);
    const paths = parts.map((p) => (p.kind === "image" ? p.image.path : null)).filter(Boolean);
    expect(paths).toEqual(["/tmp/1.png", "/tmp/2.png"]);
  });

  it("位置对不上时按顺序往后找，仍然只画一个", () => {
    const text = "说明 [📷 a] 尾巴";
    const parts = splitUserText(text, [img("/tmp/a.png", "[📷 a]", 999)]);
    const found = parts.filter((p) => p.kind === "image");
    expect(found).toHaveLength(1);
    expect(parts[1].kind === "image" && parts[1].image.path).toBe("/tmp/a.png");
  });

  it("正文里根本没有这个标记时不画（错位的图比缺一张更难看出是错的）", () => {
    const parts = splitUserText("纯文字", [img("/tmp/a.png", "[📷 a]", 0)]);
    expect(parts).toEqual([{ kind: "text", text: "纯文字" }]);
  });

  it("没有图时原样一段文字；空正文 + 有图时没有可画的段", () => {
    expect(splitUserText("只有字")).toEqual([{ kind: "text", text: "只有字" }]);
    expect(splitUserText("", [img("/tmp/a.png", "[📷 a]", 0)])).toEqual([]);
  });

  it("同一个标记只被第一张图认领，第二张标记不存在时不重复画", () => {
    const text = "[📷 a] 只有一次";
    const parts = splitUserText(text, [
      img("/tmp/1.png", "[📷 a]", 0),
      img("/tmp/2.png", "[📷 a]", 0),
    ]);
    expect(parts.filter((p) => p.kind === "image")).toHaveLength(1);
  });
});
