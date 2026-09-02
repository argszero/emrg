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
  /** 图片附件（rant 2026-09-02T15:23:53：busy 重发须携带原图，否则丢图） */
  images?: ImageAttach[] | null;
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

/* ── 图片附加（rant 2026-09-02T15:23:53：GUI 粘贴/拖拽图片，语义对齐 TUI）── */

/**
 * 输入图元数据（对齐 TUI _pending_images 语义）：
 * - label = **完整占位符文本**（"[📷 …]"）——TUI 发送前以 `label in text` 判活、
 *   daemon 非 vision 降级文案直接引用该字段。
 * - position 在发送时计算（= 序列化 markdown 中占位符的字符偏移）。
 */
export interface ImageAttach {
  path: string;
  label: string;
  position?: number;
  mime?: string;
}

/** 构建占位符文本（双端一致的字面量：TUI 用 f"[📷 {label}]"） */
export function imagePlaceholder(display: string): string {
  return `[📷 ${display}]`;
}

/**
 * 文件名/展示标签清洗（镜像 TUI safe_label 规则）：非 [a-zA-Z0-9._-] → "_"，
 * 截 40 字符、去尾 ._、空则回落 "image"。
 */
export function toSafeImageLabel(raw: string): string {
  const cleaned = raw
    .split("")
    .map((c) => (c.match(/[a-zA-Z0-9._-]/) ? c : "_"))
    .join("")
    .slice(0, 40)
    .replace(/[._]+$/, "");
  return cleaned || "image";
}

/**
 * tiptap-markdown 序列化会把字面占位符转义为 `\[📷 …\]`（防被当作链接/图片语法），
 * 发送前还原已知标签的字面量——保证 daemon 收到的文本与 TUI 完全一致（位置/判活
 * 都以还原后的字面量占位符为基准）。
 */
export function normalizePlaceholders(md: string, labels: string[]): string {
  let out = md;
  for (const label of labels) {
    const escaped = label.replace(/[\[\]]/g, (ch) => `\\${ch}`);
    if (escaped !== label) out = out.split(escaped).join(label);
  }
  return out;
}

/**
 * 发送前收敛 pending 图片：占位符仍在文本中的保留（删了占位符即丢弃，TUI 同语义），
 * position = 字面占位符在文本中的字符偏移；返回按 position 升序的新数组。
 */
export function resolveSendImages(pending: ImageAttach[], text: string): ImageAttach[] {
  const out: ImageAttach[] = [];
  for (const img of pending) {
    const pos = text.indexOf(img.label);
    if (pos >= 0) out.push({ ...img, position: pos });
  }
  return out.sort((a, b) => (a.position ?? -1) - (b.position ?? -1));
}

