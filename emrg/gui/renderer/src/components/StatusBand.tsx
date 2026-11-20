import { useI18n } from "../lib/i18n";
import { formatElapsed } from "../lib/sidebar";
import { STATUS_BAND_HEIGHT, formatTokens } from "../lib/cockpit";
import type { UsageWindowReading } from "../lib/daemonBridge";

/**
 * StatusBand — the 44px row that replaces the application header (issue #2959 item 1).
 *
 * It carries **only what moves** (issue #2969): whether a turn is running, how long it
 * has been running, and what the last 24 hours cost. Three readings, and that is the
 * whole row's left side. The last one is a *window*, not this turn
 * (rant 2026-11-19T09:31:00): it survives a session switch and a GUI restart, which is
 * the whole reason it replaced the per-turn total.
 *
 * The one thing here that does not move is the session's **name and id** (issue #3089):
 * they used to ride the window title, which the operating system draws, so neither could
 * be dragged or copied — and the id is the part a reader copies most (into an issue, into
 * a log, to say to another agent which session is meant). Here they are ordinary text in a
 * `span`: not a button, not a link, not a hover card, because a reading that answers a
 * click teaches the next reader to click readings. The model and the message count stay
 * out — the model switches at the composer, and a second place for a fact turns a status
 * row into a header by accretion.
 *
 * The right end is exactly the two pane toggles — the session column and the inspector.
 * Search and new-session are not here: the session column's own header carries them, and
 * a second entry point for one action reads as a second feature (issue #3028).
 */
export interface StatusBandProps {
  running: boolean;
  /** Turn start, epoch ms — the daemon's instant, never this client's receipt time. */
  startedAtMs?: number;
  /**
   * The daemon's rolling 24-hour window; `null` = it has not answered yet (not zero).
   *
   * Three segments, never a total (rant 2026-11-19T09:31:00): the host reads this to
   * answer "what have I spent today", and a single figure cannot say whether the spend
   * went into the prompt or into the answer.
   *
   * Each segment shows two numbers since issue #3072 — **this session's** and then the
   * **machine's**, separated by a slash — because the two answer different questions
   * ("what did this conversation cost" and "what did the machine spend"), and one
   * figure cannot be read as both. The session side is `—` whenever nothing measured it:
   * there is no session, or the daemon has not answered the narrowed question yet.
   */
  usage: UsageWindowReading | null;
  /**
   * The session the band is showing, as text the host can select (issue #3089).
   *
   * `sessionId` is the whole cell's existence: no session ⇒ the cell is not rendered at
   * all, because a placeholder would read as a session whose id could not be read.
   * `sessionName` may be empty — an unnamed session shows its id alone, never an empty
   * pair of quotes or a stray separator.
   */
  sessionId?: string | null;
  sessionName?: string;
  leftOpen: boolean;
  rightOpen: boolean;
  onToggleLeft: () => void;
  onToggleRight: () => void;
  /** Injected clock: the shell re-renders once a second while a turn is running. */
  nowMs?: number;
}

export function StatusBand({
  running,
  startedAtMs,
  usage,
  sessionId,
  sessionName,
  leftOpen,
  rightOpen,
  onToggleLeft,
  onToggleRight,
  nowMs,
}: StatusBandProps) {
  const { t } = useI18n();
  const now = nowMs ?? Date.now();
  const elapsed = formatElapsed(startedAtMs, now);

  return (
    <div
      className="status-band"
      data-testid="status-band"
      role="status"
      style={{ height: STATUS_BAND_HEIGHT }}
    >
      <span className={`band-state ${running ? "running" : "idle"}`} data-testid="band-state">
        <i />
        {running ? t("band.running") : t("band.idle")}
      </span>
      <span className="band-sep" />
      <span className="band-item band-mono" data-testid="band-elapsed">
        {elapsed || "—"}
      </span>
      <span className="band-sep" />
      <span className="band-item" data-testid="band-usage" title={t("band.usageTitle")}>
        {t("band.usage", {
          input: formatTokens(usage?.sessionInput),
          machineInput: formatTokens(usage?.input),
          cache: formatTokens(usage?.sessionCache),
          machineCache: formatTokens(usage?.cache),
          output: formatTokens(usage?.sessionOutput),
          machineOutput: formatTokens(usage?.output),
        })}
      </span>
      {sessionId ? (
        <>
          <span className="band-sep" />
          <span className="band-session" data-testid="band-session" title={t("band.sessionTitle")}>
            {sessionName ? (
              <span className="band-session-name" data-testid="band-session-name">
                {sessionName}
              </span>
            ) : null}
            <span className="band-session-id" data-testid="band-session-id">
              {sessionId}
            </span>
          </span>
        </>
      ) : null}
      <span className="band-right">
        <button
          type="button"
          className={`band-btn band-toggle${leftOpen ? " on" : ""}`}
          data-testid="band-toggle-left"
          aria-pressed={leftOpen}
          title={t("band.toggleLeft")}
          onClick={onToggleLeft}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
            <path d="M9 4.5v15" />
          </svg>
        </button>
        <button
          type="button"
          className={`band-btn band-toggle${rightOpen ? " on" : ""}`}
          data-testid="band-toggle-right"
          aria-pressed={rightOpen}
          title={t("band.toggleRight")}
          onClick={onToggleRight}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
            <path d="M15 4.5v15" />
          </svg>
        </button>
      </span>
    </div>
  );
}
