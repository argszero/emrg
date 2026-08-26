import { useI18n } from "../lib/i18n";
import { helpRows, type HelpRowView } from "../lib/dialogLists";
import { Dialog } from "./Dialog";

/**
 * HelpDialog — /help 指令帮助对话框（Batch 4 slice 2）。
 * 镜像 vanilla `showHelpDialog()`：列出全部指令 + i18n 解析的 hint。
 * 数据注入：rows 由调用方（Batch 5 接线到 lib/commands.ts）传入。
 */
export interface HelpDialogProps {
  open: boolean;
  /** 帮助行（cmd + hint）；undefined → 不渲染（调用方负责构造） */
  rows?: HelpRowView[];
  onDismiss?: () => void;
}

export function HelpDialog({ open, rows = [], onDismiss }: HelpDialogProps) {
  const { t } = useI18n();
  return (
    <Dialog open={open} title={t("help.title")} onClose={onDismiss} testId="help-dialog">
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-3)" }}>
        {t("help.desc")}
      </p>
      <div className="help-list" data-testid="help-list">
        {rows.map((r) => (
          <div className="help-row" key={r.cmd} data-testid="help-row">
            <span className="help-cmd">{r.cmd}</span>
            <span className="help-hint">{r.hint}</span>
          </div>
        ))}
      </div>
      <DialogActions onDismiss={onDismiss} />
    </Dialog>
  );
}

/** 底部关闭按钮（help/memory/skills 共用：vanilla help.close 文案） */
export function DialogActions({ onDismiss }: { onDismiss?: () => void }) {
  const { t } = useI18n();
  return (
    <div className="dialog-actions">
      <button type="button" className="btn btn-ghost" data-testid="dlg-close" onClick={onDismiss}>
        {t("help.close")}
      </button>
    </div>
  );
}

// re-export for consumers that build rows via commands.ts hintText
export { helpRows };
