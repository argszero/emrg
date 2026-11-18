import { useState } from "react";
import { useI18n } from "../lib/i18n";
import { formatActionElapsed, type TurnAction, type TurnFile } from "../lib/inspector";

/**
 * InspectorPanel — the page's fourth section, three tabs wide (issue #2969).
 *
 * It answers three questions about the turn being watched: what did it do (the tool
 * calls, in order, each with the duration the daemon measured), which files did it
 * name, and where does this session's memory live. The last one is a door rather than
 * a copy: the memory surface is a dialog that can read, scope and page itself, and a
 * second reader here would be a second answer to the same question.
 *
 * What this component is *not*: the old results panel. That panel brought its own tab
 * names, its own width, its own persistor and an English empty state, and it survived
 * the shell rebuild by being rendered inside the new skeleton. None of it is carried
 * over — this column's width belongs to the shell, which is also what makes it a
 * sibling of its drag handle rather than the owner of it.
 */
export type InspectorTabId = "actions" | "files" | "memory";

const TABS: { id: InspectorTabId; labelKey: string; testId: string }[] = [
  { id: "actions", labelKey: "inspector.tabActions", testId: "inspector-tab-actions" },
  { id: "files", labelKey: "inspector.tabFiles", testId: "inspector-tab-files" },
  { id: "memory", labelKey: "inspector.tabMemory", testId: "inspector-tab-memory" },
];

export interface InspectorPanelProps {
  /** 当前会话；`null` = 还没选中会话 */
  sid?: string | null;
  /** 本轮已经跑过的工具调用（lib/inspector 从 transcript 读出来，顺序即执行顺序） */
  actions: TurnAction[];
  /** 本轮点过名的文件 */
  files: TurnFile[];
  /** 打开记忆面板（记忆列表归那个对话框，这里只给门） */
  onOpenMemory?: () => void;
}

export function InspectorPanel({ sid = null, actions, files, onOpenMemory }: InspectorPanelProps) {
  const { t } = useI18n();
  const [active, setActive] = useState<InspectorTabId>("actions");

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
      <div className="inspector-body" role="tabpanel">
        {active === "actions" ? (
          actions.length === 0 ? (
            <p className="inspector-empty" data-testid="inspector-actions-empty">
              {t("inspector.emptyActions")}
            </p>
          ) : (
            <ol className="inspector-list" data-testid="inspector-actions">
              {actions.map((a) => (
                <li className="inspector-row" data-testid="inspector-action-row" key={a.callId}>
                  <span className="inspector-row-name" title={a.toolName}>
                    {a.toolName}
                  </span>
                  <span className="inspector-row-meta" data-testid="inspector-action-elapsed">
                    {formatActionElapsed(a.elapsed)}
                  </span>
                </li>
              ))}
            </ol>
          )
        ) : null}
        {active === "files" ? (
          files.length === 0 ? (
            <p className="inspector-empty" data-testid="inspector-files-empty">
              {t("inspector.emptyFiles")}
            </p>
          ) : (
            <ul className="inspector-list" data-testid="inspector-files">
              {files.map((f) => (
                <li className="inspector-row" data-testid="inspector-file-row" key={f.path}>
                  <span className="inspector-row-name" title={f.path}>
                    {f.path}
                  </span>
                  <span className="inspector-row-meta">{f.toolName}</span>
                </li>
              ))}
            </ul>
          )
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
