/**
 * workspaceView.test.ts — 工作区面板纯逻辑测试（Batch 3 WorkspaceView）。
 * 镜像 vanilla dialogs.js 行为：formatCountdown / formatRelativeTime / relTime /
 * rantTimestamp / rantFirstLine / rantStatusMeta / preprocessRantMarkdown /
 * buildEvolveProjects / projectHint / taskStatusBadge / taskHint / taskMeta。
 */
import { describe, expect, it, vi } from "vitest";
import {
  buildEvolveProjects,
  formatCountdown,
  formatRelativeTime,
  preprocessRantMarkdown,
  projectHint,
  rantFirstLine,
  rantStatusMeta,
  rantTimestamp,
  relTime,
  taskHint,
  taskMeta,
  taskStatusBadge,
  type TaskRec,
} from "./workspaceView";

const t = ((k: string, v?: Record<string, unknown>) =>
  v ? `${k}=${JSON.stringify(v)}` : k) as (k: string, v?: Record<string, unknown>) => string;

describe("formatCountdown", () => {
  it("≤60s → 纯秒", () => {
    expect(formatCountdown(43)).toBe("43s");
    expect(formatCountdown(0)).toBe("0s");
  });
  it("≤1h → m:ss", () => {
    expect(formatCountdown(83)).toBe("1m23s");
    expect(formatCountdown(3599)).toBe("59m59s");
  });
  it(">1h → h:mm", () => {
    expect(formatCountdown(3600)).toBe("1h00m");
    expect(formatCountdown(3930)).toBe("1h05m");
    expect(formatCountdown(7200)).toBe("2h00m");
  });
  it("负数/非数钳制为 0", () => {
    expect(formatCountdown(-5)).toBe("0s");
    expect(formatCountdown(null)).toBe("0s");
    expect(formatCountdown(undefined)).toBe("0s");
  });
});

describe("formatRelativeTime", () => {
  it("秒/分/时/天 ago", () => {
    const now = Date.now();
    expect(formatRelativeTime(new Date(now - 30_000).toISOString())).toBe("30s ago");
    expect(formatRelativeTime(new Date(now - 5 * 60_000).toISOString())).toBe("5m ago");
    expect(formatRelativeTime(new Date(now - 3 * 3600_000).toISOString())).toBe("3h ago");
    expect(formatRelativeTime(new Date(now - 2 * 86400_000).toISOString())).toBe("2d ago");
  });
  it("空/非法回退", () => {
    expect(formatRelativeTime(null)).toBe("");
    expect(formatRelativeTime("")).toBe("");
    expect(formatRelativeTime("not-a-date")).toBe("not-a-date");
  });
});

describe("relTime", () => {
  it("刚刚/分钟/小时/天（走 i18n）", () => {
    const now = Date.now();
    expect(relTime(new Date(now - 10_000).toISOString(), t)).toBe("relTime.justNow");
    expect(relTime(new Date(now - 5 * 60_000).toISOString(), t)).toBe('relTime.minutesAgo={"n":5}');
    expect(relTime(new Date(now - 2 * 3600_000).toISOString(), t)).toBe('relTime.hoursAgo={"n":2}');
    expect(relTime(new Date(now - 3 * 86400_000).toISOString(), t)).toBe('relTime.daysAgo={"n":3}');
  });
  it("缺失/非法 → 空串", () => {
    expect(relTime(null, t)).toBe("");
    expect(relTime("", t)).toBe("");
    expect(relTime("garbage", t)).toBe("");
  });
});

describe("rantTimestamp", () => {
  it("ISO → YYYY-MM-DD HH:MM", () => {
    expect(rantTimestamp("2026-08-26T12:16:48.896474+08:00")).toBe("2026-08-26 12:16");
  });
  it("缺失 → —", () => {
    expect(rantTimestamp(null)).toBe("—");
    expect(rantTimestamp("")).toBe("—");
  });
});

describe("rantFirstLine", () => {
  it("首行摘要去 md 标题/列表标记", () => {
    expect(rantFirstLine("## 标题\n正文第二行")).toBe("标题");
    expect(rantFirstLine("- 列表项\nnext")).toBe("列表项");
    expect(rantFirstLine("\n  \n正文")).toBe("正文");
  });
  it("空 → —", () => {
    expect(rantFirstLine(null)).toBe("—");
    expect(rantFirstLine("")).toBe("—");
    expect(rantFirstLine("  \n ")).toBe("—");
  });
});

describe("rantStatusMeta", () => {
  it("三态徽标类 + i18n 文案", () => {
    expect(rantStatusMeta("completed", t)).toEqual({ text: "rants.statusCompleted", badgeCls: "badge-done" });
    expect(rantStatusMeta("in_progress", t)).toEqual({ text: "rants.statusInProgress", badgeCls: "badge-warn" });
    expect(rantStatusMeta("pending", t)).toEqual({ text: "rants.statusPending", badgeCls: "badge-muted" });
    expect(rantStatusMeta(undefined, t)).toEqual({ text: "rants.statusPending", badgeCls: "badge-muted" });
  });
});

describe("preprocessRantMarkdown", () => {
  it("【段标记】短行 → #### 标题；长行/非标记保留", () => {
    expect(preprocessRantMarkdown("【现象】这里有问题")).toBe("#### 【现象】这里有问题");
    expect(preprocessRantMarkdown("【长行】" + "x".repeat(80))).toBe("【长行】" + "x".repeat(80));
    expect(preprocessRantMarkdown("普通正文")).toBe("普通正文");
    expect(preprocessRantMarkdown(null)).toBe("");
  });
});

describe("buildEvolveProjects", () => {
  it("收集 config.project 集合（含 evolution 任务）", () => {
    const tasks: TaskRec[] = [
      { name: "a", config: { project: "emrg" } },
      { name: "b", config: { project: "aitokenpool" } },
      { name: "c", config: {} },
    ];
    const set = buildEvolveProjects(tasks);
    expect(set.has("emrg")).toBe(true);
    expect(set.has("aitokenpool")).toBe(true);
    expect(set.size).toBe(2);
  });
  it("空/缺省安全", () => {
    expect(buildEvolveProjects(null).size).toBe(0);
    expect(buildEvolveProjects(undefined).size).toBe(0);
  });
});

describe("projectHint", () => {
  it("路径 + 活跃时间 join ·", () => {
    const p = { name: "emrg", path: "/proj", latest_session_at: new Date(Date.now() - 10_000).toISOString() };
    expect(projectHint(p, t)).toBe("/proj · relTime.justNow");
  });
  it("无路径/无活跃 → 单段/空", () => {
    expect(projectHint({ name: "x", path: "/a" }, t)).toBe("/a");
    expect(projectHint({ name: "x" }, t)).toBe("");
  });
});

describe("taskStatusBadge", () => {
  const mk = (over: Partial<TaskRec>): TaskRec => ({ name: "t", ...over });
  it("running → 运行中 + 时长", () => {
    const b = taskStatusBadge(mk({ running: true, started_at: 100 }), t);
    expect(b?.cls).toBe("task-running-badge");
    expect(b?.text).toBe("app.taskRunningBadge");
    expect(b?.countdown).toContain("app.taskRunningDuration");
  });
  it("next_run → 待运行 + 倒计时", () => {
    const b = taskStatusBadge(mk({ next_run_in_seconds: 83 }), t);
    expect(b?.cls).toBe("task-pending-badge");
    expect(b?.text).toBe("app.taskPendingBadge");
    expect(b?.countdown).toContain('"n":"1m23s"');
  });
  it("enabled → 待调度", () => {
    expect(taskStatusBadge(mk({ enabled: true }), t)?.cls).toBe("task-idle-badge");
    expect(taskStatusBadge(mk({}), t)?.cls).toBe("task-idle-badge");
  });
  it("disabled → null", () => {
    expect(taskStatusBadge(mk({ enabled: false }), t)).toBeNull();
  });
});

describe("taskHint", () => {
  it("项目 / 间隔 / 停用 join ·", () => {
    const h = taskHint({ config: { project: "emrg" }, interval: 1800 }, t);
    expect(h).toBe('emrg · app.taskInterval={"n":1800}');
    expect(taskHint({ enabled: false }, t)).toBe("app.taskDisabled");
  });
});

describe("taskMeta", () => {
  it("上次运行 + 降频（只认 heartbeat_active）", () => {
    const m = taskMeta(
      { last_run_at: new Date(Date.now() - 300_000).toISOString(), saturation: { heartbeat_active: true, heartbeat_interval: 60 } },
      t,
    );
    expect(m.length).toBe(2);
    expect(m[0].text).toContain("app.taskLastRun");
    expect(m[1].key).toBe("throttled");
    expect(m[1].badgeCls).toBe("task-saturation-badge");
  });
  it("未运行 + 无降频", () => {
    const m = taskMeta({}, t);
    expect(m.length).toBe(1);
    expect(m[0].text).toBe("app.taskNoRunYet");
  });
  it("empty_cycles 不触发降频（只认 heartbeat_active）", () => {
    const m = taskMeta({ saturation: { heartbeat_active: false } as TaskRec["saturation"] }, t);
    expect(m.some((x) => x.key === "throttled")).toBe(false);
  });
});

// 确保 t 被使用（避免 lint 未使用告警）
void vi;
