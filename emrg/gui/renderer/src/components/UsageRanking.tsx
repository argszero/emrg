import { Dialog } from "./Dialog";
import { useI18n } from "../lib/i18n";
import { formatTokens } from "../lib/cockpit";
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
 */
export interface UsageRankingProps {
  open: boolean;
  segment: UsageSegment;
  /** The daemon's window reading; `null` = it has not answered, which is not zero. */
  reading: UsageWindowReading | null;
  /** A session's title, or `undefined` when this client knows none for it. */
  nameOf?: (sid: string) => string | undefined;
  onOpenSession: (sid: string) => void;
  onClose: () => void;
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
  reading,
  nameOf,
  onOpenSession,
  onClose,
}: UsageRankingProps) {
  const { t } = useI18n();
  const groups = reading?.groups ?? [];
  const ranking = rankSegment(groups, segment, reading ? reading[segment] : null);
  const label = t(`band.seg.${segment}`);
  // The volume no session names: the row's own figure when there is one, a **measured**
  // zero when the segment was measured and nothing was left unnamed, and `null` when the
  // daemon has not answered — a `0` there reports an absence as a quantity, which is the
  // defect class this whole window exists to remove.
  const unnamedTokens = ranking.unnamed
    ? ranking.unnamed.tokens
    : ranking.total === null
      ? null
      : 0;

  return (
    <Dialog
      open={open}
      title={t("usage.title", { segment: label })}
      onClose={onClose}
      testId="usage-ranking"
      actions={
        <button type="button" className="btn" onClick={onClose} data-testid="usage-close">
          {t("usage.close")}
        </button>
      }
    >
      <div className="usage-panel" data-testid="usage-panel">
        {ranking.rows.length === 0 ? (
          <p className="usage-empty" data-testid="usage-empty">
            {t("usage.empty")}
          </p>
        ) : (
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
        )}
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
        <p className="usage-footer" data-testid="usage-footer">
          {t("usage.footer", {
            listed: formatTokens(ranking.listed),
            unnamed: formatTokens(unnamedTokens),
            total: formatTokens(ranking.total),
          })}
        </p>
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
      </div>
    </Dialog>
  );
}
