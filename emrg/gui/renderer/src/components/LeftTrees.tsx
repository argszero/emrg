import { useState, type ReactNode } from "react";
import { useI18n } from "../lib/i18n";
import { FileTree, FileTreeRoots, type LoadDir, type OpenFile } from "./FileTree";

/**
 * LeftTrees — the session column's lower half: one tree, three groups
 * (rant 2026-11-20T17:56:07).
 *
 * It used to be two sections, two trees and two scrollbars, each taking half the height,
 * so finding a file began with deciding which half it belonged to. Now there is one
 * heading (「目录」) and one scroll container, and the three groups live inside it in a
 * fixed order: the agent's directory (folded at first), the session's own directory, and
 * the session's extra writable roots.
 *
 * Neither of the old sections was one thing either: the extra writable roots hung under
 * the session directory that owned them, so the second tree carried two subjects at once.
 * They are their own group now.
 *
 * The height needs no arithmetic any more. A folded group is its header row and nothing
 * else — its body is not rendered, so there is no height to hide — and everything below
 * the heading belongs to the one scroll container, which is why `sectionFlex` and the two
 * inline flex values it produced are gone rather than ported.
 *
 * A group header is a grouping control, not a selection: it folds, and it does not touch
 * the session or the file the right-hand column is showing. Which groups are folded lives
 * in this component and nowhere else — no disk, no carry-over to the next session; the
 * defaults below are the first render's only.
 *
 * The tree itself is untouched (row height, indent, icons, guide lines, the shared set of
 * open folders): this file decides only what hangs under which group.
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

  const toggle = (id: GroupId): void => setCollapsed((c) => ({ ...c, [id]: !c[id] }));

  const group = (id: GroupId, label: string, body: ReactNode): ReactNode => (
    <div
      key={id}
      className="left-group"
      data-testid={`left-group-${id}`}
      data-collapsed={collapsed[id] ? "true" : "false"}
    >
      <button
        type="button"
        className="left-group-header"
        data-testid={`left-group-${id}-header`}
        aria-expanded={!collapsed[id]}
        onClick={() => toggle(id)}
      >
        <span className="left-group-caret" aria-hidden="true">
          {collapsed[id] ? "▸" : "▾"}
        </span>
        {label}
      </button>
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
            <div className="left-group-empty" data-testid="left-group-extra-empty">
              {t("leftTree.noExtraRoots")}
            </div>
          ) : (
            <FileTreeRoots
              testId="left-tree-extra"
              roots={extras}
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
