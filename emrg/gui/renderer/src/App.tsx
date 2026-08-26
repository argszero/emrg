import { ErrorBoundary } from "./components/ErrorBoundary";
import { Shell } from "./components/Shell";
import { I18nProvider } from "./lib/i18n";

/**
 * App — React 组件树根（Batch 0 骨架）。
 *
 * 组件树与设计文档 §4.1 对齐：
 *   <App>
 *   └── <ErrorBoundary>          # 全局错误边界（React 版，替代 error-boundary.js）
 *       └── <AppProviders>       # i18n / theme / daemon-event context（本期：i18n）
 *           └── <Layout>         # 本期为 <Shell> 占位
 *
 * 后续批次将 Shell 逐步替换为 Sidebar / TranscriptView / ResultPanel / Dialogs；
 * 旧 vanilla renderer（js/*.js）保持不动直到 Batch 5 一次性切换（设计 D3）。
 */
export function App() {
  return (
    <ErrorBoundary>
      <I18nProvider>
        <Shell />
      </I18nProvider>
    </ErrorBoundary>
  );
}
