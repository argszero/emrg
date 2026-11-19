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
 */

/** What one directory's listing resolves to; `null` = this build cannot ask at all. */
export type LoadDir = (path: string) => Promise<{ entries: TreeEntry[]; truncated: boolean } | null>;

export interface FileTreeProps {
  /** Absolute path the tree is rooted at; `null` = no session to root at. */
  root: string | null;
  loadDir?: LoadDir;
  /** test-id prefix, so the two trees in one panel stay distinguishable. */
  testId: string;
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

export function FileTree({ root, loadDir, testId }: FileTreeProps) {
  const { t } = useI18n();
  const [dirs, setDirs] = useState<DirMap>({});
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [selected, setSelected] = useState<string | null>(null);
  // A refresh re-reads; the counter is the effect's trigger, not a value it reads.
  const [refreshSeq, setRefreshSeq] = useState(0);
  const bodyRef = useRef<HTMLDivElement | null>(null);

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
  // under it, so both are dropped rather than carried across.
  useEffect(() => {
    setExpanded(new Set());
    setSelected(null);
    setDirs({});
    if (root) void read(root);
    // `read` is re-created when the loader or the dictionary changes; the root and the
    // refresh counter are the events this effect answers.
  }, [root, refreshSeq]); // eslint-disable-line react-hooks/exhaustive-deps

  const rows = useMemo(() => rowsFor(root, dirs, expanded), [root, dirs, expanded]);
  const rootState = root ? dirs[root] : undefined;

  const open = useCallback(
    (row: TreeRow): void => {
      if (row.type !== "dir") return;
      setExpanded((prev) => {
        const next = new Set(prev);
        if (next.has(row.path)) next.delete(row.path);
        else next.add(row.path);
        return next;
      });
      if (dirs[row.path] === undefined) void read(row.path);
    },
    [dirs, read],
  );

  const onKeyDown = (e: React.KeyboardEvent): void => {
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
        <span className="ft-root" data-testid={`${testId}-root`} title={root ?? ""}>
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
          onClick={() => setExpanded(new Set())}
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
              }}
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
    </div>
  );
}
