import { useI18n } from "../lib/i18n";
import { skillRowView, skillShadowView, type SkillRow, type SkillShadowRow } from "../lib/dialogLists";
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
  /** 同名冲突里被顶替的声明 —— 每项点名「生效的那份」与「落败的那份」，各带 source 与 path */
  shadows?: SkillShadowRow[];
  error?: string | null;
  onDismiss?: () => void;
}

export function SkillsDialog({
  open,
  skills = null,
  shadows = [],
  error = null,
  onDismiss,
}: SkillsDialogProps) {
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
      {!error && skills !== null && shadows.length > 0 ? (
        <div className="help-list" data-testid="skills-shadows">
          <div className="help-row" data-testid="skills-shadows-title">
            <span className="help-cmd">{t("skills.shadows")}</span>
          </div>
          {shadows.map((s, i) => {
            const v = skillShadowView(s, unnamed);
            return (
              <div className="help-row" key={`${v.name}-${i}`} data-testid="skill-shadow-row">
                <span className="help-cmd">{v.name}</span>
                <span className="help-hint">
                  {t("skills.shadowRow", { kept: v.kept, dropped: v.dropped })}
                </span>
              </div>
            );
          })}
        </div>
      ) : null}
      <DialogActions onDismiss={onDismiss} />
    </Dialog>
  );
}
