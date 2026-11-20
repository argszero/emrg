import { useState } from "react";
import { useI18n } from "../lib/i18n";
import { FileTree, FileTreeRoots, type LoadDir, type OpenFile } from "./FileTree";
import { sectionFlex } from "../lib/editor";

/**
 * LeftTrees — the session column's lower half: the agent's working directory above, the
 * session's own directory below (issue #3068).
 *
 * Both trees used to be tabs of the right-hand inspector, which meant reading a directory
 * required looking away from the list of what is running, and looking at a second file
 * required closing the first. They live here now, beside the sessions, and the right-hand
 * column became an editor.
 *
 * Each section collapses from its header, and collapsing only unmounts the tree: the
 * header stays where it is, which is what keeps a collapsed section's title at its
 * previous height. The heights themselves are `sectionFlex`'s job — the lower section
 * keeps the height the upper one gives up, and the height the lower one gives up stays at
 * the bottom of the column instead of being handed back.
 */
export interface LeftTreesProps {
  /** The agent's working directory; `null` = unknown, and the trees say so. */
  agentRoot?: string | null;
  /** The session's own directory; `null` = unknown. */
  sessionRoot?: string | null;
  /** The session's extra writable roots, in the order they were added; `null` = unknown. */
  sessionRoots?: string[] | null;
  loadDir?: LoadDir;
  /** A file row was clicked: open it in the right-hand editor. */
  onOpenFile?: OpenFile;
}

export function LeftTrees({
  agentRoot = null,
  sessionRoot = null,
  sessionRoots = null,
  loadDir,
  onOpenFile,
}: LeftTreesProps) {
  const { t } = useI18n();
  const [upperCollapsed, setUpperCollapsed] = useState(false);
  const [lowerCollapsed, setLowerCollapsed] = useState(false);
  // Open folders are shared by both sections and keyed by path, so folding the same
  // directory in either tree keeps them in step, and a path belonging to neither simply
  // contributes nothing.
  const [openFolders, setOpenFolders] = useState<Set<string>>(() => new Set());

  return (
    <div className="left-trees" data-testid="left-trees">
      <section
        className="left-section"
        data-testid="left-section-agent"
        data-collapsed={upperCollapsed ? "true" : "false"}
        style={{ display: "flex", flexDirection: "column", minHeight: 0, flex: sectionFlex("upper", upperCollapsed, lowerCollapsed) }}
      >
        <button
          type="button"
          className="left-section-header"
          data-testid="left-section-agent-header"
          aria-expanded={!upperCollapsed}
          onClick={() => setUpperCollapsed((v) => !v)}
        >
          <span className="left-section-caret" aria-hidden="true">
            {upperCollapsed ? "▸" : "▾"}
          </span>
          {t("leftTree.agentDir")}
        </button>
        {upperCollapsed ? null : (
          <div className="left-section-body">
            <FileTree
              testId="left-tree-agent"
              root={agentRoot}
              loadDir={loadDir}
              expanded={openFolders}
              onExpandedChange={setOpenFolders}
              onOpenFile={onOpenFile}
            />
          </div>
        )}
      </section>
      <section
        className="left-section"
        data-testid="left-section-session"
        data-collapsed={lowerCollapsed ? "true" : "false"}
        style={{ display: "flex", flexDirection: "column", minHeight: 0, flex: sectionFlex("lower", upperCollapsed, lowerCollapsed) }}
      >
        <button
          type="button"
          className="left-section-header"
          data-testid="left-section-session-header"
          aria-expanded={!lowerCollapsed}
          onClick={() => setLowerCollapsed((v) => !v)}
        >
          <span className="left-section-caret" aria-hidden="true">
            {lowerCollapsed ? "▸" : "▾"}
          </span>
          {t("leftTree.sessionDir")}
        </button>
        {lowerCollapsed ? null : (
          <div className="left-section-body">
            <FileTreeRoots
              testId="left-tree-session"
              roots={sessionRoot ? [sessionRoot, ...(sessionRoots ?? [])] : [null]}
              loadDir={loadDir}
              expanded={openFolders}
              onExpandedChange={setOpenFolders}
              onOpenFile={onOpenFile}
            />
          </div>
        )}
      </section>
    </div>
  );
}
