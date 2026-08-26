/**
 * sidebar.ts — Batch 3: Sidebar 纯逻辑（从 vanilla `js/sidebar.js` + `js/app.js` 提取）。
 *
 * 对话列表 = 跨项目打开会话区（P4 slice 2）：条目 = 项目名/会话标题，
 * lastActive 倒序（main 已排序，此处防御性稳定排序兜底）。
 * 仅显示标题，不显示 session ID / 消息数（去黑话，sidebar.js 头注）。
 */

export interface OpenSessionEntry {
  sid: string;
  /** 跨项目标题（entry 自带，优先） */
  title?: string;
  /** 项目名（空 → 归入当前项目） */
  projectName?: string;
  /** ISO 时间戳，用于排序 */
  lastActive?: string;
}

export interface SessionInfo {
  session_id: string;
  title?: string;
  [k: string]: unknown;
}

/** 会话条目统一格式：有标题显示 project/title，无标题显示 project/id（rant 2026-08-20T22:04:57）。 */
export function sessionLabel(project: string, title: string, sid: string): string {
  return title ? `${project}/${title}` : `${project}/${sid}`;
}

/**
 * 标题解析：entry.title 优先（跨项目自带），否则回退本地已知会话标题；
 * 两者皆无 → 空串（此时 label 降级为 project/sid）。
 * （sidebar.js renderOpenSessions: `entry.title || cur.title || ""`）
 */
export function resolveEntryTitle(entry: OpenSessionEntry, knownSessions: SessionInfo[]): string {
  if (entry.title) return entry.title;
  const cur = knownSessions.find((s) => s.session_id === entry.sid);
  return (cur && cur.title) || "";
}

/**
 * 打开会话排序：lastActive 倒序（最新在前）。无时间戳 → 视为最早（排后）。
 * 返回新数组（不修改入参）。main 已排序时结果不变（幂等）。
 */
export function sortOpenSessions(entries: OpenSessionEntry[]): OpenSessionEntry[] {
  return [...entries].sort((a, b) => {
    const ta = a.lastActive ? Date.parse(a.lastActive) : 0;
    const tb = b.lastActive ? Date.parse(b.lastActive) : 0;
    if (Number.isNaN(ta) && Number.isNaN(tb)) return 0;
    if (Number.isNaN(ta)) return 1;
    if (Number.isNaN(tb)) return -1;
    return tb - ta;
  });
}

/** 当前会话高亮判定（sidebar.js highlight: `item.dataset.sid === sid`）。 */
export function isActive(sid: string | null | undefined, activeSid: string | null | undefined): boolean {
  return !!sid && sid === activeSid;
}
