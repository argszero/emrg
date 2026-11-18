import { useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import { groupSize, searchSessions, type SessionHit } from "../lib/cockpit";

/**
 * SearchOverlay — one input, three groups (issue #2959 item 6).
 *
 * The left column holds the open sessions only, so the history needs a door of its
 * own: this overlay filters **open / today / earlier** at once and matches on title,
 * session id and agent name. Picking a session in either history group opens it and
 * selects it — which is the same thing the left column does for an open one.
 *
 * Grouping is `searchSessions`' business (a pure rule with its own test); this file
 * only renders what it is handed and owns the keyboard: Escape closes, a click on the
 * backdrop closes.
 */
export interface SearchOverlayProps {
  open: boolean;
  hits: SessionHit[];
  onClose: () => void;
  onPick: (sid: string) => void;
  nowMs?: number;
}

export function SearchOverlay({ open, hits, onClose, onPick, nowMs }: SearchOverlayProps) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  // A fresh open starts from an empty query: a filter left over from last time would
  // hide the sessions the host opened the overlay to find.
  useEffect(() => {
    if (!open) return;
    setQuery("");
    inputRef.current?.focus();
  }, [open]);

  const groups = useMemo(
    () => searchSessions(hits, query, nowMs ?? Date.now()),
    [hits, query, nowMs],
  );
  const total = groupSize(groups);

  if (!open) return null;

  const row = (h: SessionHit) => (
    <button
      type="button"
      className="search-hit"
      key={h.sid}
      data-sid={h.sid}
      data-testid="search-hit"
      title={h.sid}
      onClick={() => onPick(h.sid)}
    >
      <span className="search-hit-title">{h.title || h.sid}</span>
      <span className="search-hit-meta">{h.agent ? `${h.agent} · ${h.sid}` : h.sid}</span>
    </button>
  );

  return (
    <div
      className="search-overlay"
      data-testid="search-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={t("search.title")}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="search-card">
        <input
          ref={inputRef}
          className="search-input"
          data-testid="search-input"
          placeholder={t("search.placeholder")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              onClose();
            }
          }}
        />
        <div className="search-count" data-testid="search-count">
          {total}
        </div>
        <div className="search-groups">
          {groups.open.length > 0 ? (
            <div data-testid="search-group-open">
              <div className="search-group-label">{t("search.open")}</div>
              {groups.open.map(row)}
            </div>
          ) : null}
          {groups.today.length > 0 ? (
            <div data-testid="search-group-today">
              <div className="search-group-label">{t("search.today")}</div>
              {groups.today.map(row)}
            </div>
          ) : null}
          {groups.earlier.length > 0 ? (
            <div data-testid="search-group-earlier">
              <div className="search-group-label">{t("search.earlier")}</div>
              {groups.earlier.map(row)}
            </div>
          ) : null}
          {total === 0 ? (
            <div className="search-empty" data-testid="search-empty">
              {t("search.empty")}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
