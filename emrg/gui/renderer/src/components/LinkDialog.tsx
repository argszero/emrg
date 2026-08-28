import { useEffect, useRef } from "react";
import { useI18n } from "../lib/i18n";
import { Dialog } from "./Dialog";

/**
 * LinkDialog — 链接 URL 输入对话框（rant 2026-08-28T22:27:01）。
 * Electron 的 Chromium 禁用了 window.prompt()（安全考虑，直接抛错），
 * tiptap 默认 Link 扩展的 prompt 路径必然报错。此对话框用应用内 UI
 * 收集 URL：输入 → Enter/确定 → onApply(href)；取消/ESC → onCancel。
 */
export interface LinkDialogProps {
  open: boolean;
  /** 已激活链接的当前 href（编辑时预填）；无则空 */
  currentHref?: string;
  onApply?: (href: string) => void;
  onCancel?: () => void;
}

export function LinkDialog({ open, currentHref = "", onApply, onCancel }: LinkDialogProps) {
  const { t } = useI18n();
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open && inputRef.current) {
      inputRef.current.value = currentHref || "https://";
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [open, currentHref]);

  const submit = () => {
    const input = inputRef.current;
    if (!input) return;
    const href = input.value.trim();
    if (!href) return; // 空 URL 不提交
    onApply?.(href);
  };

  if (!open) return null;
  return (
    <Dialog
      open
      title={t("composer.linkDialogTitle")}
      onClose={onCancel}
      testId="link-dialog"
      actions={
        <>
          <button type="button" className="btn btn-ghost" data-testid="link-cancel" onClick={onCancel}>
            {t("settings.cancel")}
          </button>
          <button type="button" className="btn btn-primary" data-testid="link-ok" onClick={submit}>
            {t("settings.save")}
          </button>
        </>
      }
    >
      <label>
        <span>{t("composer.linkPrompt")}</span>
        <input
          ref={inputRef}
          type="text"
          placeholder="https://"
          data-testid="link-url"
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submit();
            }
          }}
        />
      </label>
    </Dialog>
  );
}
