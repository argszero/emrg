import { useEffect, useRef, useState, type ReactNode } from "react";
import { parseInput } from "../lib/commands";
import {
  CMD_MENU_CLOSED,
  menuForPrefix,
  menuNavigate,
  queueSend,
  imagePlaceholder,
  toSafeImageLabel,
  collectFiles,
  isImageFile,
  declaredTypes,
  declaresImageOrFile,
  resolveSendImages,
  type CmdMenuState,
  type ImageAttach,
  type ImageCarrier,
} from "../lib/composer";
import { genRequestId } from "../lib/utils";
import { useI18n } from "../lib/i18n";
import type { TranscriptStore } from "../lib/transcript";

/**
 * Composer — 输入框 + / 补全菜单 + 发送流（issue #2920：纯文本）。
 *
 * 这个框的用途是把宿主想说的话原样打进去、原样发出去。它此前是一个 markdown 富文本
 * 编辑器（tiptap + tiptap-markdown）：字被当格式渲染、发送前先序列化再补一道反转义、
 * 粘贴来回改写字符——三件事都与「原样」相反。现在它是一个 `<textarea>`：不解析、不渲染、
 * 不转义，发送的字符串就是框里的字符串。
 *
 * 图片插在文字中间这件事与富文本无关，只是当年搭在富文本上：现在它是一段字面标记
 * `[📷 label]`，落在光标处，三个入口（粘贴 / 拖拽 / 选择文件）都走同一条落盘路径。
 * 任意文件同样三个入口，区别只在结尾——图片按视觉交给模型（vision 内容），其它文件落盘
 * 后把**路径**写进正文，模型自己用 read 工具去读。
 *
 * 保留的行为一条都没动（每条都有测试）：Enter 发送 / Shift+Enter 换行 / 开头 `/` 唤起
 * 补全 / ↑↓ 召回历史 / 草稿实时保存与恢复 / 高度自适应到上限后内滚 / 发送失败回填 /
 * busy 时排队。格式栏、格式快捷键与链接对话框随 markdown 编辑体验一起删除。
 */

export interface SendOptions {
  sessionId: string | null;
  text: string;
  requestId: string;
  sandbox?: string | null;
  /** 图片附件（rant 2026-09-02T15:23:53：粘贴/拖拽 → 落盘 → 随消息发送） */
  images?: ImageAttach[] | null;
}
export interface SendResult {
  requestId?: string;
}
/** emrg:saveImage 入参（renderer → main IPC，base64 数据 + 展示标签） */
export interface SaveImagePayload {
  sessionId?: string | null;
  data: string;
  label: string;
  mime?: string;
}
/** emrg:saveImage 返回值（落盘路径 + 归一 mime） */
export interface SaveImageResult {
  path: string;
  mime?: string;
}
/** emrg:saveFile 入参（issue #2920：任意文件，保留原名以便认得出来） */
export interface SaveFilePayload {
  sessionId?: string | null;
  data: string;
  name: string;
  mime?: string;
}
/** emrg:saveFile 返回值（落盘路径——这一行就是模型读它所需的全部） */
export interface SaveFileResult {
  path: string;
}
/**
 * emrg:readClipboardImage 返回值——剪贴板里的图，已由 main 归一为 PNG（rant
 * 2026-09-30T09:35:04 要求 4）。`null` = 剪贴板里没有图（不是失败）。
 */
export interface ClipboardImage {
  data: string;
  mime?: string;
  name?: string;
}
/** 指令路由入参（parseInput 结果；type:"unknown" 无 args——vanilla 走 default 提示） */
export interface CommandRouting {
  type: "command" | "unknown";
  cmd: string;
  args?: string[];
}

export interface ComposerProps {
  store: TranscriptStore;
  sid?: string | null;
  sandbox?: string | null;
  /** busy 受控态（外部 daemon 广播驱动）；缺省内部管理 */
  busy?: boolean;
  /** 注入发送函数（默认 window.emrg.sendMessage；测试传假实现） */
  sendMessage?: (opts: SendOptions) => Promise<SendResult>;
  /** 注入图片落盘函数（默认 window.emrg.saveImage；测试传假实现） */
  saveImage?: (payload: SaveImagePayload) => Promise<SaveImageResult>;
  /** 注入文件落盘函数（默认 window.emrg.saveFile；测试传假实现） */
  saveFile?: (payload: SaveFilePayload) => Promise<SaveFileResult>;
  /**
   * 注入剪贴板读图函数（默认 window.emrg.readClipboardImage；测试传假实现）。
   * 只在粘贴路径上、且事件里没有任何**本端能直接收**的图片时使用：macOS 给的
   * `image/tiff` renderer 解不了，只能请有 NSImage 的 main 归一成 PNG——这正是
   * TUI 向 macOS 要 `«class PNGf»` 的同一个动作（rant 2026-09-30T09:35:04 要求 4）。
   */
  readClipboardImage?: () => Promise<ClipboardImage | null>;
  /**
   * 注入日志函数（默认 preload 的 window.emrg.log → ~/.emrg/emrg-gui.log）。
   * 图片/文件路径的每一次成功与拒绝都要留痕（rant 2026-09-30T09:35:04 要求 3）：
   * 该功能上线至今 IPC 一次没被走到，而日志里查不出为什么——因为拒绝全被吞了。
   */
  logLine?: (level: string, msg: string) => void;
  /** 注入中断函数（默认 window.emrg.cancel；busy 时发送按钮切换为停止按钮，rant 2026-09-02T20:30:05） */
  cancel?: (sessionId: string) => Promise<unknown>;
  /** / 指令路由回调（Batch 5 接线：/clear /model /memory …） */
  onCommand?: (routing: CommandRouting) => void;
  /**
   * 父级接管档位时传入：点击只上报意图，显示值取自 `sandbox` prop（rant
   * 2026-09-30T09:30:16，GUI 半边）。档位属于**会话**、由 daemon 落盘并广播，
   * 所以有它时本组件不再持副本——客户端先改自己那份，一旦 daemon 拒绝
   * （非法档位、无会话）它就会「对自己正确、对别的客户端错误」。
   *
   * 不传 = 维持原样（单挂载/单测）：本地 state 决定了显示与随消息下发的档位。
   */
  onSandboxChange?: (mode: string) => void;
  /**
   * The session's extra writable roots, as the daemon last reported them, or
   * null/undefined when it has not said (rant 2026-10-09T09:43:39, GUI half).
   *
   * Display only — same authority rule as `sandbox`: the list is the daemon's,
   * and this component renders it rather than holding one. `null` and `[]` are
   * deliberately different: `[]` is "this session has no extra roots", `null` is
   * "nobody has asked", and only the second should read as unknown.
   */
  sandboxRoots?: string[] | null;
  /**
   * The prompts the host typed in this session, oldest first, exactly as the daemon
   * holds them (issue #2349, part 2/2 — the GUI half of the input-history work).
   *
   * Same authority rule as `sandboxRoots`: the daemon files every accepted prompt
   * in `<session_dir>/input_history.jsonl`, so this component recalls from a list it
   * was handed rather than from one it keeps. A client-side list is what the issue
   * was opened about — it served every session at once, died with the process, and
   * could not be shared with the other client.
   *
   * `null`/`undefined` = nobody has asked about this session yet; `[]` = the session
   * has nothing typed in it. Both make the arrow keys move the cursor, which is the
   * same thing the host sees, so only the *presence* of entries changes behaviour.
   */
  inputHistory?: string[] | null;
  /**
   * Called once a prompt has actually been accepted by the daemon (issue #2349,
   * part 2/2).
   *
   * The recall list is the daemon's, and this component neither holds nor extends
   * it — but between a read and the next session switch the list would stay frozen
   * where the session was opened, so ↑ in a fresh session would recall nothing this
   * host just typed. The parent hangs this callback on the daemon's own write rule
   * (append, collapsing consecutive duplicates), and the next read replaces the
   * list whole.
   *
   * Only a *successful* send reaches it: a failed send means the daemon accepted
   * nothing, so there is nothing to be recallable.
   *
   * The **session id goes with the text**, and it is the id the send itself used —
   * not whatever is active when the await resolves. `submit` already keeps that id
   * for everything else it does with this message (`store.addUserMessage` and
   * `sendFn({ sessionId })`), and this is the same message: a host who switches
   * sessions while a send is in flight would otherwise have the prompt filed under
   * the session they switched *to*, whose daemon file does not contain it — which is
   * the defect issue #2349 was opened about, one layer down.
   */
  onPromptSent?: (sid: string, text: string) => void;
  /**
   * 父级接管「额外可写根」入口时传入：点击只上报意图（打开管理对话框）。
   * 不传 = 不渲染入口（单挂载/单测），与 `onSandboxChange` 的约定一致。
   */
  onManageSandboxRoots?: () => void;
  /**
   * 把一段文本放进输入框（issue #2384，GUI 半边）。
   *
   * `/rewind` 的用意是「这条说错了，我要重新说」，所以它除了改写历史，还要把那条
   * 消息**原文**放回输入框：光标在末尾、不自动发送、多行照原样保留。父级给出的是
   * 一次**请求**（`id` 单调递增），不是受控值——输入框的实时内容属于宿主，把外部
   * 一次性的插入做成受控值，会让每个按键都变成一次覆盖。同 `id` 只应用一次。
   */
  insertRequest?: { id: number; text: string } | null;
  /**
   * 模型与连接状态（issue #2969）：状态带只放在变的东西，不动的信息移到这里——
   * 输入框上方那一行 chip 是原型的「模型在输入框底部切换」。节点由 Shell 组装，
   * 输入框不解释它是什么。
   */
  modelChip?: ReactNode;
}

/** 输入框的高度上限：超过它就内滚（CSS 与这里读的是同一个数） */
const MAX_INPUT_HEIGHT = 150;

/** 沙箱三档（rant 2026-08-20T18:18，vanilla mode-switcher 同款；重构回归恢复） */
export const SANDBOX_TIERS = ["read-only", "workspace-write", "danger-full-access"] as const;
export type SandboxTier = (typeof SANDBOX_TIERS)[number];
const SANDBOX_SET: ReadonlySet<string> = new Set(SANDBOX_TIERS);

/** 校验 sandbox 值（vanilla setSandbox 同款：非法值忽略 → null → 调用方回落默认） */
function sanitizeSandbox(v: string | null | undefined): SandboxTier | null {
  return v != null && SANDBOX_SET.has(v) ? (v as SandboxTier) : null;
}

/**
 * 本端**不必转换就能直接收**的图片 mime（与 main.js `emrg:saveImage` 的扩展名白名单一致）。
 *
 * 它是一条快路径，不是「系统给什么」的边界（rant 2026-09-30T09:35:04 要求 4）：名单外的
 * 图片不再是静默丢弃，而是先请 main 把剪贴板里的图归一成 PNG，只有那一步也拿不到才拒绝。
 * 提到模块级是为了让图片落盘与粘贴入口读**同一份**名单——两份名单必然漂移。
 */
const SUPPORTED_IMAGE_MIME: ReadonlySet<string> = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/bmp",
  "image/svg+xml",
]);

/** 本端能否直接收这个 File（无 type 视为不能）。 */
function isSupportedImage(f: File): boolean {
  return !!f.type && SUPPORTED_IMAGE_MIME.has(f.type.toLowerCase());
}

/** base64 → 字节。剪贴板回图是 base64，包成 File 才能走同一条 attach 路径。 */
function base64Bytes(b64: string): ArrayBuffer {
  const bin = atob(b64);
  const buf = new ArrayBuffer(bin.length);
  const view = new Uint8Array(buf);
  for (let i = 0; i < bin.length; i += 1) view[i] = bin.charCodeAt(i);
  return buf;
}

/**
 * 一批待落盘的附件（issue #2920）。三个字段对应三条互不相同的结尾：
 * - `images` —— 本端能直接收的图 → `emrg:saveImage` → 正文一段 `[📷 label]` + vision 内容；
 * - `askClipboard` —— 事件里没有能直接收的图（macOS 的 TIFF 就是这个形状）→ 先问 main 要剪贴板里的图；
 * - `files` —— 其它一切 → `emrg:saveFile` → 正文里一行落盘路径。
 */
interface AttachPlan {
  images?: File[];
  files?: File[];
  askClipboard?: { declared: string[]; refused: string[] } | null;
}

export function Composer({
  store,
  sid = null,
  sandbox = null,
  busy: busyProp,
  sendMessage: send,
  saveImage: saveImageProp,
  saveFile: saveFileProp,
  readClipboardImage: readClipboardProp,
  logLine: logLineProp,
  cancel: cancelProp,
  onCommand,
  onSandboxChange,
  sandboxRoots = null,
  inputHistory = null,
  onPromptSent,
  onManageSandboxRoots,
  modelChip,
  insertRequest = null,
}: ComposerProps) {
  const { t } = useI18n();
  const [menu, setMenu] = useState<CmdMenuState>(CMD_MENU_CLOSED);
  const [text, setText] = useState("");
  const [hasText, setHasText] = useState(false);
  const [internalBusy, setInternalBusy] = useState(false);
  const busy = busyProp ?? internalBusy;
  // busy 的 ref 桥接（keydown 闭包内读实时 busy——Esc 停止判断，rant 2026-09-02T20:30:05）
  const busyRef = useRef(busy);
  busyRef.current = busy;
  // 沙箱档位（重构回归恢复，rant 2026-08-30T16:34:29）：默认 workspace-write（vanilla 同款），
  // 发送时随消息下发；切换仅允许三档（与 vanilla setSandbox 校验一致）。
  // 父级接管后（rant 2026-09-30T09:30:16 GUI 半边）本 state 只是**回落**：档位归
  // daemon 的会话属性，显示读 `sandbox` prop，点击走 onSandboxChange 上报。
  const [localTier, setLocalTier] = useState<SandboxTier>(sanitizeSandbox(sandbox) ?? "workspace-write");
  const parentOwnsTier = typeof onSandboxChange === "function";
  const tier: SandboxTier = parentOwnsTier
    ? (sanitizeSandbox(sandbox) ?? "workspace-write")
    : localTier;
  const chooseTier = (next: SandboxTier): void => {
    if (parentOwnsTier) onSandboxChange?.(next);
    else setLocalTier(next);
  };
  const queuedRef = useRef<Map<string, Array<{ requestId: string; text: string; sandbox?: string | null; images?: ImageAttach[] | null }>>>(new Map());
  // 图片附加（rant 2026-09-02T15:23:53）：pending 语义对齐 TUI _pending_images——
  // 占位符删除即弃图（发送时 filter）；ref 是同步副本（异步落盘期间文本可能又被改动）
  const [pending, setPending] = useState<ImageAttach[]>([]);
  const pendingRef = useRef<ImageAttach[]>([]);
  const attachRef = useRef<(plan: AttachPlan, at?: number | null) => void>(() => {});
  /** 隐藏的附件入口（粘贴、拖拽之外的第三个）。issue #2920 起它不再只收图片：`accept`
   * 去掉了，图片与任意文件都从这里进来，落盘走两条 IPC、正文里留两种东西。 */
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  // 输入框内容的同步副本：异步落盘回调在 await 之后要读「现在的文本」，不能读闭包里那份
  const textRef = useRef(text);
  const menuRef = useRef(menu);
  menuRef.current = menu;
  const tRef = useRef(t);
  tRef.current = t;
  // 草稿读写（rant 2026-09-01T20:28:31）：store/sid 走 ref 桥接
  const storeRef = useRef(store);
  storeRef.current = store;
  const sidRef = useRef(sid);
  sidRef.current = sid;
  // 输入历史召回（issue #2349 part 2/2）：列表来自 daemon（per-session、耐久、跨客户端），
  // 本组件只走指针：
  //   * inputHistoryRef —— prop 的实时镜像（列表随会话切换、或 daemon 重新读过而变）；
  //   * historyIndexRef —— 当前召回位置，-1 = 未在召回中；
  //   * historySavedRef —— 第一次按 ↑ 之前宿主正在写的那份草稿，走到最新一条再按 ↓ 时还给他。
  const inputHistoryRef = useRef<string[] | null>(inputHistory);
  inputHistoryRef.current = inputHistory;
  const historyIndexRef = useRef(-1);
  const historySavedRef = useRef("");
  // 回调也要 ref 桥接：submit 是每次渲染重建的函数，但发送发生在 await 之后，
  // 那一刻要用的是最新的父级实现。
  const onPromptSentRef = useRef(onPromptSent);
  onPromptSentRef.current = onPromptSent;
  /** 插入后把光标放回去（渲染之后才能设：受控 textarea 的值在这一帧才更新） */
  const caretAfterRenderRef = useRef<number | null>(null);

  // 发送函数解析（默认走 preload 桥）
  const sendFn =
    send ??
    ((opts: SendOptions) => {
      return (window as unknown as { emrg?: { sendMessage: (o: SendOptions) => Promise<SendResult> } }).emrg?.sendMessage(opts) ??
        Promise.reject(new Error("window.emrg.sendMessage unavailable"));
    });

  // 中断函数解析（rant 2026-09-02T20:30:05：busy 时发送按钮切换为停止按钮，
  // 默认走 preload 桥 emrg:cancel；测试注入假实现）
  // Rant 2026-09-20T12:50:13：cancel 必须点名会话——没有会话可点名就不发。
  const cancelFn =
    cancelProp ??
    ((sessionId: string) =>
      (window as unknown as { emrg?: { cancel?: (sid: string) => Promise<unknown> } }).emrg?.cancel?.(sessionId) ??
      Promise.resolve());
  const cancelRef = useRef(cancelFn);
  cancelRef.current = cancelFn;

  // 图片落盘函数解析（rant 2026-09-02T15:23:53：默认走 preload 桥 emrg:saveImage）
  const saveFn =
    saveImageProp ??
    ((payload: SaveImagePayload) =>
      (window as unknown as { emrg?: { saveImage?: (p: SaveImagePayload) => Promise<SaveImageResult> } }).emrg
        ?.saveImage?.(payload) ?? Promise.reject(new Error("window.emrg.saveImage unavailable")));
  const saveRef = useRef(saveFn);
  saveRef.current = saveFn;

  // 文件落盘函数解析（issue #2920：默认走 preload 桥 emrg:saveFile）
  const saveFileFn =
    saveFileProp ??
    ((payload: SaveFilePayload) =>
      (window as unknown as { emrg?: { saveFile?: (p: SaveFilePayload) => Promise<SaveFileResult> } }).emrg
        ?.saveFile?.(payload) ?? Promise.reject(new Error("window.emrg.saveFile unavailable")));
  const saveFileRef = useRef(saveFileFn);
  saveFileRef.current = saveFileFn;

  // 剪贴板读图解析（rant 2026-09-30T09:35:04 要求 4：默认走 preload 桥
  // emrg:readClipboardImage）。缺桥（jsdom / 旧 preload）→ reject，由调用方变成可见提示。
  const readClipboardFn =
    readClipboardProp ??
    (() =>
      (window as unknown as { emrg?: { readClipboardImage?: () => Promise<ClipboardImage | null> } }).emrg
        ?.readClipboardImage?.() ?? Promise.reject(new Error("window.emrg.readClipboardImage unavailable")));
  const readClipboardRef = useRef(readClipboardFn);
  readClipboardRef.current = readClipboardFn;
  // 日志桥（rant 2026-09-30T09:35:04 要求 3）：默认 preload 的 window.emrg.log；
  // 缺失时静默降级（jsdom / 预览环境不能因此崩溃）
  const logFn =
    logLineProp ??
    ((level: string, msg: string) => {
      try {
        (window as unknown as { emrg?: { log?: (l: string, m: string) => void } }).emrg?.log?.(level, msg);
      } catch {
        /* 日志失败不得影响输入 */
      }
    });
  const logRef = useRef(logFn);
  logRef.current = logFn;
  /** 附件路径的可见拒绝（要求 3：每次拒绝都要看得见，且有对称的日志行） */
  const [notice, setNotice] = useState<string | null>(null);
  const refuse = (key: string, params?: Record<string, unknown>): void => {
    const msg = tRef.current(key, params);
    setNotice(msg);
    logRef.current("warn", `[composer:attach] refused: ${msg}`);
  };

  /** 光标位置（没有元素时落到末尾） */
  const caretNow = (): number => taRef.current?.selectionStart ?? textRef.current.length;

  /**
   * 写入输入框内容。**同步先写 ref 再写 state**：一次点击可能连着插两段（先文本、后图片
   * 标记），后一段必须看见前一段——state 要等下一帧，ref 不等。
   */
  function applyText(next: string, caret?: number | null): void {
    textRef.current = next;
    setText(next);
    setHasText(next.trim().length > 0);
    const trimmed = next.trim();
    // / 指令补全：以 / 开头且无空格（仍处于指令词）→ 弹出菜单
    if (trimmed.startsWith("/") && !trimmed.includes(" ")) setMenu(menuForPrefix(trimmed, tRef.current));
    else setMenu(CMD_MENU_CLOSED);
    // 草稿持久化（rant 2026-09-01T20:28:31）：实时写 store，切视图（组件卸载）不丢
    storeRef.current.setComposerDraft(next, sidRef.current);
    if (caret != null) caretAfterRenderRef.current = caret;
  }

  /** 在光标处（或 `at` 处）原样插入一段字面文本，返回插入后的位置 */
  function insertRaw(chunk: string, at?: number | null): number {
    const cur = textRef.current;
    const pos = Math.max(0, Math.min(at ?? caretNow(), cur.length));
    applyText(cur.slice(0, pos) + chunk + cur.slice(pos), pos + chunk.length);
    return pos + chunk.length;
  }

  // 高度自适应（≤ MAX_INPUT_HEIGHT，之后内滚）+ 插入后的光标复位。
  // 无依赖数组 = 每次渲染都量一次：内容只可能随渲染变，量早了用的是上一帧的高度。
  useEffect(() => {
    const el = taRef.current;
    if (el) {
      el.style.height = "auto";
      const content = el.scrollHeight || 0;
      el.style.height = `${Math.min(content, MAX_INPUT_HEIGHT)}px`;
      el.style.overflowY = content > MAX_INPUT_HEIGHT ? "auto" : "hidden";
    }
    const caret = caretAfterRenderRef.current;
    if (caret != null && el) {
      caretAfterRenderRef.current = null;
      el.focus();
      el.setSelectionRange(caret, caret);
    }
  });

  // 草稿恢复（rant 2026-09-01T20:28:31）：挂载后把这份会话的草稿放回来
  useEffect(() => {
    const d = storeRef.current.getComposerDraft(sidRef.current);
    if (d) applyText(d);
    // 只在挂载时读一次：之后输入框的实时内容属于宿主，不是受控值
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 会话切换：保存旧会话草稿 → 载入新会话草稿（组件不卸载）
  const prevSidRef = useRef(sid);
  useEffect(() => {
    const prev = prevSidRef.current;
    if (prev === sid) return;
    if (prev != null) storeRef.current.setComposerDraft(textRef.current, prev);
    const d = sid != null ? storeRef.current.getComposerDraft(sid) : "";
    applyText(d ?? "");
    prevSidRef.current = sid;
    // 召回指针属于会话（issue #2349 part 2/2）：换了会话，位置与草稿快照都归零，
    // 否则在新会话里按 ↓ 会把上一个会话的草稿塞进来。
    historyIndexRef.current = -1;
    historySavedRef.current = "";
    // 会话切换：pending 图片属旧会话输入（TUI 语义——占位符即图的生命周期），丢弃
    pendingRef.current = [];
    setPending([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid, store]);

  // 外部一次性插入（issue #2384，GUI 半边）：`/rewind` 选中那条的原文回到输入框。
  //
  // 以 `id` 去重而不是「值变了就覆盖」：宿主的实时输入属于宿主，一次请求只应生效
  // 一次，否则之后每次重渲染都会把它写回输入框，把宿主接着打的字盖掉。光标置于末尾、
  // 绝不自动发送——发不发由宿主决定。
  const appliedInsertRef = useRef<number | null>(null);
  useEffect(() => {
    if (!insertRequest) return;
    if (appliedInsertRef.current === insertRequest.id) return;
    appliedInsertRef.current = insertRequest.id;
    applyText(insertRequest.text, insertRequest.text.length);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [insertRequest]);

  /* ── 键盘：补全菜单 / 输入历史 / Esc 停止 / Enter 发送 ── */
  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>): void {
    // / 补全菜单键盘导航（vanilla rant 19:44 P1）：↑↓ 移动、Enter 选择、Esc 关闭
    const m = menuRef.current;
    if (m.items.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMenu(menuNavigate(m, 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMenu(menuNavigate(m, -1));
        return;
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        const item = m.items[m.index];
        if (item) selectCmdRef.current(item.cmd);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMenu(CMD_MENU_CLOSED);
        return;
      }
    }
    // 输入历史召回（issue #2349 part 2/2，GUI 半边）：↑ 往回走、↓ 往前走到最新一条再
    // 一次就还回草稿。三条边界与 TUI 同语义，且都不是"顺手"能对的：
    //   1. **只在行的边界上**——`\n` 是纯文本框里唯一可靠的行边界（自动折行的半行不该算
    //      "已经离开第一行"），所以光标前没有换行才算在首行、后没有换行才算在末行；
    //   2. **未在召回中时 ↓ 什么也不做**——否则往下一行打字会突然吞掉光标；
    //   3. **草稿先存再覆盖**，并在走过最新一条之后原样还回去。
    // 列表为 null（还没问过 daemon）或空（这个会话没输入过）时一律放行，于是"读不到
    // 历史"和"历史是空的"在键盘上表现一致，正是宿主感受到的那个事实。
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      const list = inputHistoryRef.current;
      const up = e.key === "ArrowUp";
      if (!list || list.length === 0) return;
      const cur = textRef.current;
      const caret = taRef.current?.selectionStart ?? cur.length;
      const onFirstLine = cur.lastIndexOf("\n", Math.max(0, caret - 1)) === -1;
      const onLastLine = cur.indexOf("\n", caret) === -1;
      if (up ? !onFirstLine : !onLastLine) return;
      const idx = historyIndexRef.current;
      // ↓ 且不在召回中：光标照常下移。
      if (!up && idx < 0) return;
      const next = up
        ? idx < 0
          ? list.length - 1
          : Math.max(0, idx - 1)
        : idx + 1 < list.length
          ? idx + 1
          : -1;
      e.preventDefault();
      historyIndexRef.current = next;
      if (next < 0) {
        // 走过最新一条：召回结束，草稿原样回来（空草稿就是清空框）。
        const draft = historySavedRef.current;
        historySavedRef.current = "";
        applyText(draft, draft.length);
      } else {
        if (idx < 0) historySavedRef.current = cur;
        applyText(list[next], list[next].length);
      }
      return;
    }
    // Escape 停止回复（rant 2026-09-02T20:30:05：busy 时副入口，对齐 TUI ESC 中断肌肉记忆）。
    // 菜单开着时上方分支已拦截 Esc 只关菜单不误停；非 busy 时 Esc 放行（无副作用）。
    if (e.key === "Escape" && busyRef.current) {
      e.preventDefault();
      stopRef.current();
      return;
    }
    // Enter（非 Shift）与 Ctrl+Enter 同发送；Shift+Enter 交给 textarea 原生换行
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submitRef.current();
    }
  }

  /** 选择补全项：填充输入框 + 关菜单（用户可继续回车执行，vanilla selectCmd） */
  function selectCmd(cmd: string): void {
    applyText(cmd, cmd.length);
    // applyText 会按前缀重新算菜单；选完这一项就该收起（vanilla selectCmd 同行为），
    // 否则同一次回车既选了项又把菜单原样留在屏幕上。
    setMenu(CMD_MENU_CLOSED);
    taRef.current?.focus();
  }
  const selectCmdRef = useRef<(cmd: string) => void>(selectCmd);
  selectCmdRef.current = selectCmd;

  /* ── 附件落盘：图片按图片、其它文件给路径（issue #2920） ── */

  /** File → base64（FileReader，保留字节序与 data URL 解码一致） */
  function fileToBase64(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => {
        const dataUrl = String(r.result || "");
        resolve(dataUrl.split(",", 2)[1] ?? "");
      };
      r.onerror = () => reject(r.error ?? new Error("FileReader error"));
      r.readAsDataURL(file);
    });
  }

  /**
   * 逐张「附加图片」：落盘（IPC saveImage，返回绝对路径）→ 光标处插入占位符 →
   * 记 pendingImages（path/label 语义同 TUI _pending_images；position 发送时算）。
   * 返回下一个可用的插入位置。
   */
  async function attachImages(files: File[], pos: number, currentSid: string): Promise<number> {
    let at = pos;
    for (const file of files) {
      const mime = (file.type || "").toLowerCase();
      let b64: string;
      try {
        b64 = await fileToBase64(file);
      } catch (err) {
        refuse("composer.imageReasonRead", { msg: String((err as Error)?.message ?? err) });
        continue;
      }
      const n = pendingRef.current.length + 1;
      const stem = file.name ? file.name.replace(/\.[^.]+$/, "") : "";
      const display = stem ? toSafeImageLabel(stem) : `Image ${n}`;
      let res: SaveImageResult;
      try {
        res = await saveRef.current({ sessionId: currentSid, data: b64, label: display, mime });
      } catch (err) {
        // 落盘失败（无会话/类型不支持/超限）——说清是哪一条，并留一行对称的日志
        refuse("composer.imageReasonSave", { msg: String((err as Error)?.message ?? err) });
        continue;
      }
      const placeholder = imagePlaceholder(display);
      // 分隔符收在插入里（TUI 同形：`inp.insert(placeholder + "\n")`，`emrg/client/app.py`）：
      // 两个附件连着落盘时，没有它就拼成一个谁也不是的字面量（`…/a_1.txt…/b_2.txt`），
      // 模型按这个「路径」去读，两个文件一个都打不开。
      at = insertRaw(placeholder + "\n", at);
      pendingRef.current = [...pendingRef.current, { path: res.path, label: placeholder, mime: res.mime || mime }];
      setPending(pendingRef.current);
      logRef.current("info", `[composer:attach] image ${res.path}`);
    }
    return at;
  }

  /**
   * 逐个「附加文件」（issue #2920）：落盘（IPC saveFile）→ 光标处插入**一行路径**。
   *
   * 为什么是路径而不是内容：模型有一整套文件工具，一个路径让它按需读、按需截取，
   * 而把整个文件塞进消息只会把上下文一次性烧掉。图片是例外——它没有「读一部分」这回事。
   */
  async function attachFiles(files: File[], pos: number, currentSid: string): Promise<number> {
    let at = pos;
    for (const file of files) {
      let b64: string;
      try {
        b64 = await fileToBase64(file);
      } catch (err) {
        refuse("composer.fileReasonRead", { msg: String((err as Error)?.message ?? err) });
        continue;
      }
      let res: SaveFileResult;
      try {
        res = await saveFileRef.current({
          sessionId: currentSid,
          data: b64,
          name: file.name || "file",
          mime: file.type || undefined,
        });
      } catch (err) {
        refuse("composer.fileReasonSave", { msg: String((err as Error)?.message ?? err) });
        continue;
      }
      logRef.current("info", `[composer:attach] file ${res.path}`);
      // 同 `attachImages` 的分隔符：两个文件连着拖进来时，正文里必须是两个路径，
      // 不是一个把第二个路径接在第一个末尾的字面量。
      at = insertRaw(res.path + "\n", at);
    }
    return at;
  }

  /**
   * 事件里没有**本端能直接收**的图片时，改问 main 要剪贴板里的图（rant 要求 4）。
   *
   * macOS 把菜单栏、截图工具给的图声明为 `image/tiff`，renderer 解不了（Chromium 只出
   * png/jpeg/gif/webp/bmp/svg，canvas 也没有 TIFF 解码器）——同一次粘贴，TUI 收得到、
   * GUI 只能拒绝。转换发生在有 NSImage 的一侧（main 的 `clipboard.readImage()` →
   * `toPNG()`），拿回来的 PNG 走**同一条**图片落盘路径：同一套占位符、同一套拒绝。
   *
   * 拿不到就拒绝，且不静默（要求 3）：可见的提示说清**事件里到底给了什么**——
   * 名单外的文件（`imageReasonClipboard`，报出它的类型）或什么都没取到
   * （`imageReasonEmpty`，报出事件声明的类型）——而「剪贴板里没有图 / 桥不可用 /
   * IPC 报错」的具体原因另留一行 warn 日志。
   */
  async function attachClipboard(
    plan: { declared: string[]; refused: string[] },
    pos: number,
    currentSid: string,
  ): Promise<number> {
    let img: ClipboardImage | null = null;
    let why = "the clipboard holds no image";
    try {
      img = await readClipboardRef.current();
    } catch (err) {
      why = String((err as Error)?.message ?? err);
    }
    if (!img || !img.data) {
      // 转换这一路也拿不到：可见的提示说清**事件里给了什么**（要求 3），具体原因
      // （剪贴板里没有图 / 桥不可用 / IPC 报错）另留一行日志，两者都不静默。
      logRef.current("warn", `[composer:attach] clipboard fallback failed: ${why}`);
      if (plan.refused.length > 0) refuse("composer.imageReasonClipboard", { types: plan.refused.join(", ") });
      else refuse("composer.imageReasonEmpty", { types: plan.declared.join(", ") || "(nothing)" });
      return pos;
    }
    logRef.current(
      "info",
      `[composer:attach] clipboard fallback: ${plan.refused.join(", ") || "(no file)"} → ${img.mime || "image/png"}`,
    );
    const file = new File([base64Bytes(img.data)], img.name || "clipboard.png", { type: img.mime || "image/png" });
    return attachImages([file], pos, currentSid);
  }

  /**
   * 一批附件的入口（三个来源共用）：图片 → 剪贴板归一（若需要）→ 其它文件。
   * 顺序就是它们在正文里出现的顺序，位置从 `at` 起逐个顺延。
   */
  async function attach(plan: AttachPlan, at?: number | null): Promise<void> {
    const currentSid = sidRef.current;
    if (!currentSid) {
      refuse("composer.imageReasonNoSession");
      return;
    }
    const images = plan.images ?? [];
    const files = plan.files ?? [];
    logRef.current(
      "info",
      `[composer:attach] ${images.length} image(s), ${files.length} file(s)${
        plan.askClipboard ? ", clipboard fallback" : ""
      }`,
    );
    let pos = at ?? caretNow();
    if (images.length > 0) pos = await attachImages(images, pos, currentSid);
    if (plan.askClipboard) pos = await attachClipboard(plan.askClipboard, pos, currentSid);
    if (files.length > 0) pos = await attachFiles(files, pos, currentSid);
    taRef.current?.focus();
  }
  attachRef.current = (plan, at) => {
    void attach(plan, at);
  };

  /**
   * 把一次事件里的文件分成三类（粘贴、拖拽、选择文件共用）：
   * - `images`：本端能直接收的图；
   * - `undecodable`：声明是图、但本端解不了的类型（macOS 的 TIFF）——粘贴时请剪贴板归一，
   *   拖拽/选择文件时当作普通文件落盘（那本来就是磁盘上的一个文件，不是剪贴板里的图）；
   * - `files`：其它一切。
   */
  function classify(list: File[]): { images: File[]; undecodable: File[]; files: File[] } {
    const images = list.filter(isSupportedImage);
    const others = list.filter((f) => !isSupportedImage(f));
    const undecodable = others.filter((f) => isImageFile(f) && !isSupportedImage(f));
    const files = others.filter((f) => !undecodable.includes(f));
    return { images, undecodable, files };
  }

  /** 粘贴：文件先取文本（对齐 TUI「文本落、附件随后」），再交给 attach */
  function onPaste(e: React.ClipboardEvent<HTMLTextAreaElement>): void {
    const cd = e.clipboardData as unknown as ImageCarrier | null;
    if (!cd) return;
    setNotice(null);
    const list = collectFiles(cd);
    const types = declaredTypes(cd);
    const { images, undecodable, files } = classify(list);
    const pasted = (cd as unknown as DataTransfer).getData("text/plain");
    // 纯文本粘贴：交给 textarea 的原生行为，一个字都不改
    if (list.length === 0 && !declaresImageOrFile(types)) return;
    e.preventDefault();
    let at: number | null = null;
    if (pasted) at = insertRaw(pasted);
    // 名单外的图：只有在**一张能直接收的都没有**时才去问剪贴板——混合粘贴（png + tiff）里
    // png 已经能落盘，再取一次剪贴板会把同一张图重复附加（rant 要求 4 的既有教训）。
    let askClipboard: AttachPlan["askClipboard"] = null;
    if (images.length === 0 && (undecodable.length > 0 || list.length === 0)) {
      askClipboard = { declared: types, refused: undecodable.map((f) => f.type || "(no type)") };
    } else if (undecodable.length > 0) {
      // 有能直接收的图：不重复取剪贴板，只把收不了的那几张的类型说出来
      refuse("composer.imageReasonType", { types: undecodable.map((f) => f.type || "(no type)").join(", ") });
    }
    if (images.length === 0 && files.length === 0 && !askClipboard) return;
    attachRef.current({ images, files, askClipboard }, at);
  }

  /** 拖拽 / 选择文件：一律当磁盘上的文件——解不了的图也落盘给路径，不问剪贴板 */
  function onTakeFiles(list: File[]): void {
    if (list.length === 0) return;
    setNotice(null);
    const { images, undecodable, files } = classify(list);
    const all = [...files, ...undecodable];
    if (images.length === 0 && all.length === 0) return;
    attachRef.current({ images, files: all, askClipboard: null }, null);
  }

  async function submit(): Promise<void> {
    // 发送即原文（issue #2920 要求 2）：这里取的字符串就是框里的字符串，只去掉首尾空白
    // （纯空白不算一条消息）。没有 markdown 序列化，也没有发送前的反转义。
    const value = textRef.current.trim();
    if (!value) return;
    // 发送即召回结束（issue #2349 part 2/2）：下一次 ↑ 从最新一条重新开始，而保存的草稿
    // 已经随这条消息离手——留着它会在 ↓ 走到尽头时把上一条消息塞回空输入框。
    historyIndexRef.current = -1;
    historySavedRef.current = "";
    // 图片收敛（TUI 同语义）：占位符仍在文本中的保留（删了占位符即弃图）；
    // position = 标记在文本中的字符偏移（daemon 按 position 切块）
    const images = resolveSendImages(pendingRef.current, value);
    const parsed = parseInput(value);
    if (parsed.type !== "message") {
      // 指令：清输入 + 关菜单 + 路由（vanilla rant 19:44 P1：/ 开头不进 sendMessage）
      applyText("", 0);
      setMenu(CMD_MENU_CLOSED);
      onCommand?.({
        type: parsed.type,
        cmd: parsed.cmd,
        args: parsed.type === "command" ? parsed.args : undefined,
      });
      return;
    }
    if (!sid) {
      store.addSystemMessage(t("app.needSession"), sid);
      return;
    }
    // P2 queue-injection（#655）：busy 不再拦截——daemon 排队注入（task_queued），
    // 回合结束未注入则 queued_requeue 以原 requestId 重发。busy 时记录待重发条目。
    const wasBusy = busy;
    setInternalBusy(true);
    // G143：send 前预生成 requestId——消除 IPC 往返竞态窗口。（G143 原本还用它给消息打
    // 「自有流」标；那是客户端自持的“谁的消息”判定，已按 rant 2026-09-29T15:52:49 要求 3
    // 移除：daemon 是唯一状态源，客户端只是渲染器。）
    const requestId = genRequestId();
    store.addUserMessage(value, sid, { requestId });
    // 消息已发送 → 清输入（输入框内容即草稿，发送即清）
    applyText("", 0);
    if (wasBusy) {
      queueSend(queuedRef.current, sid, { requestId, text: value, sandbox: tier, images });
    }
    try {
      await sendFn({
        sessionId: sid,
        text: value,
        requestId,
        sandbox: tier,
        images: images.length ? images : null,
      });
      // 发送成功 → 清 pending（图片已随消息交给 daemon；失败保留以便重发）
      pendingRef.current = [];
      setPending([]);
      // 召回列表跟上这一条（issue #2349 part 2/2）：daemon 刚接受了它，所以它的文件
      // 现在以这段文字结尾。只报**成功**的发送——失败时 daemon 什么都没收下，也就
      // 没有可翻回来的东西；列表的权威副本仍是 daemon 的，下次会话切换整份替换。
      onPromptSentRef.current?.(sid, value);
    } catch {
      setInternalBusy(false);
      // G49：失败恢复输入框，文案不责怪用户（copy.sendFailed）；原文回填
      store.addSystemMessage(t("copy.sendFailed"), sid);
      applyText(value, value.length);
    }
  }
  const submitRef = useRef<() => Promise<void>>(submit);
  submitRef.current = submit;

  /**
   * 停止回复（rant 2026-09-02T20:30:05）：busy 时 stop-btn / Esc 触发。
   *
   * Rant 2026-09-20T12:50:13：这里只发一个**点名会话**的 cancel，别的什么都不做。
   * 曾经它在本地乐观地清 typing、打「已停止响应。」、置 busy=false —— 于是按 Esc 的
   * 一端「已经中断」，而真正跑着这轮的另一端的服务端毫无反应；现在结束这轮的唯一
   * 陈述是 daemon 的会话级 `cancelled` 回执（daemonBridge 的 cancelled 分支：清
   * typing、清计时、打那一行），谁问都一样，谁看也都一样。
   * 没有会话可点名就没有可问的事：不发帧（与 TUI `request_cancel` 同一规则）。
   */
  function stop(): void {
    const cur = sidRef.current;
    if (!cur) return;
    void cancelRef.current(cur);
  }
  const stopRef = useRef<() => void>(stop);
  stopRef.current = stop;

  return (
    <div
      className="composer"
      data-testid="composer"
      onDragOver={(e) => {
        // 允许文件拖入（否则浏览器显示 no-drop，drop 事件不触发）
        const t = e.dataTransfer?.types;
        if (t && Array.from(t).includes("Files")) e.preventDefault();
      }}
      onDrop={(e) => {
        const dt = e.dataTransfer as unknown as ImageCarrier | null;
        if (!dt) return;
        const list = collectFiles(dt);
        if (!list.length) return;
        e.preventDefault();
        onTakeFiles(list);
      }}
    >
      {menu.items.length > 0 ? (
        <div className="cmd-menu" role="menu" data-testid="cmd-menu">
          {menu.items.map((it, i) => (
            <button
              key={it.cmd}
              type="button"
              className={`cmd-menu-item${i === menu.index ? " selected" : ""}`}
              role="menuitem"
              data-testid={`cmd-item-${it.cmd}`}
              onMouseDown={(e) => {
                e.preventDefault(); // 防输入框失焦（vanilla mousedown + preventDefault）
                selectCmd(it.cmd);
              }}
            >
              <span className="cmd-menu-name">{it.cmd}</span>
              <span className="cmd-menu-hint">{it.hint}</span>
            </button>
          ))}
        </div>
      ) : null}
      <div className="mode-switcher" role="group" aria-label={t("composer.sandboxTitle")} data-testid="sandbox-switcher">
        {SANDBOX_TIERS.map((tierKey) => {
          const i18nKey =
            tierKey === "read-only"
              ? "composer.sandboxReadOnly"
              : tierKey === "workspace-write"
                ? "composer.sandboxWorkspaceWrite"
                : "composer.sandboxFullAccess";
          const shortKey =
            tierKey === "read-only"
              ? "composer.sandboxShortReadOnly"
              : tierKey === "workspace-write"
                ? "composer.sandboxShortWorkspaceWrite"
                : "composer.sandboxShortFullAccess";
          return (
            <button
              key={tierKey}
              type="button"
              className={`mode-btn${tier === tierKey ? " active" : ""}`}
              data-sandbox={tierKey}
              title={t(i18nKey)}
              aria-label={t(i18nKey)}
              aria-pressed={tier === tierKey}
              data-testid={`sandbox-${tierKey}`}
              onClick={() => chooseTier(tierKey)}
            >
              {t(shortKey)}
            </button>
          );
        })}
        {typeof onManageSandboxRoots === "function" ? (
          // The entry to the extra writable roots (rant 2026-10-09T09:43:39, GUI
          // half). It sits inside the tier group because the two are one subject:
          // a root is the narrower instrument you reach for *instead of* lifting
          // the tier, and a host looking for either looks here. The count is the
          // daemon's list, and it is omitted while nobody has asked — a `0` there
          // would claim a state the daemon never reported.
          <button
            type="button"
            className="mode-btn"
            title={t("composer.rootsTitle")}
            aria-label={t("composer.rootsTitle")}
            data-testid="sandbox-roots-open"
            onClick={onManageSandboxRoots}
          >
            {sandboxRoots === null
              ? t("composer.rootsShort")
              : t("composer.rootsShortCount", { n: String(sandboxRoots.length) })}
          </button>
        ) : null}
        {modelChip}
      </div>
      <div className="composer-card">
        <textarea
          ref={taRef}
          className="composer-textarea"
          data-testid="composer-input"
          aria-label={t("composer.placeholder")}
          placeholder={t("composer.placeholder")}
          rows={1}
          spellCheck={false}
          value={text}
          onChange={(e) => applyText(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
        />
        {/* 第三条入口：选择图片或任意文件（与粘贴、拖拽并列）。按钮只负责打开系统文件框，
            落盘与插入全交给 attachRef —— 三条入口一条路径，失败提示也共用一套 */}
        <button
          type="button"
          className="attach-btn"
          title={t("composer.attach")}
          aria-label={t("composer.attach")}
          data-testid="composer-attach"
          onMouseDown={(e) => e.preventDefault()} // 防失焦——插入要落在原光标处
          onClick={() => fileInputRef.current?.click()}
        >
          📎
        </button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          data-testid="composer-file-input"
          onChange={(e) => {
            const el = e.currentTarget;
            const picked = Array.from(el.files ?? []);
            // 复位：同一个文件连续选两次也要再触发 change（不清空则第二次没有事件）
            el.value = "";
            onTakeFiles(picked);
          }}
        />
        {busy ? (
          <button
            type="button"
            className="stop-btn"
            title={t("composer.stop")}
            aria-label={t("composer.stop")}
            onClick={() => stop()}
            data-testid="composer-stop"
          >
            ■
          </button>
        ) : (
          <button
            type="button"
            className="send-btn"
            disabled={!hasText}
            title={t("composer.send")}
            aria-label={t("composer.send")}
            onClick={() => void submit()}
            data-testid="composer-send"
          >
            ↑
          </button>
        )}
      </div>
      {notice ? (
        <div className="composer-notice" role="alert" data-testid="composer-notice">
          {notice}
        </div>
      ) : null}
    </div>
  );
}
