import { useI18n } from "../lib/i18n";
import { formatElapsed } from "../lib/sidebar";
import { STATUS_BAND_HEIGHT, formatTokens } from "../lib/cockpit";
import { USAGE_SEGMENTS, type UsageSegment } from "../lib/usageRanking";
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
  /**
   * Open the ranking for one segment (rant 2026-11-20T17:30:50).
   *
   * Each reading is its own control because each answers its own question: one table can
   * be sorted by one scale, so the reading the host clicks is the ranking they get. The
   * band itself ranks nothing — it states three numbers and hands the choice on.
   *
   * The button's own element goes with the segment (issue #3123): the panel hangs 8px
   * under the reading that opened it, so the caller needs the thing that was clicked and
   * not only the reading it stands for.
   */
  onOpenRanking: (segment: UsageSegment, anchor: HTMLElement | null) => void;
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
  onOpenRanking,
  nowMs,
}: StatusBandProps) {
  const { t } = useI18n();
  const now = nowMs ?? Date.now();
  const elapsed = formatElapsed(startedAtMs, now);
  // The two sides of every reading, named once so the three cells cannot drift apart.
  const sessionSide = {
    input: usage?.sessionInput,
    cache: usage?.sessionCache,
    output: usage?.sessionOutput,
  };
  const machineSide = {
    input: usage?.input,
    cache: usage?.cache,
    output: usage?.output,
  };

  return (
    <div
      className="status-band"
      data-testid="status-band"
      role="status"
      style={{ height: STATUS_BAND_HEIGHT }}
    >
      <span
        className={`band-state band-yield ${running ? "running" : "idle"}`}
        data-testid="band-state"
      >
        <i />
        {running ? t("band.running") : t("band.idle")}
      </span>
      <span className="band-sep band-yield" />
      <span className="band-item band-mono band-yield" data-testid="band-elapsed">
        {elapsed || "—"}
      </span>
      <span className="band-sep band-yield" />
      {USAGE_SEGMENTS.map((segment) => (
        <button
          key={segment}
          type="button"
          className="band-item band-usage"
          data-testid={`band-usage-${segment}`}
          // Marks the elements the panel must leave alone: a click here is this button's
          // own decision — close the panel, switch it, or open it — and the panel's
          // outside-click dismissal has to let it through rather than pre-empt it.
          data-band-usage={segment}
          title={t("band.usageTitle")}
          onClick={(e) => onOpenRanking(segment, e.currentTarget)}
        >
          {t("band.segment", {
            label: t(`band.seg.${segment}`),
            mine: formatTokens(sessionSide[segment]),
            machine: formatTokens(machineSide[segment]),
          })}
        </button>
      ))}
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
