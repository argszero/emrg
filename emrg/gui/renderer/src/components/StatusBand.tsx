import type { ReactNode } from "react";
import { useI18n } from "../lib/i18n";
import { formatElapsed } from "../lib/sidebar";
import { STATUS_BAND_HEIGHT, formatRound, formatTokens } from "../lib/cockpit";

/**
 * StatusBand — the 44px row that replaces the application header (issue #2959 item 1).
 *
 * It carries **only what moves**: whether a turn is running, which round it is on, how
 * long it has been running, and how many tokens it has spent so far. Everything that
 * does not move (the session's title, the model, the connection) still rides here
 * rather than in a second header, because the point of the rebuild is that one row
 * holds the always-on facts and the body gets the rest of the window.
 *
 * The two buttons at the right end open and close the side columns. Search and `+`
 * live here too (item 6): they act on sessions wherever the host happens to be.
 */
export interface StatusBandProps {
  running: boolean;
  /** Rounds this turn has run; `null` = the daemon has not reported one. */
  round: number | null;
  /** Turn start, epoch ms — the daemon's instant, never this client's receipt time. */
  startedAtMs?: number;
  /** Cumulative tokens for this turn; `null` = not reported (not zero). */
  tokens: number | null;
  /** The session line the shell already renders (title/sid/agent/message count). */
  sessionInfo?: ReactNode;
  /** The connection badge (model, dot, image capability) — the shell's own markup. */
  conn?: ReactNode;
  connected: boolean;
  model?: string;
  leftOpen: boolean;
  rightOpen: boolean;
  onToggleLeft: () => void;
  onToggleRight: () => void;
  onSearch: () => void;
  onNew: () => void;
  /** Injected clock: the shell re-renders once a second while a turn is running. */
  nowMs?: number;
}

export function StatusBand({
  running,
  round,
  startedAtMs,
  tokens,
  sessionInfo,
  conn,
  connected,
  model,
  leftOpen,
  rightOpen,
  onToggleLeft,
  onToggleRight,
  onSearch,
  onNew,
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
      <span className="band-item" data-testid="band-spend" title={t("band.spendTitle")}>
        {t("band.spend", { n: formatTokens(tokens) })}
      </span>
      {sessionInfo ? (
        <>
          <span className="band-sep" />
          {sessionInfo}
        </>
      ) : null}
      <span className="band-right">
        {conn}
        <button
          type="button"
          className="band-btn"
          data-testid="band-search"
          title={t("band.searchTitle")}
          onClick={onSearch}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="11" cy="11" r="7" />
            <path d="M20 20l-4.2-4.2" />
          </svg>
        </button>
        <button
          type="button"
          className="band-btn"
          data-testid="band-new"
          title={t("sidebar.newChatTitle")}
          onClick={onNew}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 5v14M5 12h14" />
          </svg>
        </button>
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
