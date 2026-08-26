import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { parseInput } from "../lib/commands";
import {
  CMD_MENU_CLOSED,
  menuForPrefix,
  menuNavigate,
  queueSend,
  type CmdMenuState,
} from "../lib/composer";
import { genRequestId, type TranslateFn } from "../lib/utils";
import { useI18n } from "../lib/i18n";
import type { TranscriptStore } from "../lib/transcript";

/**
 * Composer — 输入框 + / 补全菜单 + 发送流（Batch 2 remainder，蓝图 cycle-155056）。
 * 源：vanilla renderer/js/app.js sendMessage（131-170）+ CommandMenu（480-526）+
 * keydown 导航（~1710-1735）+ P2 queue-injection（#655）。
 *
 * - 纯逻辑在 lib/composer.ts / lib/history.ts / lib/commands.ts（可单测）；
 *   本组件负责 DOM 接线（textarea 自适应高度 ≤150px、键盘导航、mousedown 选择）。
 * - 发送流：parseInput → / 指令走 onCommand 回调（Batch 5 由 App 路由到对话框）；
 *   message → busy 队列注入（requestId 预生成、wasBusy 时入 queuedSends）。
 * - sendMessage 注入式（测试传假实现；默认 window.emrg.sendMessage）。
 * - busy 为受控可选 prop：Batch 5 接线后由 daemon 广播（done/cancelled/error）驱动；
 *   未受控时组件内部管理（发送置忙、失败复位）。
 * - 类名与 vanilla 一致（composer-card / cmd-menu / cmd-menu-item / send-btn），
 *   Batch 5 切换复用现有 CSS。
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
}

const MAX_INPUT_HEIGHT = 150;

export function Composer({
  store,
  sid = null,
  sandbox = null,
  busy: busyProp,
  sendMessage: send,
  onCommand,
}: ComposerProps) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [menu, setMenu] = useState<CmdMenuState>(CMD_MENU_CLOSED);
  const [internalBusy, setInternalBusy] = useState(false);
  const busy = busyProp ?? internalBusy;
  const queuedRef = useRef<Map<string, Array<{ requestId: string; text: string; sandbox?: string | null }>>>(new Map());
  const taRef = useRef<HTMLTextAreaElement>(null);

  // 发送函数解析（默认走 preload 桥）
  const sendFn =
    send ??
    ((opts: SendOptions) => {
      return (window as unknown as { emrg?: { sendMessage: (o: SendOptions) => Promise<SendResult> } }).emrg?.sendMessage(opts) ??
        Promise.reject(new Error("window.emrg.sendMessage unavailable"));
    });

  // textarea 自适应高度（vanilla：auto → min(scrollHeight,150)）
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, MAX_INPUT_HEIGHT)}px`;
  }, [text]);

  async function submit(): Promise<void> {
    const value = text.trim();
    if (!value) return;
    const parsed = parseInput(value);
    if (parsed.type !== "message") {
      // 指令：清输入 + 关菜单 + 路由（vanilla rant 19:44 P1：/ 开头不进 sendMessage）
      setText("");
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
    setText("");
    // B3：消息已发送 → 清除该会话草稿（React 版草稿由 text state 承载，发送即清）
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
      // G49：失败恢复输入框，文案不责怪用户（copy.sendFailed）
      store.addSystemMessage(t("copy.sendFailed"), sid);
      setText(value);
    }
  }

  /** 选择补全项：填充输入框 + 关菜单（用户可继续回车执行，vanilla selectCmd） */
  function selectCmd(cmd: string): void {
    setText(cmd);
    setMenu(CMD_MENU_CLOSED);
    taRef.current?.focus();
  }

  function onChange(v: string): void {
    setText(v);
    // / 指令补全：以 / 开头且无空格（仍处于指令词）→ 弹出菜单
    const trimmed = v.trim();
    if (trimmed.startsWith("/") && !trimmed.includes(" ")) setMenu(menuForPrefix(trimmed, t));
    else setMenu(CMD_MENU_CLOSED);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>): void {
    // / 补全菜单键盘导航（rant 19:44 P1）：↑↓ 移动、Enter 选择、Esc 关闭
    if (menu.items.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMenu(menuNavigate(menu, 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMenu(menuNavigate(menu, -1));
        return;
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        const item = menu.items[menu.index];
        if (item) selectCmd(item.cmd);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMenu(CMD_MENU_CLOSED);
        return;
      }
    }
    // Enter（非 Shift）与 Ctrl+Enter 同发送
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit();
    } else if (e.key === "Enter" && e.ctrlKey) {
      e.preventDefault();
      void submit();
    }
  }

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
      <div className="composer-card">
        <textarea
          ref={taRef}
          id="input"
          rows={1}
          value={text}
          placeholder={t("composer.placeholder")}
          aria-label={t("composer.placeholder")}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          data-testid="composer-input"
        />
        <button
          type="button"
          className="send-btn"
          disabled={!text.trim()}
          title={t("composer.send")}
          aria-label={t("composer.send")}
          onClick={() => void submit()}
          data-testid="composer-send"
        >
          ↑
        </button>
      </div>
    </div>
  );
}
