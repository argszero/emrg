import { relTime, type TFunc } from "./workspaceView";

/**
 * openSession.ts — 打开/新建会话对话框的纯行格式化逻辑（Batch 4 slice 3）。
 * 镜像 vanilla `js/dialogs.js` 的 showOpenSessionDialog / showProjectSessions /
 * showNewSessionDialog / isProtectedProject：
 * - projectRowView：label = name || path || ""；hint = path；active = relTime(latest_session_at)
 * - sessionRowView：label = title || session_id（name-or-id，不截短）；
 *   marks = [current?, relTime(updated_at)?]（vanilla marks.join(" · ")）
 * - isProtectedProject：内置 emrg / emrg-task（演化依赖）不可删
 */

export interface ProjectRow {
  name?: string;
  path?: string;
  latest_session_at?: string | null;
}

export interface ProjectRowView {
  label: string;
  hint: string;
  /** 相对时间（"刚刚 / 5 分钟前…"）；无 → "" */
  active: string;
}

/** 项目行视图（vanilla showOpenSessionDialog/showNewSessionDialog 共用） */
export function projectRowView(p: ProjectRow, t?: TFunc): ProjectRowView {
  return {
    label: p.name || p.path || "",
    hint: p.path || "",
    active: relTime(p.latest_session_at, t),
  };
}

/** 受保护项目（vanilla isProtectedProject）——内置项目，删除须拒绝 */
export function isProtectedProject(p: ProjectRow): boolean {
  return Boolean(p && (p.name === "emrg" || p.name === "emrg-task"));
}

export interface SessionRow {
  session_id: string;
  title?: string;
  updated_at?: string | null;
}

export interface SessionRowView {
  label: string;
  /** 标记列表（vanilla marks.join(" · ")）："当前" + 相对时间 */
  marks: string[];
}

/** 会话行视图（vanilla showProjectSessions） */
export function sessionRowView(s: SessionRow, currentSid: string | null, t?: TFunc): SessionRowView {
  const marks: string[] = [];
  if (s.session_id === currentSid) marks.push(t ? t("app.current") : "current");
  const act = relTime(s.updated_at, t);
  if (act) marks.push(act);
  return { label: s.title || s.session_id, marks };
}
