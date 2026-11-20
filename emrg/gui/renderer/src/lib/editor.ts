/**
 * editor.ts — the right panel's tab rules, without a DOM (issue #3068).
 *
 * The panel used to hold three tabs and one of them at a time: reading a second file
 * meant closing the first. It is an editor now — one tab strip over one read-only code
 * area — so what a click opens, what a second click does, and which tab is active after
 * a close are rules rather than JSX, and they live here where they can be asserted
 * without mounting anything.
 *
 * Two of them are the reason this file exists at all:
 *
 * - **The Memory tab is not a file.** It is first, it cannot be closed, and it is what
 *   is left when every file tab is gone. Written as "the first tab's close button is
 *   not rendered" that is a stylesheet; written here it is a state invariant the close
 *   path cannot break.
 * - **A single click previews, a second pins.** A preview tab is replaced by the next
 *   single click, so a host clicking down a directory to read one file ends with one
 *   tab rather than one per file, and pinning is what keeps a tab.
 *
 * Nothing here reads a file or knows a path's contents; the panel hands the active
 * path to the daemon's `read_file`, which is a separate decision.
 */

import { baseName } from "./fileTree";

/** The tab that is always there, first, and never closes. */
export const MEMORY_TAB_ID = "memory";

export type EditorTabKind = "memory" | "file";

export interface EditorTab {
  /** `MEMORY_TAB_ID` for the door; the absolute path for a file. */
  id: string;
  kind: EditorTabKind;
  /** Absolute path; `null` for the memory door. */
  path: string | null;
  /** What the strip prints: the file's base name, or empty for the door. */
  title: string;
  /**
   * `true` = this tab stays until its ✕; `false` = a preview, drawn in italics and
   * replaced by the next single click on another file.
   */
  pinned: boolean;
}

export interface EditorState {
  tabs: EditorTab[];
  /** The tab whose content the panel shows. Always names a tab that exists. */
  activeId: string;
}

/** The memory door, as a tab. Its label comes from the dictionary, not from here. */
export function memoryTab(): EditorTab {
  return { id: MEMORY_TAB_ID, kind: "memory", path: null, title: "", pinned: true };
}

/** The panel before any file is opened: just the door. */
export function initialEditor(): EditorState {
  return { tabs: [memoryTab()], activeId: MEMORY_TAB_ID };
}

/** The index of the one preview tab, or -1 when every open file tab is pinned. */
export function previewIndex(tabs: EditorTab[]): number {
  return tabs.findIndex((t) => t.kind === "file" && !t.pinned);
}

/** The active tab; never undefined, because `activeId` always names one. */
export function activeTab(state: EditorState): EditorTab {
  return state.tabs.find((t) => t.id === state.activeId) ?? state.tabs[0] ?? memoryTab();
}

/** How many file tabs are open (the Memory door is not one of them). */
export function fileTabCount(state: EditorState): number {
  return state.tabs.filter((t) => t.kind === "file").length;
}

/**
 * Open a file — or, when it is already open, walk to it.
 *
 * A single click opens a *preview*: it replaces the tab that is already a preview, and
 * when there is none it appends. A double click pins, which means the same click on a
 * file already open in preview promotes that tab instead of adding a second one — one
 * file is one tab, whatever was clicked to get there.
 */
export function openFile(state: EditorState, path: string, opts: { pin?: boolean } = {}): EditorState {
  const pin = opts.pin === true;
  const existing = state.tabs.findIndex((t) => t.kind === "file" && t.path === path);
  if (existing >= 0) {
    const tabs = state.tabs.map((t, i) => (i === existing ? { ...t, pinned: pin || t.pinned } : t));
    return { tabs, activeId: tabs[existing].id };
  }
  const tab: EditorTab = { id: path, kind: "file", path, title: baseName(path), pinned: pin };
  const preview = pin ? -1 : previewIndex(state.tabs);
  if (preview >= 0) {
    // The preview is the one slot a single click may overwrite, so clicking down a
    // directory does not grow the strip by one tab per file.
    const tabs = state.tabs.slice();
    tabs[preview] = tab;
    return { tabs, activeId: tab.id };
  }
  return { tabs: [...state.tabs, tab], activeId: tab.id };
}

/** Make an existing tab active. An id that names no tab changes nothing. */
export function activate(state: EditorState, id: string): EditorState {
  if (!state.tabs.some((t) => t.id === id)) return state;
  return { tabs: state.tabs, activeId: id };
}

/** Promote a tab from preview to pinned (a double click on the tab itself). */
export function pinTab(state: EditorState, id: string): EditorState {
  const i = state.tabs.findIndex((t) => t.id === id);
  if (i < 0 || state.tabs[i].pinned) return state;
  const tabs = state.tabs.slice();
  tabs[i] = { ...tabs[i], pinned: true };
  return { tabs, activeId: state.activeId };
}

/**
 * Close a tab. The memory door refuses; anything else goes.
 *
 * When the closed tab was the active one, the tab to its **right** becomes active —
 * reading position moves forward, which is what closing a tab means when the strip is
 * a reading queue. Nothing to the right (it was last) falls back to its left neighbour,
 * and closing the last file tab leaves the memory door active, which is also what the
 * panel shows when it has nothing else.
 */
export function closeTab(state: EditorState, id: string): EditorState {
  const i = state.tabs.findIndex((t) => t.id === id);
  if (i < 0) return state;
  if (state.tabs[i].kind === "memory") return state;
  const tabs = state.tabs.filter((_, j) => j !== i);
  if (state.activeId !== id) return { tabs, activeId: state.activeId };
  const next = tabs[i] ?? tabs[i - 1] ?? tabs[0];
  return { tabs, activeId: next ? next.id : MEMORY_TAB_ID };
}

/**
 * Close every file tab, keeping the door.
 *
 * The board's "close them all" is one rule rather than a loop over `closeTab`: with a
 * loop the intermediate states decide which tab ends up active, and the answer the
 * issue asks for is the door.
 */
export function closeAllFiles(state: EditorState): EditorState {
  return { tabs: state.tabs.filter((t) => t.kind === "memory"), activeId: MEMORY_TAB_ID };
}

export interface Breadcrumb {
  /** The tree root the file sits under, when it is known. */
  root: string | null;
  /** The segments below that root. */
  parts: string[];
}

/**
 * The breadcrumb's two pieces: the root a file came from, and the way down to it.
 *
 * The root is what the tree that opened the tab was rooted at, passed in by the caller
 * because only the tree knows it. A file outside that root — or a tab opened without
 * one — keeps its whole path as segments and names no root, rather than printing a root
 * the file is not under.
 */
export function breadcrumb(path: string, root: string | null): Breadcrumb {
  const norm = (p: string): string => p.replace(/[\\/]+$/, "");
  const r = root ? norm(root) : null;
  if (r) {
    const sep = r.includes("\\") && !r.includes("/") ? "\\" : "/";
    if (path === r || path.startsWith(`${r}${sep}`)) {
      const rest = path.slice(r.length).split(/[\\/]+/).filter(Boolean);
      return { root: r, parts: rest };
    }
  }
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return { root: null, parts };
}

/**
 * The lines of a file, as the code area draws them.
 *
 * Split on newlines only: the code area does not wrap (a long line scrolls sideways,
 * which is what keeps the line numbers in one fixed column aligned with their code), so
 * one entry here is exactly one rendered row.
 */
export function fileLines(content: string): string[] {
  return content.split("\n");
}
