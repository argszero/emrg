/**
 * The editor's tab model — one file per tab, at most one preview, Memory always first
 * (issue #3068).
 *
 * Kept out of the component because these are the rules that decide what a click does,
 * and each of them has a wrong-but-plausible neighbour: a preview that does not replace
 * the previous one, a de-duplication that also rewrites the tab it found, a close that
 * falls back to Memory instead of the right-hand neighbour. Pure functions let a test
 * state the rule once and a mutation arm break exactly that rule.
 */

/** The tab that is always there and cannot be closed. */
export const MEMORY_TAB_ID = "memory";

export interface FileTab {
  /** The tab's identity: its absolute path. One file, one tab. */
  id: string;
  /** Absolute path of the file this tab shows. */
  path: string;
  /** The tree root the file was opened under, for the breadcrumb. */
  root: string;
  /** A preview tab is replaced by the next preview; its name renders in italics. */
  preview: boolean;
}

export interface EditorState {
  /** Pinned and preview file tabs, in the order they were opened. */
  tabs: FileTab[];
  /** The active tab's id: a file path, or `MEMORY_TAB_ID`. */
  active: string;
}

export const EMPTY_EDITOR: EditorState = { tabs: [], active: MEMORY_TAB_ID };

/**
 * Open (or re-focus) a file.
 *
 * Three rules, in order:
 *
 * 1. A file already open is only **focused** — no second tab, and its preview/pinned
 *    state is left exactly as it was. That is what lets a double click arrive as two
 *    clicks plus a pin without the clicks demoting the tab they just pinned.
 * 2. A preview replaces the previous preview rather than joining it, so single-clicking
 *    through a directory leaves one tab, not one per file looked at.
 * 3. Anything else appends a pinned tab.
 */
export function openFile(
  state: EditorState,
  req: { path: string; root: string; preview: boolean },
): EditorState {
  if (state.tabs.some((tab) => tab.path === req.path)) {
    return { tabs: state.tabs, active: req.path };
  }
  const kept = req.preview ? state.tabs.filter((tab) => !tab.preview) : state.tabs;
  const tab: FileTab = { id: req.path, path: req.path, root: req.root, preview: req.preview };
  return { tabs: [...kept, tab], active: tab.id };
}

/** Activate one tab; an id that names no tab leaves the state alone. */
export function activate(state: EditorState, id: string): EditorState {
  if (id !== MEMORY_TAB_ID && !state.tabs.some((tab) => tab.id === id)) return state;
  return { tabs: state.tabs, active: id };
}

/**
 * Pin a preview tab — a double click on the file row or on the tab itself.
 *
 * A pinned tab stays when the next preview arrives, which is the whole point of pinning.
 */
export function pinTab(state: EditorState, id: string): EditorState {
  if (!state.tabs.some((tab) => tab.preview && tab.id === id)) return state;
  return {
    tabs: state.tabs.map((tab) => (tab.id === id ? { ...tab, preview: false } : tab)),
    active: id,
  };
}

/**
 * Close one tab.
 *
 * Memory is never closed. When the closed tab was the active one, its **right-hand
 * neighbour** takes over; with nothing to the right, the left-hand neighbour does; with
 * nothing left at all, the editor returns to the Memory door. A tab closed while another
 * one is active leaves the active tab alone.
 */
export function closeTab(state: EditorState, id: string): EditorState {
  if (id === MEMORY_TAB_ID) return state;
  const index = state.tabs.findIndex((tab) => tab.id === id);
  if (index < 0) return state;
  const tabs = state.tabs.filter((tab) => tab.id !== id);
  if (state.active !== id) return { tabs, active: state.active };
  const neighbour = tabs[index] ?? tabs[index - 1] ?? null;
  return { tabs, active: neighbour ? neighbour.id : MEMORY_TAB_ID };
}

/**
 * The flex a left-column section declares, from which sections are collapsed (issue
 * #3068, item 2).
 *
 * Two sections share the height. Collapsing the upper one gives its height to the lower
 * one, which is why the lower one keeps `flex-grow: 1`. Collapsing the **lower** one
 * must leave the freed height at the bottom of the column rather than handing it back
 * to the upper one, so the upper section stops growing (`0 0 50%`) the moment the lower
 * one collapses. A width is never involved: this is the vertical axis only.
 */
export function sectionFlex(which: "upper" | "lower", upperCollapsed: boolean, lowerCollapsed: boolean): string {
  if (which === "upper") {
    if (upperCollapsed) return "0 0 auto";
    return lowerCollapsed ? "0 0 50%" : "1 1 50%";
  }
  if (lowerCollapsed) return "0 0 auto";
  return "1 1 50%";
}
