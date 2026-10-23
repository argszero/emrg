import { useEffect, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import type { ProjectRec, ScheduleWindow, TaskRec } from "../lib/workspaceView";
import { Dialog } from "./Dialog";

/**
 * TaskFormDialog — 任务创建/编辑表单（Batch 5 slice 8：工作区任务面板表单）。
 * 镜像 vanilla `openTaskForm(task)` / `saveTaskForm()`（renderer/js/dialogs.js）：
 *
 * - 编辑时 name 只读（决策点：daemon 以 name 定位任务 → 不可改名）；
 * - 类型下拉 = 内置 + 自定义类型（taskTemplateList，调用方注入）；项目下拉 = 仅已注册项目；
 * - 间隔默认 1800s；**下限由 daemon 上报**（`minInterval`，见 issue #2490），本组件不写死数值；
 * - sandbox 下拉 workspace-write / read-only / danger-full-access（vanilla 同款）；
 * - 分时段频次窗口（`schedule`，issue #2490）：编辑时逐窗口预填，可增行、改行、删行；
 *   提交时全空行忽略；原本有窗口而全删 → `schedule: null`（daemon 语义＝全天单一频次）；
 *   从未有过窗口且没填 → 不带 `schedule` 键（保持旧行为，daemon 不动该字段）；
 * - 保存 → onSubmit(payload)（调用方接线 taskCreate/taskUpdate + 刷新 + 反馈）。
 */
export interface TaskFormPayload {
  name: string;
  type: string;
  agent: string;
  interval: number;
  enabled: boolean;
  repo?: string;
  sandbox?: string;
  /** 窗口列表；`null` = 显式清空；`undefined` = 不声明（无该键） */
  schedule?: ScheduleWindow[] | null;
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
  /**
   * 间隔下限，由 daemon 上报（`tasks_list` 帧的 `min_interval`）。
   * 缺省（未拿到）时不做客户端预检，交给 daemon 权威校验 —— 这样渲染层
   * 永远没有第二份下限数字（issue #2490）。
   */
  minInterval?: number;
  onSubmit?: (payload: TaskFormPayload) => void | Promise<void>;
  onDismiss?: () => void;
}

const SANDBOXES = ["workspace-write", "read-only", "danger-full-access"];

/** 表单里一行窗口的编辑态：输入框原样字符串，提交时才成形。 */
interface WindowRow {
  days: string;
  from: string;
  to: string;
  interval: string;
}

const EMPTY_ROW: WindowRow = { days: "", from: "", to: "", interval: "" };

function toRow(w: ScheduleWindow): WindowRow {
  const raw = w as ScheduleWindow & { days?: unknown; interval?: unknown };
  return {
    days: typeof raw.days === "string" ? raw.days : "",
    from: String(raw.from ?? ""),
    to: String(raw.to ?? ""),
    interval: raw.interval === undefined || raw.interval === null ? "" : String(raw.interval),
  };
}

/** 记录里的窗口（容忍字段缺失或形状不对：读不出就当作没有）。 */
function windowsOf(task: TaskRec | null): ScheduleWindow[] {
  const s = task?.schedule;
  return Array.isArray(s) ? s : [];
}

export function TaskFormDialog({
  request,
  types = [],
  projects = [],
  minInterval,
  onSubmit,
  onDismiss,
}: TaskFormDialogProps) {
  const { t } = useI18n();
  const open = request !== null;
  const task = request?.task ?? null;
  const editing = Boolean(task?.name);
  const [err, setErr] = useState<string | null>(null);
  const [rows, setRows] = useState<WindowRow[]>([]);
  /** 打开时该任务本就带窗口 —— 决定「全删」是清空（null）还是「本来就没有」（不声明） */
  const [hadWindows, setHadWindows] = useState(false);

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
    const curProj = projects.some((p) => (p.name || p.path || "") === task?.config?.agent)
      ? (task?.config?.agent as string)
      : (projects[0]?.name || projects[0]?.path || "");
    if (projectRef.current) projectRef.current.value = curProj;
    if (intervalRef.current) intervalRef.current.value = String(task?.interval ?? 1800);
    if (enabledRef.current) enabledRef.current.checked = task ? task.enabled !== false : true;
    if (repoRef.current) repoRef.current.value = String(cfg.repo || "");
    if (sandboxRef.current) sandboxRef.current.value = (task as TaskRec & { sandbox?: string })?.sandbox || "workspace-write";
    const existing = windowsOf(task);
    setRows(existing.map(toRow));
    setHadWindows(existing.length > 0);
    nameRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, task, types.length, projects.length]);

  const patchRow = (index: number, field: keyof WindowRow, value: string) => {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, [field]: value } : row)));
  };
  const addRow = () => setRows((prev) => [...prev, { ...EMPTY_ROW }]);
  const removeRow = (index: number) => setRows((prev) => prev.filter((_, i) => i !== index));

  /** 从编辑行成形为 daemon 的窗口；有内容但不完整 → 报错返回 null。 */
  const buildWindows = (): ScheduleWindow[] | null => {
    const out: ScheduleWindow[] = [];
    for (const row of rows) {
      const days = row.days.trim();
      const from = row.from.trim();
      const to = row.to.trim();
      const rawInterval = row.interval.trim();
      // 全空行 = 点了「添加」但没填，忽略
      if (!days && !from && !to && !rawInterval) continue;
      if (!from || !to || !rawInterval) {
        setErr(t("settings.taskScheduleInvalid"));
        return null;
      }
      const n = parseInt(rawInterval, 10);
      if (!Number.isFinite(n) || (minInterval !== undefined && n < minInterval)) {
        setErr(t("settings.taskScheduleInvalid"));
        return null;
      }
      out.push(days ? { days, from, to, interval: n } : { from, to, interval: n });
    }
    return out;
  };

  const submit = async () => {
    if (!nameRef.current || !typeRef.current || !projectRef.current || !intervalRef.current) return;
    const name = nameRef.current.value.trim();
    if (!name) {
      setErr(t("settings.taskNameRequired"));
      return;
    }
    const interval = parseInt(intervalRef.current.value, 10);
    if (!Number.isFinite(interval) || (minInterval !== undefined && interval < minInterval)) {
      setErr(t("settings.taskIntervalInvalid", { n: minInterval ?? "" }));
      return;
    }
    const windows = buildWindows();
    if (windows === null) return;
    const payload: TaskFormPayload = {
      name,
      type: typeRef.current.value,
      agent: projectRef.current.value,
      interval,
      enabled: enabledRef.current?.checked ?? true,
      repo: repoRef.current?.value.trim() || undefined,
      sandbox: sandboxRef.current?.value || "workspace-write",
    };
    // 键的存在本身就是语义（daemon 的 update 以「键存在」决定是否动该字段）：
    // 有窗口才写；原本有而全删 → null（显式清空）；从未有过 → 不写这个键。
    if (windows.length) payload.schedule = windows;
    else if (hadWindows) payload.schedule = null;
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
          <span>
            {minInterval === undefined
              ? t("settings.taskIntervalAny")
              : t("settings.taskInterval", { n: minInterval })}
          </span>
          <input ref={intervalRef} data-testid="task-form-interval" type="number" min={minInterval} />
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
        <div className="form-row" data-testid="task-form-windows">
          <span>{t("settings.taskSchedule")}</span>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {rows.length === 0 && (
              <div className="hint" data-testid="task-form-windows-empty">
                {t("settings.taskScheduleEmpty")}
              </div>
            )}
            {rows.map((row, i) => (
              <div
                key={i}
                data-testid={`task-form-window-${i}`}
                style={{ display: "flex", gap: 6, alignItems: "center" }}
              >
                <input
                  data-testid={`task-form-window-${i}-days`}
                  type="text"
                  placeholder={t("settings.taskScheduleDays")}
                  value={row.days}
                  onChange={(e) => patchRow(i, "days", e.target.value)}
                  style={{ width: 90 }}
                />
                <input
                  data-testid={`task-form-window-${i}-from`}
                  type="text"
                  placeholder="09:00"
                  value={row.from}
                  onChange={(e) => patchRow(i, "from", e.target.value)}
                  style={{ width: 70 }}
                />
                <input
                  data-testid={`task-form-window-${i}-to`}
                  type="text"
                  placeholder="18:00"
                  value={row.to}
                  onChange={(e) => patchRow(i, "to", e.target.value)}
                  style={{ width: 70 }}
                />
                <input
                  data-testid={`task-form-window-${i}-interval`}
                  type="number"
                  min={minInterval}
                  placeholder={t("settings.taskIntervalShort")}
                  value={row.interval}
                  onChange={(e) => patchRow(i, "interval", e.target.value)}
                  style={{ width: 90 }}
                />
                <button
                  type="button"
                  className="btn btn-ghost"
                  data-testid={`task-form-window-${i}-remove`}
                  onClick={() => removeRow(i)}
                >
                  {t("settings.taskScheduleRemove")}
                </button>
              </div>
            ))}
            <div>
              <button type="button" className="btn btn-ghost" data-testid="task-form-window-add" onClick={addRow}>
                {t("settings.taskScheduleAdd")}
              </button>
            </div>
            <div className="hint">{t("settings.taskScheduleHint")}</div>
          </div>
        </div>
      </div>
    </Dialog>
  );
}
