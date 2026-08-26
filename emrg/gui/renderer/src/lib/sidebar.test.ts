import { describe, expect, it } from "vitest";
import {
  isActive,
  resolveEntryTitle,
  sessionLabel,
  sortOpenSessions,
  type OpenSessionEntry,
  type SessionInfo,
} from "./sidebar";

/**
 * sidebar.test.ts — Sidebar 纯逻辑测试（Batch 3，镜像 vanilla sidebar.js 行为）。
 */

describe("sessionLabel", () => {
  it("有标题 → project/title 格式", () => {
    expect(sessionLabel("emrg", "我的会话", "s1")).toBe("emrg/我的会话");
  });

  it("无标题 → project/sid 格式（不降级为标题，rant 2026-08-20T22:04:57）", () => {
    expect(sessionLabel("emrg", "", "s1")).toBe("emrg/s1");
  });

  it("project 为空时仍输出 /sid 或 /title 分隔（保持与 vanilla 一致）", () => {
    expect(sessionLabel("", "", "s1")).toBe("/s1");
  });
});

describe("resolveEntryTitle", () => {
  const known: SessionInfo[] = [
    { session_id: "s1", title: "本地标题" },
    { session_id: "s2" },
  ];

  it("entry.title 优先（跨项目自带标题覆盖本地）", () => {
    expect(resolveEntryTitle({ sid: "s1", title: "跨项目标题" }, known)).toBe("跨项目标题");
  });

  it("无 entry.title → 回退本地已知会话标题", () => {
    expect(resolveEntryTitle({ sid: "s1" }, known)).toBe("本地标题");
  });

  it("两者皆无 → 空串", () => {
    expect(resolveEntryTitle({ sid: "s2" }, known)).toBe("");
    expect(resolveEntryTitle({ sid: "nope" }, known)).toBe("");
  });

  it("knownSessions 为空数组 → 空串", () => {
    expect(resolveEntryTitle({ sid: "s1", title: "跨项目标题" }, [])).toBe("跨项目标题");
    expect(resolveEntryTitle({ sid: "s1" }, [])).toBe("");
  });
});

describe("sortOpenSessions", () => {
  const mk = (sid: string, lastActive?: string): OpenSessionEntry => ({ sid, lastActive });

  it("lastActive 倒序（最新在前）", () => {
    const list = [
      mk("a", "2026-08-26T08:00:00Z"),
      mk("b", "2026-08-26T09:00:00Z"),
      mk("c", "2026-08-25T10:00:00Z"),
    ];
    expect(sortOpenSessions(list).map((e) => e.sid)).toEqual(["b", "a", "c"]);
  });

  it("无时间戳 → 排最后；不修改入参数组", () => {
    const list = [mk("a", "2026-08-26T08:00:00Z"), mk("b"), mk("c", "2026-08-25T10:00:00Z")];
    const sorted = sortOpenSessions(list);
    expect(sorted.map((e) => e.sid)).toEqual(["a", "c", "b"]);
    expect(list.map((e) => e.sid)).toEqual(["a", "b", "c"]); // 入参未变
  });

  it("非法时间戳按 NaN 处理（排后），空数组幂等", () => {
    expect(sortOpenSessions([mk("a", "not-a-date"), mk("b", "2026-08-26T08:00:00Z")]).map((e) => e.sid)).toEqual([
      "b",
      "a",
    ]);
    expect(sortOpenSessions([])).toEqual([]);
  });
});

describe("isActive", () => {
  it("相等 → true；不等 → false", () => {
    expect(isActive("s1", "s1")).toBe(true);
    expect(isActive("s1", "s2")).toBe(false);
  });

  it("null/undefined 安全", () => {
    expect(isActive(null, "s1")).toBe(false);
    expect(isActive("s1", null)).toBe(false);
    expect(isActive(undefined, undefined)).toBe(false);
  });
});
