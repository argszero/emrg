/**
 * WorkspaceView.test.tsx — 工作区面板 React 组件测试（Batch 3）。
 * 镜像 vanilla dialogs.js 面板行为：视图切换、项目列表（auto_evolve 徽标）、
 * 项目会话子视图、任务列表（状态徽标/倒计时/meta）、Rant 列表（筛选 + 详情展开）。
 * 类名与 vanilla CSS 一致（Batch 5 复用）。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nProvider } from "../lib/i18n";
import { WorkspaceView, type WorkspaceViewProps } from "./WorkspaceView";
import type { ProjectRec, RantRec, TaskRec } from "../lib/workspaceView";

function setup(props: Partial<WorkspaceViewProps> = {}) {
  const utils = render(
    <I18nProvider lang="zh">
      <WorkspaceView activeView="projects" projects={[]} tasks={[]} rants={[]} {...props} />
    </I18nProvider>,
  );
  return { ...utils };
}

const project = (name: string, path?: string): ProjectRec => ({
  name,
  path: path || `/proj/${name}`,
});

const task = (name: string, over: Partial<TaskRec> = {}): TaskRec => ({
  name,
  type: "evolution",
  enabled: true,
  ...over,
});

const rant = (over: Partial<RantRec> = {}): RantRec => ({
  timestamp: "2026-08-26T12:16:48+08:00",
  project: "emrg",
  status: "pending",
  progress: null,
  message: "## 测试 rant\n正文",
  ...over,
});

describe("WorkspaceView", () => {
  it("面板切换：projects/tasks/rants/settings 各自渲染", async () => {
    const onSwitch = vi.fn();
    const { rerender } = setup({ onSwitch });
    expect(screen.getByTestId("panel-projects")).toBeInTheDocument();

    rerender(
      <I18nProvider lang="zh">
        <WorkspaceView activeView="tasks" onSwitch={onSwitch} />
      </I18nProvider>,
    );
    expect(screen.getByTestId("panel-tasks")).toBeInTheDocument();

    rerender(
      <I18nProvider lang="zh">
        <WorkspaceView activeView="rants" onSwitch={onSwitch} />
      </I18nProvider>,
    );
    expect(screen.getByTestId("panel-rants")).toBeInTheDocument();

    rerender(
      <I18nProvider lang="zh">
        <WorkspaceView activeView="settings" onSwitch={onSwitch} />
      </I18nProvider>,
    );
    expect(screen.getByTestId("panel-settings")).toBeInTheDocument();
  });

  it("项目列表：空态 → empty 提示；有数据 → 行 + auto_evolve 徽标", async () => {
    setup({ projects: [] });
    expect(screen.getByTestId("projects-empty")).toBeInTheDocument();

    const projects = [project("emrg"), project("aitokenpool", "/scm/aitokenpool")];
    const tasks = [{ name: "emrg-task", config: { project: "emrg" } }] as TaskRec[];
    const { rerender } = setup({ projects, tasks });
    expect(screen.getAllByTestId("project-row")).toHaveLength(2);
    // emrg 有演化任务 → ⚡ 徽标；aitokenpool 无
    const badges = screen.getAllByTestId("project-evolve-badge");
    expect(badges).toHaveLength(1);
    expect(badges[0]).toHaveTextContent("演化");

    rerender(
      <I18nProvider lang="zh">
        <WorkspaceView activeView="projects" projects={[project("emrg")]} tasks={tasks} />
      </I18nProvider>,
    );
  });

  it("项目查看会话 → 子视图；返回 → 回列表", async () => {
    const p = project("emrg");
    const onViewSessions = vi.fn();
    setup({ projects: [p], onViewProjectSessions: onViewSessions });
    await userEvent.click(screen.getByText("查看会话"));
    expect(onViewSessions).toHaveBeenCalledWith(p);
    expect(screen.getByTestId("project-sessions")).toBeInTheDocument();
    expect(screen.getByText("emrg 的会话")).toBeInTheDocument();
    // 返回
    await userEvent.click(screen.getByText("← 返回项目列表"));
    expect(screen.getByTestId("project-list")).toBeInTheDocument();
  });

  it("任务列表：状态徽标 + 倒计时 + meta + 操作回调", async () => {
    const onTrigger = vi.fn();
    const onEdit = vi.fn();
    const onDelete = vi.fn();
    const tasks = [
      task("daily-report", { running: true, started_at: 100 }),
      task("evolve", { next_run_in_seconds: 83 }),
      task("idle", {}),
    ];
    setup({ activeView: "tasks", tasks, onTriggerTask: onTrigger, onEditTask: onEdit, onDeleteTask: onDelete });
    expect(screen.getAllByTestId("task-row")).toHaveLength(3);
    const badges = screen.getAllByTestId("task-status-badge");
    expect(badges[0]).toHaveTextContent("运行中");
    expect(badges[1]).toHaveTextContent("待运行");
    expect(screen.getByText(/1m23s/)).toBeInTheDocument();
    // 触发：running 行按钮禁用，idle 行可点
    const triggerBtns = screen.getAllByTitle("触发");
    expect(triggerBtns[0]).toBeDisabled();
    await userEvent.click(triggerBtns[2]);
    expect(onTrigger).toHaveBeenCalledWith(tasks[2]);
    // 编辑/删除
    await userEvent.click(screen.getAllByTitle("编辑")[0]);
    expect(onEdit).toHaveBeenCalledWith(tasks[0]);
    await userEvent.click(screen.getAllByTitle("删除")[0]);
    expect(onDelete).toHaveBeenCalledWith(tasks[0]);
  });

  it("任务空态 → taskEmpty", () => {
    setup({ activeView: "tasks", tasks: [] });
    expect(screen.getByTestId("tasks-empty")).toBeInTheDocument();
  });

  it("Rant 列表：筛选 tab + 行渲染 + 详情展开/收起", async () => {
    const rants = [
      rant({ status: "pending", message: "## 待处理\n正文" }),
      rant({ timestamp: "2026-08-26T11:00:00+08:00", status: "completed", progress: "done", message: "【完成】已处理" }),
    ];
    setup({ activeView: "rants", rants });
    expect(screen.getAllByTestId("rant-row")).toHaveLength(2);
    // 状态徽标三态
    const badges = screen.getAllByTestId("rant-status-badge");
    expect(badges[0]).toHaveTextContent("待处理");
    expect(badges[1]).toHaveTextContent("已完成");
    // 内容列摘要：md 标题去除
    expect(screen.getByText("待处理", { selector: ".rant-col-content" })).toBeInTheDocument();
    // 筛选：点"已完成" → 只剩 1 行
    await userEvent.click(screen.getByRole("button", { name: "已完成" }));
    expect(screen.getAllByTestId("rant-row")).toHaveLength(1);
    expect(screen.getByText("【完成】已处理")).toBeInTheDocument();
    // 点"全部"回 2 行
    await userEvent.click(screen.getByRole("button", { name: "全部" }));
    expect(screen.getAllByTestId("rant-row")).toHaveLength(2);
    // 详情展开（【】→#### 预处理）再点收起
    await userEvent.click(screen.getAllByTestId("rant-row")[0]);
    expect(document.querySelector(".rant-detail")).not.toBeNull();
    expect(document.querySelector(".rant-md")).toHaveTextContent("正文");
    await userEvent.click(screen.getAllByTestId("rant-row")[0]);
    expect(document.querySelector(".rant-detail")).toBeNull();
  });

  it("Rant 空态：filter 空 → empty；filter 有值 → emptyFiltered", async () => {
    setup({ activeView: "rants", rants: [] });
    expect(screen.getByTestId("rants-empty")).toHaveTextContent("暂无 Rant");
    await userEvent.click(screen.getByText("待处理"));
    expect(screen.getByTestId("rants-empty")).toHaveTextContent("该状态下暂无 Rant");
  });
});
