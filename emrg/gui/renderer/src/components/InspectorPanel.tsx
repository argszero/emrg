import { useState } from "react";
import { useI18n } from "../lib/i18n";
import { FileTree, type LoadDir } from "./FileTree";

/**
 * InspectorPanel — the page's fourth section, three tabs wide (issue #2969 / #3026).
 *
 * Two of the three tabs are the same tree with a different root: the working directory
 * of the agent this session belongs to, and the session's own session directory. Those
 * are the two places a session actually writes, and before this they had no surface at
 * all — the column offered a list of what the current turn happened to touch, which
 * emptied itself on every new message and said nothing about either directory.
 *
 * The third tab is a door rather than a copy: the memory surface is a dialog that can
 * read, scope and page itself, and a second reader here would be a second answer to the
 * same question.
 *
 * What this component is *not*: the old results panel, and no longer the old inspector
 * either. The turn's tool calls and the paths it named are gone from here — the
 * transcript already shows both, one row per call, with the duration the daemon
 * measured. A second list of the same thing was a copy that could disagree.
 */
export type InspectorTabId = "agent" | "session" | "memory";

const TABS: { id: InspectorTabId; labelKey: string; testId: string }[] = [
  { id: "agent", labelKey: "inspector.tabAgentDir", testId: "inspector-tab-agent" },
  { id: "session", labelKey: "inspector.tabSessionDir", testId: "inspector-tab-session" },
  { id: "memory", labelKey: "inspector.tabMemory", testId: "inspector-tab-memory" },
];

export interface InspectorPanelProps {
  /** 当前会话；`null` = 还没选中会话 */
  sid?: string | null;
  /** 该会话所属 agent 的工作目录（绝对路径）；`null` = 不知道 */
  agentRoot?: string | null;
  /** 该会话自己的会话目录（绝对路径）；`null` = 不知道 */
  sessionRoot?: string | null;
  /** 读一层目录；未接线时为 undefined，树会把原因说出来而不是画一棵空树 */
  loadDir?: LoadDir;
  /** 打开记忆面板（记忆列表归那个对话框，这里只给门） */
  onOpenMemory?: () => void;
}

export function InspectorPanel({
  sid = null,
  agentRoot = null,
  sessionRoot = null,
  loadDir,
  onOpenMemory,
}: InspectorPanelProps) {
  const { t } = useI18n();
  const [active, setActive] = useState<InspectorTabId>("agent");

  return (
    <section className="inspector" data-testid="inspector-panel" data-sid={sid ?? ""}>
      <div className="inspector-tabbar" role="tablist">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={active === tab.id}
            className={`inspector-tab${active === tab.id ? " active" : ""}`}
            data-testid={tab.testId}
            onClick={() => setActive(tab.id)}
          >
            {t(tab.labelKey)}
          </button>
        ))}
      </div>
      <div className="inspector-body" role="tabpanel" data-testid="inspector-body">
        {active === "agent" ? (
          <FileTree testId="inspector-tree-agent" root={agentRoot} loadDir={loadDir} />
        ) : null}
        {active === "session" ? (
          <FileTree testId="inspector-tree-session" root={sessionRoot} loadDir={loadDir} />
        ) : null}
        {active === "memory" ? (
          <div className="inspector-memory" data-testid="inspector-memory">
            <p className="inspector-empty">{t("inspector.memoryHint")}</p>
            <button
              type="button"
              className="inspector-door"
              data-testid="inspector-memory-open"
              onClick={onOpenMemory}
            >
              {t("inspector.openMemory")}
            </button>
          </div>
        ) : null}
      </div>
    </section>
  );
}
