import { useI18n } from "../lib/i18n";
import { rewindPointRow, rewindPointsNewestFirst, type RewindPoint } from "../lib/rewind";
import { Dialog } from "./Dialog";

/**
 * RewindDialog — /rewind 历史回退对话框（Batch 4 slice 4）。
 * 镜像 vanilla `showRewindDialog()`（app.js 288-326）：
 * - loading（points=null）→ dlg.loading 行
 * - 空列表（[]）→ app.noHistory
 * - 行 = #record_index + preview（60 截断），倒序（最新在上）；点击 → onPick(index)
 * - error → app.historyFailed
 * - 底部按钮文案 = rewind.cancel（vanilla data-i18n="rewind.cancel"）
 * 数据注入式：points/error 由调用方（Batch 5 接线 window.emrg.listHistory）提供。
 */
export interface RewindDialogProps {
  open: boolean;
  /** null = loading；[] = 空；有值 = 列表（顺序任意，组件内部倒序展示） */
  points?: RewindPoint[] | null;
  error?: string | null;
  /** 点击消息点 → 回退到该点（vanilla doRewind(recordIndex)） */
  onPick?: (index: number) => void;
  onDismiss?: () => void;
}

export function RewindDialog({ open, points = null, error = null, onPick, onDismiss }: RewindDialogProps) {
  const { t } = useI18n();
  if (!open) return null;
  return (
    <Dialog open title={t("rewind.title")} onClose={onDismiss} testId="rewind-dialog">
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-3)" }}>
        {t("rewind.desc")}
      </p>
      <div className="help-list" data-testid="rewind-list">
        {error ? (
          <div className="help-row" data-testid="rewind-error">
            <span className="help-hint">{t("app.historyFailed", { msg: error })}</span>
          </div>
        ) : points === null ? (
          <div className="help-row" data-testid="rewind-loading">
            <span className="help-hint">{t("dlg.loading")}</span>
          </div>
        ) : points.length === 0 ? (
          <div className="help-row" data-testid="rewind-empty">
            <span className="help-hint">{t("app.noHistory")}</span>
          </div>
        ) : (
          rewindPointsNewestFirst(points).map((p) => {
            const v = rewindPointRow(p);
            return (
              <button
                type="button"
                className="help-row"
                key={p.record_index}
                data-testid="rewind-point"
                style={{ width: "100%", textAlign: "left", cursor: "pointer", background: "none", border: "none" }}
                onClick={() => onPick?.(p.record_index)}
              >
                <span className="help-cmd">{v.label}</span>
                <span className="help-hint">{v.hint}</span>
              </button>
            );
          })
        )}
      </div>
      <div className="dialog-actions">
        <button type="button" className="btn btn-ghost" data-testid="rewind-close" onClick={onDismiss}>
          {t("rewind.cancel")}
        </button>
      </div>
    </Dialog>
  );
}
