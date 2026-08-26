/**
 * rewind.ts — /rewind 历史回退对话框纯逻辑（Batch 4 slice 4）。
 * 源：vanilla renderer/js/app.js showRewindDialog（288-326）：
 * - listHistory → messages 倒序（最新在上），行 = #record_index + preview(60 截断)
 * - 行点击 → rewindSession(recordIndex)
 *
 * 纯逻辑、零 DOM：RewindDialog 展示；Batch 5 接线 window.emrg.listHistory/rewindSession。
 */

/** listHistory 返回的消息点（与本组件相关的字段） */
export interface RewindPoint {
  record_index: number;
  preview?: string;
  content?: string;
}

/** 行视图（label = #index，hint = preview 截断 60 字符，vanilla `.slice(0, 60)`） */
export interface RewindPointRow {
  index: number;
  label: string;
  hint: string;
}

/** 与 vanilla 一致：preview 截断长度 */
export const REWIND_PREVIEW_LEN = 60;

/** 单行视图：label = `#${record_index}`，hint = preview||content 截断 */
export function rewindPointRow(p: RewindPoint): RewindPointRow {
  const preview = String(p.preview ?? p.content ?? "").slice(0, REWIND_PREVIEW_LEN);
  return { index: p.record_index, label: `#${p.record_index}`, hint: preview };
}

/**
 * 倒序（最新消息点在最上，vanilla `[...messages].reverse()`）。
 * null/undefined → []（空列表与「无历史」同语义，由组件区分文案）。
 */
export function rewindPointsNewestFirst(messages: RewindPoint[] | null | undefined): RewindPoint[] {
  return [...(messages ?? [])].reverse();
}
