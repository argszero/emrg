import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { OpenSessionDialog } from "./OpenSessionDialog";
import { I18nProvider } from "../lib/i18n";
import type { ProjectRow, SessionRow } from "../lib/openSession";

/**
 * OpenSessionDialog.test.tsx — 两步打开会话对话框测试（Batch 4 slice 3）。
 * 镜像 vanilla showOpenSessionDialog/showProjectSessions：项目列表（含删除）→ 会话列表。
 */

const projects: ProjectRow[] = [
  { name: "emrg", path: "/x/emrg", latest_session_at: new Date().toISOString() },
  { name: "aitokenpool", path: "/x/aitokenpool" },
];

const sessions: SessionRow[] = [
  { session_id: "s1", title: "重构会话", updated_at: new Date().toISOString() },
  { session_id: "s2" },
];

function setup(props: Partial<Parameters<typeof OpenSessionDialog>[0]> = {}) {
  return render(
    <I18nProvider lang="zh">
      <OpenSessionDialog open step="projects" projects={projects} {...props} />
    </I18nProvider>,
  );
}

describe("OpenSessionDialog", () => {
  it("open=false → 不渲染", () => {
    render(
      <I18nProvider lang="zh">
        <OpenSessionDialog open={false} step="projects" />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("open-session-dialog")).toBeNull();
  });

  it("projects loading → dlg.loading 行", () => {
    setup({ projects: null });
    expect(screen.getByTestId("open-session-loading")).toHaveTextContent("加载中");
  });

  it("空项目列表 → noProjects", () => {
    setup({ projects: [] });
    expect(screen.getByTestId("open-session-empty")).toHaveTextContent("还没有项目");
  });

  it("error → loadFailed 行", () => {
    setup({ error: "boom" });
    expect(screen.getByTestId("open-session-error")).toHaveTextContent("加载失败：boom");
  });

  it("项目行：name/path/活跃 + 删除按钮；点击 → onPickProject", async () => {
    const onPickProject = vi.fn();
    setup({ onPickProject });
    const rows = screen.getAllByTestId("open-session-project");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("emrg");
    expect(rows[0]).toHaveTextContent("/x/emrg");
    await userEvent.click(screen.getAllByTestId("open-session-pick")[1]);
    expect(onPickProject).toHaveBeenCalledWith(projects[1]);
  });

  it("删除按钮 → onDeleteProject", async () => {
    const onDeleteProject = vi.fn();
    setup({ onDeleteProject });
    await userEvent.click(screen.getAllByTestId("open-session-delete")[0]);
    expect(onDeleteProject).toHaveBeenCalledWith(projects[0]);
  });

  it("sessions 步：标题插值 + 会话行（当前标记）→ onPickSession", async () => {
    const onPickSession = vi.fn();
    setup({ step: "sessions", sessions, projectName: "emrg", currentSid: "s1", onPickSession });
    expect(screen.getByText("打开会话 — emrg")).toBeInTheDocument();
    const rows = screen.getAllByTestId("open-session-session");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("重构会话");
    expect(rows[0]).toHaveTextContent("当前");
    expect(rows[1]).toHaveTextContent("s2");
    await userEvent.click(rows[1]);
    expect(onPickSession).toHaveBeenCalledWith(sessions[1]);
  });

  it("sessions 空 → noSessions", () => {
    setup({ step: "sessions", sessions: [] });
    expect(screen.getByTestId("open-session-empty")).toHaveTextContent("还没有会话");
  });

  it("底部：新建会话 + 新建项目按钮 → 各自回调", async () => {
    const onNewSession = vi.fn();
    const onNewProject = vi.fn();
    setup({ onNewSession, onNewProject });
    await userEvent.click(screen.getByTestId("open-session-new-session"));
    expect(onNewSession).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByTestId("open-session-new-project"));
    expect(onNewProject).toHaveBeenCalledTimes(1);
  });
});
