import { describe, expect, it } from "vitest";
import { isProtectedProject, projectRowView, sessionRowView } from "./openSession";
import type { ProjectRow } from "./openSession";

/**
 * openSession.test.ts — 打开/新建会话行格式化测试（Batch 4 slice 3）。
 * 镜像 vanilla showOpenSessionDialog / showProjectSessions / isProtectedProject。
 */

const t = (key: string) => key; // 透传 i18n key（relTime 缺省即回退 key）

describe("projectRowView", () => {
  it("label = name；hint = path；active = relTime", () => {
    const v = projectRowView({ name: "emrg", path: "/x/emrg", latest_session_at: new Date().toISOString() }, t);
    expect(v.label).toBe("emrg");
    expect(v.hint).toBe("/x/emrg");
    expect(v.active).not.toBe("");
  });

  it("无 name → path 兜底；无 path → 空串", () => {
    expect(projectRowView({ path: "/only/path" }).label).toBe("/only/path");
    expect(projectRowView({ name: "n" }).hint).toBe("");
    expect(projectRowView({}).label).toBe("");
  });

  it("无 latest_session_at → active 空串", () => {
    expect(projectRowView({ name: "a" }).active).toBe("");
  });
});

describe("isProtectedProject", () => {
  it("内置 emrg / emrg-task 受保护", () => {
    expect(isProtectedProject({ name: "emrg" })).toBe(true);
    expect(isProtectedProject({ name: "emrg-task" })).toBe(true);
  });

  it("其他项目不保护", () => {
    expect(isProtectedProject({ name: "aitokenpool" })).toBe(false);
    expect(isProtectedProject({})).toBe(false);
    expect(isProtectedProject(undefined as unknown as ProjectRow)).toBe(false);
  });
});

describe("sessionRowView", () => {
  it("label = title || session_id（name-or-id 规则）", () => {
    expect(sessionRowView({ session_id: "s1", title: "我的会话" }, null).label).toBe("我的会话");
    expect(sessionRowView({ session_id: "s1" }, null).label).toBe("s1");
  });

  it("当前会话 → marks 含 app.current；有 updated_at → 追加相对时间", () => {
    const v = sessionRowView(
      { session_id: "s1", updated_at: new Date().toISOString() },
      "s1",
      (k) => k,
    );
    expect(v.marks[0]).toBe("app.current");
    expect(v.marks.length).toBe(2);
  });

  it("非当前 + 无 updated_at → marks 空", () => {
    expect(sessionRowView({ session_id: "s2" }, "s1").marks).toEqual([]);
  });
});
