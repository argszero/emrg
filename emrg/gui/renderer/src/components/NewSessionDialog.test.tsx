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
    expect(screen.getByTestId("new-session-empty")).toHaveTextContent("还没有 agent");
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

/**
 * 搜索框（新建会话支持搜索 agent）。上面的固定装置与用例保持原样不动：新增的一半
 * 自带一份列表，三行各占一种命中口径——名字命中、只在路径里命中、都不命中。
 */
const searchAgents: AgentRow[] = [
  { name: "aitokenpool", path: "/x/aitokenpool", latest_session_at: null },
  { name: "emrg", path: "/x/emrg", latest_session_at: null },
  { name: "deepnest", path: "/work/deepnest-two", latest_session_at: null },
];

function searchSetup(props: Partial<Parameters<typeof NewSessionDialog>[0]> = {}) {
  return render(
    <I18nProvider lang="zh">
      <NewSessionDialog open agents={searchAgents} {...props} />
    </I18nProvider>,
  );
}

const rowCount = () => screen.queryAllByTestId("new-session-agent").length;

describe("NewSessionDialog 搜索", () => {
  it("打开时搜索框为空、有焦点，占位文案与打开会话一致", () => {
    searchSetup();
    const box = screen.getByTestId("new-session-search");
    expect(box).toHaveValue("");
    expect(box).toHaveFocus();
    expect(box).toHaveAttribute("placeholder", "按名字或路径筛选");
    expect(rowCount()).toBe(3);
  });

  it("首尾空格 + 大小写混写：只剩命中行，不需要回车", async () => {
    searchSetup();
    await userEvent.type(screen.getByTestId("new-session-search"), "  AITOKEN  ");
    expect(rowCount()).toBe(1);
    expect(screen.getByTestId("new-session-agent")).toHaveTextContent("aitokenpool");
  });

  it("只出现在路径里的串也能命中", async () => {
    searchSetup();
    await userEvent.type(screen.getByTestId("new-session-search"), "nest-two");
    expect(rowCount()).toBe(1);
    expect(screen.getByTestId("new-session-agent")).toHaveTextContent("/work/deepnest-two");
  });

  it("清空搜索框即恢复完整列表", async () => {
    searchSetup();
    const box = screen.getByTestId("new-session-search");
    await userEvent.type(box, "deep");
    expect(rowCount()).toBe(1);
    await userEvent.clear(box);
    expect(rowCount()).toBe(3);
  });

  it("无命中显示「没有匹配的结果」，不显示「还没有 agent」", async () => {
    searchSetup();
    await userEvent.type(screen.getByTestId("new-session-search"), "zzz");
    expect(rowCount()).toBe(0);
    expect(screen.getByTestId("new-session-no-matches")).toHaveTextContent("没有匹配的结果");
    expect(screen.queryByTestId("new-session-empty")).toBeNull();
  });

  it("没有 agent 时仍显示「还没有 agent」，与查询无关", () => {
    searchSetup({ agents: [] });
    expect(screen.getByTestId("new-session-empty")).toHaveTextContent("还没有 agent");
    expect(screen.queryByTestId("new-session-no-matches")).toBeNull();
  });

  it("关掉再打开：上一次的查询不带进这一次，焦点回搜索框", async () => {
    const { rerender } = searchSetup();
    await userEvent.type(screen.getByTestId("new-session-search"), "deep");
    expect(rowCount()).toBe(1);
    const view = (open: boolean) => (
      <I18nProvider lang="zh">
        <NewSessionDialog open={open} agents={searchAgents} />
      </I18nProvider>
    );
    rerender(view(false));
    rerender(view(true));
    expect(screen.getByTestId("new-session-search")).toHaveValue("");
    expect(screen.getByTestId("new-session-search")).toHaveFocus();
    expect(rowCount()).toBe(3);
  });
});
