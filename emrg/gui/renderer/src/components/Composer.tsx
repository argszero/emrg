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
  type CmdMenuState,
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
}
export interface SendResult {
  requestId?: string;
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
  /** / 指令路由回调（Batch 5 接线：/clear /model /memory …） */
  onCommand?: (routing: CommandRouting) => void;
  /** 测试注入：挂载后回填 tiptap Editor 实例（命令驱动测试用） */
  editorRef?: MutableRefObject<Editor | null>;
}

const MAX_INPUT_HEIGHT = 150;

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
  onCommand,
  editorRef,
}: ComposerProps) {
  const { t } = useI18n();
  const [menu, setMenu] = useState<CmdMenuState>(CMD_MENU_CLOSED);
  const [hasText, setHasText] = useState(false);
  const [internalBusy, setInternalBusy] = useState(false);
  const busy = busyProp ?? internalBusy;
  // ⚠️ (rant 2026-08-28T22:27:01) 链接改用应用内对话框收集 URL（Electron 禁用 window.prompt）
  const [linkDialogOpen, setLinkDialogOpen] = useState(false);
  const [linkHref, setLinkHref] = useState("");
  const queuedRef = useRef<Map<string, Array<{ requestId: string; text: string; sandbox?: string | null }>>>(new Map());
  // tiptap 选项闭包只创建一次 → 变化值走 ref 桥接（menu/submit/selectCmd/t）
  const menuRef = useRef(menu);
  menuRef.current = menu;
  const tRef = useRef(t);
  tRef.current = t;
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
    },
    onUpdate: ({ editor }) => {
      const text = editor.getText();
      const trimmed = text.trim();
      setHasText(trimmed.length > 0);
      // / 指令补全：以 / 开头且无空格（仍处于指令词）→ 弹出菜单
      if (trimmed.startsWith("/") && !trimmed.includes(" ")) setMenu(menuForPrefix(trimmed, tRef.current));
      else setMenu(CMD_MENU_CLOSED);
    },
    onCreate: ({ editor }) => {
      setHasText(!editor.isEmpty);
    },
    immediatelyRender: false,
  });

  // 测试注入：editor 实例回填
  useEffect(() => {
    if (editorRef) editorRef.current = editor;
    return () => {
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
    const md = (editor.storage as unknown as { markdown: { getMarkdown(): string } }).markdown.getMarkdown();
    const value = md.trim();
    if (!value) return;
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
      queueSend(queuedRef.current, sid, { requestId, text: value, sandbox });
    }
    try {
      const res = await sendFn({ sessionId: sid, text: value, requestId, sandbox });
      store.setOwnStream(res.requestId || requestId); // G124：以 daemon 回显为准
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

  /** 选择补全项：填充输入框 + 关菜单（用户可继续回车执行，vanilla selectCmd） */
  function selectCmd(cmd: string): void {
    if (!editor) return;
    editor.commands.setContent(cmd); // tiptap-markdown setContent 解析 markdown → 纯文本段落
    setMenu(CMD_MENU_CLOSED);
    editor.commands.focus();
  }
  const selectCmdRef = useRef<(cmd: string) => void>(selectCmd);
  selectCmdRef.current = selectCmd;

  return (
    <div className="composer" data-testid="composer">
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
      <div className="composer-card">
        <EditorContent editor={editor} className="composer-editor" />
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
