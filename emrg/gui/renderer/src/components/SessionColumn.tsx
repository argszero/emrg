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
 * SessionColumn — the page's second section: the sessions that are open, and a header
 * that does nothing but count them (issue #2969).
 *
 * The header is one row — `Open N` and the two buttons that bring a session in (search)
 * or start one (`+`). It carries no section label, because there is only one section:
 * the history list and the two-level grouping it used to share this column with are
 * gone, and the search overlay is where a session that is not open is found.
 *
 * The stopwatch is the daemon's own reading: `turnStartBySid` is written from
 * `turn_start`'s instant (or a `resume_result` snapshot), never from this client's
 * receipt time, so a session opened halfway through a turn shows the time already
 * spent rather than starting from zero.
 */
export interface SessionColumnProps {
  openSessions: OpenSessionEntry[];
  knownSessions?: SessionInfo[];
  activeSid?: string | null;
  onSelect?: (sid: string) => void;
  /**
   * 关闭这条会话（保留磁盘数据）。
   *
   * 不传就不渲染关闭控件：关闭能力由调用方决定，这一层不自己猜后台有没有这条通道。
   * 控件是行内**另一个**按钮而不是行按钮的一部分 —— 嵌套按钮在 HTML 里不合法，
   * 而把行改成 div 会让每一处按 `open-session-item` 读行的断言改变语义。
   */
  onClose?: (sid: string) => void;
  onSearch?: () => void;
  onNew?: () => void;
  /** 可注入的标题格式化函数（测试用；默认 sessionLabel） */
  labelFn?: (agent: string, title: string, sid: string) => string;
  /** 每会话 turn 开始时刻（epoch ms；busy 会话显示 [m:ss] 计时） */
  turnStartBySid?: Record<string, number>;
}

export function SessionColumn({
  openSessions,
  knownSessions = [],
  activeSid = null,
  onSelect,
  onClose,
  onSearch,
  onNew,
  labelFn = sessionLabel,
  turnStartBySid = {},
}: SessionColumnProps) {
  const { t } = useI18n();
  const entries = sortOpenSessions(openSessions);
  const nowMs = Date.now();

  return (
    <div className="session-col" data-testid="session-column">
      <div className="session-col-head">
        <span className="session-col-count" data-testid="session-col-count">
          {/* The word and the number are two spans because they are two colours in
              the prototype: the count is the reading, the label is only its subject. */}
          <span className="session-col-count-label">{t("sessions.openCountLabel")}</span>{" "}
          <span className="session-col-count-n" data-testid="session-col-count-n">
            {entries.length}
          </span>
        </span>
        <span className="session-col-actions">
          <button
            type="button"
            className="session-col-btn"
            data-testid="session-col-search"
            title={t("band.searchTitle")}
            aria-label={t("band.searchTitle")}
            onClick={onSearch}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="11" cy="11" r="7" />
              <path d="M20 20l-4.2-4.2" />
            </svg>
          </button>
          <button
            type="button"
            className="session-col-btn"
            data-testid="session-col-new"
            title={t("sidebar.newChatTitle")}
            aria-label={t("sidebar.newChatTitle")}
            onClick={onNew}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </button>
        </span>
      </div>
      <nav id="open-sessions" className="session-list" data-testid="open-sessions">
        {entries.map((entry) => {
          const title = resolveEntryTitle(entry, knownSessions);
          const label = labelFn(entry.projectName || "", title, entry.sid);
          const active = isActive(entry.sid, activeSid);
          const timer = formatElapsed(turnStartBySid[entry.sid], nowMs);
          return (
            <div className="session-row-wrap" key={entry.sid}>
              <button
                type="button"
                className={`session-row${active ? " active" : ""}`}
                data-sid={entry.sid}
                data-testid="open-session-item"
                onClick={() => onSelect && onSelect(entry.sid)}
              >
                {timer ? <span className="session-run-dot" data-testid="open-session-run-dot" /> : null}
                <span className="session-label">{label}</span>
                {timer ? (
                  <span className="session-stopwatch" data-testid="open-session-timer">
                    {timer}
                  </span>
                ) : null}
              </button>
              {onClose ? (
                <button
                  type="button"
                  className="session-row-close"
                  data-sid={entry.sid}
                  data-testid="close-open-session"
                  title={t("app.closeSession")}
                  aria-label={t("app.closeSession")}
                  onClick={() => onClose(entry.sid)}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M6 6l12 12M18 6L6 18" />
                  </svg>
                </button>
              ) : null}
            </div>
          );
        })}
      </nav>
    </div>
  );
}
