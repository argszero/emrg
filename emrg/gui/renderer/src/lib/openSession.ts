import { relTime, type TFunc } from "./workspaceView";

/**
 * openSession.ts — 打开/新建会话对话框的纯行格式化逻辑（Batch 4 slice 3）。
 * 镜像 vanilla `js/dialogs.js` 的 showOpenSessionDialog / showAgentSessions /
 * showNewSessionDialog / isProtectedAgent：
 * - agentRowView：label = name || path || ""；hint = path；active = relTime(latest_session_at)
 * - sessionRowView：label = title || session_id（name-or-id，不截短）；
 *   marks = [current?, relTime(updated_at)?]（vanilla marks.join(" · ")）
 * - isProtectedAgent：内置 emrg / emrg-task（演化依赖）不可删
 */

export interface AgentRow {
  name?: string;
  path?: string;
  latest_session_at?: string | null;
}

export interface AgentRowView {
  label: string;
  hint: string;
  /** 相对时间（"刚刚 / 5 分钟前…"）；无 → "" */
  active: string;
}

/** 项目行视图（vanilla showOpenSessionDialog/showNewSessionDialog 共用） */
export function agentRowView(p: AgentRow, t?: TFunc): AgentRowView {
  return {
    label: p.name || p.path || "",
    hint: p.path || "",
    active: relTime(p.latest_session_at, t),
  };
}

/** 受保护项目（vanilla isProtectedAgent）——内置项目，删除须拒绝 */
export function isProtectedAgent(p: AgentRow): boolean {
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

/** 会话行视图（vanilla showAgentSessions） */
export function sessionRowView(s: SessionRow, currentSid: string | null, t?: TFunc): SessionRowView {
  const marks: string[] = [];
  if (s.session_id === currentSid) marks.push(t ? t("app.current") : "current");
  const act = relTime(s.updated_at, t);
  if (act) marks.push(act);
  return { label: s.title || s.session_id, marks };
}

/**
 * 搜索框的匹配规则——两个列表共用一条，避免各写一遍就各错一处。
 *
 * 忽略首尾空格与大小写；空查询（或只有空格）匹配全部，所以清空输入即恢复整个列表。
 * 多个字段是「或」的关系：agent 按名字与路径都能找到，会话按标题与会话 id 都能找到。
 */
export function matchesQuery(query: string, ...fields: Array<string | null | undefined>): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return fields.some((field) => (field || "").toLowerCase().includes(needle));
}
