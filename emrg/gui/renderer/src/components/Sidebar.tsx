import { useI18n } from "../lib/i18n";
import {
  isActive,
  resolveEntryTitle,
  sessionLabel,
  sortOpenSessions,
  type OpenSessionEntry,
  type SessionInfo,
} from "../lib/sidebar";

/**
 * Sidebar — Batch 3: 对话列表（跨项目打开会话区，P4 slice 2）。
 * 从 vanilla `js/sidebar.js` renderOpenSessions/highlight 迁移为 React 组件。
 *
 * - 数据注入（不直接碰 daemon/IPC，Batch 5 接线）；类名与 vanilla CSS 一致
 *   （.conv-item/.conv-title/.open-sessions），Batch 5 CSS 直接复用。
 * - 点击 → onSelect(sid)；右键 → onContextMenu(entry, event)（调用方弹菜单）。
 * - activeSid 匹配条目加 .active（对应 vanilla highlight 逻辑）。
 * - Batch 5 slice 4：新对话/打开会话按钮（vanilla new-chat-btn / open-chat-btn）。
 */
export interface SidebarProps {
  openSessions: OpenSessionEntry[];
  knownSessions?: SessionInfo[];
  activeSid?: string | null;
  onSelect?: (sid: string) => void;
  onContextMenu?: (entry: OpenSessionEntry, event: React.MouseEvent) => void;
  /** 可注入的标题格式化函数（测试用；默认 sessionLabel） */
  labelFn?: (project: string, title: string, sid: string) => string;
  /** 新对话按钮 → NewSessionDialog（Batch 5 slice 4） */
  onNewChat?: () => void;
  /** 打开会话按钮 → OpenSessionDialog（Batch 5 slice 4） */
  onOpenChat?: () => void;
}

export function Sidebar({
  openSessions,
  knownSessions = [],
  activeSid = null,
  onSelect,
  onContextMenu,
  labelFn = sessionLabel,
  onNewChat,
  onOpenChat,
}: SidebarProps) {
  const { t } = useI18n();
  const entries = sortOpenSessions(openSessions);

  if (!entries.length) {
    // vanilla：无打开会话 → label hidden + 空 nav
    return (
      <div className="react-sidebar" data-testid="sidebar">
        <button type="button" className="new-chat-btn" data-testid="new-chat-btn" title={t("sidebar.newChatTitle")} onClick={onNewChat}>
          {t("sidebar.newChat")}
        </button>
        <button type="button" className="open-chat-btn" data-testid="open-chat-btn" title={t("sidebar.openChatTitle")} onClick={onOpenChat}>
          {t("sidebar.openChat")}
        </button>
        <div
          className="conv-group-label open-sessions-label"
          data-testid="open-sessions-label"
          hidden
        />
        <nav id="open-sessions" className="open-sessions" data-testid="open-sessions" />
      </div>
    );
  }

  return (
    <div className="react-sidebar" data-testid="sidebar">
      <button type="button" className="new-chat-btn" data-testid="new-chat-btn" title={t("sidebar.newChatTitle")} onClick={onNewChat}>
        {t("sidebar.newChat")}
      </button>
      <button type="button" className="open-chat-btn" data-testid="open-chat-btn" title={t("sidebar.openChatTitle")} onClick={onOpenChat}>
        {t("sidebar.openChat")}
      </button>
      <div className="conv-group-label open-sessions-label" data-testid="open-sessions-label">
        {t("sidebar.openSessions")}
      </div>
      <nav id="open-sessions" className="open-sessions" data-testid="open-sessions">
        {entries.map((entry) => {
          const title = resolveEntryTitle(entry, knownSessions);
          const label = labelFn(entry.projectName || "", title, entry.sid);
          const active = isActive(entry.sid, activeSid);
          return (
            <div
              key={entry.sid}
              className={`conv-item open-session-item${active ? " active" : ""}`}
              data-sid={entry.sid}
              data-testid="open-session-item"
              onClick={() => onSelect && onSelect(entry.sid)}
              onContextMenu={(e) => {
                e.preventDefault();
                onContextMenu && onContextMenu(entry, e);
              }}
            >
              <span className="conv-title">{label}</span>
            </div>
          );
        })}
      </nav>
    </div>
  );
}
