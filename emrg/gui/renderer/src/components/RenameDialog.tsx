import { useEffect, useRef } from "react";
import { useI18n } from "../lib/i18n";
import type { RenameRequest } from "../lib/dialog";
import { Dialog } from "./Dialog";

/**
 * RenameDialog — 重命名对话对话框（Batch 4 slice 1）。
 * 镜像 vanilla `showRename(sid, currentTitle)` / `submitRename()`：
 * - 预填：currentTitle 非空且 ≠ sid → 填入；否则留空（未命名会话）
 * - 打开即 focus + select（vanilla 同）
 * - 空名不提交（vanilla `if (!title) return`）；Enter 提交；取消/ESC → onDismiss
 * - 提交 → onSubmit(sessionId, title)（Batch 5 接线到 window.emrg.renameSession）
 */
export interface RenameDialogProps {
  request: RenameRequest | null;
  /** 提交回调（外部接线 renameSession + 刷新会话列表） */
  onSubmit?: (sessionId: string, title: string) => void | Promise<void>;
  onDismiss?: () => void;
  maxLength?: number;
}

export function RenameDialog({ request, onSubmit, onDismiss, maxLength = 80 }: RenameDialogProps) {
  const { t } = useI18n();
  const inputRef = useRef<HTMLInputElement>(null);
  const open = request !== null;

  useEffect(() => {
    if (open && inputRef.current) {
      const initial =
        request?.currentTitle && request.currentTitle !== request.sessionId ? request.currentTitle : "";
      inputRef.current.value = initial;
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [open, request]);

  const submit = async () => {
    const req = request;
    const input = inputRef.current;
    if (!req || !input) return;
    const title = input.value.trim();
    if (!title) return; // 空名不提交（vanilla 语义）
    try {
      await onSubmit?.(req.sessionId, title);
    } finally {
      onDismiss?.();
    }
  };

  if (!open) return null;
  return (
    <Dialog
      open
      title={t("rename.title")}
      onClose={onDismiss}
      testId="rename-dialog"
      actions={
        <>
          <button type="button" className="btn btn-ghost" data-testid="rename-cancel" onClick={onDismiss}>
            {t("settings.cancel")}
          </button>
          <button type="button" className="btn btn-primary" data-testid="rename-ok" onClick={submit}>
            {t("settings.save")}
          </button>
        </>
      }
    >
      <label>
        <span>{t("rename.label")}</span>
        <input
          ref={inputRef}
          type="text"
          maxLength={maxLength}
          placeholder={t("rename.placeholder")}
          data-testid="rename-input"
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
