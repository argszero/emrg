import { describe, expect, it } from "vitest";
import * as sidebarModule from "./sidebar";
import {
  formatElapsed,
  isActive,
  resolveEntryTitle,
  sessionLabel,
  type SessionInfo,
} from "./sidebar";

/**
 * sidebar.test.ts — Sidebar 纯逻辑测试（Batch 3，镜像 vanilla sidebar.js 行为）。
 */

describe("顺序由主进程决定（issue #3022）", () => {
  it("这一层不再导出排序函数：给它什么数组，它就按什么顺序画", () => {
    // 一个「暂时没人调用」的排序函数迟早会被再调用一次，而它一跑，用户拖出来的
    // 位置就被一次消息到达冲掉。所以它不能只是没人用，它必须不在。
    expect(Object.keys(sidebarModule)).not.toContain("sortOpenSessions");
  });
});

describe("sessionLabel", () => {
  it("有标题 → agent/title 格式", () => {
    expect(sessionLabel("emrg", "我的会话", "s1")).toBe("emrg/我的会话");
  });

  it("无标题 → agent/sid 格式（不降级为标题，rant 2026-08-20T22:04:57）", () => {
    expect(sessionLabel("emrg", "", "s1")).toBe("emrg/s1");
  });

  it("agent 为空时仍输出 /sid 或 /title 分隔（保持与 vanilla 一致）", () => {
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

describe("formatElapsed（rant 2026-09-02T10:36:26：与 TUI 状态栏同格式 [m:ss]）", () => {
  const T0 = 1_756_800_000_000; // 任意 epoch ms 基准

  it("无 startedAt（非运行）→ 空串", () => {
    expect(formatElapsed(undefined, T0)).toBe("");
    expect(formatElapsed(0, T0)).toBe("");
  });

  it("<60s → [0:ss]（补零）", () => {
    expect(formatElapsed(T0, T0 + 5000)).toBe("[0:05]");
    expect(formatElapsed(T0, T0 + 59_000)).toBe("[0:59]");
  });

  it("≥60s → [m:ss]（m 不补零，与 TUI divmod 一致）", () => {
    expect(formatElapsed(T0, T0 + 60_000)).toBe("[1:00]");
    expect(formatElapsed(T0, T0 + 97_800)).toBe("[1:37]"); // 宿主实测样例 97.8s
    expect(formatElapsed(T0, T0 + 3_600_000)).toBe("[60:00]");
  });

  it("负 elapsed（时钟偏差）钳制为 0", () => {
    expect(formatElapsed(T0 + 5000, T0)).toBe("[0:00]");
  });
});
