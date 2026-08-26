import { describe, expect, it } from "vitest";
import {
  helpRows,
  memoryRowView,
  skillRowView,
  truncate,
  type MemoryRow,
  type SkillRow,
} from "./dialogLists";

/**
 * dialogLists.test.ts — 列表式对话框行格式化测试（Batch 4 slice 2）。
 * 镜像 vanilla showMemoryDialog/showSkillsDialog 的截断与拼接规则。
 */

describe("truncate", () => {
  it("超长截断 + 短文本原样", () => {
    expect(truncate("abcde", 3)).toBe("abc");
    expect(truncate("ab", 3)).toBe("ab");
  });
});

describe("memoryRowView", () => {
  it("label = title 截 40；hint = summary 截 50", () => {
    const m: MemoryRow = { id: "m1", title: "T".repeat(50), summary: "S".repeat(60) };
    const v = memoryRowView(m, "未命名");
    expect(v.label).toBe("T".repeat(40));
    expect(v.hint).toBe("S".repeat(50));
    expect(v.id).toBe("m1");
  });

  it("无 title → 用 id（vanilla m.title || m.id）", () => {
    expect(memoryRowView({ id: "m2" }, "未命名").label).toBe("m2");
  });

  it("无 title/id → unnamed 兜底", () => {
    expect(memoryRowView({ id: "" }, "未命名").label).toBe("未命名");
  });

  it("无 summary → 用 content（vanilla m.summary || m.content）", () => {
    expect(memoryRowView({ id: "m3", content: "正文" }, "未命名").hint).toBe("正文");
  });

  it("hint 全缺 → 空串", () => {
    expect(memoryRowView({ id: "m4" }, "未命名").hint).toBe("");
  });
});

describe("skillRowView", () => {
  it("label = name；hint = source · description(截50)", () => {
    const s: SkillRow = { name: "browser-harness", source: "project", description: "D".repeat(60) };
    const v = skillRowView(s, "未命名");
    expect(v.label).toBe("browser-harness");
    expect(v.hint).toBe(`project · ${"D".repeat(50)}`);
  });

  it("无 name → unnamed；无 description → 仅 source", () => {
    expect(skillRowView({ source: "user" }, "未命名")).toEqual({ label: "未命名", hint: "user" });
  });

  it("无 source → description 前无分隔符（vanilla 拼接语义）", () => {
    expect(skillRowView({ name: "x", description: "desc" }, "未命名").hint).toBe(" · desc");
  });
});

describe("helpRows", () => {
  it("COMMANDS 条目 → {cmd, hint}（hint 经 hintOf 解析）", () => {
    const commands = { "/help": {}, "/clear": {} };
    const hintOf = (cmd: string) => `hint:${cmd}`;
    expect(helpRows(commands, hintOf)).toEqual([
      { cmd: "/help", hint: "hint:/help" },
      { cmd: "/clear", hint: "hint:/clear" },
    ]);
  });
});
