import type { ReactNode } from "react";
import { useI18n } from "../lib/i18n";
import { RAIL_WIDTH, type SidebarViewId } from "../lib/cockpit";
import { useTheme, nextMode } from "../lib/theme";
import { EmrgMark } from "./EmrgMark";

/**
 * IconRail — the 60px column at the far left, drawn to the prototype (issue #2986).
 *
 * What the prototype fixes, and what this file therefore states literally:
 *
 *  - the top mark is the repository's own EMRG icon, not a character;
 *  - five page buttons in one contiguous run — sessions, agents, tasks, rants,
 *    settings — because settings is a page like the other four. It used to sit on its
 *    own at the bottom behind a flexible spacer, which read as a different kind of
 *    thing from the four it opens;
 *  - the spacer, the theme toggle and the daemon dot come *after* the five;
 *  - nothing here wears a class from the sidebar nav this rail replaced. Those rules
 *    outlived the layout that used them (`css/layout.css`), and `.side-nav-item`'s
 *    `flex: 1` kept matching the new buttons by class name: five 40px boxes became
 *    119px tall each and the icons spread down the whole column. A class name is not
 *    a namespace — the rail owns `icon-rail` / `rail-item` and only those;
 *  - the tooltip is drawn, not the browser's `title`. A native tooltip is painted by
 *    the window manager: it cannot be offset from the button, it cannot fade, and on a
 *    rail this narrow it covers the icon it names.
 *
 * Every icon path is the prototype's `d` data verbatim. Redrawing one approximately is
 * the defect that made the last rail look like a different product.
 */
export interface IconRailProps {
  activeView: SidebarViewId;
  onSwitchView: (view: SidebarViewId) => void;
  /** The daemon dot: filled while this client holds a connection. */
  connected?: boolean;
}

interface RailItem {
  view: SidebarViewId;
  labelKey: string;
  icon: ReactNode;
}

const svg = (children: ReactNode) => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    {children}
  </svg>
);

/** The five pages, in the prototype's order, settings last among them. */
const ITEMS: RailItem[] = [
  {
    view: "sessions",
    labelKey: "nav.sessions",
    icon: svg(
      <>
        <path d="M20 15a3 3 0 0 1-3 3H9l-4 3v-3.5A3 3 0 0 1 3 15V6a3 3 0 0 1 3-3h11a3 3 0 0 1 3 3Z" />
        <path d="M8 8h8M8 12h5" />
      </>,
    ),
  },
  {
    view: "agents",
    labelKey: "nav.agents",
    icon: svg(
      <>
        <path d="M4 8.5 12 4l8 4.5-8 4.5-8-4.5Z" />
        <path d="M4 12.5 12 17l8-4.5M4 16.5 12 21l8-4.5" />
      </>,
    ),
  },
  {
    view: "tasks",
    labelKey: "nav.tasks",
    icon: svg(
      <>
        <circle cx="12" cy="12" r="8.5" />
        <path d="M12 7.5V12l3 2" />
      </>,
    ),
  },
  {
    view: "rants",
    labelKey: "nav.rants",
    icon: svg(
      <>
        <path d="M4 10v4a1 1 0 0 0 1 1h2l3.5 3.5V5.5L7 9H5a1 1 0 0 0-1 1Z" />
        <path d="M15 8.5a4 4 0 0 1 0 7M18 6a7.5 7.5 0 0 1 0 12" />
      </>,
    ),
  },
  {
    view: "settings",
    labelKey: "nav.settings",
    icon: svg(
      <>
        <path d="M4 7h9M17 7h3M4 12h3M11 12h9M4 17h9M17 17h3" />
        <circle cx="15" cy="7" r="2" />
        <circle cx="9" cy="12" r="2" />
        <circle cx="15" cy="17" r="2" />
      </>,
    ),
  },
];

const MOON = svg(<path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11Z" />);

const SUN = svg(
  <>
    <circle cx="12" cy="12" r="4.2" />
    <path d="M12 2.8v2.3M12 18.9v2.3M2.8 12h2.3M18.9 12h2.3M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M18.5 5.5l-1.7 1.7M7.2 16.8l-1.7 1.7" />
  </>,
);

export function IconRail({ activeView, onSwitchView, connected = false }: IconRailProps) {
  const { t } = useI18n();
  const { resolved, setMode } = useTheme();

  const button = (item: RailItem) => {
    const label = t(item.labelKey);
    return (
      <button
        key={item.view}
        type="button"
        className={`rail-item${activeView === item.view ? " active" : ""}`}
        data-view={item.view}
        data-testid={`nav-${item.view}`}
        aria-label={label}
        // No `title`: the tooltip below is drawn, and a native one would appear on top
        // of it a second later (issue #2986).
        onClick={() => onSwitchView(item.view)}
      >
        {item.icon}
        <span className="rail-tip" role="tooltip" data-testid={`rail-tip-${item.view}`}>
          {label}
        </span>
      </button>
    );
  };

  const themeLabel = t(resolved === "light" ? "rail.toDark" : "rail.toLight");

  return (
    <nav
      className="icon-rail"
      data-testid="side-nav"
      aria-label="Navigation"
      style={{ width: RAIL_WIDTH }}
    >
      <div className="rail-mark" data-testid="rail-mark">
        <EmrgMark size={32} />
      </div>
      {ITEMS.map(button)}
      <div className="rail-spacer" />
      <button
        type="button"
        className="rail-item rail-theme"
        data-testid="rail-theme"
        aria-label={themeLabel}
        data-theme-mode={resolved}
        onClick={() => setMode(nextMode(resolved))}
      >
        {resolved === "light" ? MOON : SUN}
        <span className="rail-tip" role="tooltip" data-testid="rail-tip-theme">
          {themeLabel}
        </span>
      </button>
      <div
        className={`rail-conn${connected ? " on" : ""}`}
        data-testid="rail-conn"
        data-connected={connected ? "true" : "false"}
        aria-label={t("rail.connected")}
      >
        <span className="rail-conn-dot" />
      </div>
    </nav>
  );
}
