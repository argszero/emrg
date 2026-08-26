import { useI18n } from "../lib/i18n";
import { Sidebar } from "./Sidebar";
import { ResultPanel } from "./ResultPanel";
import { WorkspaceView } from "./WorkspaceView";

/**
 * Shell — React 骨架占位布局（Batch 0 + Batch 3 Sidebar/ResultPanel 挂载）。
 * 设计文档 §5 Batch 0 项 4：错误边界/横幅/菜单等零风险组件先就位。
 * Batch 3：左侧挂载 <Sidebar> + 右侧挂载 <ResultPanel>（当前空数据，Batch 5 接线 daemon 事件）。
 * 后续批次逐个替换为真实组件：TranscriptView / Dialogs。
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
      <div className="react-shell-body">
        <aside className="react-shell-sidebar" data-testid="react-shell-sidebar">
          <Sidebar openSessions={[]} />
        </aside>
        <main className="react-shell-main">
          <WorkspaceView activeView="projects" />
        </main>
        <ResultPanel sid={null} workspaceRoot="" />
      </div>
    </div>
  );
}
