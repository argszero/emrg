/**
 * cockpit.ts — the shell's pure rules (issue #2959).
 *
 * The cockpit is one agent's console, not a chat client, and every rule that can be
 * stated without a DOM lives here so it can be tested on its own：column geometry,
 * the status band's readings, the search overlay's three groups, and the scanner that
 * proves no fixed width caps the body.
 *
 * Nothing here reads a frame or a file: callers pass the numbers in.
 */

import type { OpenSessionEntry, SessionInfo } from "./sidebar";

/**
 * The pages the rail switches between, and the only set of views the shell has.
 *
 * Declared here rather than in a component because three of them need it — the rail,
 * the shell, and the workspace panel — and a union declared in one component makes
 * that component every other one's dependency.
 */
export type SidebarViewId = "sessions" | "agents" | "tasks" | "rants" | "settings";

/* ── Geometry ─────────────────────────────────────────────── */

/** The band is a fixed 44px: it is the one row that is always on screen. */
export const STATUS_BAND_HEIGHT = 44;
/** The icon rail's written width. */
export const RAIL_WIDTH = 60;
/** A resize handle is 6px of drag surface between two columns. */
export const HANDLE_WIDTH = 6;

/** The session column's written width, and the narrowest it may be dragged. */
export const LEFT_COLUMN_DEFAULT = 218;
export const LEFT_COLUMN_MIN = 180;
/** The inspector column's written width, and the narrowest it may be dragged. */
export const RIGHT_COLUMN_DEFAULT = 262;
export const RIGHT_COLUMN_MIN = 220;

/**
 * What the body may never be squeezed below: nothing.
 *
 * The two columns are the host's to size, and a drag to the window's edge is a
 * legitimate thing to want (issue #2969). The floor that keeps them retrievable is
 * each column's own minimum, not a reservation taken out of the body: a body reduced
 * to nothing is a state the host can drag back out of, and a body that silently refuses
 * to shrink leaves a drag that does nothing for no stated reason.
 */
export const BODY_MIN = 0;

/** Width persistence keys. One per column, so closing one never forgets the other. */
export const LS_LEFT_WIDTH = "emrg.cockpit.leftWidth";
export const LS_RIGHT_WIDTH = "emrg.cockpit.rightWidth";

export interface ColumnBounds {
  min: number;
  /** The widest this column may get: what the window leaves the body. */
  max: number;
}

/**
 * The bounds of one side column, from the window it sits in.
 *
 * `reserved` is every other fixed cost in the row — the rail, both handles, and the
 * opposite column when it is open. Growth has **no bound but the window** (issue #2959
 * item 4): the only ceiling is the body's own minimum.
 */
export function columnBounds(windowWidth: number, reserved: number, min: number): ColumnBounds {
  const max = windowWidth - reserved - BODY_MIN;
  return { min, max: max < min ? min : max };
}

/** Round and clamp a width into its bounds. A non-finite input answers the minimum. */
export function clampWidth(px: number, bounds: ColumnBounds): number {
  if (!Number.isFinite(px)) return bounds.min;
  return Math.min(bounds.max, Math.max(bounds.min, Math.round(px)));
}

/** The width a drag of `dx` from `startPx` lands on. */
export function resizedWidth(startPx: number, dx: number, bounds: ColumnBounds): number {
  return clampWidth(startPx + dx, bounds);
}

/**
 * A column's written width, if one was ever written and it is still usable.
 *
 * Kept beside `persistColumnWidth` so the two cannot disagree about the key or the
 * encoding; a stored value outside today's window is clamped by the caller, not here,
 * because only the caller knows the bounds.
 */
export function storedColumnWidth(key: string, fallback: number): number {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    const n = parseInt(raw, 10);
    return Number.isFinite(n) ? n : fallback;
  } catch {
    return fallback;
  }
}

export function persistColumnWidth(key: string, px: number): void {
  try {
    localStorage.setItem(key, String(Math.round(px)));
  } catch {
    /* a storage-denied renderer keeps working, it just forgets next launch */
  }
}

/* ── The left column's three sections (issue #3068) ────────── */

/**
 * The header of one tree section, in pixels.
 *
 * A section's height includes its header: a collapsed section is exactly this tall, and
 * keeping the number here rather than in the stylesheet is what lets the layout below
 * hand a collapsed section's *body* to the section beneath it without measuring one.
 */
export const LEFT_TREE_HEADER_PX = 24;

/**
 * How much of the column one tree section takes when nothing is collapsed.
 *
 * Two of these are half the column, which is the reading the issue asks for: the two
 * trees share the height the session list leaves, and the session list keeps the other
 * half. It is a share of how much, not of which section, and the arithmetic below is
 * what makes the collapse rule work on top of it.
 */
export const LEFT_TREE_SHARE = 0.25;

export interface LeftTreeLayout {
  /** Each tree section's height in pixels, header included. */
  heights: [number, number];
  /** Each tree section's share of the column: its allotment, or zero when collapsed. */
  shares: [number, number];
  /** The height standing empty at the bottom of the column. */
  spacer: number;
}

/**
 * The two tree sections' heights, from the column's height and which are collapsed.
 *
 * The rule (issue #3068): a collapsed section keeps its header where it is and gives its
 * height to the section below it; when the bottom section collapses, the freed height
 * stays at the bottom of the column rather than moving up into the sections above.
 *
 * The freed amount is therefore the collapsed section's *body* — its share less the
 * header it keeps — and it travels downward only. That is why this is a loop over the
 * sections in order rather than a proportional redistribution: `flex-grow` would spread
 * the freed height over every remaining section, which is the behaviour the last clause
 * rules out, and it would do so in a way no test could name.
 *
 * Measured on a 400px column with a 24px header, the four cases are
 * `[100,100]`, `[24,176]`, `[100,24]` with 76 left standing, and `[24,24]` with 152 —
 * each leaving the session list its 200px.
 *
 * A column that has not been measured (`total <= 0`) answers the shares and no heights;
 * the caller then draws those as flex bases, which is the same proportions with no
 * measurement to apply them to.
 */
export function leftTreeLayout(
  total: number,
  collapsed: [boolean, boolean],
  header: number = LEFT_TREE_HEADER_PX,
): LeftTreeLayout {
  const bases: [number, number] = [LEFT_TREE_SHARE, LEFT_TREE_SHARE];
  const bodyOf = (i: 0 | 1): number => Math.max(0, bases[i] * total - header);
  const bodies = [collapsed[0] ? 0 : bodyOf(0), collapsed[1] ? 0 : bodyOf(1)];
  let spacer = 0;
  const freed0 = collapsed[0] ? bodyOf(0) : 0;
  const freed1 = collapsed[1] ? bodyOf(1) : 0;
  // Downward only. With the bottom section already collapsed there is nowhere below,
  // so the height from above joins it at the bottom of the column.
  if (freed0 > 0) {
    if (collapsed[1]) spacer += freed0;
    else bodies[1] += freed0;
  }
  if (freed1 > 0) spacer += freed1;
  const heights: [number, number] = [
    collapsed[0] ? header : header + Math.round(bodies[0]),
    collapsed[1] ? header : header + Math.round(bodies[1]),
  ];
  const shares: [number, number] = [collapsed[0] ? 0 : bases[0], collapsed[1] ? 0 : bases[1]];
  return { heights, shares, spacer: Math.round(spacer) };
}

/* ── The status band's readings ───────────────────────────── */

/**
 * Cumulative token count, shortened for a 44px band.
 *
 * Money is *not* derived: no price is configured anywhere this client can read, so a
 * currency figure would be invented. The band reports the quantity the daemon actually
 * measured. `null` is "the daemon has not said", which is not zero.
 */
export function formatTokens(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n) || n < 0) return "—";
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/* ── The search overlay's three groups ────────────────────── */

/** One session a search can reach: open now, or known from the daemon's listing. */
export interface SessionHit {
  sid: string;
  title: string;
  agent: string;
  /**
   * The directory of the agent this session belongs to, when the listing named it.
   *
   * A history row is opened under its own agent (issue #2978), and this is the path
   * that carries it there. Empty means "this listing did not say" — the caller then
   * falls back to the daemon's own resolution rather than to a default directory.
   */
  cwd?: string;
  /** Last activity, epoch ms; 0 when nothing said. */
  at: number;
  /** Already open in the left column. */
  open: boolean;
}

export interface SessionGroups {
  open: SessionHit[];
  today: SessionHit[];
  earlier: SessionHit[];
}

function knownTime(s: SessionInfo): number {
  const raw = s.updated_at ?? s.created_at;
  if (typeof raw !== "string") return 0;
  const t = Date.parse(raw);
  return Number.isNaN(t) ? 0 : t;
}

function knownTitle(s: SessionInfo): string {
  return typeof s.title === "string" ? s.title : "";
}

function knownAgent(s: SessionInfo): string {
  const a = s.agent;
  return typeof a === "string" ? a : "";
}

function knownCwd(s: SessionInfo): string {
  const c = s.cwd;
  return typeof c === "string" ? c : "";
}

/**
 * Two listings of the same sessions, merged into one row per session id (issue #2978).
 *
 * The overlay reads two sources: the active agent's listing, which the app state keeps
 * live, and the daemon's whole-register listing, which is fetched afresh each time the
 * overlay opens. They overlap — the active agent's sessions are in both — and a row
 * that appeared twice would be counted twice, so the merge is by id.
 *
 * Which copy wins is the part worth stating: the row that **names its agent**. A row
 * without one cannot be found by agent name, and that is half of what the box is for;
 * a row without a directory cannot open under its own agent. Everything else is the
 * same `meta.json` read twice, so a field one copy is missing is the read that missed
 * it, not a different fact, and the other copy fills it.
 *
 * Order is not this function's business: `sessionHits` stamps each row's time and
 * `searchSessions` sorts the groups.
 */
export function mergeSessionRows(primary: SessionInfo[], all: SessionInfo[]): SessionInfo[] {
  const bySid = new Map<string, SessionInfo>();
  for (const s of [...all, ...primary]) {
    if (!s || typeof s.session_id !== "string" || !s.session_id) continue;
    const kept = bySid.get(s.session_id);
    if (!kept) {
      bySid.set(s.session_id, s);
      continue;
    }
    const named = knownAgent(kept) || knownCwd(kept) ? kept : s;
    const other = named === kept ? s : kept;
    bySid.set(s.session_id, {
      ...other,
      ...named,
      title: knownTitle(named) || knownTitle(other),
      agent: knownAgent(named) || knownAgent(other),
      cwd: knownCwd(named) || knownCwd(other),
    });
  }
  return [...bySid.values()];
}

/**
 * Every session this client can name, open ones first and each list by last activity.
 *
 * The open list is the authority for what is open — a session the daemon lists but the
 * host has not opened is a *history* row, and keeping the two apart is what stops the
 * left column from growing into the history list it used to be.
 */
export function sessionHits(openSessions: OpenSessionEntry[], knownSessions: SessionInfo[]): SessionHit[] {
  const known = new Map(knownSessions.map((s) => [s.session_id, s]));
  const hits: SessionHit[] = [];
  const seen = new Set<string>();
  for (const entry of openSessions) {
    const k = known.get(entry.sid);
    const at = entry.lastActive ? Date.parse(entry.lastActive) : k ? knownTime(k) : 0;
    hits.push({
      sid: entry.sid,
      title: entry.title || (k ? knownTitle(k) : ""),
      agent: entry.projectName || (k ? knownAgent(k) : ""),
      cwd: k ? knownCwd(k) : "",
      at: Number.isNaN(at) ? 0 : at,
      open: true,
    });
    seen.add(entry.sid);
  }
  for (const s of knownSessions) {
    if (seen.has(s.session_id)) continue;
    hits.push({
      sid: s.session_id,
      title: knownTitle(s),
      agent: knownAgent(s),
      cwd: knownCwd(s),
      at: knownTime(s),
      open: false,
    });
  }
  return hits;
}

function sameLocalDay(a: number, b: number): boolean {
  return new Date(a).toDateString() === new Date(b).toDateString();
}

function byRecent(a: SessionHit, b: SessionHit): number {
  return b.at - a.at;
}

/**
 * Split the hits into the overlay's three groups and filter them by one query.
 *
 * The query matches title, session id **and** agent name (issue #2959 item 6): a host
 * who remembers which agent it was should not have to remember the session's name. An
 * empty query keeps everything — the overlay doubles as the history browser.
 */
export function searchSessions(
  hits: SessionHit[],
  query: string,
  nowMs: number,
): SessionGroups {
  const q = query.trim().toLowerCase();
  const matched = q
    ? hits.filter((h) =>
        `${h.title}\n${h.sid}\n${h.agent}`.toLowerCase().includes(q),
      )
    : hits;
  const groups: SessionGroups = { open: [], today: [], earlier: [] };
  for (const h of matched) {
    if (h.open) groups.open.push(h);
    else if (h.at > 0 && sameLocalDay(h.at, nowMs)) groups.today.push(h);
    else groups.earlier.push(h);
  }
  groups.open.sort(byRecent);
  groups.today.sort(byRecent);
  groups.earlier.sort(byRecent);
  return groups;
}

export function groupSize(g: SessionGroups): number {
  return g.open.length + g.today.length + g.earlier.length;
}

/* ── The three wide pages: search, counting, ordering ─────── */

/** `hits / total`, the reading a search box always owes the reader. */
export function countLabel(hits: number, total: number): string {
  return `${hits} / ${total}`;
}

interface Named {
  name?: string;
}

/** The default searchable field: a row's own name. */
function nameOf(row: unknown): string {
  return String((row as Named | null | undefined)?.name ?? "");
}

/** Sorted by name, case-insensitively, so a list of agents reads the same twice. */
export function sortByName<T>(rows: T[], key: (row: T) => string = nameOf): T[] {
  return [...rows].sort((a, b) =>
    key(a).localeCompare(key(b), undefined, { sensitivity: "base" }),
  );
}

/**
 * Rows whose searchable text matches the query; an empty query keeps every row.
 *
 * The accessor is a parameter because not every page's row has a `name`: a rant is
 * identified by its timestamp and its text, and searching a rant list by a field it
 * does not have would answer zero for every query.
 */
export function filterByName<T>(rows: T[], query: string, key: (row: T) => string = nameOf): T[] {
  const q = query.trim().toLowerCase();
  if (!q) return rows;
  return rows.filter((r) => key(r).toLowerCase().includes(q));
}

/* ── The body-width guard ─────────────────────────────────── */

/**
 * Selectors whose `max-width` would cap the reading surface.
 *
 * Each pattern ends with a "not a longer identifier" assertion: `.composer` must not
 * match `.composer-card`, which is a bubble and legitimately narrower than its row.
 */
const BODY_SELECTORS: RegExp[] = [
  /\.react-shell-main(?![\w-])/,
  /\.transcript-view(?![\w-])/,
  /\.composer(?![\w-])/,
  /#composer-wrap(?![\w-])/,
  /\.msg-body(?![\w-])/,
  /\.workspace(?![\w-])/,
];

export interface WidthCap {
  selector: string;
  value: string;
}

/**
 * Every fixed width that caps the body, read out of a stylesheet's text.
 *
 * The rule (issue #2959 item 3) is that **no maximum width survives anywhere in the
 * shell**: a wider window always yields a wider body. Three declaration forms are not
 * caps and are skipped on purpose — `none`, a `calc()`/`var()` (not a number this
 * scanner can judge), and a percentage, which is a share of a container that itself
 * grows with the window. What is left is a number of px or rem, which is exactly the
 * kind that makes a 2000px window render a 760px body.
 *
 * The scanner is fed the text by its caller (a test globs the real stylesheets), so it
 * can be exercised in both directions: a fixture that caps and a fixture that does not.
 */
export function findBodyWidthCaps(css: string): WidthCap[] {
  const out: WidthCap[] = [];
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const block = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = block.exec(stripped)) !== null) {
    const selector = m[1].trim();
    if (!selector || selector.startsWith("@")) continue;
    const decl = /(?:^|;)\s*max-width\s*:\s*([^;]+)/.exec(m[2]);
    if (!decl) continue;
    const value = decl[1].trim();
    // A cap is a fixed length. Everything else is a share, a keyword, or arithmetic.
    const fixed = /^(\d+(?:\.\d+)?)(px|rem|em)$/.exec(value);
    if (!fixed) continue;
    if (!BODY_SELECTORS.some((re) => re.test(selector))) continue;
    out.push({ selector, value });
  }
  return out;
}
