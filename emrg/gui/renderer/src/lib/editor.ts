/**
 * The editor's tab model — one file per tab, at most one preview, and nothing open when
 * there is nothing to show (rant 2026-11-21T10:18:48).
 *
 * Kept out of the component because these are the rules that decide what a click does,
 * and each of them has a wrong-but-plausible neighbour: a preview that does not replace
 * the previous one, a de-duplication that also rewrites the tab it found, a close that
 * falls back to the wrong neighbour. Pure functions let a test state the rule once and a
 * mutation arm break exactly that rule.
 */

/**
 * The state of a column with no file open.
 *
 * An empty string, because every tab's id is an absolute path and no absolute path is
 * empty: the one value that can never name a tab is what makes "nothing is open" a value
 * of `active` rather than a second field every reader has to remember to check.
 */
export const NO_FILE_OPEN = "";

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
  /** The active tab's id: a file path, or `NO_FILE_OPEN` when nothing is open. */
  active: string;
}

export const EMPTY_EDITOR: EditorState = { tabs: [], active: NO_FILE_OPEN };

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

/** Activate one tab; an id that names no open tab leaves the state alone. */
export function activate(state: EditorState, id: string): EditorState {
  if (id !== NO_FILE_OPEN && !state.tabs.some((tab) => tab.id === id)) return state;
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
 * Close one tab. Every tab closes — there is no permanence left in the strip.
 *
 * When the closed tab was the active one, its **right-hand neighbour** takes over; with
 * nothing to the right, the left-hand neighbour does; with nothing left at all, nothing
 * is open and the column renders empty. A tab closed while another one is active leaves
 * the active tab alone.
 */
export function closeTab(state: EditorState, id: string): EditorState {
  const index = state.tabs.findIndex((tab) => tab.id === id);
  if (index < 0) return state;
  const tabs = state.tabs.filter((tab) => tab.id !== id);
  if (state.active !== id) return { tabs, active: state.active };
  const neighbour = tabs[index] ?? tabs[index - 1] ?? null;
  return { tabs, active: neighbour ? neighbour.id : NO_FILE_OPEN };
}

/**
 * Close every tab but this one.
 *
 * An id that names no open tab changes nothing, rather than emptying the strip: the caller
 * is a menu drawn from a tab that existed when the menu opened, and "close the others of a
 * tab that is gone" has no answer that is better than doing nothing.
 */
export function closeOthers(state: EditorState, id: string): EditorState {
  if (!state.tabs.some((tab) => tab.id === id)) return state;
  return { tabs: state.tabs.filter((tab) => tab.id === id), active: id };
}

/**
 * Close every tab to the right of this one.
 *
 * The tabs left of it keep their order, and the named tab stays active — closing what is
 * *after* a tab cannot make a different one current.
 */
export function closeToTheRight(state: EditorState, id: string): EditorState {
  const index = state.tabs.findIndex((tab) => tab.id === id);
  if (index < 0) return state;
  const tabs = state.tabs.slice(0, index + 1);
  const activeStillOpen = tabs.some((tab) => tab.id === state.active);
  return { tabs, active: activeStillOpen ? state.active : id };
}

/**
 * Move one tab to where another one is, which is what a drag between two tabs means.
 *
 * The moved tab takes the target's **index**, so dropping on a tab to the right lands
 * before it in the original order and a drop to the left lands after it — the two cases a
 * single "insert at the target index" gets wrong when the source sits to the left, because
 * removing the source first shifts every later index down by one. That is why the index is
 * read from the list the move is applied to, after the removal.
 */
export function moveTab(state: EditorState, fromId: string, toId: string): EditorState {
  if (fromId === toId) return state;
  const from = state.tabs.findIndex((tab) => tab.id === fromId);
  const to = state.tabs.findIndex((tab) => tab.id === toId);
  if (from < 0 || to < 0) return state;
  const moved = state.tabs[from];
  const rest = state.tabs.filter((_, i) => i !== from);
  const at = rest.findIndex((tab) => tab.id === toId);
  const insertAt = from < to ? at + 1 : at;
  return { tabs: [...rest.slice(0, insertAt), moved, ...rest.slice(insertAt)], active: state.active };
}
