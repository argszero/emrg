import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import {
  ICON_PX,
  INDENT_PX,
  ROW_HEIGHT,
  baseName,
  iconKind,
  neighbourRow,
  rowsFor,
  type DirMap,
  type TreeEntry,
  type TreeRow,
} from "../lib/fileTree";
import { ContextMenu, type ContextMenuId } from "./ContextMenu";

/**
 * FileTree — one directory tree, rooted wherever its caller says (issue #3026).
 *
 * Two of these sit in the inspector with different roots, so everything that could be
 * shared is shared and the root is the only difference. What the daemon answers
 * (`list_files`: directories first, sorted by name, dotfiles included, a `truncated`
 * flag, a readable error) is taken as it comes; what this component owns is which
 * directories are open, which rows are therefore drawn, and what an unreadable or empty
 * directory says in words.
 *
 * The levels are lazy: a folder's children are asked for when it is expanded, never
 * before, because the two roots here are working directories and a recursive read of one
 * is exactly the cost this tree exists to avoid.
 *
 * Right-clicking a row — or the root name in the toolbar, for the root itself — offers
 * the two things a tree row is for: take its absolute path away, or hand it to the
 * system (issue #3041). The menu is the repository's existing `.ctx-menu` block, and the
 * notice rows that stand in for an empty, loading or unreadable directory answer no
 * menu at all, because there is no path behind them.
 */

/** How long «已复制 ✓» stays before the menu closes itself. */
const COPY_FEEDBACK_MS = 1000;

/** The one main-process call this component makes (preload.js `emrg:openFile`). */
interface OpenFileBridge {
  openFile?: (payload: { filePath: string }) => Promise<unknown>;
}

/** What one directory's listing resolves to; `null` = this build cannot ask at all. */
export type LoadDir = (path: string) => Promise<{ entries: TreeEntry[]; truncated: boolean } | null>;

/**
 * A file row was clicked, with the absolute path and the root it hangs from (issue
 * #3068).
 *
 * `pin` is false for the first click and true for a double click: the editor opens a
 * preview for the former and a pinned tab for the latter. Directory rows never report —
 * a click on one only opens or closes it — so the editor has no way to open a folder by
 * accident.
 */
export type OpenFile = (req: { path: string; root: string; pin: boolean }) => void;

export interface FileTreeProps {
  /** Absolute path the tree is rooted at; `null` = no session to root at. */
  root: string | null;
  loadDir?: LoadDir;
  /** A file row was clicked; omitted = this tree is a reading only. */
  onOpenFile?: OpenFile;
  /** test-id prefix, so the two trees in one panel stay distinguishable. */
  testId: string;
  /**
   * The open folders, by absolute path; omitted = this tree keeps its own.
   *
   * Supplied by a caller that outlives the tree — the inspector unmounts a tab's tree
   * when another tab is chosen, so a set held here would be folded away and lost. Keyed
   * by path rather than by row, because a path means the same directory to every root
   * that shows it (issue #3063).
   */
  expanded?: Set<string>;
  /** The whole next set, whenever a folder is opened, closed, or all collapsed. */
  onExpandedChange?: (next: Set<string>) => void;
}

/** The glyph each kind draws. The kind is the contract; the character is decoration. */
const GLYPHS: Record<string, string> = {
  dir: "■",
  "dir-open": "□",
  markdown: "≡",
  text: "⋮",
  json: "{}",
  code: "</>",
  image: "▦",
  file: "▤",
};

export function FileTree({ root, loadDir, testId, expanded, onExpandedChange, onOpenFile }: FileTreeProps) {
  const { t } = useI18n();
  const [dirs, setDirs] = useState<DirMap>({});
  // Open folders are the caller's when it supplies `expanded`, and this tree's own
  // otherwise. Both readings go through `applyExpanded`, so the buttons, the rows and
  // the arrow keys cannot disagree about which one is in force.
  const [ownExpanded, setOwnExpanded] = useState<Set<string>>(() => new Set());
  const controlled = expanded !== undefined;
  const openSet = controlled ? expanded : ownExpanded;
  const applyExpanded = useCallback(
    (next: Set<string>): void => {
      onExpandedChange?.(next);
      if (!controlled) setOwnExpanded(next);
    },
    [controlled, onExpandedChange],
  );
  const [selected, setSelected] = useState<string | null>(null);
  // A refresh re-reads; the counter is the effect's trigger, not a value it reads.
  const [refreshSeq, setRefreshSeq] = useState(0);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  // Right-click state: where the menu sits, which path it acts on, which item the arrow
  // keys light up, and what the copy item says once it has been tried.
  const menuRef = useRef<HTMLDivElement | null>(null);
  const copyTimer = useRef<number | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; path: string } | null>(null);
  const [menuActive, setMenuActive] = useState(0);
  const [copyState, setCopyState] = useState<"ok" | "fail" | null>(null);

  const read = useCallback(
    async (dir: string): Promise<void> => {
      setDirs((d) => ({ ...d, [dir]: { status: "loading", entries: [], error: null, truncated: false } }));
      try {
        const answer = loadDir ? await loadDir(dir) : null;
        if (answer === null) {
          setDirs((d) => ({
            ...d,
            [dir]: { status: "error", entries: [], error: t("inspector.treeUnavailable"), truncated: false },
          }));
          return;
        }
        setDirs((d) => ({
          ...d,
          [dir]: {
            status: "loaded",
            entries: Array.isArray(answer.entries) ? answer.entries : [],
            error: null,
            truncated: !!answer.truncated,
          },
        }));
      } catch (e) {
        setDirs((d) => ({
          ...d,
          [dir]: {
            status: "error",
            entries: [],
            error: e instanceof Error && e.message ? e.message : String(e),
            truncated: false,
          },
        }));
      }
    },
    [loadDir, t],
  );

  // A new root is a different tree: the old levels and the old open folders mean nothing
  // under it, so both are dropped rather than carried across — unless the caller owns the
  // open folders, in which case they are its to keep (a path that does not sit under the
  // new root simply contributes no rows).
  useEffect(() => {
    if (!controlled) setOwnExpanded(new Set());
    setSelected(null);
    // A menu opened on the old tree points at a path this root no longer shows.
    setMenu(null);
    setCopyState(null);
    setDirs({});
    if (root) void read(root);
    // `read` is re-created when the loader or the dictionary changes; the root and the
    // refresh counter are the events this effect answers.
  }, [root, refreshSeq]); // eslint-disable-line react-hooks/exhaustive-deps

  const rows = useMemo(() => rowsFor(root, dirs, openSet), [root, dirs, openSet]);

  // An open folder is a promise that its children are drawn, and the listings behind
  // them do not survive this component. So a tree that comes back with folders already
  // open — the host switched tabs and returned — asks for those levels again, and only
  // for paths under its own root, since the set is shared across a session's roots.
  useEffect(() => {
    if (!root) return;
    if (dirs[root]?.status !== "loaded") return;
    const sep = root.includes("\\") && !root.includes("/") ? "\\" : "/";
    const prefix = root.endsWith(sep) ? root : `${root}${sep}`;
    for (const path of openSet) {
      if (!path.startsWith(prefix)) continue;
      if (dirs[path] === undefined) void read(path);
    }
  }, [root, dirs, openSet, read]);
  const rootState = root ? dirs[root] : undefined;

  const open = useCallback(
    (row: TreeRow): void => {
      if (row.type !== "dir") return;
      const next = new Set(openSet);
      if (next.has(row.path)) next.delete(row.path);
      else next.add(row.path);
      applyExpanded(next);
      if (dirs[row.path] === undefined) void read(row.path);
    },
    [dirs, read, openSet, applyExpanded],
  );

  const closeMenu = useCallback((): void => {
    setMenu(null);
    setCopyState(null);
    if (copyTimer.current !== null) {
      window.clearTimeout(copyTimer.current);
      copyTimer.current = null;
    }
  }, []);

  const openMenuAt = (e: React.MouseEvent, path: string): void => {
    e.preventDefault();
    e.stopPropagation();
    // The menu acts on one row, so that row is the selected one too — without it the
    // menu has no visible subject.
    setSelected(path);
    setMenu({ x: e.clientX, y: e.clientY, path });
    setMenuActive(0);
    setCopyState(null);
    // ↑↓ / Enter / Esc arrive on the body; the root name and the rows are not focusable.
    bodyRef.current?.focus();
  };

  const copyPath = async (path: string): Promise<void> => {
    try {
      const board = typeof navigator === "undefined" ? null : navigator.clipboard;
      if (!board || typeof board.writeText !== "function") throw new Error("clipboard unavailable");
      await board.writeText(path);
      setCopyState("ok");
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => closeMenu(), COPY_FEEDBACK_MS);
    } catch {
      // No clipboard to write into: say so and leave the menu standing, so trying again
      // is one more click rather than reopening the row.
      setCopyState("fail");
    }
  };

  const openWithDefault = async (path: string): Promise<void> => {
    closeMenu();
    const bridge = (window as unknown as { emrg?: OpenFileBridge }).emrg;
    try {
      await bridge?.openFile?.({ filePath: path });
    } catch {
      // The main process reports its own failures and the menu is already gone; there is
      // no second surface here to report into.
    }
  };

  const pick = (id: ContextMenuId): void => {
    const path = menu?.path;
    if (!path) return;
    if (id === "copy") void copyPath(path);
    else void openWithDefault(path);
  };

  const menuItems = useMemo(
    () => [
      {
        id: "copy" as const,
        label:
          copyState === "ok"
            ? t("inspector.treeCopied")
            : copyState === "fail"
              ? t("inspector.treeCopyFailed")
              : t("inspector.treeCopyPath"),
        testId: `${testId}-menu-copy`,
      },
      {
        id: "open" as const,
        label: t("inspector.treeOpenDefault"),
        testId: `${testId}-menu-open`,
      },
    ],
    [copyState, t, testId],
  );

  // A press anywhere but the menu dismisses it; the menu's own items stop that press
  // before it gets here.
  useEffect(() => {
    if (!menu) return;
    const onDown = (e: MouseEvent): void => {
      if (menuRef.current?.contains(e.target as Node)) return;
      closeMenu();
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [menu, closeMenu]);

  // The copy timer must not outlive the tree.
  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    },
    [],
  );

  const onKeyDown = (e: React.KeyboardEvent): void => {
    if (menu) {
      if (e.key === "Escape") {
        e.preventDefault();
        closeMenu();
        return;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : menuItems.length - 1;
        setMenuActive((i) => (i + step) % menuItems.length);
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        pick(menuItems[menuActive]?.id ?? "copy");
        return;
      }
    }
    if (rows.length === 0) return;
    const current = rows.find((r) => r.path === selected) ?? null;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setSelected(neighbourRow(rows, selected, e.key === "ArrowDown" ? 1 : -1));
      return;
    }
    if (e.key === "ArrowRight") {
      e.preventDefault();
      if (current && current.type === "dir" && !current.expanded) open(current);
      return;
    }
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (current && current.type === "dir" && current.expanded) open(current);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      if (current) open(current);
    }
  };

  const refresh = (): void => {
    // Every level already read is re-read: a refresh that only re-read the root would
    // leave every open folder showing what it held before the button was pressed.
    const loaded = Object.keys(dirs).filter((p) => dirs[p].status === "loaded");
    for (const p of loaded) void read(p);
    setRefreshSeq((n) => n + 1);
  };

  return (
    <div className="ft" data-testid={testId} data-root={root ?? ""}>
      <div className="ft-toolbar">
        <span
          className="ft-root"
          data-testid={`${testId}-root`}
          title={root ?? ""}
          onContextMenu={(e) => {
            // The toolbar's root name stands for the root directory itself.
            if (root) openMenuAt(e, root);
          }}
        >
          {root ? baseName(root) : "—"}
        </span>
        <button
          type="button"
          className="ft-tool"
          data-testid={`${testId}-refresh`}
          onClick={refresh}
          disabled={!root}
          title={t("inspector.treeRefresh")}
        >
          ⟳
        </button>
        <button
          type="button"
          className="ft-tool"
          data-testid={`${testId}-collapse`}
          onClick={() => applyExpanded(new Set())}
          disabled={!root}
          title={t("inspector.treeCollapse")}
        >
          ⊟
        </button>
      </div>
      <div
        className="ft-body"
        ref={bodyRef}
        role="tree"
        tabIndex={0}
        onKeyDown={onKeyDown}
        onScroll={() => {
          // The menu is fixed at the pointer, so a scroll leaves its row behind.
          if (menu) closeMenu();
        }}
        data-testid={`${testId}-body`}
      >
        {!root ? (
          <p className="ft-note" data-testid={`${testId}-no-session`}>
            {t("inspector.treeNoSession")}
          </p>
        ) : rootState?.status === "error" ? (
          <p className="ft-note ft-note-error" data-testid={`${testId}-error`}>
            {rootState.error}
          </p>
        ) : rootState === undefined || rootState.status === "loading" ? (
          <p className="ft-note" data-testid={`${testId}-loading`}>
            {t("inspector.treeLoading")}
          </p>
        ) : rows.length === 0 ? (
          <p className="ft-note" data-testid={`${testId}-empty`}>
            {t("inspector.treeEmpty")}
          </p>
        ) : null}
        {rows.map((row) => {
          const kind = iconKind(row);
          return (
            <div
              key={row.path}
              className={`ft-row${selected === row.path ? " selected" : ""}`}
              role="treeitem"
              aria-level={row.depth + 1}
              aria-selected={selected === row.path}
              aria-expanded={row.type === "dir" ? row.expanded : undefined}
              data-testid={`${testId}-row`}
              data-depth={row.depth}
              data-kind={kind}
              data-path={row.path}
              style={{ height: ROW_HEIGHT, paddingLeft: row.depth * INDENT_PX + 6 }}
              onClick={() => {
                setSelected(row.path);
                open(row);
                // A file row is also a request to see the file (issue #3068, item 6).
                // `open` above does nothing for a file, so a directory click reports
                // nothing and only expands.
                if (row.type === "file" && root) onOpenFile?.({ path: row.path, root, pin: false });
              }}
              onDoubleClick={() => {
                // A double click pins: the two single clicks before it opened a preview
                // (the second one only re-focused that same tab), and this makes it stay.
                if (row.type === "file" && root) onOpenFile?.({ path: row.path, root, pin: true });
              }}
              onContextMenu={(e) => openMenuAt(e, row.path)}
              title={row.name}
            >
              {/* One 1px guide per level: what makes a deep row's parentage readable
                  without counting pixels (issue #3026, item 6). */}
              {Array.from({ length: row.depth }, (_, i) => (
                <span
                  key={i}
                  className="ft-guide"
                  data-testid={`${testId}-guide`}
                  style={{ left: i * INDENT_PX + 4 }}
                  aria-hidden="true"
                />
              ))}
              <span className="ft-caret" aria-hidden="true">
                {row.type === "dir" ? (row.expanded ? "▾" : "▸") : ""}
              </span>
              <span className="ft-glyph" data-kind={kind} style={{ width: ICON_PX, height: ICON_PX }} aria-hidden="true">
                {GLYPHS[kind] ?? GLYPHS.file}
              </span>
              <span className="ft-name">{row.name}</span>
            </div>
          );
        })}
        {rootState?.status === "loaded" && rootState.truncated ? (
          <p className="ft-note" data-testid={`${testId}-more`}>
            {t("inspector.treeMore")}
          </p>
        ) : null}
      </div>
      {menu ? (
        <ContextMenu
          anchor={{ x: menu.x, y: menu.y }}
          items={menuItems}
          active={menuActive}
          testId={`${testId}-menu`}
          menuRef={menuRef}
          onPick={pick}
          onHover={setMenuActive}
        />
      ) : null}
    </div>
  );
}

/**
 * FileTreeRoots — the same tree drawn once per root, for a session's writable places
 * (issue #3063).
 *
 * A session writes under its own session directory and under every extra writable root
 * the host has named for it (`/sandbox add`), and only the first of those had a surface.
 * The roots are cells of one list rather than one tree with a synthetic top row, because
 * they are unrelated directories: a session root and a checkout elsewhere share no parent
 * to hang a common row under.
 *
 * The first root is drawn with the caller's `testId` unchanged and every later one as
 * `<testId>-extra-<n>`, so a single-root call renders exactly the DOM it rendered before
 * this component existed — no wrapper, no extra shell. One root is therefore the ordinary
 * case, and `roots` with one entry is how a session with no extra roots is drawn.
 *
 * The open folders are shared by all the roots and keyed by path, so a path named twice
 * is open once, and folding one root's folder never touches another's.
 */
export interface FileTreeRootsProps {
  /** The roots, in drawing order; a repeated path is drawn once, and `null` = unknown. */
  roots: (string | null)[];
  loadDir?: LoadDir;
  testId: string;
  expanded?: Set<string>;
  onExpandedChange?: (next: Set<string>) => void;
  /** A file row was clicked; forwarded to every root this wrapper draws. */
  onOpenFile?: OpenFile;
}

export function FileTreeRoots({ roots, loadDir, testId, expanded, onExpandedChange, onOpenFile }: FileTreeRootsProps) {
  const [own, setOwn] = useState<Set<string>>(() => new Set());
  const openSet = expanded ?? own;
  const setOpenSet = onExpandedChange ?? setOwn;
  // Same path twice is one root: the session directory and an extra root that names it
  // are the same place, and drawing both would show one directory as two rows that
  // expand separately. A trailing separator is a spelling of the same path, not a
  // different one.
  const unique: (string | null)[] = [];
  const seen = new Set<string>();
  for (const root of roots) {
    if (root === null || root === "") {
      if (seen.has("")) continue;
      seen.add("");
      unique.push(null);
      continue;
    }
    const key = root.replace(/[\\/]+$/, "");
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(root);
  }

  if (unique.length <= 1) {
    return (
      <FileTree
        testId={testId}
        root={unique[0] ?? null}
        loadDir={loadDir}
        expanded={openSet}
        onExpandedChange={setOpenSet}
        onOpenFile={onOpenFile}
      />
    );
  }

  return (
    <div className="ft-roots" data-testid={`${testId}-roots`} data-root-count={unique.length}>
      {unique.map((root, i) => (
        <FileTree
          key={root ?? `unknown-${i}`}
          testId={i === 0 ? testId : `${testId}-extra-${i}`}
          root={root}
          loadDir={loadDir}
          expanded={openSet}
          onExpandedChange={setOpenSet}
          onOpenFile={onOpenFile}
        />
      ))}
    </div>
  );
}
