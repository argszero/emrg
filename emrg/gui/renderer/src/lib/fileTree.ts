/**
 * fileTree.ts — the inspector's two directory trees (rant 2026-11-19T09:20:16).
 *
 * The daemon already answers one directory level: `list_files` sends the entries
 * directories-first, sorted by name, dotfiles included, with a `truncated` flag and a
 * readable error for a path it cannot read. What is left is the part a client owns —
 * which directories are open, which rows are therefore visible, and what icon each row
 * carries. Every rule here is pure, so it can be tested without a window.
 */

/** One entry of one directory level, exactly as `files_list` spells it. */
export interface TreeEntry {
  name: string;
  path: string;
  type: "dir" | "file";
}

/** One directory's reading, keyed by its absolute path. */
export interface DirState {
  status: "loading" | "loaded" | "error";
  entries: TreeEntry[];
  error: string | null;
  truncated: boolean;
}

export type DirMap = Record<string, DirState>;

/** One visible row: an entry, the depth it sits at, and whether it is open. */
export interface TreeRow {
  path: string;
  name: string;
  type: "dir" | "file";
  depth: number;
  expanded: boolean;
}

/**
 * The three visual numbers the host asked for, exported rather than written into CSS
 * alone: the row renders its own height, indent and icon size from these, so a test can
 * read them off the DOM instead of trusting a stylesheet that may not have loaded.
 */
export const ROW_HEIGHT = 22;
export const INDENT_PX = 8;
export const ICON_PX = 16;

/**
 * Directories first, then files, each sorted by name.
 *
 * The daemon already sorts this way; the client sorts again because the tree is also
 * fed by tests and by any future source, and an order rule that only one side applies
 * is a rule the other side can silently lose.
 */
export function sortEntries(entries: TreeEntry[]): TreeEntry[] {
  return [...entries].sort((a, b) => {
    if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

/**
 * The rows the tree draws, in display order.
 *
 * A directory contributes its children only when it is both expanded *and* loaded, so
 * an open-but-loading directory shows nothing under itself rather than an empty list —
 * the two states mean different things and the second one is a lie until it is read.
 */
export function rowsFor(
  root: string | null,
  dirs: DirMap,
  expanded: Set<string>,
): TreeRow[] {
  if (!root) return [];
  const out: TreeRow[] = [];
  const walk = (dir: string, depth: number): void => {
    const state = dirs[dir];
    if (!state || state.status !== "loaded") return;
    for (const entry of sortEntries(state.entries)) {
      const open = entry.type === "dir" && expanded.has(entry.path);
      out.push({
        path: entry.path,
        name: entry.name,
        type: entry.type,
        depth,
        expanded: open,
      });
      if (open) walk(entry.path, depth + 1);
    }
  };
  walk(root, 0);
  return out;
}

export type IconKind =
  | "dir"
  | "dir-open"
  | "markdown"
  | "text"
  | "json"
  | "code"
  | "image"
  | "file";

const BY_EXTENSION: Record<string, IconKind> = {
  md: "markdown",
  markdown: "markdown",
  mdx: "markdown",
  txt: "text",
  log: "text",
  json: "json",
  jsonl: "json",
  py: "code",
  ts: "code",
  tsx: "code",
  js: "code",
  jsx: "code",
  cjs: "code",
  mjs: "code",
  css: "code",
  html: "code",
  sh: "code",
  toml: "code",
  yml: "code",
  yaml: "code",
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  svg: "image",
  ico: "image",
};

/** The text after the last dot, lowercased; `""` when the name carries none. */
export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return "";
  return name.slice(dot + 1).toLowerCase();
}

/** A folder shows its open or closed shape; a file is classified by its extension. */
export function iconKind(row: Pick<TreeRow, "name" | "type" | "expanded">): IconKind {
  if (row.type === "dir") return row.expanded ? "dir-open" : "dir";
  return BY_EXTENSION[extensionOf(row.name)] ?? "file";
}

/**
 * The row a keyboard move lands on: the neighbour of `selected` in `rows`, wrapping at
 * the ends so that ↑ on the first row and ↓ on the last one both still move.
 */
export function neighbourRow(rows: TreeRow[], selected: string | null, delta: 1 | -1): string | null {
  if (rows.length === 0) return null;
  const at = selected === null ? -1 : rows.findIndex((r) => r.path === selected);
  if (at === -1) return rows[delta === 1 ? 0 : rows.length - 1].path;
  const next = (at + delta + rows.length) % rows.length;
  return rows[next].path;
}

/** The last path segment, which is what the tree's toolbar names its root by. */
export function baseName(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  return cut === -1 ? trimmed : trimmed.slice(cut + 1);
}
