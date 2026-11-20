import { useState, type ReactNode } from "react";
import { useI18n } from "../lib/i18n";
import { FOLDER_GLYPH, FileTree, FileTreeRoots, TreeLine, type LoadDir, type OpenFile } from "./FileTree";

/**
 * LeftTrees — the session column's lower half: one tree, three groups
 * (rant 2026-11-20T17:56:07, then 2026-11-20T21:27:29).
 *
 * It used to be two sections, two trees and two scrollbars, each taking half the height,
 * so finding a file began with deciding which half it belonged to. Now there is one
 * heading (「目录」) and one scroll container, and the three groups live inside it in a
 * fixed order: the agent's directory (folded at first), the session's own directory, and
 * the session's extra writable roots.
 *
 * The groups are the tree's *first level* (rant 2026-11-20T21:27:29). Each one used to be
 * a header button above a body of its own — a taller line, no indent, no icon, and a
 * toolbar the host never asked for. A group is now a `TreeLine` at depth 0: the same row
 * height, indent step, caret and folder glyph an ordinary directory row draws, differing
 * only in its type (11.5px, 700, secondary colour). Its next level is the real directory
 * drawn as a row carrying its name — `depthOffset={1}` — and that directory's contents
 * hang under it, so a group folds as a directory does rather than as a panel section.
 *
 * A group row is a grouping control, not a selection: clicking it folds, and it touches
 * neither the session nor the file the right-hand column is showing. Folding a group also
 * folds the folders under it, so re-opening a group shows its roots rather than a
 * screenful of levels opened an hour ago. Which groups are folded lives in this component
 * and nowhere else — no disk, no carry-over to the next session; the defaults below are
 * the first render's only.
 */
export interface LeftTreesProps {
  /** The agent's working directory; `null` = unknown, and the tree says so. */
  agentRoot?: string | null;
  /** The session's own directory; `null` = unknown. */
  sessionRoot?: string | null;
  /** The session's extra writable roots, in the order they were added; `null` = unknown. */
  sessionRoots?: string[] | null;
  loadDir?: LoadDir;
  /** A file row was clicked: open it in the right-hand editor. */
  onOpenFile?: OpenFile;
}

/** The three groups, in the order they are drawn. */
type GroupId = "agent" | "session" | "extra";

/**
 * Which groups start folded.
 *
 * The agent directory is the wide one and the one a session almost never edits, so it
 * starts folded; the session's own directory is where the work is, so it starts open — and
 * so do the extra roots, because a root the host added by hand is a root the host is
 * looking for.
 */
const DEFAULT_COLLAPSED: Record<GroupId, boolean> = { agent: true, session: false, extra: false };

/** The separator a path uses, read off the path itself rather than off the platform. */
const sepOf = (path: string): string => (path.includes("\\") && !path.includes("/") ? "\\" : "/");

/** Whether `path` is `root` or sits under it. */
function underOrEqual(path: string, root: string): boolean {
  if (path === root) return true;
  const sep = sepOf(root);
  return path.startsWith(root.endsWith(sep) ? root : root + sep);
}

export function LeftTrees({
  agentRoot = null,
  sessionRoot = null,
  sessionRoots = null,
  loadDir,
  onOpenFile,
}: LeftTreesProps) {
  const { t } = useI18n();
  const [collapsed, setCollapsed] = useState<Record<GroupId, boolean>>(DEFAULT_COLLAPSED);
  // Open folders are shared by all three groups and keyed by path (issue #3063), so
  // folding the same directory in one group keeps it folded in the others, and a path
  // belonging to no root contributes nothing.
  const [openFolders, setOpenFolders] = useState<Set<string>>(() => new Set());
  // `null` = the bridge has not answered yet, which reads as none: it answers an empty
  // list for a session that added no root, and there is no third thing to draw.
  const extras = sessionRoots ?? [];

  const rootsOf = (id: GroupId): string[] =>
    (id === "agent" ? [agentRoot] : id === "session" ? [sessionRoot] : extras).filter(
      (r): r is string => typeof r === "string" && r !== "",
    );

  const toggle = (id: GroupId): void => {
    const folding = !collapsed[id];
    if (folding) {
      // A folded group shows one row; the folders under it fold with it, so re-opening
      // the group does not bring back levels opened in an earlier pass.
      const roots = rootsOf(id);
      setOpenFolders((set) => new Set([...set].filter((p) => !roots.some((r) => underOrEqual(p, r)))));
    }
    setCollapsed((c) => ({ ...c, [id]: folding }));
  };

  const group = (id: GroupId, label: string, body: ReactNode): ReactNode => (
    <div
      key={id}
      className="left-group"
      data-testid={`left-group-${id}`}
      data-collapsed={collapsed[id] ? "true" : "false"}
    >
      <TreeLine
        testId={`left-group-${id}-row`}
        guideTestId={`left-group-${id}-row`}
        depth={0}
        variant="group"
        caret={collapsed[id] ? "▸" : "▾"}
        glyph={collapsed[id] ? FOLDER_GLYPH.closed : FOLDER_GLYPH.open}
        glyphKind={collapsed[id] ? "dir" : "dir-open"}
        name={label}
        ariaExpanded={!collapsed[id]}
        onClick={() => toggle(id)}
      />
      {collapsed[id] ? null : (
        <div className="left-group-body" data-testid={`left-group-${id}-body`}>
          {body}
        </div>
      )}
    </div>
  );

  return (
    <div className="left-trees" data-testid="left-trees">
      <div className="left-trees-header" data-testid="left-trees-header">
        {t("leftTree.dirs")}
      </div>
      {/* The one scrollbar: three groups inside a single scrolling column. */}
      <div className="left-trees-scroll" data-testid="left-trees-scroll">
        {group(
          "agent",
          t("leftTree.agentDir"),
          <FileTree
            testId="left-tree-agent"
            root={agentRoot}
            depthOffset={1}
            loadDir={loadDir}
            expanded={openFolders}
            onExpandedChange={setOpenFolders}
            onOpenFile={onOpenFile}
          />,
        )}
        {group(
          "session",
          t("leftTree.sessionDir"),
          <FileTree
            testId="left-tree-session"
            root={sessionRoot}
            depthOffset={1}
            loadDir={loadDir}
            expanded={openFolders}
            onExpandedChange={setOpenFolders}
            onOpenFile={onOpenFile}
          />,
        )}
        {group(
          "extra",
          t("leftTree.extraRoots"),
          extras.length === 0 ? (
            <TreeLine
              testId="left-group-extra-empty"
              guideTestId="left-group-extra-empty"
              depth={1}
              variant="empty"
              name={t("leftTree.noExtraRoots")}
            />
          ) : (
            <FileTreeRoots
              testId="left-tree-extra"
              roots={extras}
              depthOffset={1}
              loadDir={loadDir}
              expanded={openFolders}
              onExpandedChange={setOpenFolders}
              onOpenFile={onOpenFile}
            />
          ),
        )}
      </div>
    </div>
  );
}
