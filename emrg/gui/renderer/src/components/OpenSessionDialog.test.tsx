import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { OpenSessionDialog } from "./OpenSessionDialog";
import { I18nProvider } from "../lib/i18n";
import type { AgentRow, SessionRow } from "../lib/openSession";

/**
 * OpenSessionDialog.test.tsx — 两步打开会话对话框测试（Batch 4 slice 3）。
 * 镜像 vanilla showOpenSessionDialog/showAgentSessions：项目列表（含删除）→ 会话列表。
 */

const agents: AgentRow[] = [
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
      <OpenSessionDialog open step="agents" agents={agents} {...props} />
    </I18nProvider>,
  );
}

describe("OpenSessionDialog", () => {
  it("open=false → 不渲染", () => {
    render(
      <I18nProvider lang="zh">
        <OpenSessionDialog open={false} step="agents" />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("open-session-dialog")).toBeNull();
  });

  it("agents loading → dlg.loading 行", () => {
    setup({ agents: null });
    expect(screen.getByTestId("open-session-loading")).toHaveTextContent("加载中");
  });

  it("空项目列表 → noAgents", () => {
    setup({ agents: [] });
    expect(screen.getByTestId("open-session-empty")).toHaveTextContent("还没有 agent");
  });

  it("error → loadFailed 行", () => {
    setup({ error: "boom" });
    expect(screen.getByTestId("open-session-error")).toHaveTextContent("加载失败：boom");
  });

  it("项目行：name/path/活跃 + 删除按钮；点击 → onPickAgent", async () => {
    const onPickAgent = vi.fn();
    setup({ onPickAgent });
    const rows = screen.getAllByTestId("open-session-agent");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("emrg");
    expect(rows[0]).toHaveTextContent("/x/emrg");
    await userEvent.click(screen.getAllByTestId("open-session-pick")[1]);
    expect(onPickAgent).toHaveBeenCalledWith(agents[1]);
  });

  it("删除按钮 → onDeleteAgent", async () => {
    const onDeleteAgent = vi.fn();
    setup({ onDeleteAgent });
    await userEvent.click(screen.getAllByTestId("open-session-delete")[0]);
    expect(onDeleteAgent).toHaveBeenCalledWith(agents[0]);
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
    const onNewAgent = vi.fn();
    setup({ onNewSession, onNewAgent });
    await userEvent.click(screen.getByTestId("open-session-new-session"));
    expect(onNewSession).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByTestId("open-session-new-agent"));
    expect(onNewAgent).toHaveBeenCalledTimes(1);
  });

  // ── issue #2924：两步各一个搜索框 ──────────────────────────────────────

  it("agent 搜索：按名字过滤，不区分大小写、忽略首尾空格（不用回车）", async () => {
    setup();
    await userEvent.type(screen.getByTestId("open-session-search"), "  AITOKEN  ");
    const rows = screen.getAllByTestId("open-session-agent");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("aitokenpool");
  });

  it("agent 搜索：路径也能匹配，不只名字", async () => {
    setup();
    // "/x/emrg" 只在 path 里；名字是 "emrg"，两者都命中这一行——用只出现在路径里的串
    await userEvent.type(screen.getByTestId("open-session-search"), "/x/emrg");
    const rows = screen.getAllByTestId("open-session-agent");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("emrg");
  });

  it("清空搜索恢复整个列表", async () => {
    setup();
    const box = screen.getByTestId("open-session-search");
    await userEvent.type(box, "aitoken");
    expect(screen.getAllByTestId("open-session-agent")).toHaveLength(1);
    await userEvent.clear(box);
    expect(screen.getAllByTestId("open-session-agent")).toHaveLength(2);
  });

  it("会话搜索：标题与会话 id 都能匹配", async () => {
    setup({ step: "sessions", sessions, projectName: "emrg", currentSid: "s1" });
    const box = screen.getByTestId("open-session-search");
    await userEvent.type(box, "重构");
    expect(screen.getAllByTestId("open-session-session")).toHaveLength(1);
    await userEvent.clear(box);
    // 大小写不敏感：id 存的是小写 "s2"
    await userEvent.type(box, "S2");
    const rows = screen.getAllByTestId("open-session-session");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("s2");
  });

  it("有列表但没匹配 → 说「没有匹配的结果」，不落进「还没有」那个空态", async () => {
    setup();
    await userEvent.type(screen.getByTestId("open-session-search"), "zzz");
    expect(screen.queryAllByTestId("open-session-agent")).toHaveLength(0);
    expect(screen.getByTestId("open-session-no-matches")).toHaveTextContent("没有匹配的结果");
    expect(screen.queryByTestId("open-session-empty")).toBeNull();
  });

  it("会话步：没匹配同样说「没有匹配的结果」，不是「还没有会话」", async () => {
    setup({ step: "sessions", sessions, projectName: "emrg" });
    await userEvent.type(screen.getByTestId("open-session-search"), "zzz");
    expect(screen.getByTestId("open-session-no-matches")).toHaveTextContent("没有匹配的结果");
    expect(screen.queryByTestId("open-session-empty")).toBeNull();
  });

  it("本来一条都没有 → 仍是「还没有」，不是「没有匹配」", () => {
    setup({ agents: [] });
    expect(screen.getByTestId("open-session-empty")).toHaveTextContent("还没有 agent");
    expect(screen.queryByTestId("open-session-no-matches")).toBeNull();
  });

  it("打开时焦点就在搜索框", () => {
    setup();
    expect(screen.getByTestId("open-session-search")).toHaveFocus();
  });

  it("走到下一步：查询清空、焦点回到搜索框", async () => {
    const { rerender } = setup();
    await userEvent.type(screen.getByTestId("open-session-search"), "emrg");
    rerender(
      <I18nProvider lang="zh">
        <OpenSessionDialog open step="sessions" sessions={sessions} projectName="emrg" />
      </I18nProvider>,
    );
    const box = screen.getByTestId("open-session-search");
    expect(box).toHaveValue("");
    expect(box).toHaveFocus();
    expect(screen.getAllByTestId("open-session-session")).toHaveLength(2);
  });

  it("加载中与加载失败仍然只渲染各自那一行（搜索框不改变这两个态）", () => {
    setup({ agents: null });
    expect(screen.getByTestId("open-session-loading")).toHaveTextContent("加载中");
    expect(screen.queryByTestId("open-session-no-matches")).toBeNull();
  });

  it("中文文案指 agent 时说 agent，不说「项目」", () => {
    setup();
    expect(screen.getByTestId("open-session-search")).toHaveAttribute("placeholder", "按名字或路径筛选");
    const dialog = screen.getByTestId("open-session-dialog");
    expect(dialog.textContent).not.toContain("项目");
    expect(dialog.textContent).toContain("agent");
  });
});
