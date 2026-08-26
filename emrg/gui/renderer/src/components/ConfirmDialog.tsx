import { useI18n } from "../lib/i18n";
import { resolveConfirmButton, type ConfirmRequest } from "../lib/dialog";
import { Dialog } from "./Dialog";

/**
 * ConfirmDialog — 确认对话框（Batch 4 slice 1）。
 * 镜像 vanilla `showConfirm(title, message, opts)`：
 * - 默认 OK = 删除确认（confirm.delete 文案 + btn-danger）；danger:false → primary + settings.save
 * - OK → await onOk() 后 onDismiss()；取消/ESC → 仅 onDismiss()
 */
export interface ConfirmDialogProps {
  request: ConfirmRequest | null;
  /** 关闭回调（OK 执行完或取消/ESC） */
  onDismiss?: () => void;
  /** 取消按钮文案覆盖（默认 settings.cancel） */
  cancelText?: string;
}

export function ConfirmDialog({ request, onDismiss, cancelText }: ConfirmDialogProps) {
  const { t } = useI18n();
  if (!request) return null;
  const { okText, danger } = resolveConfirmButton(request.okText, request.danger);

  const handleOk = async () => {
    try {
      await request.onOk?.();
    } catch (e) {
      // Batch 5 wires real async IPC (window.emrg) which can reject on network
      // failure — swallow the rejection so the dialog still closes without an
      // unhandled promise rejection in the console (reviewer suggestion, #1009).
      console.error("[ConfirmDialog] onOk failed:", e);
    } finally {
      onDismiss?.();
    }
  };

  return (
    <Dialog
      open
      title={request.title}
      onClose={onDismiss}
      testId="confirm-dialog"
      actions={
        <>
          <button type="button" className="btn btn-ghost" data-testid="confirm-cancel" onClick={onDismiss}>
            {cancelText ?? t("settings.cancel")}
          </button>
          <button
            type="button"
            className={`btn ${danger ? "btn-danger" : "btn-primary"}`}
            data-testid="confirm-ok"
            onClick={handleOk}
          >
            {t(okText)}
          </button>
        </>
      }
    >
      <p className="confirm-message" style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)" }}>
        {request.message}
      </p>
    </Dialog>
  );
}
