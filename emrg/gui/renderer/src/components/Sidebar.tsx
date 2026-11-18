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
 * Sidebar — the open sessions, and nothing else (issue #2959 item 5).
 *
 * What lived here before was two things in one column: a navigation rail, and a list
 * that mixed the sessions currently open with the whole history. The rail became
 * `IconRail` (a 60px column of its own) and the history moved behind the search
 * overlay, which leaves this component with one job — the sessions that are open right
 * now, each 26px tall, the running one breathing with a stopwatch beside it.
 *
 * The stopwatch is the daemon's own reading: `turnStartBySid` is written from
 * `turn_start`'s instant (or a `resume_result` snapshot), never from this client's
 * receipt time, so a session opened halfway through a turn shows the time already
 * spent rather than starting from zero.
 */
export interface SidebarProps {
  openSessions: OpenSessionEntry[];
  knownSessions?: SessionInfo[];
  activeSid?: string | null;
  onSelect?: (sid: string) => void;
  onContextMenu?: (entry: OpenSessionEntry, event: React.MouseEvent) => void;
  /** 可注入的标题格式化函数（测试用；默认 sessionLabel） */
  labelFn?: (agent: string, title: string, sid: string) => string;
  /** 每会话 turn 开始时刻（epoch ms；busy 会话显示 [m:ss] 计时） */
  turnStartBySid?: Record<string, number>;
}

export function Sidebar({
  openSessions,
  knownSessions = [],
  activeSid = null,
  onSelect,
  onContextMenu,
  labelFn = sessionLabel,
  turnStartBySid = {},
}: SidebarProps) {
  const { t } = useI18n();
  const entries = sortOpenSessions(openSessions);
  const nowMs = Date.now();

  return (
    <div className="react-sidebar" data-testid="sidebar">
      <div className="sidebar-head">
        <span
          className="conv-group-label open-sessions-label"
          data-testid="open-sessions-label"
          // An empty list keeps its heading hidden rather than showing a section
          // title with nothing under it (vanilla renderOpenSessions' behaviour).
          hidden={entries.length === 0}
        >
          {t("sidebar.openSessions")}
        </span>
        <span className="sidebar-count" data-testid="open-sessions-count">
          {t("sessions.openCount", { n: entries.length })}
        </span>
      </div>
      <nav id="open-sessions" className="open-sessions session-list" data-testid="open-sessions">
        {entries.map((entry) => {
          const title = resolveEntryTitle(entry, knownSessions);
          const label = labelFn(entry.projectName || "", title, entry.sid);
          const active = isActive(entry.sid, activeSid);
          const timer = formatElapsed(turnStartBySid[entry.sid], nowMs);
          return (
            <button
              type="button"
              key={entry.sid}
              className={`conv-item open-session-item session-row${active ? " active" : ""}`}
              data-sid={entry.sid}
              data-testid="open-session-item"
              onClick={() => onSelect && onSelect(entry.sid)}
              onContextMenu={(e) => {
                e.preventDefault();
                onContextMenu && onContextMenu(entry, e);
              }}
            >
              {timer ? <span className="session-run-dot" data-testid="open-session-run-dot" /> : null}
              <span className="conv-title session-label">
                {label}
              </span>
              {timer ? (
                <span className="open-session-timer session-stopwatch" data-testid="open-session-timer">
                  {timer}
                </span>
              ) : null}
            </button>
          );
        })}
      </nav>
    </div>
  );
}
