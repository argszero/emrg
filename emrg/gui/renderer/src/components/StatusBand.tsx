import { useI18n } from "../lib/i18n";
import { formatElapsed } from "../lib/sidebar";
import { STATUS_BAND_HEIGHT, formatRound, formatTokens } from "../lib/cockpit";
import type { UsageWindowReading } from "../lib/daemonBridge";

/**
 * StatusBand — the 44px row that replaces the application header (issue #2959 item 1).
 *
 * It carries **only what moves** (issue #2969): whether a turn is running, which round
 * it is on, how long it has been running, and what the last 24 hours cost. Four readings,
 * and that is the whole row's left side. The last one is a *window*, not this turn
 * (rant 2026-11-19T09:31:00): it survives a session switch and a GUI restart, which is
 * the whole reason it replaced the per-turn total. What does not move — the
 * session's title, its id, the model, the message count — is not here: the title is in
 * the window's title bar already, the model switches at the composer, and a second
 * place for a fact turns a status row into a header by accretion.
 *
 * The right end is exactly the two pane toggles — the session column and the inspector.
 * Search and new-session are not here: the session column's own header carries them, and
 * a second entry point for one action reads as a second feature (issue #3028).
 */
export interface StatusBandProps {
  running: boolean;
  /** Rounds this turn has run; `null` = the daemon has not reported one. */
  round: number | null;
  /** Turn start, epoch ms — the daemon's instant, never this client's receipt time. */
  startedAtMs?: number;
  /**
   * The daemon's rolling 24-hour window; `null` = it has not answered yet (not zero).
   *
   * Three segments, never a total (rant 2026-11-19T09:31:00): the host reads this to
   * answer "what have I spent today", and a single figure cannot say whether the spend
   * went into the prompt or into the answer.
   */
  usage: UsageWindowReading | null;
  leftOpen: boolean;
  rightOpen: boolean;
  onToggleLeft: () => void;
  onToggleRight: () => void;
  /** Injected clock: the shell re-renders once a second while a turn is running. */
  nowMs?: number;
}

export function StatusBand({
  running,
  round,
  startedAtMs,
  usage,
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
      <span className="band-item" data-testid="band-round" title={t("band.roundTitle")}>
        {t("band.round", { n: formatRound(round) })}
      </span>
      <span className="band-sep" />
      <span className="band-item band-mono" data-testid="band-elapsed">
        {elapsed || "—"}
      </span>
      <span className="band-sep" />
      <span className="band-item" data-testid="band-usage" title={t("band.usageTitle")}>
        {t("band.usage", {
          input: formatTokens(usage?.input),
          cache: formatTokens(usage?.cache),
          output: formatTokens(usage?.output),
        })}
      </span>
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
