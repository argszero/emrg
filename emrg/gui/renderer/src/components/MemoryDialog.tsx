import { useI18n } from "../lib/i18n";
import { memoryRowView, type MemoryRow, type MemoryRowView } from "../lib/dialogLists";
import { Dialog } from "./Dialog";
import { DialogActions } from "./HelpDialog";

/**
 * MemoryDialog — /memory 记忆浏览器对话框（Batch 4 slice 2）。
 * 镜像 vanilla `showMemoryDialog(sub)`：
 * - loading（memories=null）→ dlg.loading 行
 * - 空列表 → app.noMemories（scope 区分 session/project 文案）
 * - 行 = 标题(40 截断) + 摘要(50 截断)；点击 → onSelect(id)（vanilla 内联 readMemory）
 * - detail 传入 → 显示详情块（title + pre body）
 * - error → app.memFailed
 * 数据注入式：memories/detail/error 由调用方（Batch 5 接线 window.emrg.listMemories/readMemory）提供。
 */
export interface MemoryDetailView {
  title: string;
  body: string;
}

export interface MemoryDialogProps {
  open: boolean;
  /** null = loading；[] = 空；有值 = 列表 */
  memories?: MemoryRow[] | null;
  scope?: "session" | "project";
  error?: string | null;
  detail?: MemoryDetailView | null;
  onSelect?: (id: string) => void;
  onDismiss?: () => void;
}

export function MemoryDialog({
  open,
  memories = null,
  scope = "project",
  error = null,
  detail = null,
  onSelect,
  onDismiss,
}: MemoryDialogProps) {
  const { t } = useI18n();
  const unnamed = t("app.unnamed");
  const rows: MemoryRowView[] = (memories ?? []).map((m) => memoryRowView(m, unnamed));

  return (
    <Dialog open={open} title={t("memory.title")} onClose={onDismiss} testId="memory-dialog">
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-3)" }}>
        {t("memory.desc")}
      </p>
      <div className="help-list" data-testid="memory-list">
        {error ? (
          <div className="help-row" data-testid="memory-error">
            <span className="help-hint">{t("app.memFailed", { msg: error })}</span>
          </div>
        ) : memories === null ? (
          <div className="help-row" data-testid="memory-loading">
            <span className="help-hint">{t("dlg.loading")}</span>
          </div>
        ) : rows.length === 0 ? (
          <div className="help-row" data-testid="memory-empty">
            <span className="help-hint">
              {t("app.noMemories", {
                scope: scope === "session" ? t("app.sessionMem") : t("app.projectMem"),
              })}
            </span>
          </div>
        ) : (
          rows.map((r) => (
            <button
              type="button"
              className="help-row"
              key={r.id}
              data-testid="memory-row"
              style={{ width: "100%", textAlign: "left", cursor: "pointer", background: "none", border: "none" }}
              onClick={() => onSelect && onSelect(r.id)}
            >
              <span className="help-cmd">{r.label}</span>
              <span className="help-hint">{r.hint}</span>
            </button>
          ))
        )}
      </div>
      {detail ? (
        <div className="memory-detail" data-testid="memory-detail">
          <div className="memory-detail-title">{detail.title}</div>
          <pre className="memory-detail-body">{detail.body}</pre>
        </div>
      ) : null}
      <DialogActions onDismiss={onDismiss} />
    </Dialog>
  );
}
