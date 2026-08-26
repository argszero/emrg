import { describe, expect, it } from "vitest";
import { COMMANDS, getCompletions, hintText, parseInput } from "./commands";
import type { TranslateFn } from "./utils";

/** hint 键 → 固定文案的测试翻译函数（i18n 未就绪路径的替身） */
const fakeT: TranslateFn = (key) => `[${key}]`;

describe("commands registry", () => {
  it("contains all 15 TUI commands + /open (rant 19:44 acceptance + P5 extension)", () => {
    const expected = [
      "/clear", "/compact", "/delete", "/help", "/image", "/memory", "/model",
      "/open", "/rant", "/rename", "/resume", "/rewind", "/sessions", "/skills", "/trigger", "/version",
    ];
    for (const cmd of expected) {
      expect(COMMANDS[cmd]).toBeDefined();
    }
    expect(Object.keys(COMMANDS).length).toBe(16);
    // 每条指令都有 hint + phase 1-4
    for (const [cmd, meta] of Object.entries(COMMANDS)) {
      expect(meta.hint.length).toBeGreaterThan(0);
      expect(meta.phase).toBeGreaterThanOrEqual(1);
      expect(meta.phase).toBeLessThanOrEqual(4);
    }
  });
});

describe("parseInput", () => {
  it("plain message", () => {
    expect(parseInput("你好，帮我写周报")).toEqual({ type: "message" });
    expect(parseInput("  你好  ")).toEqual({ type: "message" });
  });
  it("known command (case-insensitive, with args)", () => {
    expect(parseInput("/clear")).toEqual({ type: "command", cmd: "/clear", args: [] });
    expect(parseInput("/Clear")).toEqual({ type: "command", cmd: "/clear", args: [] });
    expect(parseInput("/rant 希望支持主题切换")).toEqual({
      type: "command",
      cmd: "/rant",
      args: ["希望支持主题切换"],
    });
  });
  it("unknown command", () => {
    expect(parseInput("/foobar")).toEqual({ type: "unknown", cmd: "/foobar" });
  });
});

describe("getCompletions", () => {
  it("empty prefix returns all 16", () => {
    expect(getCompletions("", fakeT).length).toBe(16);
  });
  it("/r prefix filters to /rant /rename /resume /rewind", () => {
    const cmds = getCompletions("/r", fakeT).map((i) => i.cmd).sort();
    expect(cmds).toEqual(["/rant", "/rename", "/resume", "/rewind"].sort());
  });
  it("exact match returns single entry with hint", () => {
    const c = getCompletions("/clear", fakeT);
    expect(c.length).toBe(1);
    expect(c[0].cmd).toBe("/clear");
    expect(c[0].hint.length).toBeGreaterThan(0);
  });
  it("no match returns empty", () => {
    expect(getCompletions("/zzz", fakeT).length).toBe(0);
  });
});

describe("hintText", () => {
  it("returns translated hint when t available", () => {
    const t: TranslateFn = (key) => (key === "cmd.clear.hint" ? "清空会话" : key);
    expect(hintText("/clear", t)).toBe("清空会话");
  });
  it("falls back to the dict key when t is unavailable", () => {
    const t: TranslateFn = (key) => key;
    expect(hintText("/clear", t)).toBe("cmd.clear.hint");
  });
  it("returns empty string for unknown command", () => {
    expect(hintText("/nope", fakeT)).toBe("");
  });
});
