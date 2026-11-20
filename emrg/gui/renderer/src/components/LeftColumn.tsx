import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useI18n } from "../lib/i18n";
import { LEFT_TREE_HEADER_PX, leftTreeLayout } from "../lib/cockpit";

/**
 * LeftColumn — the left column's three stacked sections (issue #3068).
 *
 * The trees used to live in the right-hand inspector, two of its three tabs. Reading a
 * directory therefore meant looking away from the session list, which is the one thing
 * that says what is running. They sit under it now: the open sessions first, then the
 * agent's working directory, then this session's own directory.
 *
 * The session list is passed in whole and stays exactly what it was — it is the section
 * that absorbs whatever height the two trees leave, so its own scrolling is untouched.
 * The two trees are the sections this component sizes and collapses.
 *
 * **The collapse rule is arithmetic, not flex.** Collapsing a section hands its height
 * to the section below it; collapsing the bottom one leaves that height standing at the
 * bottom of the column. Proportional `flex-grow` would redistribute it over *every*
 * section, which is the one thing the rule forbids, so the heights come from
 * `leftTreeLayout` and are written in pixels.
 *
 * The column measures itself rather than trusting a written number: a host who drags
 * the window shorter gets the same shares of a smaller column. Before the first
 * measurement — and in a DOM with no layout at all — the two sections fall back to a
 * quarter of the column each, which is the same reading the layout function produces.
 */
export interface LeftColumnProps {
  /** The open-sessions list, drawn unchanged. */
  sessions: ReactNode;
  /** The current agent's working directory. */
  agentTree: ReactNode;
  /** This session's own directory (one tree per writable root). */
  sessionTree: ReactNode;
}

type TreeSectionId = "agent" | "session";

export function LeftColumn({ sessions, agentTree, sessionTree }: LeftColumnProps) {
  const { t } = useI18n();
  const stackRef = useRef<HTMLDivElement | null>(null);
  const [total, setTotal] = useState(0);
  const [collapsed, setCollapsed] = useState<[boolean, boolean]>([false, false]);

  // The height the shares are taken of. A ResizeObserver is the reading that keeps up
  // with a dragged window; the window's own resize event is the fallback for a DOM
  // without one, and `clientHeight` is the only answer either way.
  useLayoutEffect(() => {
    const el = stackRef.current;
    if (!el) return;
    const measure = (): void => setTotal(el.clientHeight);
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(el);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);

  const toggle = useCallback((id: TreeSectionId): void => {
    setCollapsed(([a, b]) => (id === "agent" ? [!a, b] : [a, !b]));
  }, []);

  const layout = leftTreeLayout(total, collapsed, LEFT_TREE_HEADER_PX);

  const sections: { id: TreeSectionId; titleKey: string; testId: string; body: ReactNode }[] = [
    { id: "agent", titleKey: "inspector.tabAgentDir", testId: "left-section-agent", body: agentTree },
    { id: "session", titleKey: "inspector.tabSessionDir", testId: "left-section-session", body: sessionTree },
  ];

  return (
    <div className="left-stack" data-testid="left-stack" data-total={total} ref={stackRef}>
      <div className="left-sessions" data-testid="left-sessions">
        {sessions}
      </div>
      {sections.map((s, i) => {
        const isCollapsed = collapsed[i];
        const height = layout.heights[i];
        return (
          <section
            key={s.id}
            className="left-section"
            data-testid={s.testId}
            // The share is the rule; the height is that rule applied to this column.
            data-share={layout.shares[i]}
            data-collapsed={isCollapsed ? "1" : "0"}
            data-header-height={LEFT_TREE_HEADER_PX}
            // Measured: a pixel height. Unmeasured: the share as a flex basis, so the
            // column still opens at the right proportions with no layout engine.
            style={
              total > 0
                ? { height, flex: "none" }
                : { flex: `0 0 ${Math.round(layout.shares[i] * 100)}%`, minHeight: 0 }
            }
          >
            <button
              type="button"
              className="left-section-header"
              data-testid={`${s.testId}-header`}
              aria-expanded={!isCollapsed}
              onClick={() => toggle(s.id)}
            >
              <span className="left-section-caret" aria-hidden="true">
                {isCollapsed ? "▸" : "▾"}
              </span>
              <span className="left-section-title">{t(s.titleKey)}</span>
            </button>
            {isCollapsed ? null : (
              <div className="left-section-body" data-testid={`${s.testId}-body`}>
                {s.body}
              </div>
            )}
          </section>
        );
      })}
      {/* The height the bottom section gave up stays here: it is what keeps the
          sections above it where they were when the last one collapsed. */}
      <div
        className="left-spacer"
        data-testid="left-spacer"
        data-height={layout.spacer}
        style={{ flex: layout.spacer > 0 ? `0 0 ${layout.spacer}px` : "0 0 0px" }}
      />
    </div>
  );
}
