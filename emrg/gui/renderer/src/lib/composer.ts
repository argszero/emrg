import { getCompletions } from "./commands";
import type { TranslateFn } from "./utils";

/**
 * composer.ts — 输入框发送流 + / 补全菜单状态机（Batch 2 remainder，蓝图 cycle-155056）。
 * 源：vanilla renderer/js/app.js sendMessage（131-170）+ CommandMenu（480-526）+
 * keydown 导航（~1710-1735）+ P2 queue-injection 队列（1450-1510，#655）。
 *
 * 纯逻辑、零 DOM：React Composer 组件消费。发送队列与 daemon 广播帧协议对齐：
 * - task_queued：排队提示（组件层 addSystemMessage）
 * - steer_committed：已注入 → 从队列移除该 requestId
 * - queued_requeue：回合结束未注入 → 按 request_ids 拆分重发；wasBusy||i>0 再跟踪
 * - queued_cancelled / disconnected：整条队列丢弃
 */

/** 补全菜单条目（commands.getCompletions 返回 + 展示位） */
export interface CommandItem {
  cmd: string;
  hint: string;
  phase: number;
}

/** 补全菜单状态（vanilla state.cmdMenu = { items, index }） */
export interface CmdMenuState {
  items: CommandItem[];
  index: number;
}

/** 关闭态（vanilla hideCmdMenu：items=[], index=-1） */
export const CMD_MENU_CLOSED: CmdMenuState = { items: [], index: -1 };

/**
 * 按输入前缀生成菜单（vanilla showCmdMenu）：以 / 开头且无空格时调用；
 * 无匹配 → 关闭态（vanilla：items.length===0 → hideCmdMenu）。
 */
export function menuForPrefix(prefix: string, t: TranslateFn): CmdMenuState {
  const items = getCompletions(prefix, t);
  return items.length ? { items, index: 0 } : CMD_MENU_CLOSED;
}

/**
 * ↑↓ 导航（vanilla：`(index ± 1 + n) % n` 环绕）。返回新状态（不可变）。
 */
export function menuNavigate(state: CmdMenuState, dir: 1 | -1): CmdMenuState {
  const n = state.items.length;
  if (n === 0 || state.index < 0) return state;
  return { ...state, index: ((state.index + dir) % n + n) % n };
}

/* ── P2 queue-injection（#655）发送队列 ── */

/** 待重发条目（sid → QueuedSend[]） */
export interface QueuedSend {
  requestId: string;
  text: string;
  sandbox?: string | null;
}

/** sendMessage busy 时入队（vanilla：queuedSends 不存在则建 [] 再 push） */
export function queueSend(
  queuedSends: Map<string, QueuedSend[]>,
  sid: string,
  item: QueuedSend,
): void {
  const q = queuedSends.get(sid);
  if (q) q.push(item);
  else queuedSends.set(sid, [item]);
}

/** steer_committed：回合已注入 → 移除该 requestId 条目；空则删键（vanilla 同） */
export function steerCommitted(
  queuedSends: Map<string, QueuedSend[]>,
  sid: string,
  requestId: string,
): void {
  const q = queuedSends.get(sid);
  if (!q) return;
  const idx = q.findIndex((e) => e.requestId === requestId);
  if (idx >= 0) q.splice(idx, 1);
  if (q.length === 0) queuedSends.delete(sid);
}

/**
 * queued_requeue：按 daemon 回传的 request_ids 拆分「待重发」与「剩余」。
 * 纯拆分不改 Map（调用方重发后经 trackAfterResend 写回）。
 */
export function partitionRequeue(
  queuedSends: Map<string, QueuedSend[]>,
  sid: string,
  requestIds: string[],
): { toResend: QueuedSend[]; remaining: QueuedSend[] } {
  const q = queuedSends.get(sid) ?? [];
  const ids = new Set(requestIds);
  return {
    toResend: q.filter((e) => ids.has(e.requestId)),
    remaining: q.filter((e) => !ids.has(e.requestId)),
  };
}

/**
 * 重发后的再跟踪（P2 审查同 #695 教训）：wasBusy 在循环前捕获，单客户端时首条
 * 重发开启新回合，M2+ 到达时 daemon busy 被再排队但客户端未跟踪 → 下个
 * queued_requeue 找不到 → 静默丢失。修复：wasBusy || i>0 的条目重新入队，
 * steer_committed 移除已注入的，下个 queued_requeue 重发其余，收敛。
 * 返回写回 Map 的最终数组。
 */
export function trackAfterResend(
  remaining: QueuedSend[],
  toResend: QueuedSend[],
  wasBusy: boolean,
): QueuedSend[] {
  const out = [...remaining];
  toResend.forEach((item, i) => {
    if (wasBusy || i > 0) out.push(item);
  });
  return out;
}

/** queued_cancelled / 断连：丢弃整条队列；返回是否存在（决定是否提示） */
export function clearQueued(queuedSends: Map<string, QueuedSend[]>, sid: string): boolean {
  return queuedSends.delete(sid);
}
