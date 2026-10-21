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

/** 事件里可能携带图片的两个面（DataTransfer 与 ClipboardEvent.clipboardData 同形） */
export interface ImageCarrier {
  files?: ArrayLike<File> | null;
  items?: ArrayLike<{ kind?: string; getAsFile?: () => File | null }> | null;
  /** 事件自己声明的类型（`cd.types` / `dt.types`）——「声明了却没取到」时用来出声 */
  types?: ArrayLike<string> | null;
}

/**
 * 从一次粘贴/拖拽事件里取图片文件：`files` 里有图就用 `files`；只有当 `files` 一个图都没有时，
 * 才去读 `items[i].getAsFile()`。
 *
 * 为什么不「两个都读、按对象去重」（2026-10-21 在 EMRG.app 里跑探针应用实测）：同一次粘贴里，
 * `files[0]` 和 `items[0].getAsFile()` 是**两个不同的 File 对象、内容相同**，而且每次调
 * `getAsFile()` 都会新造一个对象 —— 按对象同一性去重挡不住它们，于是粘贴一张图会进来两张
 * （日志里 7 次粘贴、7 次都是 `attach 2 file(s): image/png, image/png`，两次落盘还是同一个
 * 文件），图片字节在请求体里也跟着翻倍。实测粘贴时 `files.length === 1`，所以「`files` 有值
 * 就够用」这件事本身成立。
 *
 * ⚠️ 这里顺带推翻一条沿用很久的前提：2026-09-30「GUI 输入框里 Cmd+V 没有任何效果」的真因是
 * 主进程用 Electron 不支持的 `blake2b512` 算摘要抛错（#2363 已修），**不是**「粘贴时 `files`
 * 为空」。旧注释据此要求把 `items` 与 `files` 合并读，那是错的，也是双份的来源。
 *
 * `items` 仍然留作兜底：`files` 为空时它是唯一的取法。这个形状在本机没有被复现过，保留它是
 * 为了不重新引入一次静默丢弃，代价是零 —— `files` 有值时这里根本不读它。
 *
 * 拖入两个同名同大小的不同文件仍然返回两个：这里不按内容去重，`files` 里有几个就是几个。
 */
export function collectImageFiles(carrier: ImageCarrier | null | undefined): File[] {
  if (!carrier) return [];
  const isImage = (f: File | null | undefined): f is File =>
    !!f && typeof f.type === "string" && f.type.startsWith("image/");
  const fromFiles = Array.from(carrier.files ?? []).filter(isImage);
  if (fromFiles.length > 0) return fromFiles;
  const out: File[] = [];
  for (const it of Array.from(carrier.items ?? [])) {
    if (!it || it.kind !== "file" || typeof it.getAsFile !== "function") continue;
    const f = it.getAsFile();
    if (isImage(f) && !out.includes(f)) out.push(f);
  }
  return out;
}

/**
 * 事件上声明的类型（`cd.types` / `dt.types`）——用于「声明了图片/文件，却一个都没取到」
 * 时把**事件里到底有什么**写进提示与日志，而不是只说一句「没图片」。
 */
export function declaredTypes(carrier: { types?: ArrayLike<string> | null } | null | undefined): string[] {
  if (!carrier || !carrier.types) return [];
  return Array.from(carrier.types).map((s) => String(s));
}

/** 声明里有文件/图片的迹象（Files / image/*）——用于区分「本来是文字粘贴」与「图片丢了」 */
export function declaresImageOrFile(types: readonly string[]): boolean {
  return types.some((t) => /^image\//i.test(t) || t === "Files");
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

