import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import { DaemonBridgeProvider, useDaemonBridge } from "./DaemonBridgeProvider";
import { ErrorBoundary } from "./ErrorBoundary";
import { I18nProvider } from "../lib/i18n";
import { useSnapshotStore } from "../hooks/useSnapshotStore";
import type { DaemonEventFrame } from "../lib/daemonBridge";

/**
 * DaemonBridgeProvider.test.tsx — Batch 5 slice 2：AppProviders daemon-event
 * context 层测试。模拟 preload 的 window.emrg（ipcRenderer.on 多订阅者语义）：
 * 验证订阅创建、事件端到端路由进共享 TranscriptStore、卸载取消订阅、
 * window.emrg 缺失降级、Provider 外 useDaemonBridge 抛错。
 */

/** 模拟 preload 暴露的 window.emrg（多订阅者通道，与 ipcRenderer.on 语义一致） */
function mockEmrg() {
  const listeners = new Set<(evt: DaemonEventFrame) => void>();
  const onEvent = vi.fn((cb: (evt: DaemonEventFrame) => void) => {
    listeners.add(cb);
    return () => listeners.delete(cb);
  });
  const sendMessage = vi.fn().mockResolvedValue({ requestId: "req-9" });
  const init = vi.fn().mockResolvedValue({
    config_exists: true,
    api_key_configured: true,
    server_id: "inst-1",
    model: "gpt-4o",
    evolution_count: 42,
    current_version: "0.2.81",
    sessions: [{ session_id: "s1", title: "hello" }],
    open_sessions: [{ sid: "s1", projectName: "p" }],
    active_sid: "s1",
  });
  (window as unknown as { emrg?: unknown }).emrg = { onEvent, sendMessage, init };
  return {
    listeners,
    onEvent,
    sendMessage,
    init,
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

describe("DaemonBridgeProvider (Batch 5 slice 2)", () => {
  afterEach(() => {
    delete (window as unknown as { emrg?: unknown }).emrg;
  });

  it("在 effect 中创建桥并订阅 window.emrg.onEvent", async () => {
    const m = mockEmrg();
    render(wrapper(<div data-testid="child" />));
    await waitFor(() => expect(screen.getByTestId("child")).toBeInTheDocument());
    expect(m.onEvent).toHaveBeenCalledTimes(1);
  });

  it("把 daemon 事件端到端路由进共享 TranscriptStore（message_delta 双帧拼接）", async () => {
    const m = mockEmrg();
    let text = "";
    function Probe() {
      const { transcript } = useDaemonBridge();
      // 与 TranscriptView 相同的订阅方式：store 版本号变更触发重渲染
      useSyncExternalStore(transcript.subscribe, transcript.getVersion);
      text = transcript
        .getEntries("s1")
        .map((e) => (e.kind === "assistant" ? e.segments.map((s) => s.text).join("") : e.kind))
        .join(",");
      return <div />;
    }
    render(wrapper(<Probe />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "Hel" }] }, sid: "s1" });
    m.emit({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "lo" }] }, sid: "s1" });
    await waitFor(() => expect(text).toContain("Hello"));
  });

  it("sessions 事件更新桥 store（Shell 侧边栏数据源）", async () => {
    const m = mockEmrg();
    let openSessions = 0;
    function Probe() {
      const { bridge } = useDaemonBridge();
      // 与 Shell 相同的订阅方式：bridge.store 快照订阅
      const appState = useSnapshotStore(bridge.store);
      openSessions = appState.openSessions.length;
      return <div />;
    }
    render(wrapper(<Probe />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit({ type: "open_sessions", data: { openSessions: [{ sid: "s1", projectName: "p" }] }, sid: null });
    await waitFor(() => expect(openSessions).toBe(1));
  });

  it("挂载时调用 window.emrg.init() 并把结果融合进 bridge store（connected/会话/model）", async () => {
    const m = mockEmrg();
    let connected = false;
    let model = "";
    function Probe() {
      const { bridge } = useDaemonBridge();
      const appState = useSnapshotStore(bridge.store);
      connected = appState.connected;
      model = appState.model;
      return <div />;
    }
    render(wrapper(<Probe />));
    await waitFor(() => expect(m.init).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(connected).toBe(true));
    expect(model).toBe("gpt-4o");
  });

  it("window.emrg 缺失时优雅降级（不抛错，子组件可挂载）", async () => {
    render(
      <DaemonBridgeProvider>
        <div data-testid="child" />
      </DaemonBridgeProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("child")).toBeInTheDocument());
  });

  it("Provider 之外 useDaemonBridge 抛错（被 ErrorBoundary 捕获）", () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    function Bad() {
      useDaemonBridge();
      return null;
    }
    const { container } = render(
      <ErrorBoundary>
        <Bad />
      </ErrorBoundary>,
    );
    expect(container.querySelector(".error-boundary-overlay")).toBeInTheDocument();
    errSpy.mockRestore();
  });

  it("卸载时 dispose（取消订阅，listener 清空）", async () => {
    const m = mockEmrg();
    const { unmount } = render(wrapper(<div />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    expect(m.listeners.size).toBe(1);
    unmount();
    expect(m.listeners.size).toBe(0);
  });
});
