import { useI18n } from "../lib/i18n";
import { agentRowView, type AgentRow } from "../lib/openSession";
import { Dialog } from "./Dialog";
import { DialogActions } from "./HelpDialog";

/**
 * NewSessionDialog — 新建会话对话框（Batch 4 slice 3）。
 * 镜像 vanilla `showNewSessionDialog()`：
 * - loading（agents=null）/ empty（noAgents）/ error（loadFailed）四态
 * - 项目行（name/path/最近活跃）→ 点击 onPickAgent(p)（vanilla: newSession({projectPath})）
 * - 底部 "＋ 新建项目…" → onNewAgent（vanilla: pickAgentDir → registerAgent → newSession）
 * 数据注入式：agents 与回调由调用方（Batch 5 接线 window.emrg.listAgents/pickAgentDir）提供。
 */
export interface NewSessionDialogProps {
  open: boolean;
  /** null = loading；[] = 空；有值 = 列表 */
  agents?: AgentRow[] | null;
  error?: string | null;
  onPickAgent?: (p: AgentRow) => void;
  onNewAgent?: () => void;
  onDismiss?: () => void;
}

export function NewSessionDialog({
  open,
  agents = null,
  error = null,
  onPickAgent,
  onNewAgent,
  onDismiss,
}: NewSessionDialogProps) {
  const { t } = useI18n();

  const renderList = () => {
    if (error) {
      return (
        <div className="help-row" data-testid="new-session-error">
          <span className="help-hint">{t("newSession.loadFailed", { msg: error })}</span>
        </div>
      );
    }
    if (agents === null) {
      return (
        <div className="help-row" data-testid="new-session-loading">
          <span className="help-hint">{t("dlg.loading")}</span>
        </div>
      );
    }
    if (agents.length === 0) {
      return (
        <div className="help-row" data-testid="new-session-empty">
          <span className="help-hint">{t("openSession.noAgents")}</span>
        </div>
      );
    }
    return agents.map((p) => {
      const v = agentRowView(p, t);
      return (
        <button
          type="button"
          className="help-row"
          key={p.path || p.name || ""}
          data-testid="new-session-agent"
          onClick={() => onPickAgent?.(p)}
          style={{ width: "100%", textAlign: "left", cursor: "pointer", background: "none", border: "none" }}
        >
          <span className="help-cmd">{v.label}</span>
          <span className="help-hint">{v.hint}</span>
          {v.active ? <span className="help-hint">{v.active}</span> : null}
        </button>
      );
    });
  };

  if (!open) return null;
  return (
    <Dialog open title={t("newSession.title")} onClose={onDismiss} testId="new-session-dialog">
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-3)" }}>
        {t("newSession.desc")}
      </p>
      <div className="help-list" data-testid="new-session-list">
        {renderList()}
      </div>
      <div className="dialog-actions">
        <button type="button" className="btn btn-ghost" data-testid="new-session-new-agent" onClick={onNewAgent}>
          {t("newSession.newAgent")}
        </button>
      </div>
      <DialogActions onDismiss={onDismiss} />
    </Dialog>
  );
}
