import { useI18n } from "../lib/i18n";
import { projectRowView, sessionRowView, type ProjectRow, type SessionRow } from "../lib/openSession";
import { Dialog } from "./Dialog";
import { DialogActions } from "./HelpDialog";

/**
 * OpenSessionDialog — 两步打开会话对话框（Batch 4 slice 3）。
 * 镜像 vanilla `showOpenSessionDialog()` / `showProjectSessions()`：
 * - step="projects"：项目行（name/path/最近活跃 + 删除按钮）→ onPickProject(p)
 * - step="sessions"：标题 = 打开会话 — {project}；会话行（title||id + 当前/活跃标记）→ onPickSession(s)
 * - loading / empty / error 四态对齐 vanilla（dlg.loading / openSession.noProjects / noSessions / loadFailed）
 * - 删除按钮 → onDeleteProject(p)（Batch 5 接线 confirmDeleteProject：受保护 → 拒绝；否则 ConfirmDialog）
 * 数据注入式：step/projects/sessions 由调用方（Batch 5 接线 window.emrg.listProjects/listProjectSessions）
 * 驱动状态机。
 */
export type OpenSessionStep = "projects" | "sessions";

export interface OpenSessionDialogProps {
  open: boolean;
  step: OpenSessionStep;
  /** null = loading；[] = 空；有值 = 列表 */
  projects?: ProjectRow[] | null;
  /** step="sessions" 时的会话列表 */
  sessions?: SessionRow[] | null;
  /** step="sessions" 时的当前项目名（标题插值） */
  projectName?: string;
  currentSid?: string | null;
  error?: string | null;
  onPickProject?: (p: ProjectRow) => void;
  onPickSession?: (s: SessionRow) => void;
  onDeleteProject?: (p: ProjectRow) => void;
  onNewProject?: () => void;
  onNewSession?: () => void;
  onDismiss?: () => void;
}

export function OpenSessionDialog({
  open,
  step,
  projects = null,
  sessions = null,
  projectName = "",
  currentSid = null,
  error = null,
  onPickProject,
  onPickSession,
  onDeleteProject,
  onNewProject,
  onNewSession,
  onDismiss,
}: OpenSessionDialogProps) {
  const { t } = useI18n();
  const title = step === "sessions" ? t("openSession.titleProject", { project: projectName || "…" }) : t("openSession.title");

  const renderProjects = () => {
    if (error) {
      return (
        <div className="help-row" data-testid="open-session-error">
          <span className="help-hint">{t("openSession.loadFailed", { msg: error })}</span>
        </div>
      );
    }
    if (projects === null) {
      return (
        <div className="help-row" data-testid="open-session-loading">
          <span className="help-hint">{t("dlg.loading")}</span>
        </div>
      );
    }
    if (projects.length === 0) {
      return (
        <div className="help-row" data-testid="open-session-empty">
          <span className="help-hint">{t("openSession.noProjects")}</span>
        </div>
      );
    }
    return projects.map((p) => {
      const v = projectRowView(p, t);
      return (
        <div className="help-row" key={p.path || p.name || ""} data-testid="open-session-project" style={{ display: "flex", alignItems: "center", gap: "var(--sp-2)" }}>
          <button
            type="button"
            className="help-row-main"
            data-testid="open-session-pick"
            onClick={() => onPickProject?.(p)}
            style={{ flex: 1, textAlign: "left", cursor: "pointer", background: "none", border: "none", display: "flex", flexDirection: "column", alignItems: "flex-start", padding: 0 }}
          >
            <span className="help-cmd">{v.label}</span>
            <span className="help-hint">{v.hint}</span>
            {v.active ? <span className="help-hint">{v.active}</span> : null}
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            data-testid="open-session-delete"
            title={t("deleteProject.delete")}
            onClick={() => onDeleteProject?.(p)}
            style={{ padding: "2px 8px", flexShrink: 0 }}
          >
            {t("deleteProject.delete")}
          </button>
        </div>
      );
    });
  };

  const renderSessions = () => {
    if (error) {
      return (
        <div className="help-row" data-testid="open-session-error">
          <span className="help-hint">{t("openSession.loadFailed", { msg: error })}</span>
        </div>
      );
    }
    if (sessions === null) {
      return (
        <div className="help-row" data-testid="open-session-loading">
          <span className="help-hint">{t("dlg.loading")}</span>
        </div>
      );
    }
    if (sessions.length === 0) {
      return (
        <div className="help-row" data-testid="open-session-empty">
          <span className="help-hint">{t("openSession.noSessions")}</span>
        </div>
      );
    }
    return sessions.map((s) => {
      const v = sessionRowView(s, currentSid, t);
      return (
        <button
          type="button"
          className="help-row"
          key={s.session_id}
          data-testid="open-session-session"
          onClick={() => onPickSession?.(s)}
          style={{ width: "100%", textAlign: "left", cursor: "pointer", background: "none", border: "none" }}
        >
          <span className="help-cmd">{v.label}</span>
          <span className="help-hint">{v.marks.join(" · ")}</span>
        </button>
      );
    });
  };

  if (!open) return null;
  return (
    <Dialog open title={title} onClose={onDismiss} testId="open-session-dialog">
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-3)" }}>
        {t("openSession.desc")}
      </p>
      <div className="help-list" data-testid="open-session-list">
        {step === "projects" ? renderProjects() : renderSessions()}
      </div>
      <div className="dialog-actions">
        <button type="button" className="btn btn-ghost" data-testid="open-session-new-session" onClick={onNewSession}>
          {t("newSession.openButton")}
        </button>
        <button type="button" className="btn btn-ghost" data-testid="open-session-new-project" onClick={onNewProject}>
          {t("openSession.newProject")}
        </button>
      </div>
      <DialogActions onDismiss={onDismiss} />
    </Dialog>
  );
}
