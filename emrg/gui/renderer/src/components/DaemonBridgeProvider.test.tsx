import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import { DaemonBridgeProvider, useDaemonBridge } from "./DaemonBridgeProvider";
import { ErrorBoundary } from "./ErrorBoundary";
import { I18nProvider } from "../lib/i18n";
import { useSnapshotStore } from "../hooks/useSnapshotStore";
import type { DaemonBridge, DaemonEventFrame } from "../lib/daemonBridge";

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
  const setSandbox = vi.fn().mockResolvedValue(undefined);
  const respondApproval = vi.fn().mockResolvedValue({ ok: true });
  (window as unknown as { emrg?: unknown }).emrg = {
    onEvent,
    sendMessage,
    init,
    setSandbox,
    respondApproval,
  };
  return {
    listeners,
    onEvent,
    sendMessage,
    init,
    setSandbox,
    respondApproval,
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

  /**
   * The seam this test exists for (measured 2026-10-08): `emrg:setSandbox` and
   * `emrg:respondApproval` both existed in main.js and preload.js, and the bridge
   * declared both — but the deps literal *here* handed neither over, so the click
   * reached `emrg.setSandbox === undefined` and the bridge answered `false`
   * (fail-closed, silently). A test that injects a fake bridge cannot see this: it
   * replaces the very object the provider failed to fill in. So drive the real
   * provider against a fake `window.emrg` and assert the call that crosses the seam.
   * Both directions are checked for each channel: handed over when present, and
   * still `false` (not a throw) when the preload predates the API.
   */
  it("把 setSandbox / respondApproval 交给桥（存在但未交接 = 静默失效）", async () => {
    const m = mockEmrg();
    let api: DaemonBridge | null = null;
    function Probe() {
      api = useDaemonBridge().bridge;
      return <div />;
    }
    render(wrapper(<Probe />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api).not.toBeNull());

    // 通道 1：setSandbox —— 点击 chip 的路径
    await expect(api!.setSandbox("s1", "read-only")).resolves.toBe(true);
    expect(m.setSandbox).toHaveBeenCalledWith({ sessionId: "s1", mode: "read-only" });

    // 通道 2：respondApproval —— 先要有一封待答的提问，答案才有对象可回
    m.emit({
      type: "approval_request",
      data: { request_id: "rq-1", question: "allow?", timeout_seconds: 120 },
      sid: "s1",
    });
    await waitFor(() => expect(api!.store.get().pendingApproval).not.toBeNull());
    await expect(api!.respondApproval(true)).resolves.toBe(true);
    expect(m.respondApproval).toHaveBeenCalledWith({
      sessionId: "s1",
      requestId: "rq-1",
      approved: true,
    });
  });

  it("preload 缺这两个通道时返回 false 而非抛错（旧 preload 降级）", async () => {
    const m = mockEmrg();
    const emrg = (window as unknown as { emrg: Record<string, unknown> }).emrg;
    delete emrg.setSandbox;
    delete emrg.respondApproval;
    let api: DaemonBridge | null = null;
    function Probe() {
      api = useDaemonBridge().bridge;
      return <div />;
    }
    render(wrapper(<Probe />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api).not.toBeNull());
    await expect(api!.setSandbox("s1", "read-only")).resolves.toBe(false);
    await expect(api!.respondApproval(true)).resolves.toBe(false);
  });
});
