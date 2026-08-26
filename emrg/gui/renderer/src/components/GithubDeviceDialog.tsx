import { useI18n } from "../lib/i18n";
import { Dialog } from "./Dialog";

/**
 * GithubDeviceDialog — GitHub device-flow 授权对话框（Batch 4 slice 4）。
 * 镜像 vanilla `startDeviceFlow()` / `initDeviceDialog()`（dialogs.js 399-461）：
 * - 大号等宽 code 展示（未获取到 → "—"；vanilla 初始 textContent = "—"）
 * - 「打开浏览器」按钮 → onOpenBrowser（vanilla `window.emrg.openExternal({ url })`）
 * - 「取消」按钮 → onDismiss（vanilla 停轮询 + close）
 * - waiting=true → 显示「等待浏览器中确认…」（vanilla github-status 轮询期间）
 * - error → settings.githubDeviceFailed
 * 数据注入式：code/url/waiting/error 由调用方（Batch 5 接线 window.emrg.githubConnectWeb
 * + githubStatus 轮询状态机）提供；组件本身不持有定时器，保持纯展示。
 */
export interface GithubDeviceDialogProps {
  open: boolean;
  /** 一次性授权码；null = 尚未获取（请求中/未开始） */
  code?: string | null;
  /** 授权页 URL（打开浏览器用） */
  url?: string | null;
  /** true = 正在等待浏览器中确认（轮询 github_status 期间） */
  waiting?: boolean;
  error?: string | null;
  onOpenBrowser?: () => void;
  onDismiss?: () => void;
}

export function GithubDeviceDialog({
  open,
  code = null,
  url = null,
  waiting = false,
  error = null,
  onOpenBrowser,
  onDismiss,
}: GithubDeviceDialogProps) {
  const { t } = useI18n();
  if (!open) return null;
  return (
    <Dialog open title={t("settings.githubDeviceTitle")} onClose={onDismiss} testId="github-device-dialog">
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-3)" }}>
        {t("settings.githubDeviceDesc")}
      </p>
      <div
        className="github-device-code"
        data-testid="github-device-code"
        style={{
          fontSize: 28,
          fontWeight: 700,
          letterSpacing: 3,
          textAlign: "center",
          padding: "var(--sp-3)",
          background: "var(--bg-2, rgba(0,0,0,.04))",
          borderRadius: 8,
          marginBottom: "var(--sp-3)",
          fontFamily: "monospace",
        }}
      >
        {code ?? "—"}
      </div>
      {error ? (
        <div className="help-row" data-testid="github-device-error">
          <span className="help-hint">{t("settings.githubDeviceFailed", { msg: error })}</span>
        </div>
      ) : null}
      <div className="dialog-actions" style={{ justifyContent: "center", flexWrap: "wrap", gap: 8 }}>
        <button type="button" className="btn btn-primary" data-testid="github-device-open" onClick={onOpenBrowser}>
          {t("settings.githubDeviceOpen")}
        </button>
        <button type="button" className="btn btn-ghost" data-testid="github-device-close" onClick={onDismiss}>
          {t("settings.githubDeviceCancel")}
        </button>
      </div>
      {waiting ? (
        <div className="hint" style={{ textAlign: "center", marginTop: "var(--sp-2)" }} data-testid="github-device-wait">
          {t("settings.githubDeviceWait")}
        </div>
      ) : null}
    </Dialog>
  );
}
