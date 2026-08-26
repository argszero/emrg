import { useEffect, useState } from "react";
import { useI18n } from "../lib/i18n";
import { Dialog } from "./Dialog";

/**
 * WelcomeDialog — 首启引导对话框（Batch 4 slice 3）。
 * 镜像 vanilla `showWelcome()` / `saveWelcome()`：
 * - 打开时重置表单：apiKey/baseUrl 清空（数据注入式：initial* 由调用方提供）、
 *   model 下拉 = modelDetails names || models || fallback 三选；默认选中 defaultModel
 * - 空 API Key → 内联错误（vanilla showConfirm authKeyRequired 语义，React 版直接行内提示）
 * - 保存 → onSave({ apiKey, baseUrl, model })（Batch 5 接线 window.emrg.saveSettings + boot）
 * 数据注入式：不直接调用 window.emrg（jsdom 无 preload 桥）。
 */
export interface WelcomeSettings {
  apiKey: string;
  baseUrl: string;
  model: string;
}

export interface WelcomeDialogProps {
  open: boolean;
  initialApiKey?: string;
  initialBaseUrl?: string;
  /** 模型选项（vanilla: modelDetails names || models || fallback） */
  models?: string[];
  defaultModel?: string;
  /** 保存回调（Batch 5 接线 saveSettings + App.boot） */
  onSave?: (config: WelcomeSettings) => void | Promise<void>;
  onDismiss?: () => void;
}

const FALLBACK_MODELS = ["deepseek-chat", "deepseek-v3", "gpt-4o"];

export function WelcomeDialog({
  open,
  initialApiKey = "",
  initialBaseUrl = "",
  models = [],
  defaultModel = "",
  onSave,
  onDismiss,
}: WelcomeDialogProps) {
  const { t } = useI18n();
  const [apiKey, setApiKey] = useState(initialApiKey);
  const [baseUrl, setBaseUrl] = useState(initialBaseUrl);
  const [model, setModel] = useState("");
  const [error, setError] = useState<string | null>(null);

  const options = models.length > 0 ? models : FALLBACK_MODELS;

  useEffect(() => {
    if (!open) return;
    setApiKey(initialApiKey);
    setBaseUrl(initialBaseUrl);
    setModel(defaultModel || options[0] || "");
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialApiKey, initialBaseUrl, defaultModel, models]);

  const handleSave = async () => {
    if (!apiKey.trim()) {
      setError(t("app.authKeyRequired"));
      return;
    }
    setError(null);
    try {
      await onSave?.({
        apiKey: apiKey.trim(),
        baseUrl: baseUrl.trim(),
        model: model || options[0] || "deepseek-chat",
      });
    } finally {
      onDismiss?.();
    }
  };

  if (!open) return null;
  return (
    <Dialog open title={t("welcome.title")} onClose={onDismiss} testId="welcome-dialog">
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-3)" }}>
        {t("welcome.sub")}
      </p>
      <p style={{ margin: "0 0 var(--sp-1)", fontWeight: 600 }}>{t("welcome.step1")}</p>
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-2)" }}>
        {t("welcome.step1Hint")}
      </p>
      <label style={{ display: "block", margin: "var(--sp-2) 0 var(--sp-1)", fontSize: "var(--fs-secondary)" }}>
        {t("settings.apiKey")}
      </label>
      <input
        type="password"
        data-testid="welcome-api-key"
        value={apiKey}
        placeholder="sk-..."
        onChange={(e) => setApiKey(e.target.value)}
        style={{ width: "100%", boxSizing: "border-box" }}
      />
      <label style={{ display: "block", margin: "var(--sp-2) 0 var(--sp-1)", fontSize: "var(--fs-secondary)" }}>
        {t("welcome.baseUrl")}
      </label>
      <input
        type="text"
        data-testid="welcome-base-url"
        value={baseUrl}
        placeholder="https://api.openai.com/v1"
        onChange={(e) => setBaseUrl(e.target.value)}
        style={{ width: "100%", boxSizing: "border-box" }}
      />
      <label style={{ display: "block", margin: "var(--sp-2) 0 var(--sp-1)", fontSize: "var(--fs-secondary)" }}>
        {t("welcome.defaultModel")}
      </label>
      <select
        data-testid="welcome-model"
        value={model}
        onChange={(e) => setModel(e.target.value)}
        style={{ width: "100%", boxSizing: "border-box" }}
      >
        {options.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
      {error ? (
        <div className="welcome-error" data-testid="welcome-error" style={{ color: "var(--danger, #d33)", marginTop: "var(--sp-2)", fontSize: "var(--fs-secondary)" }}>
          {error}
        </div>
      ) : null}
      <div className="dialog-actions">
        <button type="button" className="btn btn-ghost" data-testid="welcome-cancel" onClick={onDismiss}>
          {t("settings.cancel")}
        </button>
        <button type="button" className="btn btn-primary" data-testid="welcome-save" onClick={handleSave}>
          {t("welcome.saveStart")}
        </button>
      </div>
    </Dialog>
  );
}
