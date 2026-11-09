import { useState } from "react";
import { useI18n } from "../lib/i18n";
import { Dialog } from "./Dialog";

/**
 * AtpLoginDialog — aitokenpool 的 GitHub 账号登录对话框（issue #2774 要求 2）。
 *
 * 与 GithubDeviceDialog 同形（大号一次性码 + 「打开浏览器」+ 「取消」+ 等待提示），
 * 两处不同：
 *  - 码可以被复制（要求 2 的「可复制」）。复制失败不挡授权：码仍然看得见、打得出来。
 *  - 失败文案由调用方**译好后**传进来，因为这个组件不认识服务端状态码：
 *    四种失败各自要说下一步（要求 11），而那句下一步是设置页按状态码选的。
 *
 * 纯展示：轮询计时器在调用方（设置页），组件不持有它——同一个理由，
 * GithubDeviceDialog 也不持有。
 */
export interface AtpLoginDialogProps {
  open: boolean;
  /** 一次性授权码；null = 尚未取到 */
  code?: string | null;
  /** 授权页地址（「打开浏览器」用的那个） */
  url?: string | null;
  /** true = 正在等浏览器里确认 */
  waiting?: boolean;
  /** 已译好的失败文案（四种失败各一句，见设置页） */
  error?: string | null;
  onOpenBrowser?: () => void;
  onDismiss?: () => void;
}

export function AtpLoginDialog({
  open,
  code = null,
  url = null,
  waiting = false,
  error = null,
  onOpenBrowser,
  onDismiss,
}: AtpLoginDialogProps) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  if (!open) return null;

  const copy = () => {
    void (async () => {
      try {
        await navigator.clipboard.writeText(code ?? "");
        setCopied(true);
      } catch {
        /* 剪贴板不可用（权限/无 https）——码仍在屏幕上，用户可以自己选中 */
      }
    })();
  };

  return (
    <Dialog open title={t("settings.atpDeviceTitle")} onClose={onDismiss} testId="atp-login-dialog">
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-3)" }}>
        {t("settings.atpDeviceDesc")}
      </p>
      <div
        className="atp-device-code"
        data-testid="atp-device-code"
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
          userSelect: "all",
        }}
      >
        {code ?? "—"}
      </div>
      {error ? (
        <div className="help-row" data-testid="atp-login-error">
          <span className="help-hint">{error}</span>
        </div>
      ) : null}
      <div className="dialog-actions" style={{ justifyContent: "center", flexWrap: "wrap", gap: 8 }}>
        <button type="button" className="btn btn-ghost" data-testid="atp-copy" onClick={copy} disabled={!code}>
          {copied ? t("settings.atpDeviceCopied") : t("settings.atpDeviceCopy")}
        </button>
        <button type="button" className="btn btn-primary" data-testid="atp-open" onClick={onOpenBrowser} disabled={!url}>
          {t("settings.atpDeviceOpen")}
        </button>
        <button type="button" className="btn btn-ghost" data-testid="atp-login-close" onClick={onDismiss}>
          {t("settings.atpDeviceCancel")}
        </button>
      </div>
      {waiting ? (
        <div className="hint" style={{ textAlign: "center", marginTop: "var(--sp-2)" }} data-testid="atp-login-wait">
          {t("settings.atpDeviceWait")}
        </div>
      ) : null}
    </Dialog>
  );
}
