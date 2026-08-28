import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { Shell } from "./Shell";
import { DaemonBridgeProvider } from "./DaemonBridgeProvider";
import { I18nProvider } from "../lib/i18n";
import type { DaemonEventFrame } from "../lib/daemonBridge";

/**
 * Shell.test.tsx — Batch 5 slice 3：聊天回路接线测试。
 * 模拟 preload 的 window.emrg（多订阅者通道），通过 emit 投递 daemon 事件帧，
 * 验证：会话自动选择、侧边栏切换、transcript 按 sid 切片、连接状态指示、
 * 断连横幅、无会话发送提示。
 */

/** 模拟 preload 暴露的 window.emrg（与 ipcRenderer.on 语义一致） */
function mockEmrg() {
  const listeners = new Set<(evt: DaemonEventFrame) => void>();
  const onEvent = vi.fn((cb: (evt: DaemonEventFrame) => void) => {
    listeners.add(cb);
    return () => listeners.delete(cb);
  });
  const sendMessage = vi.fn().mockResolvedValue({ requestId: "req-9" });
  // DialogHost 用到的 IPC 通道（Batch 5 slice 4）：对话框数据加载 + 直接执行类指令
  const clearSession = vi.fn().mockResolvedValue({ ok: true });
  const compactSession = vi.fn().mockResolvedValue({ ok: true });
  const listProjects = vi.fn().mockResolvedValue([{ name: "emrg", path: "/p/emrg" }]);
  const listTasks = vi.fn().mockResolvedValue([{ name: "evo", type: "evolution", enabled: true, session_id: "emrg-evolution-evo", project_path: "/p/emrg" }]);
  const listRants = vi.fn().mockResolvedValue([{ timestamp: "2026-08-26T12:00:00+08:00", project: "emrg", status: "pending", message: "x" }]);
  const listMemories = vi.fn().mockResolvedValue([]);
  const listSkills = vi.fn().mockResolvedValue([]);
  const listHistory = vi.fn().mockResolvedValue({ messages: [] });
  // Batch 5 slice 8：任务 CRUD + Rant 提交（window.emrg 桥已暴露，main.js IPC 在列）
  const taskCreate = vi.fn().mockResolvedValue({ ok: true });
  const taskUpdate = vi.fn().mockResolvedValue({ ok: true });
  const taskDelete = vi.fn().mockResolvedValue({ ok: true });
  const taskTemplateList = vi.fn().mockResolvedValue([{ name: "journal" }]);
  const sendRant = vi.fn().mockResolvedValue({ ok: true, count: 11 });
  const relaunchGui = vi.fn().mockResolvedValue({ ok: true });
  const switchSession = vi.fn().mockResolvedValue({ ok: true });
  (window as unknown as { emrg?: unknown }).emrg = {
    onEvent,
    sendMessage,
    clearSession,
    compactSession,
    listProjects,
    listTasks,
    listRants,
    listMemories,
    listSkills,
    listHistory,
    taskCreate,
    taskUpdate,
    taskDelete,
    taskTemplateList,
    sendRant,
    relaunchGui,
    switchSession,
  };
  return {
    listeners,
    onEvent,
    sendMessage,
    clearSession,
    listProjects,
    listTasks,
    listRants,
    taskCreate,
    taskUpdate,
    taskDelete,
    taskTemplateList,
    sendRant,
    relaunchGui,
    switchSession,
    emit: (evt: DaemonEventFrame) => listeners.forEach((cb) => cb(evt)),
  };
}

function wrapper(children: ReactNode) {
  return (
    <I18nProvider lang="en">
      <DaemonBridgeProvider>{children}</DaemonBridgeProvider>
    </I18nProvider>
  );
}

function openSessionsFrame(entries: { sid: string; title?: string }[]): DaemonEventFrame {
  return { type: "open_sessions", data: { openSessions: entries } };
}

function sessionsFrame(sessions: { session_id: string; title?: string }[]): DaemonEventFrame {
  return { type: "sessions", data: { sessions } };
}

function deltaFrame(sid: string, text: string): DaemonEventFrame {
  return { type: "message_delta", sid, data: { chunks: [{ request_id: `req-${sid}`, content: text }] } };
}

/** 向 tiptap 编辑器输入文本（Stage 1：textarea → contenteditable，fireEvent.change 失效） */
async function typeIntoComposer(text: string) {
  const input = await screen.findByTestId("composer-input");
  await userEvent.type(input, text);
}

describe("Shell (Batch 5 slice 3 chat wiring)", () => {
  afterEach(() => {
    delete (window as unknown as { emrg?: unknown }).emrg;
  });

  it("renders the chat loop (transcript + composer) without window.emrg (degradation)", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("transcript-view")).toBeInTheDocument());
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.getByTestId("react-shell-sidebar")).toBeInTheDocument();
    // 无 emrg → 连接状态点灰色（未连接）
    expect(screen.getByTestId("conn-status").querySelector(".conn-dot")?.className).toContain("gray");
  });

  it("auto-selects the first open session and renders its transcript from broadcasts", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }, { sid: "s2", title: "Beta" }]));
    m.emit(sessionsFrame([{ session_id: "s1", title: "Alpha" }, { session_id: "s2", title: "Beta" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(2));
    // 自动选中第一个会话 → s1 条目带 active
    expect(screen.getAllByTestId("open-session-item")[0].className).toContain("active");
    // s1 的 delta 到达 → transcript 显示
    m.emit(deltaFrame("s1", "Hello from s1"));
    await waitFor(() => expect(screen.getByText("Hello from s1")).toBeInTheDocument());
  });

  it("clicking a sidebar session switches the transcript to that session (sid-scoped)", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }, { sid: "s2", title: "Beta" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(2));
    // 两个会话都有内容，但 s1 激活 → 只显示 s1
    m.emit(deltaFrame("s1", "content-one"));
    m.emit(deltaFrame("s2", "content-two"));
    await waitFor(() => expect(screen.getByText("content-one")).toBeInTheDocument());
    expect(screen.queryByText("content-two")).not.toBeInTheDocument();
    // 点击 s2 → 激活切换，transcript 显示 s2 内容
    const s2 = screen.getAllByTestId("open-session-item").find((el) => el.dataset.sid === "s2");
    expect(s2).toBeTruthy();
    fireEvent.click(s2 as HTMLElement);
    await waitFor(() => expect(screen.getByText("content-two")).toBeInTheDocument());
    expect(screen.queryByText("content-one")).not.toBeInTheDocument();
    expect((screen.getAllByTestId("open-session-item").find((el) => el.dataset.sid === "s2"))?.className).toContain("active");
  });

  it("shows the connection status + model from the status broadcast", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit({ type: "status", data: { connected: true, model: "claude-3.7" } });
    await waitFor(() => expect(screen.getByTestId("conn-status").textContent).toContain("claude-3.7"));
    expect(screen.getByTestId("conn-status").querySelector(".conn-dot")?.className).toContain("green");
  });

  it("shows the disconnected banner when the active session broadcasts disconnected", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(1));
    m.emit({ type: "disconnected", sid: "s1", data: {} });
    await waitFor(() => expect(screen.getByTestId("conn-banner")).toBeInTheDocument());
  });

  it("composer send with no active session shows the need-session hint", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    await typeIntoComposer("hello");
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(screen.getByText("Start a conversation first.")).toBeInTheDocument());
  });

  // ── Batch 5 slice 4：/指令路由 + 侧边栏按钮 ──

  it("/help command opens the help dialog", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    await typeIntoComposer("/help");
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(screen.getByTestId("help-dialog")).toBeInTheDocument());
    expect(screen.getAllByTestId("help-row").length).toBeGreaterThanOrEqual(16);
  });

  it("/rename with an active session opens the rename dialog", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    m.emit(sessionsFrame([{ session_id: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(1));
    await typeIntoComposer("/rename");
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(screen.getByTestId("rename-dialog")).toBeInTheDocument());
  });

  it("sidebar new-chat button opens the new-session dialog", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("new-chat-btn")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("new-chat-btn"));
    await waitFor(() => expect(screen.getByTestId("new-session-dialog")).toBeInTheDocument());
  });

  it("sidebar open-chat button opens the open-session dialog", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("open-chat-btn")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("open-chat-btn"));
    await waitFor(() => expect(screen.getByTestId("open-session-dialog")).toBeInTheDocument());
  });

  it("/clear with an active session calls clearSession and shows the cleared message", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(1));
    await typeIntoComposer("/clear");
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(screen.getByText("Current conversation cleared.")).toBeInTheDocument());
  });

  // ── Batch 5 slice 5：workspace 视图切换（vanilla switchView 语义） ──

  it("默认 sessions 视图：显示会话区（transcript/composer），不显示面板", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("transcript-view")).toBeInTheDocument());
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.queryByTestId("workspace-view")).not.toBeInTheDocument();
    expect(screen.queryByTestId("panel-projects")).not.toBeInTheDocument();
  });

  it("点击 nav-projects → 面板视图：显示项目面板，隐藏会话 chrome", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-projects"));
    await waitFor(() => expect(screen.getByTestId("panel-projects")).toBeInTheDocument());
    expect(screen.getByTestId("nav-projects").className).toContain("active");
    // 面板视图隐藏会话 chrome（vanilla setWorkspaceChrome("panel")）
    expect(screen.queryByTestId("transcript-view")).not.toBeInTheDocument();
    expect(screen.queryByTestId("composer")).not.toBeInTheDocument();
    expect(screen.queryByTestId("result-panel")).not.toBeInTheDocument();
  });

  it("点击当前激活面板 → toggle 回会话视图（vanilla 语义）", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-projects"));
    await waitFor(() => expect(screen.getByTestId("panel-projects")).toBeInTheDocument());
    // 再点同一面板 → 关闭回会话
    fireEvent.click(screen.getByTestId("nav-projects"));
    await waitFor(() => expect(screen.queryByTestId("panel-projects")).not.toBeInTheDocument());
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.getByTestId("nav-projects").className).not.toContain("active");
  });

  it("面板间切换：projects → tasks → settings", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-projects"));
    await waitFor(() => expect(screen.getByTestId("panel-projects")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("panel-tasks")).toBeInTheDocument());
    expect(screen.queryByTestId("panel-projects")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("nav-settings"));
    await waitFor(() => expect(screen.getByTestId("panel-settings")).toBeInTheDocument());
    expect(screen.queryByTestId("panel-tasks")).not.toBeInTheDocument();
  });

  it("面板视图下点击会话条目 → 回会话视图并切换 sid", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }, { sid: "s2", title: "Beta" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(2));
    // 打开面板
    fireEvent.click(screen.getByTestId("nav-rants"));
    await waitFor(() => expect(screen.getByTestId("panel-rants")).toBeInTheDocument());
    // 点击 s2 → 回会话视图，s2 激活
    const s2 = screen.getAllByTestId("open-session-item").find((el) => el.dataset.sid === "s2");
    expect(s2).toBeTruthy();
    fireEvent.click(s2 as HTMLElement);
    await waitFor(() => expect(screen.queryByTestId("panel-rants")).not.toBeInTheDocument());
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect((screen.getAllByTestId("open-session-item").find((el) => el.dataset.sid === "s2"))?.className).toContain("active");
    expect(screen.getByTestId("nav-sessions").className).toContain("active");
  });

  // ── Batch 5 slice 7：workspace 面板数据接线（真实 listProjects/listTasks/listRants） ──

  it("进入任务面板 → 加载 listTasks 并渲染任务行", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(m.listTasks).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    expect(screen.getByTestId("task-row")).toHaveTextContent("evo");
  });

  it("任务行「打开会话」→ switchSession(sessionId, projectPath) + 回到会话视图（vanilla #924）", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    // 打开会话按钮存在且可点（有 session_id）
    const openBtn = screen.getByTitle("Open session");
    expect(openBtn).toBeEnabled();
    fireEvent.click(openBtn);
    await waitFor(() =>
      expect(m.switchSession).toHaveBeenCalledWith({ sessionId: "emrg-evolution-evo", projectPath: "/p/emrg" }),
    );
    // 切回会话视图（transcript + composer 可见）
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    expect(screen.queryByTestId("panel-tasks")).not.toBeInTheDocument();
  });

  it("进入 Rant 面板 → 加载 listRants 并渲染 rant 行", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-rants"));
    await waitFor(() => expect(m.listRants).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("rant-row")).toBeInTheDocument());
  });

  it("进入项目面板 → 加载 listProjects；查看会话 → listProjectSessions 加载会话行", async () => {
    const m = mockEmrg();
    m.listProjects.mockResolvedValue([{ name: "p1", path: "/p/p1" }]);
    (window as unknown as { emrg: { listProjectSessions?: unknown } }).emrg.listProjectSessions =
      vi.fn().mockResolvedValue({ sessions: [{ session_id: "s1", title: "会话一" }] });
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-projects"));
    await waitFor(() => expect(m.listProjects).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId("project-row")).toBeInTheDocument());
    // 查看会话 → 会话子视图加载（en 标题 "Sessions"；限定在项目行内避免与侧栏撞名）
    const row = screen.getByTestId("project-row");
    fireEvent.click(row.querySelector('button[title="Sessions"]') as HTMLButtonElement);
    await waitFor(() =>
      expect((window as unknown as { emrg: { listProjectSessions?: unknown } }).emrg.listProjectSessions).toHaveBeenCalled(),
    );
    await waitFor(() => expect(screen.getByTestId("project-session-row")).toBeInTheDocument());
    expect(screen.getByTestId("project-session-row")).toHaveTextContent("会话一");
  });

  // ── Batch 5 slice 8：任务表单 + Rant 对话框接线 ──

  it("任务面板激活时每 5s 轮询 listTasks（vanilla startTaskPoll 语义）", async () => {
    vi.useFakeTimers();
    try {
      const m = mockEmrg();
      render(wrapper(<Shell />));
      await vi.waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
      fireEvent.click(screen.getByTestId("nav-tasks"));
      await vi.waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
      const callsAfterActivation = m.listTasks.mock.calls.length;
      expect(callsAfterActivation).toBeGreaterThanOrEqual(1);
      // 快进 5s → 再次轮询；再快进 10s → 至少再两次
      await act(async () => {
        vi.advanceTimersByTime(5000);
      });
      await act(async () => {
        vi.advanceTimersByTime(10_000);
      });
      expect(m.listTasks.mock.calls.length).toBeGreaterThanOrEqual(callsAfterActivation + 2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("任务面板「＋ 添加任务」→ 打开表单 → 保存 → taskCreate + 刷新列表", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    // 打开表单（任务行按钮 + 底部添加按钮均触发 openTaskForm）
    fireEvent.click(screen.getByText("＋ Add task"));
    await waitFor(() => expect(screen.getByTestId("task-form-dialog")).toBeInTheDocument());
    // 类型下拉来自 taskTemplateList（journal）+ evolution 兜底
    const typeSel = screen.getByTestId("task-form-type") as HTMLSelectElement;
    await waitFor(() => expect(m.taskTemplateList).toHaveBeenCalled());
    expect(typeSel.options.length).toBeGreaterThanOrEqual(2);
    // 填表保存
    fireEvent.change(screen.getByTestId("task-form-name"), { target: { value: "t2" } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    await waitFor(() => expect(m.taskCreate).toHaveBeenCalledTimes(1));
    const payload = m.taskCreate.mock.calls[0][0];
    expect(payload.name).toBe("t2");
    expect(payload.interval).toBe(1800);
    await waitFor(() => expect(m.listTasks).toHaveBeenCalledTimes(2)); // 初始 + 保存后刷新
    expect(screen.queryByTestId("task-form-dialog")).not.toBeInTheDocument();
  });

  it("任务行「Edit」→ 表单预填 + name 只读 → 保存 → taskUpdate（不改名）", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    fireEvent.click(screen.getByTitle("Edit"));
    await waitFor(() => expect(screen.getByTestId("task-form-dialog")).toBeInTheDocument());
    // 预填由 TaskFormDialog 的 useEffect 在 mount 后执行（vanilla openTaskForm 语义，
    // 见 #1031 同类 flaky 修复）。用 waitFor 等待 effect 填充值，避免在 effect 跑完前
    // 读到空 name.value（#1035 轮询测试的 fake-timer/async 交错曾偶发暴露此竞态）。
    const name = screen.getByTestId("task-form-name") as HTMLInputElement;
    await waitFor(() => expect(name.value).toBe("evo"));
    expect(name.disabled).toBe(true);
    fireEvent.change(screen.getByTestId("task-form-interval"), { target: { value: "7200" } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    await waitFor(() => expect(m.taskUpdate).toHaveBeenCalledTimes(1));
    expect(m.taskUpdate.mock.calls[0][0]).toMatchObject({ name: "evo", interval: 7200 });
    expect(m.taskCreate).not.toHaveBeenCalled();
  });

  it("任务行「Delete」→ 确认弹窗 → taskDelete + 刷新", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    fireEvent.click(screen.getByTitle("Delete"));
    await waitFor(() => expect(screen.getByTestId("confirm-dialog")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("confirm-ok"));
    await waitFor(() => expect(m.taskDelete).toHaveBeenCalledWith({ name: "evo" }));
    await waitFor(() => expect(m.listTasks).toHaveBeenCalledTimes(2));
  });

  it("Rant 面板「＋ New rant」→ 对话框 → 提交 → sendRant + 刷新列表", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-rants"));
    await waitFor(() => expect(screen.getByTestId("rant-row")).toBeInTheDocument());
    fireEvent.click(screen.getByText("＋ New rant"));
    await waitFor(() => expect(screen.getByTestId("rant-dialog")).toBeInTheDocument());
    // 项目下拉含已注册项目 emrg
    const sel = screen.getByTestId("rant-project") as HTMLSelectElement;
    expect(Array.from(sel.options).some((o) => o.value === "emrg")).toBe(true);
    fireEvent.change(screen.getByTestId("rant-message"), { target: { value: "测试 rant" } });
    fireEvent.change(screen.getByTestId("rant-project"), { target: { value: "emrg" } });
    fireEvent.click(screen.getByTestId("rant-submit"));
    await waitFor(() => expect(m.sendRant).toHaveBeenCalledWith({ message: "测试 rant", project: "emrg" }));
    await waitFor(() => expect(m.listRants).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("rant-dialog")).not.toBeInTheDocument();
  });

  it("upgrade 事件 → 渲染升级横幅；重启按钮调 relaunchGui（仅 GUI，不碰 daemon）；dismiss 后同版本不重现", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    // 初始无横幅
    expect(screen.queryByTestId("upgrade-banner")).not.toBeInTheDocument();
    // 心跳 upgrade 帧（installed 0.2.84 ≠ current 0.2.83）
    act(() => {
      m.emit({ type: "upgrade", data: { current_version: "0.2.83", installed_version: "0.2.84" } });
    });
    const banner = await screen.findByTestId("upgrade-banner");
    expect(banner).toBeInTheDocument();
    // from→to 文案（en 词典）
    expect(screen.getByText(/upgraded from 0\.2\.83 to 0\.2\.84/i)).toBeInTheDocument();
    // 重启按钮 → relaunchGui（仅 GUI 进程重启，绝不走 restartDaemon stop 链）
    fireEvent.click(screen.getByTestId("upgrade-banner-restart"));
    await waitFor(() => expect(m.relaunchGui).toHaveBeenCalledTimes(1));
    // dismiss → 隐藏；同版本心跳重发不重现（vanilla lastKnownVersion 语义）
    fireEvent.click(screen.getByTestId("upgrade-banner-dismiss"));
    expect(screen.queryByTestId("upgrade-banner")).not.toBeInTheDocument();
    act(() => {
      m.emit({ type: "upgrade", data: { current_version: "0.2.83", installed_version: "0.2.84" } });
    });
    expect(screen.queryByTestId("upgrade-banner")).not.toBeInTheDocument();
  });
});
