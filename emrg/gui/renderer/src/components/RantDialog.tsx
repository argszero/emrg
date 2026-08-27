import { useEffect, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import type { ProjectRec } from "../lib/workspaceView";
import { Dialog } from "./Dialog";

/**
 * RantDialog — Rant 提交对话框（Batch 5 slice 8：工作区 Rant 面板"新建 Rant"）。
 * 镜像 vanilla `openRantForm()` / `submitRantForm()`（renderer/js/dialogs.js）：
 *
 * - 打开即清空消息、聚焦输入；
 * - 项目下拉 = 已注册项目（选填，value=name||path，与 daemon rant 协议 project 字段一致）；
 * - 消息必填（空 → 内联错误；vanilla 用 showConfirm 提示）；
 * - 提交 → onSubmit({message, project})（调用方接线 sendRant + 刷新列表 + 反馈）。
 */
export interface RantDialogProps {
  open: boolean;
  /** 项目下拉选项（仅已注册项目；选填） */
  projects?: ProjectRec[];
  onSubmit?: (payload: { message: string; project: string }) => void | Promise<void>;
  onDismiss?: () => void;
}

export function RantDialog({ open, projects = [], onSubmit, onDismiss }: RantDialogProps) {
  const { t } = useI18n();
  const [err, setErr] = useState<string | null>(null);
  const msgRef = useRef<HTMLTextAreaElement>(null);
  const projectRef = useRef<HTMLSelectElement>(null);

  useEffect(() => {
    if (!open) return;
    setErr(null);
    if (msgRef.current) msgRef.current.value = "";
    if (projectRef.current) projectRef.current.value = "";
    msgRef.current?.focus();
  }, [open]);

  const submit = async () => {
    const msg = msgRef.current?.value.trim() ?? "";
    if (!msg) {
      setErr(t("rants.messageRequired"));
      return;
    }
    try {
      await onSubmit?.({ message: msg, project: projectRef.current?.value ?? "" });
    } finally {
      onDismiss?.();
    }
  };

  if (!open) return null;
  return (
    <Dialog
      open
      title={t("rants.new")}
      onClose={onDismiss}
      testId="rant-dialog"
      actions={
        <>
          <button type="button" className="btn btn-ghost" data-testid="rant-cancel" onClick={onDismiss}>
            {t("settings.cancel")}
          </button>
          <button type="button" className="btn btn-primary" data-testid="rant-submit" onClick={() => void submit()}>
            {t("rants.submit")}
          </button>
        </>
      }
    >
      <div className="dialog-form">
        {err && (
          <div className="hint" style={{ color: "var(--danger, #c0392b)", marginBottom: 8 }} data-testid="rant-error">
            {err}
          </div>
        )}
        <label className="form-row">
          <span>{t("rants.message")}</span>
          <textarea ref={msgRef} data-testid="rant-message" rows={5} placeholder={t("rants.placeholder")} />
        </label>
        <label className="form-row">
          <span>{t("rants.project")}</span>
          <select ref={projectRef} data-testid="rant-project">
            <option value="">—</option>
            {projects.map((p) => {
              const v = p.name || p.path || "";
              return (
                <option key={v} value={v}>
                  {v}
                </option>
              );
            })}
          </select>
        </label>
      </div>
    </Dialog>
  );
}
