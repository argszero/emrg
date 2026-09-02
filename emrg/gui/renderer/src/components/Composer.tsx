import { useEffect, useRef, useState, type MutableRefObject } from "react";
import { EditorContent, useEditor, useEditorState, type Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Placeholder from "@tiptap/extension-placeholder";
import { Markdown } from "tiptap-markdown";
import DOMPurify from "../../../vendor/dompurify.min.js";
import { parseInput } from "../lib/commands";
import {
  CMD_MENU_CLOSED,
  menuForPrefix,
  menuNavigate,
  queueSend,
  imagePlaceholder,
  toSafeImageLabel,
  normalizePlaceholders,
  resolveSendImages,
  type CmdMenuState,
  type ImageAttach,
} from "../lib/composer";
import { genRequestId } from "../lib/utils";
import { useI18n } from "../lib/i18n";
import type { TranscriptStore } from "../lib/transcript";
import { LinkDialog } from "./LinkDialog";

/**
 * Composer — 输入框 + / 补全菜单 + 发送流（tiptap 版，rant 2026-08-28T14:07:29 Stage 1）。
 *
 * Stage 1 = 编辑器核心替换：textarea → @tiptap/react 内容可编辑编辑器
 * （StarterKit + Link + Placeholder + tiptap-markdown 序列化）。
 * - 序列化（rant 核心原则）：发送时 editor.storage.markdown.getMarkdown() 出 markdown
 *   字符串，走现有 sendMessage 路径。history.jsonl / 协议层 / TUI 零改动；
 *   存储仍是 markdown str，双端渲染不变。
 * - 保留现有功能：Enter=发送 / Shift+Enter=换行（hardBreak）/ Ctrl+Enter=发送、
 *   / 补全菜单（editor.onUpdate + keydown 拦截）、高度自适应 ≤150px（CSS max-height）、
 *   G143 requestId 预生成、P2 busy 队列注入（#655）、失败恢复（setContent 回填 markdown）。
 * - Stage 2（本 cycle）：mini 格式栏（粗体/斜体/删除线/行内代码/链接/无序/有序/引用/标题）
 *   + 格式快捷键（⌘B/I/⇧S/E/K/⇧7/⇧8/⇧B/Alt 1-3——K/⇧7/⇧8 为显式补挂，其余 StarterKit 内置）
 *   + DOMPurify 粘贴净化（transformPastedHTML 拦截 HTML 粘贴路径，防恶意脚本）。
 * - sendMessage 注入式（测试传假实现；默认 window.emrg.sendMessage）。
 * - 类名与 vanilla 一致（composer-card / cmd-menu / cmd-menu-item / send-btn）。
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
  /** 注入中断函数（默认 window.emrg.cancel；busy 时发送按钮切换为停止按钮，rant 2026-09-02T20:30:05） */
  cancel?: () => Promise<unknown>;
  /** / 指令路由回调（Batch 5 接线：/clear /model /memory …） */
  onCommand?: (routing: CommandRouting) => void;
  /** 测试注入：挂载后回填 tiptap Editor 实例（命令驱动测试用） */
  editorRef?: MutableRefObject<Editor | null>;
}

const MAX_INPUT_HEIGHT = 150;

/** 沙箱三档（rant 2026-08-20T18:18，vanilla mode-switcher 同款；重构回归恢复） */
export const SANDBOX_TIERS = ["read-only", "workspace-write", "danger-full-access"] as const;
export type SandboxTier = (typeof SANDBOX_TIERS)[number];
const SANDBOX_SET: ReadonlySet<string> = new Set(SANDBOX_TIERS);

/** 校验 sandbox 值（vanilla setSandbox 同款：非法值忽略 → null → 调用方回落默认） */
function sanitizeSandbox(v: string | null | undefined): SandboxTier | null {
  return v != null && SANDBOX_SET.has(v) ? (v as SandboxTier) : null;
}

/** 格式命令（Stage 2）：以 editor 为参的纯函数——editor 单例闭包经 fmtRef 桥接给按钮/快捷键 */
function cycleHeading(ed: Editor | null): void {
  if (!ed) return;
  // tiptap v3 isActive/getAttributes 在混合选区（heading + 尾随空段落）不可靠 →
  // 从选区起点所在块节点直接读级别（toggleHeading 会把末块转标题并追加尾随空段落）
  const { from } = ed.state.selection;
  const parent = ed.state.doc.resolve(from).parent;
  const level = parent.type.name === "heading" ? (parent.attrs.level as number) : 0;
  const next = level >= 3 ? 0 : level + 1; // p → h1 → h2 → h3 → p
  if (next === 0) ed.chain().focus().setParagraph().run();
  else ed.chain().focus().toggleHeading({ level: next as 1 | 2 | 3 }).run();
}

/** 格式栏按钮定义（id → 命令 + 标签类 + i18n key） */
const FMT_BUTTONS = [
  { id: "bold", label: "B", labelClass: "fmt-label-bold" },
  { id: "italic", label: "I", labelClass: "fmt-label-italic" },
  { id: "strike", label: "S", labelClass: "fmt-label-strike" },
  { id: "code", label: "</>", labelClass: "fmt-label-code" },
  { id: "link", label: "🔗", labelClass: "" },
  { id: "bullet", label: "•", labelClass: "" },
  { id: "ordered", label: "1.", labelClass: "" },
  { id: "quote", label: "❝", labelClass: "" },
  { id: "heading", label: "H", labelClass: "fmt-label-heading" },
] as const;


export function Composer({
  store,
  sid = null,
  sandbox = null,
  busy: busyProp,
  sendMessage: send,
  saveImage: saveImageProp,
  cancel: cancelProp,
  onCommand,
  editorRef,
}: ComposerProps) {
  const { t } = useI18n();
  const [menu, setMenu] = useState<CmdMenuState>(CMD_MENU_CLOSED);
  const [hasText, setHasText] = useState(false);
  const [internalBusy, setInternalBusy] = useState(false);
  const busy = busyProp ?? internalBusy;
  // busy 的 ref 桥接（keydown 一次性闭包内读实时 busy——Esc 停止判断，rant 2026-09-02T20:30:05）
  const busyRef = useRef(busy);
  busyRef.current = busy;
  // 沙箱档位（重构回归恢复，rant 2026-08-30T16:34:29）：默认 workspace-write（vanilla 同款），
  // 发送时随消息下发；切换仅允许三档（与 vanilla setSandbox 校验一致）。
  const [tier, setTier] = useState<SandboxTier>(sanitizeSandbox(sandbox) ?? "workspace-write");
  // ⚠️ (rant 2026-08-28T22:27:01) 链接改用应用内对话框收集 URL（Electron 禁用 window.prompt）
  const [linkDialogOpen, setLinkDialogOpen] = useState(false);
  const [linkHref, setLinkHref] = useState("");
  const queuedRef = useRef<Map<string, Array<{ requestId: string; text: string; sandbox?: string | null; images?: ImageAttach[] | null }>>>(new Map());
  // 图片附加（rant 2026-09-02T15:23:53）：pending 语义对齐 TUI _pending_images——
  // 占位符删除即弃图（发送时 filter）；ref 镜像供 tiptap 一次性闭包读写
  const [pending, setPending] = useState<ImageAttach[]>([]);
  const pendingRef = useRef<ImageAttach[]>([]);
  const attachRef = useRef<(files: File[], at?: number | null) => void>(() => {});
  const insertRawRef = useRef<(text: string, at?: number | null) => void>(() => {});
  // tiptap 选项闭包只创建一次 → 变化值走 ref 桥接（menu/submit/selectCmd/t）
  const menuRef = useRef(menu);
  menuRef.current = menu;
  const tRef = useRef(t);
  tRef.current = t;
  // 草稿读写（rant 2026-09-01T20:28:31）：useEditor 闭包只创建一次 → store/sid 走 ref 桥接
  const storeRef = useRef(store);
  storeRef.current = store;
  const sidRef = useRef(sid);
  sidRef.current = sid;
  // 格式命令 ref 桥接（editor/菜单闭包只创建一次 → 变化值走 ref）
  const fmtRef = useRef<Record<string, () => void>>({});
  fmtRef.current = {
    bold: () => editor?.chain().focus().toggleBold().run(),
    italic: () => editor?.chain().focus().toggleItalic().run(),
    strike: () => editor?.chain().focus().toggleStrike().run(),
    code: () => editor?.chain().focus().toggleCode().run(),
    bullet: () => editor?.chain().focus().toggleBulletList().run(),
    ordered: () => editor?.chain().focus().toggleOrderedList().run(),
    quote: () => editor?.chain().focus().toggleBlockquote().run(),
    heading: () => cycleHeading(editor),
    link: () => openLinkDialog(),
  };

  // 发送函数解析（默认走 preload 桥）
  const sendFn =
    send ??
    ((opts: SendOptions) => {
      return (window as unknown as { emrg?: { sendMessage: (o: SendOptions) => Promise<SendResult> } }).emrg?.sendMessage(opts) ??
        Promise.reject(new Error("window.emrg.sendMessage unavailable"));
    });

  // 中断函数解析（rant 2026-09-02T20:30:05：busy 时发送按钮切换为停止按钮，
  // 默认走 preload 桥 emrg:cancel；测试注入假实现）
  const cancelFn =
    cancelProp ??
    (() =>
      (window as unknown as { emrg?: { cancel?: () => Promise<unknown> } }).emrg?.cancel?.() ??
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
  // tiptap 一次性闭包内读写 editor 的桥（editor 变量在 useEditor 之后才声明）
  const editorBoxRef = useRef<Editor | null>(null);

  const editor = useEditor({
    extensions: [
      StarterKit.configure({ link: { openOnClick: false } }),
      Placeholder.configure({ placeholder: tRef.current("composer.placeholder") }),
      Markdown,
    ],
    content: "",
    editorProps: {
      attributes: {
        class: "tiptap-input",
        "data-testid": "composer-input",
        "aria-label": tRef.current("composer.placeholder"),
      },
      handleKeyDown: (_view, event) => {
        // / 补全菜单键盘导航（vanilla rant 19:44 P1）：↑↓ 移动、Enter 选择、Esc 关闭
        const m = menuRef.current;
        if (m.items.length > 0) {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setMenu(menuNavigate(m, 1));
            return true;
          }
          if (event.key === "ArrowUp") {
            event.preventDefault();
            setMenu(menuNavigate(m, -1));
            return true;
          }
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            const item = m.items[m.index];
            if (item) selectCmdRef.current(item.cmd);
            return true;
          }
          if (event.key === "Escape") {
            event.preventDefault();
            setMenu(CMD_MENU_CLOSED);
            return true;
          }
        }
        // Escape 停止回复（rant 2026-09-02T20:30:05：busy 时副入口，对齐 TUI ESC 中断肌肉记忆）。
        // 菜单开着时上方分支已拦截 Esc 只关菜单不误停；非 busy 时 Esc 放行（无副作用）。
        if (event.key === "Escape" && busyRef.current) {
          event.preventDefault();
          stopRef.current();
          return true;
        }
        // Enter（非 Shift）与 Ctrl+Enter 同发送（Ctrl+Enter 的 ctrlKey 不影响首分支）；
        // Shift+Enter 交给 tiptap hardBreak（换行）
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          void submitRef.current();
          return true;
        }
        // 格式快捷键（Stage 2）：K/⇧7/⇧8 无内置绑定 → 显式补挂；其余（B/I/⇧S/E/⇧B/Alt 1-3）
        // 由 StarterKit 内置 keymap 处理（这里 return false 放行）
        const mod = event.metaKey || event.ctrlKey;
        if (mod && event.code === "Digit8" && event.shiftKey) {
          event.preventDefault();
          fmtRef.current.bullet();
          return true;
        }
        if (mod && event.code === "Digit7" && event.shiftKey) {
          event.preventDefault();
          fmtRef.current.ordered();
          return true;
        }
        if (mod && event.key.toLowerCase() === "k") {
          event.preventDefault();
          fmtRef.current.link();
          return true;
        }
        return false;
      },
      // Stage 2 粘贴安全：HTML 粘贴路径经 DOMPurify 净化（script/事件属性剥离；
      // tiptap-markdown 的纯文本粘贴分支本身无 HTML 注入面）
      transformPastedHTML: (html) => DOMPurify.sanitize(html),
      // rant 2026-09-02T15:23:53：剪贴板图片粘贴——同带 text/plain 先插文本
      // （对齐 TUI「文本粘贴后追加图」），图片逐张走「附加图片」落盘 + 占位符
      handlePaste: (_view, event) => {
        const cd = event.clipboardData;
        if (!cd) return false;
        const files = Array.from(cd.files || []);
        const imgs = files.filter((f) => f.type && f.type.startsWith("image/"));
        if (imgs.length === 0) return false;
        event.preventDefault();
        const text = cd.getData("text/plain");
        const ed = editorBoxRef.current;
        if (text && ed) {
          const from = ed.state.selection.from;
          insertRawRef.current(text, from);
          attachRef.current(imgs, from + text.length);
        } else {
          attachRef.current(imgs, null);
        }
        return true;
      },
      // rant 2026-09-02T15:23:53：拖拽图片文件进输入区
      handleDrop: (_view, event) => {
        const dt = event.dataTransfer;
        if (!dt) return false;
        const files = Array.from(dt.files || []);
        if (files.length === 0) return false;
        const imgs = files.filter((f) => f.type && f.type.startsWith("image/"));
        if (imgs.length === 0) return false;
        event.preventDefault();
        attachRef.current(imgs, null);
        return true;
      },
    },
    onUpdate: ({ editor }) => {
      const text = editor.getText();
      const trimmed = text.trim();
      setHasText(trimmed.length > 0);
      // / 指令补全：以 / 开头且无空格（仍处于指令词）→ 弹出菜单
      if (trimmed.startsWith("/") && !trimmed.includes(" ")) setMenu(menuForPrefix(trimmed, tRef.current));
      else setMenu(CMD_MENU_CLOSED);
      // 草稿持久化（rant 2026-09-01T20:28:31）：实时写 store（markdown 序列化），
      // 切视图（组件卸载）不丢；发送后 clearContent → onUpdate 空内容 → 草稿自动清空
      const md = (editor.storage as unknown as { markdown: { getMarkdown(): string } }).markdown.getMarkdown();
      storeRef.current.setComposerDraft(md, sidRef.current);
    },
    onCreate: ({ editor }) => {
      setHasText(!editor.isEmpty);
      // 草稿恢复（rant 2026-09-01T20:28:31）：重挂载后经 tiptap-markdown setContent 解析回填
      const d = storeRef.current.getComposerDraft(sidRef.current);
      if (d) {
        editor.commands.setContent(d);
        setHasText(true);
      }
    },
    immediatelyRender: false,
  });

  // 会话切换：保存旧会话草稿 → 载入新会话草稿（组件不卸载，editor 复用）
  const prevSidRef = useRef(sid);
  useEffect(() => {
    const prev = prevSidRef.current;
    if (prev === sid || !editor) return;
    if (prev != null) {
      const md = (editor.storage as unknown as { markdown: { getMarkdown(): string } }).markdown.getMarkdown();
      storeRef.current.setComposerDraft(md, prev);
    }
    const d = sid != null ? storeRef.current.getComposerDraft(sid) : "";
    if (d) {
      editor.commands.setContent(d);
      setHasText(true);
    } else {
      editor.commands.clearContent();
      setHasText(false);
    }
    prevSidRef.current = sid;
    // 会话切换：pending 图片属旧会话输入（TUI 语义——占位符即图的生命周期），丢弃
    pendingRef.current = [];
    setPending([]);
  }, [sid, editor, store]);

  // 测试注入：editor 实例回填（兼作 tiptap 一次性闭包的实例桥）
  useEffect(() => {
    editorBoxRef.current = editor;
    if (editorRef) editorRef.current = editor;
    return () => {
      editorBoxRef.current = null;
      if (editorRef) editorRef.current = null;
    };
  }, [editor, editorRef]);

  // 格式栏激活态（Stage 2）：订阅 editor 事务，按钮高亮当前格式（无 deps → 每次事务重算）
  const active =
    useEditorState({
      editor,
      selector: ({ editor: e }) => ({
        bold: e?.isActive("bold") ?? false,
        italic: e?.isActive("italic") ?? false,
        strike: e?.isActive("strike") ?? false,
        code: e?.isActive("code") ?? false,
        bullet: e?.isActive("bulletList") ?? false,
        ordered: e?.isActive("orderedList") ?? false,
        quote: e?.isActive("blockquote") ?? false,
        heading: e?.isActive("heading") ?? false,
      }),
    }) ?? {
      bold: false,
      italic: false,
      strike: false,
      code: false,
      bullet: false,
      ordered: false,
      quote: false,
      heading: false,
    };

  // ⚠️ (rant 2026-08-28T22:27:01) 链接用应用内对话框收集 URL（Electron 禁用 window.prompt）
  function openLinkDialog(): void {
    if (!editor) return;
    if (editor.isActive("link")) {
      // 已激活链接 → 直接解除（不在对话框内处理，避免歧义）
      editor.chain().focus().extendMarkRange("link").unsetLink().run();
      return;
    }
    // 预填当前选区 href（若有）或默认 https://
    let cur = "";
    try {
      cur = editor.getAttributes("link").href ?? "";
    } catch {
      cur = "";
    }
    setLinkHref(cur || "https://");
    setLinkDialogOpen(true);
  }
  function applyLink(href: string): void {
    if (!editor) return;
    editor.chain().focus().extendMarkRange("link").setLink({ href }).run();
    setLinkDialogOpen(false);
  }
  function cancelLink(): void {
    setLinkDialogOpen(false);
  }

  async function submit(): Promise<void> {
    if (!editor) return;
    // tiptap-markdown 的 storage 类型不并入 Editor.storage（Storage 泛型）——运行时存在，显式取值
    const mdRaw = (editor.storage as unknown as { markdown: { getMarkdown(): string } }).markdown.getMarkdown();
    // rant 2026-09-02T15:23:53：tiptap-markdown 会把占位符转义成 \[📷 …\]（防被当链接语法），
    // 发送前还原字面量——daemon 收到的文本与 TUI 完全一致（历史互操作）
    const md = normalizePlaceholders(mdRaw, pendingRef.current.map((p) => p.label));
    const value = md.trim();
    if (!value) return;
    // 图片收敛（TUI 同语义）：占位符仍在文本中的保留（删了占位符即弃图）；
    // position = 字面占位符在文本中的字符偏移（daemon 按 position 切块）
    const images = resolveSendImages(pendingRef.current, value);
    const parsed = parseInput(value);
    if (parsed.type !== "message") {
      // 指令：清输入 + 关菜单 + 路由（vanilla rant 19:44 P1：/ 开头不进 sendMessage）
      editor.commands.clearContent();
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
    store.addUserMessage(value, sid);
    editor.commands.clearContent();
    // B3：消息已发送 → 清除草稿（tiptap 内容即草稿，发送即清）
    // G143：send 前预生成 requestId 并标记自有流——消除 IPC 往返竞态窗口
    const requestId = genRequestId();
    store.setOwnStream(requestId);
    if (wasBusy) {
      queueSend(queuedRef.current, sid, { requestId, text: value, sandbox: tier, images });
    }
    try {
      const res = await sendFn({
        sessionId: sid,
        text: value,
        requestId,
        sandbox: tier,
        images: images.length ? images : null,
      });
      store.setOwnStream(res.requestId || requestId); // G124：以 daemon 回显为准
      // 发送成功 → 清 pending（图片已随消息交给 daemon；失败保留以便重发）
      pendingRef.current = [];
      setPending([]);
    } catch {
      setInternalBusy(false);
      store.setOwnStream(null);
      // G49：失败恢复输入框，文案不责怪用户（copy.sendFailed）；markdown 回填
      store.addSystemMessage(t("copy.sendFailed"), sid);
      editor.commands.setContent(value);
    }
  }
  const submitRef = useRef<() => Promise<void>>(submit);
  submitRef.current = submit;

  /**
   * 停止回复（rant 2026-09-02T20:30:05）：busy 时 stop-btn / Esc 触发。
   * 本地乐观恢复（清 typing + 系统消息，对齐 TUI ESC 中断提示「⏸ Interrupted」）；
   * daemon cancelled 广播随后幂等清外部 busy（daemonBridge releaseOwnStream，无需在此处理）。
   */
  function stop(): void {
    setInternalBusy(false);
    const st = storeRef.current;
    const cur = sidRef.current;
    if (cur) {
      st.clearTyping(cur);
      st.addSystemMessage(tRef.current("chat.interrupted"), cur);
    }
    void cancelRef.current();
  }
  const stopRef = useRef<() => void>(stop);
  stopRef.current = stop;

  /** 选择补全项：填充输入框 + 关菜单（用户可继续回车执行，vanilla selectCmd） */
  function selectCmd(cmd: string): void {
    if (!editor) return;
    editor.commands.setContent(cmd); // tiptap-markdown setContent 解析 markdown → 纯文本段落
    setMenu(CMD_MENU_CLOSED);
    editor.commands.focus();
  }
  const selectCmdRef = useRef<(cmd: string) => void>(selectCmd);
  selectCmdRef.current = selectCmd;

  /* ── 图片附加（rant 2026-09-02T15:23:53：粘贴/拖拽 → IPC 落盘 → 光标处占位符）── */

  /** 原始文本直插（不经 tiptap-markdown 解析——粘贴文本/占位符都要字面量） */
  function insertRawText(text: string, at?: number | null): void {
    const ed = editorBoxRef.current;
    if (!ed) return;
    const pos = at ?? ed.state.selection.from;
    ed.view.dispatch(ed.state.tr.insertText(text, pos));
  }
  insertRawRef.current = insertRawText;

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

  /** 受支持图片 mime（与 main.js emrg:saveImage 的扩展名白名单一致） */
  const SUPPORTED_IMAGE_MIME: ReadonlySet<string> = new Set([
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/svg+xml",
  ]);

  /**
   * 逐张「附加图片」：落盘（IPC saveImage，返回绝对路径）→ 光标处插入占位符 →
   * 记 pendingImages（path/label/position 语义同 TUI _pending_images）。
   */
  async function attachImages(files: File[], at?: number | null): Promise<void> {
    const ed = editorBoxRef.current;
    const sid = sidRef.current;
    if (!ed || !sid) return;
    const imgs = files.filter((f) => f.type && SUPPORTED_IMAGE_MIME.has(f.type.toLowerCase()));
    if (imgs.length === 0) return;
    let pos = at ?? ed.state.selection.from;
    for (const file of imgs) {
      const mime = file.type.toLowerCase();
      let b64: string;
      try {
        b64 = await fileToBase64(file);
      } catch {
        continue;
      }
      const n = pendingRef.current.length + 1;
      const stem = file.name ? file.name.replace(/\.[^.]+$/, "") : "";
      const display = stem ? toSafeImageLabel(stem) : `Image ${n}`;
      let res: SaveImageResult;
      try {
        res = await saveRef.current({ sessionId: sid, data: b64, label: display, mime });
      } catch {
        continue; // 落盘失败（无会话/类型不支持/超限）→ 跳过，不插幽灵占位符
      }
      const placeholder = imagePlaceholder(display);
      insertRawText(placeholder, pos);
      pos += placeholder.length;
      pendingRef.current = [...pendingRef.current, { path: res.path, label: placeholder, mime: res.mime || mime }];
      setPending(pendingRef.current);
    }
    ed.commands.focus();
  }
  attachRef.current = (files, at) => {
    void attachImages(files, at);
  };

  return (
    <div
      className="composer"
      data-testid="composer"
      onDragOver={(e) => {
        // 允许图片文件拖入（否则浏览器显示 no-drop，drop 事件不触发）
        const t = e.dataTransfer?.types;
        if (t && Array.from(t).includes("Files")) e.preventDefault();
      }}
      onDrop={(e) => {
        if (e.defaultPrevented) return; // tiptap handleDrop 已处理（编辑器内）
        const dt = e.dataTransfer;
        if (!dt) return;
        const files = Array.from(dt.files || []);
        const imgs = files.filter((f) => f.type && f.type.startsWith("image/"));
        if (!imgs.length) return;
        e.preventDefault();
        attachRef.current(imgs, null);
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
      <div className="fmt-bar" role="toolbar" aria-label={t("composer.formatBar")} data-testid="fmt-bar">
        {FMT_BUTTONS.map((b) => (
          <button
            key={b.id}
            type="button"
            className={`fmt-btn${active[b.id as keyof typeof active] ? " active" : ""}`}
            title={t(`composer.${b.id}`)}
            aria-label={t(`composer.${b.id}`)}
            aria-pressed={active[b.id as keyof typeof active]}
            data-testid={`fmt-${b.id}`}
            onMouseDown={(e) => e.preventDefault()} // 防失焦——保持选区，点击后命令可作用于选中文本
            onClick={() => fmtRef.current[b.id]()}
          >
            <span className={b.labelClass}>{b.label}</span>
          </button>
        ))}
      </div>
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
              onClick={() => setTier(tierKey)}
            >
              {t(shortKey)}
            </button>
          );
        })}
      </div>
      <div className="composer-card">
        <EditorContent editor={editor} className="composer-editor" />
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
      <LinkDialog
        open={linkDialogOpen}
        currentHref={linkHref}
        onApply={applyLink}
        onCancel={cancelLink}
      />
    </div>
  );
}
