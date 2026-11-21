import { useEffect, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import { STATUS_BAND_HEIGHT, formatTokens } from "../lib/cockpit";
import { rankSegment, type UsageSegment } from "../lib/usageRanking";
import type { UsageWindowReading } from "../lib/daemonBridge";

/**
 * UsageRanking — the last 24 hours ranked by **one** segment (rant 2026-11-20T17:30:50).
 *
 * Three readings answer three questions, and one table can only be sorted by one scale:
 * the session that spent the most input is not the one that spent the most output, and a
 * table carrying both would need two scales and be read with neither. So the band's three
 * readings each open their own ranking, and this panel shows the one that was clicked.
 *
 * Four things change with the segment, and every one of them is visible here rather than
 * implied: the title, the fourth column's name and formula, the footer's reconciliation,
 * and — for the cache segment — a line saying it is usually in the same order as input,
 * because the cache is part of the input and each session's hit rate sits near the others.
 *
 * The unnamed volume is **not a row**: it belongs to no conversation, so it has no session
 * to open. It is reported under the table and left out of the ranking — item 5, and the
 * one place the panel refuses to do the obvious thing.
 *
 * What it is not (issue #3123): a **modal**. It used to open as one — a dimmed and blurred
 * backdrop behind a centred card with a 关闭 button — and every part of that shape worked
 * against the reading it carries. These numbers summarise the transcript, so hiding the
 * transcript is the one thing the panel must not do; a centred card on a wide window sat
 * far from the band that opened it; and an unbounded row list grew the card past the window
 * edge with nothing to scroll it back. It is now a flap hanging 8px under the clicked
 * reading: the rows scroll inside a bounded box, and the unnamed block and the
 * reconciliation line sit **outside** that box so they stay readable at any scroll offset.
 *
 * Dismissal follows the same idea, and the panel owns only half of it. A click outside
 * closes; Escape closes. A click on a band reading is deliberately let through, because
 * that button decides whether it closes this panel or switches it — a dismissal handler
 * that closed first would turn "click the same reading again" into a reopen. Nothing here
 * is `aria-modal`, because nothing here blocks the window.
 */
export interface UsageRankingProps {
  open: boolean;
  segment: UsageSegment;
  /** The band reading that opened this flap; the panel hangs under it. */
  anchor?: HTMLElement | null;
  /** The daemon's window reading; `null` = it has not answered, which is not zero. */
  reading: UsageWindowReading | null;
  /** A session's title, or `undefined` when this client knows none for it. */
  nameOf?: (sid: string) => string | undefined;
  onOpenSession: (sid: string) => void;
  onClose: () => void;
}

/** The flap's width, and the room it keeps from the window's edges. */
export const PANEL_WIDTH = 680;
const PANEL_EDGE = 10;
/**
 * `window.innerWidth - PANEL_RIGHT_KEEP` is the furthest right the flap's left edge may
 * sit: the panel is 680 wide and leaves 14 to its own right, rather than hanging off the
 * edge where its numbers would be the half that is cut.
 */
const PANEL_RIGHT_KEEP = 694;
/** The gap between the band reading and the flap's top edge. */
const PANEL_GAP = 8;

/** A control the band itself renders: a click there is the band's to decide, not ours. */
const BAND_CONTROL = "[data-band-usage]";

export interface PanelPosition {
  left: number;
  top: number;
}

/**
 * Where the flap hangs, measured from the reading that opened it.
 *
 * Position comes from that button rather than from the window centre, because the panel is
 * an answer to something on the band: the closer it starts to the reading, the less the
 * host has to look for it. `clamp(10, buttonLeft, windowWidth - 694)` is the whole rule,
 * and the top edge is the band's bottom plus the gap. A panel with no anchor has not been
 * measured against anything, so it takes the inset corner under the band rather than
 * inventing a button position.
 */
export function placePanel(anchor: HTMLElement | null, viewportWidth: number): PanelPosition {
  const rect = anchor?.getBoundingClientRect();
  const wantedLeft = rect ? rect.left : PANEL_EDGE;
  const top = rect ? rect.bottom + PANEL_GAP : STATUS_BAND_HEIGHT + PANEL_GAP;
  return {
    left: Math.max(PANEL_EDGE, Math.min(wantedLeft, viewportWidth - PANEL_RIGHT_KEEP)),
    top,
  };
}

function pct(share: number | null): string {
  return share === null ? "—" : `${Math.round(share * 100)}%`;
}

function fourthText(value: number | null, segment: UsageSegment): string {
  if (value === null) return "—";
  return segment === "output" ? String(Math.round(value)) : `${Math.round(value * 100)}%`;
}

export function UsageRanking({
  open,
  segment,
  anchor,
  reading,
  nameOf,
  onOpenSession,
  onClose,
}: UsageRankingProps) {
  const { t } = useI18n();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<PanelPosition | null>(null);
  const groups = reading?.groups ?? [];
  const ranking = rankSegment(groups, segment, reading ? reading[segment] : null);
  const label = t(`band.seg.${segment}`);
  const title = t("usage.title", { segment: label });
  // The volume no session names: the row's own figure when there is one, a **measured**
  // zero when the segment was measured and nothing was left unnamed, and `null` when the
  // daemon has not answered — a `0` there reports an absence as a quantity, which is the
  // defect class this whole window exists to remove.
  const unnamedTokens = ranking.unnamed
    ? ranking.unnamed.tokens
    : ranking.total === null
      ? null
      : 0;

  // Placed on open and re-placed on every resize: a position measured once is a position
  // that is wrong after the host drags the window.
  useEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    const place = () => setPos(placePanel(anchor ?? null, window.innerWidth));
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open, anchor]);

  // The two dismissals that are not the band: Escape, and a click outside the flap.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    const onPointer = (e: MouseEvent) => {
      const target = e.target as Element | null;
      if (!target || typeof target.closest !== "function") return;
      if (panelRef.current?.contains(target)) return;
      if (target.closest(BAND_CONTROL)) return;
      onClose();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("click", onPointer);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("click", onPointer);
    };
  }, [open, onClose]);

  if (!open) return null;

  const table = (
    <table className="usage-table">
      <thead>
        <tr>
          <th>{t("usage.col.session")}</th>
          <th>{t("usage.col.share")}</th>
          <th>{t("usage.col.tokens")}</th>
          <th data-testid="usage-fourth-name">
            {segment === "output" ? t("usage.col.perCall") : t("usage.col.hitRate")}
          </th>
          <th>{t("usage.col.calls")}</th>
        </tr>
      </thead>
      <tbody>
        {ranking.rows.map((row) => (
          <tr
            key={row.key}
            className="usage-row"
            data-testid="usage-row"
            data-sid={row.session ?? ""}
            tabIndex={0}
            role="button"
            onClick={() => row.session && onOpenSession(row.session)}
            onKeyDown={(e) => {
              if ((e.key === "Enter" || e.key === " ") && row.session) {
                e.preventDefault();
                onOpenSession(row.session);
              }
            }}
          >
            <td className="usage-name" data-testid="usage-row-name">
              {nameOf?.(row.session ?? "") || row.session}
            </td>
            <td>
              <span className="usage-bar" data-testid="usage-bar">
                <i style={{ width: pct(row.share) === "—" ? "0%" : pct(row.share) }} />
              </span>
              <span className="usage-share" data-testid="usage-share">
                {pct(row.share)}
              </span>
            </td>
            <td className="usage-tokens" data-testid="usage-tokens">
              {formatTokens(row.tokens)}
            </td>
            <td className="usage-fourth" data-testid="usage-fourth">
              {fourthText(row.fourth, segment)}
            </td>
            <td className="usage-calls" data-testid="usage-calls">
              {row.calls}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  return (
    <div
      ref={panelRef}
      className="usage-panel"
      data-testid="usage-ranking"
      role="dialog"
      aria-label={title}
      style={pos ? { left: pos.left, top: pos.top } : undefined}
    >
      <h2 className="usage-title" data-testid="usage-title">
        {title}
      </h2>
      {/* The only thing that scrolls. The rows pass under a sticky header, and everything
          below this box stays put wherever the list is scrolled. */}
      <div className="usage-list" data-testid="usage-list">
        {ranking.rows.length === 0 ? (
          <p className="usage-empty" data-testid="usage-empty">
            {t("usage.empty")}
          </p>
        ) : (
          table
        )}
      </div>
      {ranking.unnamed ? (
        <div className="usage-unnamed" data-testid="usage-unnamed">
          <p>
            {t("usage.unnamed", {
              tokens: formatTokens(ranking.unnamed.tokens),
              calls: ranking.unnamed.calls,
            })}
          </p>
          <p className="usage-why">{t("usage.unnamedWhy")}</p>
        </div>
      ) : null}
      {segment === "cache" ? (
        <p className="usage-note" data-testid="usage-note-cache">
          {t("usage.cacheNote")}
        </p>
      ) : null}
      {segment === "output" ? (
        <p className="usage-note" data-testid="usage-note-output">
          {t("usage.outputNote")}
        </p>
      ) : null}
      {/* Last, and outside the scroller: the panel ends on the line that reconciles what it
          listed with what it did not, not on a button. */}
      <p className="usage-footer" data-testid="usage-footer">
        {t("usage.footer", {
          listed: formatTokens(ranking.listed),
          unnamed: formatTokens(unnamedTokens),
          total: formatTokens(ranking.total),
        })}
      </p>
    </div>
  );
}
