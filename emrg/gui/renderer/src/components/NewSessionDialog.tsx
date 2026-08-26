import { useI18n } from "../lib/i18n";
import { projectRowView, type ProjectRow } from "../lib/openSession";
import { Dialog } from "./Dialog";
import { DialogActions } from "./HelpDialog";

/**
 * NewSessionDialog — 新建会话对话框（Batch 4 slice 3）。
 * 镜像 vanilla `showNewSessionDialog()`：
 * - loading（projects=null）/ empty（noProjects）/ error（loadFailed）四态
 * - 项目行（name/path/最近活跃）→ 点击 onPickProject(p)（vanilla: newSession({projectPath})）
 * - 底部 "＋ 新建项目…" → onNewProject（vanilla: pickProjectDir → registerProject → newSession）
 * 数据注入式：projects 与回调由调用方（Batch 5 接线 window.emrg.listProjects/pickProjectDir）提供。
 */
export interface NewSessionDialogProps {
  open: boolean;
  /** null = loading；[] = 空；有值 = 列表 */
  projects?: ProjectRow[] | null;
  error?: string | null;
  onPickProject?: (p: ProjectRow) => void;
  onNewProject?: () => void;
  onDismiss?: () => void;
}

export function NewSessionDialog({
  open,
  projects = null,
  error = null,
  onPickProject,
  onNewProject,
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
    if (projects === null) {
      return (
        <div className="help-row" data-testid="new-session-loading">
          <span className="help-hint">{t("dlg.loading")}</span>
        </div>
      );
    }
    if (projects.length === 0) {
      return (
        <div className="help-row" data-testid="new-session-empty">
          <span className="help-hint">{t("openSession.noProjects")}</span>
        </div>
      );
    }
    return projects.map((p) => {
      const v = projectRowView(p, t);
      return (
        <button
          type="button"
          className="help-row"
          key={p.path || p.name || ""}
          data-testid="new-session-project"
          onClick={() => onPickProject?.(p)}
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
        <button type="button" className="btn btn-ghost" data-testid="new-session-new-project" onClick={onNewProject}>
          {t("newSession.newProject")}
        </button>
      </div>
      <DialogActions onDismiss={onDismiss} />
    </Dialog>
  );
}
