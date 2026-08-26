import { useI18n } from "../lib/i18n";

/**
 * Shell — React 骨架占位布局（Batch 0）。
 * 设计文档 §5 Batch 0 项 4：错误边界/横幅/菜单等零风险组件先就位。
 * 后续批次逐个替换为真实组件：Sidebar / TranscriptView / ResultPanel / Dialogs。
 */
export function Shell() {
  const { t } = useI18n();
  return (
    <div className="react-shell" data-testid="react-shell">
      <header className="react-shell-header">
        <span className="react-shell-brand">✦ EMRG</span>
        <span className="react-shell-badge" data-testid="react-shell-badge">
          {t("shell.batch0Notice")}
        </span>
      </header>
      <main className="react-shell-main">
        <p className="react-shell-placeholder">{t("shell.placeholder")}</p>
      </main>
    </div>
  );
}
