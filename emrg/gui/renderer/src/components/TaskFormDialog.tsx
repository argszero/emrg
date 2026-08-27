import { useEffect, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import type { ProjectRec, TaskRec } from "../lib/workspaceView";
import { Dialog } from "./Dialog";

/**
 * TaskFormDialog — 任务创建/编辑表单（Batch 5 slice 8：工作区任务面板表单）。
 * 镜像 vanilla `openTaskForm(task)` / `saveTaskForm()`（renderer/js/dialogs.js）：
 *
 * - 编辑时 name 只读（决策点：daemon 以 name 定位任务 → 不可改名）；
 * - 类型下拉 = 内置 + 自定义类型（taskTemplateList，调用方注入）；项目下拉 = 仅已注册项目；
 * - 间隔默认 1800s，客户端校验 ≥60（决策点⑤，daemon 兜底）；
 * - sandbox 下拉 workspace-write / read-only / danger-full-access（vanilla 同款）；
 * - 保存 → onSubmit(payload)（调用方接线 taskCreate/taskUpdate + 刷新 + 反馈）。
 */
export interface TaskFormPayload {
  name: string;
  type: string;
  project: string;
  interval: number;
  enabled: boolean;
  repo?: string;
  sandbox?: string;
}

export interface TaskFormRequest {
  /** null = 新建；TaskRec = 编辑（name 只读） */
  task: TaskRec | null;
}

export interface TaskFormDialogProps {
  request: TaskFormRequest | null;
  /** 类型下拉选项（内置 + 自定义，由调用方经 taskTemplateList 加载） */
  types?: string[];
  /** 项目下拉选项（仅已注册项目） */
  projects?: ProjectRec[];
  onSubmit?: (payload: TaskFormPayload) => void | Promise<void>;
  onDismiss?: () => void;
}

const SANDBOXES = ["workspace-write", "read-only", "danger-full-access"];

export function TaskFormDialog({
  request,
  types = [],
  projects = [],
  onSubmit,
  onDismiss,
}: TaskFormDialogProps) {
  const { t } = useI18n();
  const open = request !== null;
  const task = request?.task ?? null;
  const editing = Boolean(task?.name);
  const [err, setErr] = useState<string | null>(null);

  const nameRef = useRef<HTMLInputElement>(null);
  const typeRef = useRef<HTMLSelectElement>(null);
  const projectRef = useRef<HTMLSelectElement>(null);
  const intervalRef = useRef<HTMLInputElement>(null);
  const enabledRef = useRef<HTMLInputElement>(null);
  const repoRef = useRef<HTMLInputElement>(null);
  const sandboxRef = useRef<HTMLSelectElement>(null);

  // 打开即预填（vanilla openTaskForm 语义；编辑 → name 只读）
  useEffect(() => {
    if (!open) return;
    setErr(null);
    const cfg = task?.config && typeof task.config === "object" ? task.config : {};
    if (nameRef.current) {
      nameRef.current.value = task?.name ?? "";
      nameRef.current.disabled = editing;
    }
    const curType =
      (task?.type && types.includes(task.type) ? task.type : types[0]) || "evolution";
    if (typeRef.current) typeRef.current.value = curType;
    const curProj = projects.some((p) => (p.name || p.path || "") === task?.config?.project)
      ? (task?.config?.project as string)
      : (projects[0]?.name || projects[0]?.path || "");
    if (projectRef.current) projectRef.current.value = curProj;
    if (intervalRef.current) intervalRef.current.value = String(task?.interval ?? 1800);
    if (enabledRef.current) enabledRef.current.checked = task ? task.enabled !== false : true;
    if (repoRef.current) repoRef.current.value = String(cfg.repo || "");
    if (sandboxRef.current) sandboxRef.current.value = (task as TaskRec & { sandbox?: string })?.sandbox || "workspace-write";
    nameRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, task, types.length, projects.length]);

  const submit = async () => {
    if (!nameRef.current || !typeRef.current || !projectRef.current || !intervalRef.current) return;
    const name = nameRef.current.value.trim();
    if (!name) {
      setErr(t("settings.taskNameRequired"));
      return;
    }
    const interval = parseInt(intervalRef.current.value, 10);
    if (!Number.isFinite(interval) || interval < 60) {
      setErr(t("settings.taskIntervalInvalid"));
      return;
    }
    const payload: TaskFormPayload = {
      name,
      type: typeRef.current.value,
      project: projectRef.current.value,
      interval,
      enabled: enabledRef.current?.checked ?? true,
      repo: repoRef.current?.value.trim() || undefined,
      sandbox: sandboxRef.current?.value || "workspace-write",
    };
    try {
      await onSubmit?.(payload);
    } finally {
      onDismiss?.();
    }
  };

  if (!open) return null;
  return (
    <Dialog
      open
      title={editing ? t("settings.taskEdit") : t("settings.taskAdd")}
      onClose={onDismiss}
      testId="task-form-dialog"
      actions={
        <>
          <button type="button" className="btn btn-ghost" data-testid="task-form-cancel" onClick={onDismiss}>
            {t("settings.cancel")}
          </button>
          <button type="button" className="btn btn-primary" data-testid="task-form-save" onClick={() => void submit()}>
            {t("settings.taskSave")}
          </button>
        </>
      }
    >
      <div className="dialog-form">
        {err && (
          <div className="hint" style={{ color: "var(--danger, #c0392b)", marginBottom: 8 }} data-testid="task-form-error">
            {err}
          </div>
        )}
        <label className="form-row">
          <span>{t("settings.taskName")}</span>
          <input ref={nameRef} data-testid="task-form-name" type="text" />
        </label>
        <label className="form-row">
          <span>{t("settings.taskType")}</span>
          <select ref={typeRef} data-testid="task-form-type">
            {types.length === 0 && <option value="evolution">evolution</option>}
            {types.map((tp) => (
              <option key={tp} value={tp}>
                {tp}
              </option>
            ))}
          </select>
        </label>
        <label className="form-row">
          <span>{t("settings.taskProject")}</span>
          <select ref={projectRef} data-testid="task-form-project">
            {projects.length === 0 && <option value="">—</option>}
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
        <label className="form-row">
          <span>{t("settings.taskInterval")}</span>
          <input ref={intervalRef} data-testid="task-form-interval" type="number" min={60} />
        </label>
        <label className="form-row">
          <span>{t("settings.taskSandbox")}</span>
          <select ref={sandboxRef} data-testid="task-form-sandbox">
            {SANDBOXES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="form-row">
          <span>{t("settings.taskRepo")}</span>
          <input ref={repoRef} data-testid="task-form-repo" type="text" placeholder="owner/repo" />
        </label>
        <label className="form-row" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input ref={enabledRef} data-testid="task-form-enabled" type="checkbox" />
          <span>{t("settings.taskEnabled")}</span>
        </label>
      </div>
    </Dialog>
  );
}
