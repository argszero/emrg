import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
import { TaskFormDialog, type TaskFormPayload } from "./TaskFormDialog";
import type { ProjectRec, ScheduleWindow, TaskRec } from "../lib/workspaceView";

/**
 * TaskFormDialog.test.tsx — Batch 5 slice 8：任务表单（新建/编辑）。
 * 镜像 vanilla openTaskForm/saveTaskForm 语义：预填、name 编辑只读、间隔下限校验、
 * 提交 payload 形状（name/type/project/interval/enabled/repo/sandbox/schedule）。
 *
 * issue #2490：下限不再写死 —— 每处断言都传一个**不是 60** 的 `minInterval`，
 * 于是组件里若有人再藏一份 `60`，下面「低于下限被拒 / 等于下限被收」两条必然红。
 */

function wrapper(node: React.ReactNode) {
  return <I18nProvider lang="en">{node}</I18nProvider>;
}

const TYPES = ["evolution", "journal"];
const PROJECTS: ProjectRec[] = [
  { name: "emrg", path: "/p/emrg" },
  { name: "sci", path: "/p/sci" },
];

/** 下限只从 daemon 上报值来（issue #2490）——刻意不是渲染层曾写死的 60。 */
const FLOOR = 120;

const WINDOWS: ScheduleWindow[] = [
  { days: "mon-fri", from: "09:00", to: "18:00", interval: 900 },
  { from: "20:00", to: "22:00", interval: 1800 },
];

function taskWith(overrides: Partial<TaskRec> = {}): TaskRec {
  return {
    name: "windowed",
    type: "evolution",
    interval: 10800,
    enabled: true,
    config: { agent: "emrg" },
    ...overrides,
  };
}

function renderForm(
  task: TaskRec | null,
  props: Partial<React.ComponentProps<typeof TaskFormDialog>> = {},
) {
  const onSubmit = (props.onSubmit ?? vi.fn().mockResolvedValue(undefined)) as Mock;
  render(
    wrapper(
      <TaskFormDialog
        request={{ task }}
        types={TYPES}
        projects={PROJECTS}
        minInterval={FLOOR}
        onDismiss={vi.fn()}
        {...props}
        onSubmit={onSubmit}
      />,
    ),
  );
  return { onSubmit };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("TaskFormDialog", () => {
  it("新建：渲染表单字段 + 默认值（interval 1800, enabled, sandbox workspace-write）", () => {
    render(
      wrapper(
        <TaskFormDialog
          request={{ task: null }}
          types={TYPES}
          projects={PROJECTS}
          minInterval={FLOOR}
          onDismiss={vi.fn()}
        />,
      ),
    );
    expect(screen.getByTestId("task-form-dialog")).toBeInTheDocument();
    expect((screen.getByTestId("task-form-name") as HTMLInputElement).value).toBe("");
    expect((screen.getByTestId("task-form-name") as HTMLInputElement).disabled).toBe(false);
    expect((screen.getByTestId("task-form-type") as HTMLSelectElement).value).toBe("evolution");
    expect((screen.getByTestId("task-form-project") as HTMLSelectElement).value).toBe("emrg");
    expect((screen.getByTestId("task-form-interval") as HTMLInputElement).value).toBe("1800");
    expect((screen.getByTestId("task-form-enabled") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId("task-form-sandbox") as HTMLSelectElement).value).toBe("workspace-write");
  });

  it("编辑：预填任务字段，name 只读不可改名", () => {
    const task = taskWith({ type: "journal", interval: 3600, enabled: false, config: { agent: "sci" } });
    (task as TaskRec & { sandbox?: string }).sandbox = "read-only";
    render(
      wrapper(
        <TaskFormDialog
          request={{ task }}
          types={TYPES}
          projects={PROJECTS}
          minInterval={FLOOR}
          onDismiss={vi.fn()}
        />,
      ),
    );
    const name = screen.getByTestId("task-form-name") as HTMLInputElement;
    expect(name.value).toBe("windowed");
    expect(name.disabled).toBe(true);
    expect((screen.getByTestId("task-form-type") as HTMLSelectElement).value).toBe("journal");
    expect((screen.getByTestId("task-form-project") as HTMLSelectElement).value).toBe("sci");
    expect((screen.getByTestId("task-form-interval") as HTMLInputElement).value).toBe("3600");
    expect((screen.getByTestId("task-form-enabled") as HTMLInputElement).checked).toBe(false);
    expect((screen.getByTestId("task-form-sandbox") as HTMLSelectElement).value).toBe("read-only");
  });

  it("空名 → 内联错误，不提交", () => {
    const { onSubmit } = renderForm(null);
    fireEvent.click(screen.getByTestId("task-form-save"));
    expect(screen.getByTestId("task-form-error")).toHaveTextContent("required");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("间隔低于 daemon 上报的下限 → 内联错误，不提交（下限不是 60）", () => {
    const { onSubmit } = renderForm(null);
    fireEvent.change(screen.getByTestId("task-form-name"), { target: { value: "t1" } });
    fireEvent.change(screen.getByTestId("task-form-interval"), { target: { value: String(FLOOR - 1) } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    // 报错文案带的是上报值本身 —— 组件里若另有一份 60，这条会读到 60。
    expect(screen.getByTestId("task-form-error")).toHaveTextContent(`${FLOOR}`);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("间隔等于下限 → 接受（同一次上报既定义拒绝的一侧，也定义接受的一侧）", async () => {
    const { onSubmit } = renderForm(null);
    fireEvent.change(screen.getByTestId("task-form-name"), { target: { value: "t1" } });
    fireEvent.change(screen.getByTestId("task-form-interval"), { target: { value: String(FLOOR) } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect((onSubmit.mock.calls[0][0] as TaskFormPayload).interval).toBe(FLOOR);
  });

  it("标签渲染的是上报值，不是写死的 60", () => {
    renderForm(null);
    const label = screen.getByTestId("task-form-interval").closest("label");
    expect(label).toHaveTextContent(String(FLOOR));
    expect(label).not.toHaveTextContent("60");
  });

  it("提交 payload 形状正确（含 sandbox，无窗口时不带 schedule 键）", async () => {
    const { onSubmit } = renderForm(null);
    fireEvent.change(screen.getByTestId("task-form-name"), { target: { value: "t1" } });
    fireEvent.change(screen.getByTestId("task-form-type"), { target: { value: "journal" } });
    fireEvent.change(screen.getByTestId("task-form-project"), { target: { value: "sci" } });
    fireEvent.change(screen.getByTestId("task-form-interval"), { target: { value: "300" } });
    fireEvent.change(screen.getByTestId("task-form-sandbox"), { target: { value: "read-only" } });
    fireEvent.click(screen.getByTestId("task-form-enabled"));
    fireEvent.click(screen.getByTestId("task-form-save"));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const payload: TaskFormPayload = onSubmit.mock.calls[0][0];
    expect(payload).toEqual({
      name: "t1",
      type: "journal",
      agent: "sci",
      interval: 300,
      enabled: false,
      repo: undefined,
      sandbox: "read-only",
    });
    // 键本身不该出现：daemon 的 update 以「键存在」决定是否动该字段。
    expect("schedule" in payload).toBe(false);
  });

  it("编辑带窗口的任务：每个窗口一行，四个字段逐字预填", () => {
    renderForm(taskWith({ schedule: WINDOWS }));
    expect(screen.queryByTestId("task-form-windows-empty")).not.toBeInTheDocument();
    for (const [i, w] of WINDOWS.entries()) {
      expect((screen.getByTestId(`task-form-window-${i}-days`) as HTMLInputElement).value).toBe(
        w.days ?? "",
      );
      expect((screen.getByTestId(`task-form-window-${i}-from`) as HTMLInputElement).value).toBe(w.from);
      expect((screen.getByTestId(`task-form-window-${i}-to`) as HTMLInputElement).value).toBe(w.to);
      expect((screen.getByTestId(`task-form-window-${i}-interval`) as HTMLInputElement).value).toBe(
        String(w.interval),
      );
    }
    expect(screen.queryByTestId(`task-form-window-${WINDOWS.length}`)).not.toBeInTheDocument();
  });

  it("无窗口的任务：显示空提示，没有行", () => {
    renderForm(taskWith());
    expect(screen.getByTestId("task-form-windows-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("task-form-window-0")).not.toBeInTheDocument();
  });

  it("改一个窗口的字段 → 提交里就是改后的值（未改的窗口原样保留）", async () => {
    const { onSubmit } = renderForm(taskWith({ schedule: WINDOWS }));
    fireEvent.change(screen.getByTestId("task-form-window-0-from"), { target: { value: "08:30" } });
    fireEvent.change(screen.getByTestId("task-form-window-0-interval"), { target: { value: "600" } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const payload = onSubmit.mock.calls[0][0] as TaskFormPayload;
    expect(payload.schedule).toEqual([
      { days: "mon-fri", from: "08:30", to: "18:00", interval: 600 },
      { from: "20:00", to: "22:00", interval: 1800 },
    ]);
  });

  it("添加一个窗口 → 提交里多一条；days 留空时不带 days 键", async () => {
    const { onSubmit } = renderForm(taskWith({ schedule: WINDOWS }));
    fireEvent.click(screen.getByTestId("task-form-window-add"));
    fireEvent.change(screen.getByTestId("task-form-window-2-from"), { target: { value: "23:00" } });
    fireEvent.change(screen.getByTestId("task-form-window-2-to"), { target: { value: "23:30" } });
    fireEvent.change(screen.getByTestId("task-form-window-2-interval"), { target: { value: "600" } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const payload = onSubmit.mock.calls[0][0] as TaskFormPayload;
    expect(payload.schedule).toHaveLength(3);
    expect(payload.schedule?.[2]).toEqual({ from: "23:00", to: "23:30", interval: 600 });
    expect("days" in (payload.schedule?.[2] ?? {})).toBe(false);
  });

  it("删掉一个窗口 → 提交里少一条", async () => {
    const { onSubmit } = renderForm(taskWith({ schedule: WINDOWS }));
    fireEvent.click(screen.getByTestId("task-form-window-0-remove"));
    fireEvent.click(screen.getByTestId("task-form-save"));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const payload = onSubmit.mock.calls[0][0] as TaskFormPayload;
    expect(payload.schedule).toEqual([{ from: "20:00", to: "22:00", interval: 1800 }]);
  });

  it("原本有窗口而全删 → schedule: null（daemon 语义＝全天单一频次）", async () => {
    const { onSubmit } = renderForm(taskWith({ schedule: WINDOWS }));
    fireEvent.click(screen.getByTestId("task-form-window-0-remove"));
    fireEvent.click(screen.getByTestId("task-form-window-0-remove"));
    fireEvent.click(screen.getByTestId("task-form-save"));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const payload = onSubmit.mock.calls[0][0] as TaskFormPayload;
    expect("schedule" in payload).toBe(true);
    expect(payload.schedule).toBeNull();
  });

  it("只点「添加」不填的空行被忽略，不产生窗口", async () => {
    const { onSubmit } = renderForm(null);
    fireEvent.change(screen.getByTestId("task-form-name"), { target: { value: "t1" } });
    fireEvent.click(screen.getByTestId("task-form-window-add"));
    fireEvent.click(screen.getByTestId("task-form-save"));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const payload = onSubmit.mock.calls[0][0] as TaskFormPayload;
    expect("schedule" in payload).toBe(false);
  });

  it("窗口填一半 → 内联错误，不提交", () => {
    const { onSubmit } = renderForm(null);
    fireEvent.change(screen.getByTestId("task-form-name"), { target: { value: "t1" } });
    fireEvent.click(screen.getByTestId("task-form-window-add"));
    fireEvent.change(screen.getByTestId("task-form-window-0-from"), { target: { value: "09:00" } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    expect(screen.getByTestId("task-form-error")).toHaveTextContent("HH:MM");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("窗口间隔低于上报下限 → 内联错误；等于下限 → 接受", async () => {
    const { onSubmit } = renderForm(null);
    fireEvent.change(screen.getByTestId("task-form-name"), { target: { value: "t1" } });
    fireEvent.click(screen.getByTestId("task-form-window-add"));
    fireEvent.change(screen.getByTestId("task-form-window-0-from"), { target: { value: "09:00" } });
    fireEvent.change(screen.getByTestId("task-form-window-0-to"), { target: { value: "18:00" } });
    fireEvent.change(screen.getByTestId("task-form-window-0-interval"), {
      target: { value: String(FLOOR - 1) },
    });
    fireEvent.click(screen.getByTestId("task-form-save"));
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.change(screen.getByTestId("task-form-window-0-interval"), { target: { value: String(FLOOR) } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const payload = onSubmit.mock.calls[0][0] as TaskFormPayload;
    expect(payload.schedule).toEqual([{ from: "09:00", to: "18:00", interval: FLOOR }]);
  });

  it("未拿到下限（undefined）→ 不做客户端预检，把判断留给 daemon", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      wrapper(
        <TaskFormDialog
          request={{ task: null }}
          types={TYPES}
          projects={PROJECTS}
          onSubmit={onSubmit}
          onDismiss={vi.fn()}
        />,
      ),
    );
    fireEvent.change(screen.getByTestId("task-form-name"), { target: { value: "t1" } });
    fireEvent.change(screen.getByTestId("task-form-interval"), { target: { value: "1" } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect((onSubmit.mock.calls[0][0] as TaskFormPayload).interval).toBe(1);
  });

  it("closed (request=null) → 不渲染", () => {
    render(wrapper(<TaskFormDialog request={null} onDismiss={vi.fn()} />));
    expect(screen.queryByTestId("task-form-dialog")).not.toBeInTheDocument();
  });
});
