import { COMMANDS, getCompletions } from "./commands";
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
  /** 参数用法（与 `/help` 同一串，commands.usageText 解析） */
  usage: string;
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
 * 输入框内容 → 补全菜单状态。两条界线在这一个函数里，因为它们是同一个判断：
 * **什么时候该有清单**。
 *
 * - 不在指令词上（不以 `/` 开头）→ 没有清单。这里读的是**原文**，不做 trim：
 *   `"/help "` 去掉空白之后是 `"/help"`，拿它去判就把补全之后敲的那个空格当成
 *   「又回到指令词了」，清单重新弹回来（issue #3078 第 4 条）。
 *   空白本身不必另判——没有指令名含空白，`"/cle "` 做前缀匹配本来就不命中。这里
 *   曾经多一句 `/\s/` 的守卫，把它删掉之后全部测试仍然全绿，可见那句是多余的。
 * - 指令名已经打全 → 没有清单，回车交给执行（第 3 条）。
 * - 其余 → 按前缀过滤。
 */
export function menuForInput(raw: string, t: TranslateFn): CmdMenuState {
  const lead = String(raw ?? "").replace(/^[ \t]*/, "");
  if (!lead.startsWith("/")) return CMD_MENU_CLOSED;
  if (COMMANDS[lead.toLowerCase()]) return CMD_MENU_CLOSED;
  return menuForPrefix(lead, t);
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

/**
 * 撤掉**一条**排队消息（issue #3204）：从待重发清单里按 requestId 摘掉它，空则删键。
 * 返回是否真摘到了 —— `false` 不是错误：那条消息可能刚被注入、或另一个客户端已经撤了。
 *
 * 这条只改**本地重发清单**。屏幕上那个「排队中」标记不归它管：标记是那一行消息的状态，
 * 由 daemon 的队列帧驱动（见 `applyQueue`）。两者分开是因为它们回答不同的问题 ——
 * 「我手上还有什么要重发」和「daemon 的队列里现在有谁」，后者才是宿主看到的东西。
 */
export function removeQueued(
  queuedSends: Map<string, QueuedSend[]>,
  sid: string,
  requestId: string,
): boolean {
  const q = queuedSends.get(sid);
  if (!q) return false;
  const idx = q.findIndex((e) => e.requestId === requestId);
  if (idx < 0) return false;
  q.splice(idx, 1);
  if (q.length === 0) queuedSends.delete(sid);
  return true;
}

/**
 * 撤掉一条排队消息（issue #3204）：先在本地把那一行的标记撤掉，再请 daemon 撤。
 *
 * 顺序是刻意的。宿主点了删除，屏幕必须立刻应答；daemon 随后用 `queue_changed` 里
 * **它自己的**队列重画一遍标记，两边不一致时以后者为准 —— 所以这里先撤本地只是让界面
 * 不等一个往返，不是另立一个事实。IPC 不存在（单测、旧 preload）时只做本地那一半。
 *
 * 两个组件（消息行上的删除按钮、输入框上方的队列摘要）共用这一个函数：删除的语义只有
 * 一份，否则两个入口迟早有一处忘了向 daemon 报。
 */
export function cancelQueuedViaIpc(payload: {
  sessionId: string | null;
  requestId: string;
}): Promise<unknown> {
  const api = (window as unknown as {
    emrg?: { cancelQueued?: (p: { sessionId: string | null; requestId: string }) => Promise<unknown> };
  }).emrg;
  // 没有 preload 时（单测、旧壳）不发：本地标记照样撤，daemon 下一帧会纠正它。
  return api?.cancelQueued?.(payload) ?? Promise.resolve();
}

export async function withdrawQueued(
  store: { setQueuedPosition(rid: string, position: number | null, sid?: string | null): void },
  sid: string | null,
  rid: string,
  send: (payload: { sessionId: string | null; requestId: string }) => Promise<unknown> = cancelQueuedViaIpc,
): Promise<void> {
  store.setQueuedPosition(rid, null, sid);
  try {
    await send({ sessionId: sid, requestId: rid });
  } catch {
    /* daemon 侧没撤成：留下的状态由它下一帧队列说明，本地不另编一个 */
  }
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
 * 构建文件标记（issue #3111）：与 `imagePlaceholder` 并列，同一个「标记即附件」的形状。
 *
 * `display` 取 `File.name`——它本身就是 basename，不带目录；文件名清洗与落盘路径仍归
 * main 侧，这里不复制第二套规则。中括号是边界：`[📄 a][📄 b]` 连着写也分得开，
 * 所以标记后面不需要换行来分隔。
 */
export function filePlaceholder(display: string): string {
  return `[📄 ${display}]`;
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

/** 事件里可能携带图片的两个面（DataTransfer 与 ClipboardEvent.clipboardData 同形） */
export interface ImageCarrier {
  files?: ArrayLike<File> | null;
  items?: ArrayLike<{ kind?: string; getAsFile?: () => File | null }> | null;
  /** 事件自己声明的类型（`cd.types` / `dt.types`）——「声明了却没取到」时用来出声 */
  types?: ArrayLike<string> | null;
}

/**
 * 一个 File 是不是图片（无 type 视为不是）。
 *
 * 这是**唯一**的一处分类：图片按视觉交给模型，其它文件落盘、只把路径写进正文
 * （issue #2920）。两处各判一遍，必然漂移出一类「本端当图、落盘当文件」的东西。
 */
export function isImageFile(f: File | null | undefined): f is File {
  return !!f && typeof f.type === "string" && f.type.startsWith("image/");
}

/**
 * 从一次粘贴/拖拽事件里取文件：`files` 里有文件就用 `files`；只有当 `files` 一个都没有时，
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
 *
 * 取的是**全部**文件而不只是图片（issue #2920）：类型筛选从取件里搬到了 `isImageFile`，
 * 于是同一份事件不再需要读两遍才能同时拿到图和附件。
 */
export function collectFiles(carrier: ImageCarrier | null | undefined): File[] {
  if (!carrier) return [];
  const fromFiles = Array.from(carrier.files ?? []);
  if (fromFiles.length > 0) return fromFiles;
  const out: File[] = [];
  for (const it of Array.from(carrier.items ?? [])) {
    if (!it || it.kind !== "file" || typeof it.getAsFile !== "function") continue;
    const f = it.getAsFile();
    if (f && !out.includes(f)) out.push(f);
  }
  return out;
}

/** 一次事件里的图片子集（旧入口，保留给只关心图片的调用方与它们的测试）。 */
export function collectImageFiles(carrier: ImageCarrier | null | undefined): File[] {
  return collectFiles(carrier).filter(isImageFile);
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
 * 待发附件：图片与文件**同形**（issue #3111 要求 7）。
 * - `label` 是正文里那个标记的原文（`[📷 …]` / `[📄 …]`）；
 * - `position` 发送时算，等于标记在**将要发出的正文**里的字符偏移。
 */
export interface PendingAttachment {
  path: string;
  label: string;
  position?: number;
  mime?: string;
}

/** `label` 在 `text` 里第 `nth` 次出现的位置（0 起算）；没有则 -1。 */
function nthOccurrence(text: string, label: string, nth: number): number {
  let from = 0;
  for (let i = 0; i <= nth; i += 1) {
    const pos = text.indexOf(label, from);
    if (pos < 0) return -1;
    if (i === nth) return pos;
    from = pos + label.length;
  }
  return -1;
}

/**
 * 标记 ↔ 记录的配对：**按出现顺序一对一**，图片与文件共用这一条规则。
 *
 * 同名附件是这里的全部要点：两个 `screenshot.png` 的标记原文完全相同，按「标记找
 * 记录」或按「记录的第一次出现」都会让两条记录指向同一处，于是第二张图跟着第一次
 * 的位置发出去。所以每条记录认领的是它这个标记的**第 k 次**出现，k 是同类记录里
 * 它自己的序号——同名不代表同一处。
 *
 * 正文里已经找不到标记的记录直接丢掉：删掉标记即不再附这个附件，图片与文件同一套。
 * 返回按 position 升序（daemon 按 position 切块）。
 */
export function resolveSendAttachments<T extends PendingAttachment>(
  pending: T[],
  text: string,
): (T & { position: number })[] {
  const claimed = new Map<string, number>();
  const out: (T & { position: number })[] = [];
  for (const item of pending) {
    const nth = claimed.get(item.label) ?? 0;
    claimed.set(item.label, nth + 1);
    const pos = nthOccurrence(text, item.label, nth);
    if (pos < 0) continue;
    out.push({ ...item, position: pos });
  }
  return out.sort((a, b) => a.position - b.position);
}

/**
 * 发送前收敛 pending 图片：占位符仍在文本中的保留（删了占位符即丢弃，TUI 同语义），
 * position = 字面占位符在文本中的字符偏移；返回按 position 升序的新数组。
 */
export function resolveSendImages(pending: ImageAttach[], text: string): ImageAttach[] {
  return resolveSendAttachments(pending, text);
}

/**
 * 把文件标记换成它的绝对路径（issue #3111 要求 7）：模型没有「文件附件」这条通道，
 * 它拿到的必须是路径本身，而且必须**落在标记原来的位置**——顺序就是它与正文的对应关系。
 *
 * 两个标记紧挨着时（`[📄 a][📄 b]`），换成两个路径后中间补**一个空格**：不留空格就拼成
 * `/…/a.txt/…/b.txt` 一个谁也不是的字面量，模型按它去读，两个文件一个都打不开——那正是
 * 旧行为用换行想解决的问题。空格把它解决掉，而编辑器里仍然没有换行。
 *
 * 被删掉的标记不在结果里，那个文件也就不出现。
 */
export function expandFileMarkers(text: string, files: PendingAttachment[]): string {
  const resolved = resolveSendAttachments(files, text);
  let out = "";
  let cursor = 0;
  let prevEnd = -1;
  for (const f of resolved) {
    out += text.slice(cursor, f.position);
    if (prevEnd === f.position) out += " ";
    out += f.path;
    cursor = f.position + f.label.length;
    prevEnd = cursor;
  }
  return out + text.slice(cursor);
}

