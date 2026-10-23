import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NewSessionDialog } from "./NewSessionDialog";
import { I18nProvider } from "../lib/i18n";
import type { AgentRow } from "../lib/openSession";

/**
 * NewSessionDialog.test.tsx — 新建会话对话框测试（Batch 4 slice 3）。
 * 镜像 vanilla showNewSessionDialog：loading/空/error/列表 + 新建项目按钮。
 */

const agents: AgentRow[] = [
  { name: "emrg", path: "/x/emrg", latest_session_at: new Date().toISOString() },
];

function setup(props: Partial<Parameters<typeof NewSessionDialog>[0]> = {}) {
  return render(
    <I18nProvider lang="zh">
      <NewSessionDialog open agents={agents} {...props} />
    </I18nProvider>,
  );
}

describe("NewSessionDialog", () => {
  it("open=false → 不渲染", () => {
    render(
      <I18nProvider lang="zh">
        <NewSessionDialog open={false} />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("new-session-dialog")).toBeNull();
  });

  it("loading → dlg.loading 行", () => {
    setup({ agents: null });
    expect(screen.getByTestId("new-session-loading")).toHaveTextContent("加载中");
  });

  it("空项目列表 → noAgents", () => {
    setup({ agents: [] });
    expect(screen.getByTestId("new-session-empty")).toHaveTextContent("还没有项目");
  });

  it("error → loadFailed 行", () => {
    setup({ error: "boom" });
    expect(screen.getByTestId("new-session-error")).toHaveTextContent("加载失败：boom");
  });

  it("项目行：name/path/活跃；点击 → onPickAgent", async () => {
    const onPickAgent = vi.fn();
    setup({ onPickAgent });
    const row = screen.getByTestId("new-session-agent");
    expect(row).toHaveTextContent("emrg");
    expect(row).toHaveTextContent("/x/emrg");
    await userEvent.click(row);
    expect(onPickAgent).toHaveBeenCalledWith(agents[0]);
  });

  it("新建项目按钮 → onNewAgent", async () => {
    const onNewAgent = vi.fn();
    setup({ onNewAgent });
    await userEvent.click(screen.getByTestId("new-session-new-agent"));
    expect(onNewAgent).toHaveBeenCalledTimes(1);
  });

  it("关闭按钮 → onDismiss", async () => {
    const onDismiss = vi.fn();
    setup({ onDismiss });
    await userEvent.click(screen.getByTestId("dlg-close"));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
