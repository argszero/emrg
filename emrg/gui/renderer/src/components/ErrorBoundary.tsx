import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * ErrorBoundary — 全局错误边界（React 版，rant 2026-08-25T21:13:18 的 vanilla
 * error-boundary.js 迁移，行为保持）：
 * - 捕获子树渲染错误 → 全屏覆盖层（平时不渲染，零干扰）
 * - 标题 + 错误摘要（截断 500 字符）+ 时间戳 + 两按钮（复制错误 / 重新加载）
 * - 不做自动重载（防崩溃循环）；错误 handler 自身 try/catch，绝不在错误处理里再抛错
 * - 双语词典内建（zh/en，navigator.language 判定）——ErrorBoundary 位于
 *   I18nProvider 之上（设计 §4.1），不能依赖 context，与 vanilla 版内部 fallback 一致
 */
interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
  info: ErrorInfo | null;
}

const MAX_SUMMARY = 500;

const FALLBACK_DICTS: Record<string, Record<string, string>> = {
  zh: {
    "errorBoundary.title": "渲染器出错",
    "errorBoundary.copy": "复制错误",
    "errorBoundary.reload": "重新加载",
  },
  en: {
    "errorBoundary.title": "Renderer error",
    "errorBoundary.copy": "Copy error",
    "errorBoundary.reload": "Reload",
  },
};

function fallbackT(key: string): string {
  const lang =
    typeof navigator !== "undefined" && navigator.language.toLowerCase().startsWith("zh")
      ? "zh"
      : "en";
  return FALLBACK_DICTS[lang]?.[key] ?? key;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null, info: null };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 保留 console.error 原样日志（与 vanilla error-boundary.js 一致）
    console.error("React renderer error:", error, info.componentStack);
    this.setState({ info });
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return <ErrorOverlay error={error} info={this.state.info} />;
  }
}

interface ErrorOverlayProps {
  error: Error;
  info: ErrorInfo | null;
}

function ErrorOverlay({ error, info }: ErrorOverlayProps) {
  const summary = error.message.slice(0, MAX_SUMMARY);
  const stamp = new Date().toISOString();

  const handleCopy = async (): Promise<void> => {
    try {
      const text = `${error.message}\n${info?.componentStack ?? ""}`;
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // 非 secure context 兜底（与 vanilla error-boundary.js 行为一致）
        const ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
    } catch {
      /* 复制失败仅保留日志 */
    }
  };

  const handleReload = (): void => {
    window.location.reload();
  };

  return (
    <div id="error-boundary-overlay" className="error-boundary-overlay" role="alert">
      <div className="error-boundary-card">
        <h2 className="error-boundary-title">{fallbackT("errorBoundary.title")}</h2>
        <p className="error-boundary-summary">{summary}</p>
        <p className="error-boundary-stamp">{stamp}</p>
        <div className="error-boundary-actions">
          <button type="button" onClick={() => void handleCopy()}>
            {fallbackT("errorBoundary.copy")}
          </button>
          <button type="button" onClick={handleReload}>
            {fallbackT("errorBoundary.reload")}
          </button>
        </div>
      </div>
    </div>
  );
}
