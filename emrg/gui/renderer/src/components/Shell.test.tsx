import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  const listMemories = vi.fn().mockResolvedValue([]);
  const listSkills = vi.fn().mockResolvedValue([]);
  const listHistory = vi.fn().mockResolvedValue({ messages: [] });
  (window as unknown as { emrg?: unknown }).emrg = {
    onEvent,
    sendMessage,
    clearSession,
    compactSession,
    listProjects,
    listMemories,
    listSkills,
    listHistory,
  };
  return {
    listeners,
    onEvent,
    sendMessage,
    clearSession,
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
    fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "hello" } });
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(screen.getByText("Start a conversation first.")).toBeInTheDocument());
  });

  // ── Batch 5 slice 4：/指令路由 + 侧边栏按钮 ──

  it("/help command opens the help dialog", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "/help" } });
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
    fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "/rename" } });
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
    fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "/clear" } });
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
});
