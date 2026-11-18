import type { ReactNode } from "react";
import { useI18n } from "../lib/i18n";
import { RAIL_WIDTH, type SidebarViewId } from "../lib/cockpit";

/**
 * IconRail — the 60px column at the far left (issue #2959 item 2).
 *
 * Four page icons, drawn as lines rather than emoji, and the current page marked. The
 * four are the pages the contract names; settings keeps its own door at the bottom of
 * the rail, because removing a reachable surface was never part of the rebuild and a
 * settings panel nobody can open is a feature lost in a layout change.
 *
 * The rail is a *column*, not a row inside the sidebar: the sidebar became the list of
 * open sessions, and a rail that shared its width would move every time the list was
 * resized.
 */
export interface IconRailProps {
  activeView: SidebarViewId;
  onSwitchView: (view: SidebarViewId) => void;
}

interface RailItem {
  view: SidebarViewId;
  labelKey: string;
  icon: ReactNode;
}

const ITEMS: RailItem[] = [
  {
    view: "sessions",
    labelKey: "nav.sessions",
    icon: (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M20 15a3 3 0 0 1-3 3H8l-4 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3z" />
      </svg>
    ),
  },
  {
    view: "agents",
    labelKey: "nav.agents",
    icon: (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2.5h5a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2z" />
      </svg>
    ),
  },
  {
    view: "tasks",
    labelKey: "nav.tasks",
    icon: (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="8.5" />
        <path d="M12 7.5V12l3 2" />
      </svg>
    ),
  },
  {
    view: "rants",
    labelKey: "nav.rants",
    icon: (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 10v4l11 5V5z" />
        <path d="M18 9.5a4 4 0 0 1 0 5" />
      </svg>
    ),
  },
];

const SETTINGS: RailItem = {
  view: "settings",
  labelKey: "nav.settings",
  icon: (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3.5v2.2M12 18.3v2.2M3.5 12h2.2M18.3 12h2.2M6.2 6.2l1.6 1.6M16.2 16.2l1.6 1.6M17.8 6.2l-1.6 1.6M7.8 16.2l-1.6 1.6" />
    </svg>
  ),
};

export function IconRail({ activeView, onSwitchView }: IconRailProps) {
  const { t } = useI18n();
  const button = (item: RailItem) => (
    <button
      key={item.view}
      type="button"
      className={`rail-item side-nav-item${activeView === item.view ? " active" : ""}`}
      data-view={item.view}
      data-testid={`nav-${item.view}`}
      aria-label={t(item.labelKey)}
      title={t(item.labelKey)}
      onClick={() => onSwitchView(item.view)}
    >
      {item.icon}
    </button>
  );

  return (
    <nav
      className="icon-rail side-nav"
      data-testid="side-nav"
      aria-label="Navigation"
      style={{ width: RAIL_WIDTH }}
    >
      <div className="rail-mark" aria-hidden="true">
        ✦
      </div>
      {ITEMS.map(button)}
      <div className="rail-spacer" />
      {button(SETTINGS)}
    </nav>
  );
}
