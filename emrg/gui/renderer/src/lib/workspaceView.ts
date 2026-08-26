/**
 * workspaceView.ts — 工作区面板纯逻辑（Batch 3 WorkspaceView slice）。
 * 从 vanilla `js/dialogs.js`（renderProjectList / renderTaskList / renderRantList /
 * formatCountdown / formatRelativeTime / preprocessRantMarkdown）迁移为纯函数。
 *
 * - 无 DOM / 无 daemon 依赖（Batch 5 接线）；t 注入 i18n 翻译函数
 * - 类名与 vanilla CSS 一致（.task-row/.task-badge/.task-hint/.rant-*），Batch 5 复用
 */

export type RantFilter = "" | "pending" | "in_progress" | "completed";

export interface RantRec {
  timestamp?: string;
  project?: string;
  status?: string;
  progress?: string | null;
  message?: string;
}

export interface ProjectRec {
  name?: string;
  path?: string;
  latest_session_at?: string | null;
}

export interface TaskRec {
  name?: string;
  type?: string;
  running?: boolean;
  started_at?: number | null;
  next_run_in_seconds?: number | null;
  enabled?: boolean;
  interval?: number | null;
  last_run_at?: string | null;
  saturation?: { heartbeat_active?: boolean; heartbeat_interval?: number } | null;
  config?: { project?: string } | null;
}

export type TFunc = (key: string, vars?: Record<string, unknown>) => string;

/* ── 时间工具 ─────────────────────────────────────────────── */

/** 倒计时格式化（vanilla formatCountdown）：≤60s "43s"；≤1h "1m23s"；>1h "1h05m"；负数钳制 0 */
export function formatCountdown(totalSeconds: number | null | undefined): string {
  const s = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${m}m${String(sec).padStart(2, "0")}s`;
  }
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h${String(m).padStart(2, "0")}m`;
}

/** 相对时间（vanilla formatRelativeTime）："5s ago / 5m ago / 3h ago / 2d ago"；非法输入回退原文 */
export function formatRelativeTime(isoStr?: string | null): string {
  if (!isoStr) return "";
  const t = Date.parse(isoStr);
  if (Number.isNaN(t)) return String(isoStr);
  const diffSec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  return `${Math.floor(diffSec / 86400)}d ago`;
}

/** 项目行"最近活跃"（vanilla relTime）：ISO → "刚刚 / {n} 分钟前 / {n} 小时前 / {n} 天前"（走 i18n） */
export function relTime(iso?: string | null, t?: TFunc): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffMin = Math.floor((Date.now() - then) / 60000);
  const tr = t || ((k: string) => k);
  if (diffMin < 1) return tr("relTime.justNow");
  if (diffMin < 60) return tr("relTime.minutesAgo", { n: diffMin });
  const hrs = Math.floor(diffMin / 60);
  if (hrs < 24) return tr("relTime.hoursAgo", { n: hrs });
  const days = Math.floor(hrs / 24);
  return tr("relTime.daysAgo", { n: days });
}

/** Rant 时间列：ISO → "YYYY-MM-DD HH:MM"（slice 16 + T→空格；缺失 "—"） */
export function rantTimestamp(ts?: string | null): string {
  if (!ts) return "—";
  const s = String(ts).slice(0, 16).replace("T", " ");
  return s || "—";
}

/* ── Rant 行工具 ─────────────────────────────────────────── */

/** 内容列摘要：message 首行（去空行 + md 标题/列表标记）；缺失 "—" */
export function rantFirstLine(message?: string | null): string {
  const msg = message || "";
  const firstLine = (msg.split("\n").find((l) => l.trim() !== "") || "")
    .replace(/^#{1,6}\s+/, "")
    .replace(/^[>*\-\s]+/, "");
  return firstLine || "—";
}

export interface RantStatusMeta {
  text: string;
  badgeCls: string;
}

/** 状态徽标三态（completed 绿 / in_progress 琥珀 / pending 灰），文案走 i18n */
export function rantStatusMeta(status?: string | null, t?: TFunc): RantStatusMeta {
  const st = status || "pending";
  const tr = t || ((k: string) => k);
  if (st === "completed") return { text: tr("rants.statusCompleted"), badgeCls: "badge-done" };
  if (st === "in_progress") return { text: tr("rants.statusInProgress"), badgeCls: "badge-warn" };
  return { text: tr("rants.statusPending"), badgeCls: "badge-muted" };
}

/** 【xxx】段标记 → #### 标题（vanilla preprocessRantMarkdown）：仅短行（≤60 字符），原文保留 */
export function preprocessRantMarkdown(text?: string | null): string {
  if (!text) return text || "";
  return String(text).split("\n").map((line) => {
    const t = line.trim();
    if (/^【[^】]{1,24}】/.test(t) && t.length <= 60) return `#### ${t}`;
    return line;
  }).join("\n");
}

/* ── 项目面板工具 ────────────────────────────────────────── */

/** auto_evolve 徽标集合：tasks.yml 中 config.project 与项目名相同（含 evolution 类型） */
export function buildEvolveProjects(tasks: TaskRec[] | null | undefined): Set<string> {
  const set = new Set<string>();
  for (const t of tasks || []) {
    const proj = t?.config?.project;
    if (proj) set.add(proj);
  }
  return set;
}

/** 项目行 hint：路径 + 相对活跃时间（join " · "） */
export function projectHint(p: ProjectRec, t?: TFunc): string {
  const parts: string[] = [];
  if (p.path) parts.push(p.path);
  const act = relTime(p.latest_session_at, t);
  if (act) parts.push(act);
  return parts.join(" · ");
}

/* ── 任务面板工具 ────────────────────────────────────────── */

export interface TaskBadge {
  cls: string;
  text: string;
  /** 倒计时/时长显示（可选） */
  countdown?: string;
}

/** 任务行状态徽标：running → 运行中；next_run → 待运行+倒计时；enabled → 待调度；否则无徽标 */
export function taskStatusBadge(t: TaskRec, tFn?: TFunc): TaskBadge | null {
  const tr = tFn || ((k: string) => k);
  if (t.running) {
    const b: TaskBadge = { cls: "task-running-badge", text: tr("app.taskRunningBadge") };
    if (t.started_at != null) b.countdown = tr("app.taskRunningDuration", { n: formatCountdown(0) });
    return b;
  }
  if (t.next_run_in_seconds != null) {
    return {
      cls: "task-pending-badge",
      text: tr("app.taskPendingBadge"),
      countdown: tr("app.taskNextRun", { n: formatCountdown(t.next_run_in_seconds) }),
    };
  }
  if (t.enabled !== false) {
    return { cls: "task-idle-badge", text: tr("app.taskIdleBadge") };
  }
  return null;
}

/** 任务行 hint：项目 / 间隔 / 已停用（join " · "） */
export function taskHint(t: TaskRec, tFn?: TFunc): string {
  const tr = tFn || ((k: string) => k);
  const parts: string[] = [];
  if (t.config?.project) parts.push(t.config.project);
  if (t.interval != null) parts.push(tr("app.taskInterval", { n: t.interval }));
  if (t.enabled === false) parts.push(tr("app.taskDisabled"));
  return parts.join(" · ");
}

export interface TaskMeta {
  key: string;
  text: string;
  badgeCls?: string;
}

/** 任务行 meta：上次运行（相对时间）+ 降频标识（只认 heartbeat_active） */
export function taskMeta(t: TaskRec, tFn?: TFunc): TaskMeta[] {
  const tr = tFn || ((k: string) => k);
  const out: TaskMeta[] = [];
  if (t.last_run_at) {
    out.push({ key: "lastRun", text: tr("app.taskLastRun", { n: formatRelativeTime(t.last_run_at) }) });
  } else {
    out.push({ key: "noRunYet", text: tr("app.taskNoRunYet") });
  }
  const sat = t.saturation;
  if (sat && sat.heartbeat_active) {
    out.push({
      key: "throttled",
      text: tr("app.taskThrottled", { m: formatCountdown(sat.heartbeat_interval) }),
      badgeCls: "task-saturation-badge",
    });
  }
  return out;
}
