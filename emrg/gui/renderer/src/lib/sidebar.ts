/**
 * sidebar.ts — Batch 3: Sidebar 纯逻辑（从 vanilla `js/sidebar.js` + `js/app.js` 提取）。
 *
 * 对话列表 = 跨项目打开会话区（P4 slice 2）：条目 = 项目名/会话标题。
 * *顺序由主进程决定*（issue #3022）：进入列表的先后 + 用户拖拽，别的一概不动它。
 * `lastActive` 仍然带着，但它只供上限裁剪用，不再是排序依据 —— 任何在这里重排
 * 一次的实现都会让「点一下这条」或「别的会话来一条消息」把整列挪一次。
 */

export interface OpenSessionEntry {
  sid: string;
  /** 跨项目标题（entry 自带，优先） */
  title?: string;
  /** 项目名（空 → 归入当前项目） */
  projectName?: string;
  /** 进入列表的时刻（ISO）：写一次，此后不变；位置只由它与拖拽决定 */
  openedAt?: string;
  /** 最后一次活动（ISO）：只供上限裁剪挑出最久无活动的一条，不决定顺序 */
  lastActive?: string;
}

export interface SessionInfo {
  session_id: string;
  title?: string;
  [k: string]: unknown;
}

/** 会话条目统一格式：有标题显示 agent/title，无标题显示 agent/id（rant 2026-08-20T22:04:57）。 */
export function sessionLabel(agent: string, title: string, sid: string): string {
  return title ? `${agent}/${title}` : `${agent}/${sid}`;
}

/**
 * 标题解析：entry.title 优先（跨项目自带），否则回退本地已知会话标题；
 * 两者皆无 → 空串（此时 label 降级为 agent/sid）。
 * （sidebar.js renderOpenSessions: `entry.title || cur.title || ""`）
 */
export function resolveEntryTitle(entry: OpenSessionEntry, knownSessions: SessionInfo[]): string {
  if (entry.title) return entry.title;
  const cur = knownSessions.find((s) => s.session_id === entry.sid);
  return (cur && cur.title) || "";
}

/**
 * 打开会话排序：**已删除**（issue #3022）。顺序的唯一真源是主进程的数组，渲染层照画。
 * 这里留一段说明而不是留一个「暂时没人调用」的函数：一个还在的排序函数迟早会被
 * 再次调用，而它一旦跑了，用户拖动定下的位置就被一次消息到达冲掉。
 */

/** 当前会话高亮判定（sidebar.js highlight: `item.dataset.sid === sid`）。 */
export function isActive(sid: string | null | undefined, activeSid: string | null | undefined): boolean {
  return !!sid && sid === activeSid;
}

/**
 * 运行计时格式化（rant 2026-09-02T10:36:26）：与 TUI 状态栏一致的 `[m:ss]`。
 * startedAtMs/nowMs 均为 epoch 毫秒；startedAtMs 缺失（非运行中）→ ""（不显示）。
 * 负 elapsed（时钟偏差）钳制为 0。
 */
export function formatElapsed(startedAtMs: number | undefined, nowMs: number): string {
  if (!startedAtMs || startedAtMs <= 0) return "";
  const elapsed = Math.max(0, Math.floor((nowMs - startedAtMs) / 1000));
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;
  return `[${mins}:${secs.toString().padStart(2, "0")}]`;
}
