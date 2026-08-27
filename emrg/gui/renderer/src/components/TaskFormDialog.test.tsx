import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
import { TaskFormDialog, type TaskFormPayload } from "./TaskFormDialog";
import type { ProjectRec, TaskRec } from "../lib/workspaceView";

/**
 * TaskFormDialog.test.tsx — Batch 5 slice 8：任务表单（新建/编辑）。
 * 镜像 vanilla openTaskForm/saveTaskForm 语义：预填、name 编辑只读、间隔 ≥60 校验、
 * 提交 payload 形状（name/type/project/interval/enabled/repo/sandbox）。
 */

function wrapper(node: React.ReactNode) {
  return <I18nProvider lang="en">{node}</I18nProvider>;
}

const TYPES = ["evolution", "journal"];
const PROJECTS: ProjectRec[] = [
  { name: "emrg", path: "/p/emrg" },
  { name: "sci", path: "/p/sci" },
];

afterEach(() => {
  vi.restoreAllMocks();
});

describe("TaskFormDialog", () => {
  it("新建：渲染表单字段 + 默认值（interval 1800, enabled, sandbox workspace-write）", () => {
    render(
      wrapper(
        <TaskFormDialog request={{ task: null }} types={TYPES} projects={PROJECTS} onDismiss={vi.fn()} />,
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
    const task: TaskRec = {
      name: "evo",
      type: "journal",
      interval: 3600,
      enabled: false,
      config: { project: "sci" },
    } as TaskRec & { sandbox?: string };
    (task as TaskRec & { sandbox?: string }).sandbox = "read-only";
    render(wrapper(<TaskFormDialog request={{ task }} types={TYPES} projects={PROJECTS} onDismiss={vi.fn()} />));
    const name = screen.getByTestId("task-form-name") as HTMLInputElement;
    expect(name.value).toBe("evo");
    expect(name.disabled).toBe(true);
    expect((screen.getByTestId("task-form-type") as HTMLSelectElement).value).toBe("journal");
    expect((screen.getByTestId("task-form-project") as HTMLSelectElement).value).toBe("sci");
    expect((screen.getByTestId("task-form-interval") as HTMLInputElement).value).toBe("3600");
    expect((screen.getByTestId("task-form-enabled") as HTMLInputElement).checked).toBe(false);
    expect((screen.getByTestId("task-form-sandbox") as HTMLSelectElement).value).toBe("read-only");
  });

  it("空名 → 内联错误，不提交", () => {
    const onSubmit = vi.fn();
    render(wrapper(<TaskFormDialog request={{ task: null }} types={TYPES} projects={PROJECTS} onSubmit={onSubmit} onDismiss={vi.fn()} />));
    fireEvent.click(screen.getByTestId("task-form-save"));
    expect(screen.getByTestId("task-form-error")).toHaveTextContent("required");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("间隔 < 60 → 内联错误，不提交", () => {
    const onSubmit = vi.fn();
    render(wrapper(<TaskFormDialog request={{ task: null }} types={TYPES} projects={PROJECTS} onSubmit={onSubmit} onDismiss={vi.fn()} />));
    fireEvent.change(screen.getByTestId("task-form-name"), { target: { value: "t1" } });
    fireEvent.change(screen.getByTestId("task-form-interval"), { target: { value: "30" } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    expect(screen.getByTestId("task-form-error")).toHaveTextContent("Interval");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("提交 payload 形状正确（含 sandbox）", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(wrapper(<TaskFormDialog request={{ task: null }} types={TYPES} projects={PROJECTS} onSubmit={onSubmit} onDismiss={vi.fn()} />));
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
      project: "sci",
      interval: 300,
      enabled: false,
      repo: undefined,
      sandbox: "read-only",
    });
  });

  it("closed (request=null) → 不渲染", () => {
    render(wrapper(<TaskFormDialog request={null} onDismiss={vi.fn()} />));
    expect(screen.queryByTestId("task-form-dialog")).not.toBeInTheDocument();
  });
});
