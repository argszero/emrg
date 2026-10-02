import { useI18n } from "../lib/i18n";
import {
  formatElapsed,
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
 * - Batch 5 slice 5：侧边导航 rail（vanilla #side-nav .side-nav-item，data-view），
 *   activeView 高亮 + onSwitchView 回调（vanilla app.js switchView 语义）。
 */

/** 工作区视图 id（vanilla VIEWS 子集；sessions 为会话视图） */
export type SidebarViewId = "sessions" | "projects" | "tasks" | "rants" | "settings";

/** 导航按钮定义（vanilla index.html #side-nav 五个按钮，图标 + i18n title） */
const NAV_ITEMS: { view: SidebarViewId; icon: string; labelKey: string }[] = [
  { view: "sessions", icon: "💬", labelKey: "nav.sessions" },
  { view: "projects", icon: "📁", labelKey: "nav.projects" },
  { view: "tasks", icon: "⏱️", labelKey: "nav.tasks" },
  { view: "rants", icon: "📣", labelKey: "nav.rants" },
  { view: "settings", icon: "⚙️", labelKey: "nav.settings" },
];

export interface SidebarProps {
  openSessions: OpenSessionEntry[];
  knownSessions?: SessionInfo[];
  activeSid?: string | null;
  onSelect?: (sid: string) => void;
  onContextMenu?: (entry: OpenSessionEntry, event: React.MouseEvent) => void;
  /** 可注入的标题格式化函数（测试用；默认 sessionLabel） */
  labelFn?: (project: string, title: string, sid: string) => string;
  /** 每会话 turn 开始时刻（epoch ms；rant 2026-09-02T10:36:26 — busy 会话显示 [m:ss] 计时） */
  turnStartBySid?: Record<string, number>;
  /** 新对话按钮 → NewSessionDialog（Batch 5 slice 4） */
  onNewChat?: () => void;
  /** 打开会话按钮 → OpenSessionDialog（Batch 5 slice 4） */
  onOpenChat?: () => void;
  /** 当前工作区视图（Batch 5 slice 5；nav 高亮依据） */
  activeView?: SidebarViewId;
  /** 点击 nav 按钮 → 切换工作区视图（vanilla switchView 语义） */
  onSwitchView?: (view: SidebarViewId) => void;
  /**
   * 自进化计数 —— 侧边栏顶部成长卡上的那个数（vanilla `#growth-count`，
   * `updateGrowthCard()` 从 4 处被调，`js/app.js:1106`）。`null` 与 `undefined` 都
   * 按 vanilla 的 `state.evolutionCount ?? 0` 落成 `0`。
   */
  evolutionCount?: number | null;
}

export function Sidebar({
  openSessions,
  knownSessions = [],
  activeSid = null,
  onSelect,
  onContextMenu,
  labelFn = sessionLabel,
  turnStartBySid = {},
  onNewChat,
  onOpenChat,
  activeView = "sessions",
  onSwitchView,
  evolutionCount = null,
}: SidebarProps) {
  const { t } = useI18n();
  const entries = sortOpenSessions(openSessions);
  const nowMs = Date.now();

  /**
   * 成长状态卡（vanilla `#growth-card`，`index.html:24-27`，WorkBuddy P3「自进化可见化」）。
   *
   * 它是这一栏里**唯一**一件与工作内容无关的东西：宿主随时能看到「EMRG 已自我进化 N 次」，
   * 也就是「它在为我变好」。`components.css`/`layout.css` 的 `.growth-card` / `.growth-line`
   * / `.growth-note` 规则一直在（`layout.css:137-155`），词典四条串也一直在
   * （`copy.growthCardTitle` / `copy.growthCountPrefix` / `copy.times` / `copy.growthNote`），
   * 而 #1024 删 vanilla 渲染层时**元素与读者一起没了**（实测 2026-10-02：`growth-card`
   * 在 `src/` 下零命中）—— 于是 React 版里这个数只出现在**设置 → 关于**里，主界面上
   * 一句都看不到。
   *
   * 结构与 vanilla 逐字对应：`🌱 {growthCountPrefix} **{n}** {times}` + 一行 `growthNote`。
   * 数字用 `<b>` 包住不是装饰 —— `.growth-line b { color: var(--accent) }` 靠它高亮。
   */
  const growthCard = (
    <div className="growth-card" data-testid="growth-card" title={t("copy.growthCardTitle")}>
      <div className="growth-line">
        🌱 {t("copy.growthCountPrefix")} <b data-testid="growth-count">{evolutionCount ?? 0}</b>{" "}
        {t("copy.times")}
      </div>
      <div className="growth-note" data-testid="growth-note">
        {t("copy.growthNote")}
      </div>
    </div>
  );

  // vanilla #side-nav：五个导航按钮（sessions/projects/tasks/rants/settings）
  const navRail = (
    <nav className="side-nav" aria-label="Navigation" data-testid="side-nav">
      {NAV_ITEMS.map(({ view, icon, labelKey }) => (
        <button
          key={view}
          type="button"
          className={`side-nav-item${activeView === view ? " active" : ""}`}
          data-view={view}
          data-testid={`nav-${view}`}
          title={t(labelKey)}
          onClick={() => onSwitchView?.(view)}
        >
          {icon}
        </button>
      ))}
    </nav>
  );

  if (!entries.length) {
    // vanilla：无打开会话 → label hidden + 空 nav
    return (
      <div className="react-sidebar" data-testid="sidebar">
        {growthCard}
        {navRail}
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
      {growthCard}
      {navRail}
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
          // Rant 2026-09-02T10:36:26：daemon turn_start 权威计时——正在运行的会话
          // 在标题后显示 [m:ss]（与 TUI 状态栏同格式同基准）。
          const timer = formatElapsed(turnStartBySid[entry.sid], nowMs);
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
              <span className="conv-title">
                {label}
                {timer ? <span className="open-session-timer" data-testid="open-session-timer">{timer}</span> : null}
              </span>
            </div>
          );
        })}
      </nav>
    </div>
  );
}
