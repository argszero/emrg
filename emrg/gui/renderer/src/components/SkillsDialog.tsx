import { useI18n } from "../lib/i18n";
import { skillRowView, type SkillRow } from "../lib/dialogLists";
import { Dialog } from "./Dialog";
import { DialogActions } from "./HelpDialog";

/**
 * SkillsDialog — /skills 技能列表对话框（Batch 4 slice 2）。
 * 镜像 vanilla `showSkillsDialog()`：loading/空/列表/错误 四态。
 * 数据注入式：skills/error 由调用方（Batch 5 接线 window.emrg.listSkills）提供。
 */
export interface SkillsDialogProps {
  open: boolean;
  /** null = loading；[] = 空；有值 = 列表 */
  skills?: SkillRow[] | null;
  error?: string | null;
  onDismiss?: () => void;
}

export function SkillsDialog({ open, skills = null, error = null, onDismiss }: SkillsDialogProps) {
  const { t } = useI18n();
  const unnamed = t("app.unnamed");

  return (
    <Dialog open={open} title={t("skills.title")} onClose={onDismiss} testId="skills-dialog">
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-3)" }}>
        {t("skills.desc")}
      </p>
      <div className="help-list" data-testid="skills-list">
        {error ? (
          <div className="help-row" data-testid="skills-error">
            <span className="help-hint">{t("app.skillsFailed", { msg: error })}</span>
          </div>
        ) : skills === null ? (
          <div className="help-row" data-testid="skills-loading">
            <span className="help-hint">{t("dlg.loading")}</span>
          </div>
        ) : skills.length === 0 ? (
          <div className="help-row" data-testid="skills-empty">
            <span className="help-hint">{t("app.noSkills")}</span>
          </div>
        ) : (
          (skills ?? []).map((s, i) => {
            const v = skillRowView(s, unnamed);
            return (
              <div className="help-row" key={`${v.label}-${i}`} data-testid="skill-row">
                <span className="help-cmd">{v.label}</span>
                <span className="help-hint">{v.hint}</span>
              </div>
            );
          })
        )}
      </div>
      <DialogActions onDismiss={onDismiss} />
    </Dialog>
  );
}
