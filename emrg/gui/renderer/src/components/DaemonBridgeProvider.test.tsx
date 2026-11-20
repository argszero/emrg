import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import { DaemonBridgeProvider, useDaemonBridge } from "./DaemonBridgeProvider";
import { ErrorBoundary } from "./ErrorBoundary";
import { I18nProvider } from "../lib/i18n";
import { useSnapshotStore } from "../hooks/useSnapshotStore";
import type { DaemonBridge, DaemonEventFrame } from "../lib/daemonBridge";
// 源码走打包器自己的 `?raw`，不用 `node:fs`：渲染层的 tsconfig 只声明了 `vite/client`
// 与 jest-dom，`node:fs` 在本机编译不过（与 cockpit.test.ts、conversationType.test.ts 同一约定）。
import bridgeSource from "../lib/daemonBridge.ts?raw";
import providerSource from "./DaemonBridgeProvider.tsx?raw";

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
    activeSid: "s1",
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

/* ── The channel-registration guard (rant 2026-11-20T09:54:50, item 5) ────────
 *
 * The failure this file's tests could not see: `createDaemonBridge` takes its channels
 * off the literal built here, so a name that preload exposes, main handles and the
 * daemon answers still reaches the bridge as `undefined` when this literal omits it —
 * and every layer keeps its own test green. One guard, comparing the two source texts:
 * what the bridge declares it will use, against what this provider actually registers.
 */

/** Strip comments: a brace or a name inside prose is not part of the object. */
function bare(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/^\s*\/\/.*$/gm, " ");
}

/** The inside of the object literal that starts at `opener`. */
function objectBody(source: string, opener: RegExp): string {
  const text = bare(source);
  const hit = opener.exec(text);
  if (!hit) throw new Error(`no block matching ${String(opener)}`);
  const start = text.indexOf("{", hit.index);
  let depth = 0;
  for (let i = start; i < text.length; i++) {
    if (text[i] === "{") depth += 1;
    else if (text[i] === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(start + 1, i);
    }
  }
  throw new Error("unbalanced block");
}

/**
 * Names declared at the block's own level — the members of an interface, or the keys of
 * an object literal. A line is read only at depth 0, so a parameter or a nested field
 * is never mistaken for a member, and only from its start, so `p:` inside a signature
 * is not either.
 */
function memberNames(body: string): string[] {
  const names: string[] = [];
  let depth = 0;
  for (const line of body.split("\n")) {
    if (depth === 0) {
      const m = /^\s*([A-Za-z_$][\w$]*)\s*\??\s*[:(<]/.exec(line);
      if (m) names.push(m[1]);
    }
    for (const ch of line) {
      if (ch === "{") depth += 1;
      else if (ch === "}") depth -= 1;
    }
  }
  return names;
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

  /**
   * The backend's current session has to reach the renderer, or the screen and the
   * backend drift apart: the host clicks a row, the backend stays on the other
   * session, and every later "act on the current session" lands on the wrong one.
   *
   * Both carriers are asserted, because the same fact travelled under two names —
   * `active_sid` in the init response, `activeSid` in the `open_sessions` broadcast,
   * the `emrg:getOpenSessions` return and `gui_state.json`. A reader looking for the
   * odd one out finds a field no writer sends and adopts nothing, silently.
   */
  it("init 的 activeSid 进入 store（后台当前会话被渲染层采用）", async () => {
    const m = mockEmrg();
    let reported: string | null | undefined;
    function Probe() {
      const { bridge } = useDaemonBridge();
      const appState = useSnapshotStore(bridge.store);
      reported = appState.reportedSid;
      return <div />;
    }
    render(wrapper(<Probe />));
    await waitFor(() => expect(m.init).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(reported).toBe("s1"));
  });

  it("open_sessions 广播携带 activeSid 时采用它（与 init 同一个字段名）", async () => {
    const m = mockEmrg();
    let reported: string | null | undefined;
    function Probe() {
      const { bridge } = useDaemonBridge();
      const appState = useSnapshotStore(bridge.store);
      reported = appState.reportedSid;
      return <div />;
    }
    render(wrapper(<Probe />));
    await waitFor(() => expect(reported).toBe("s1"));
    m.emit({
      type: "open_sessions",
      data: { openSessions: [{ sid: "s2", projectName: "p" }], activeSid: "s2" },
      sid: null,
    });
    await waitFor(() => expect(reported).toBe("s2"));
  });

  /**
   * Three states, not two: a frame that says nothing about the current session must
   * leave the last answer standing, while a frame carrying `null` reports "there is
   * none" — the session it had was just closed. Reading the first as the second is
   * how a renderer switches the host away from a session nothing disowned.
   */
  it("广播不带 activeSid 时保留上一次的答案，带 null 时如实报告没有当前会话", async () => {
    const m = mockEmrg();
    let reported: string | null | undefined;
    let count = 0;
    function Probe() {
      const { bridge } = useDaemonBridge();
      const appState = useSnapshotStore(bridge.store);
      reported = appState.reportedSid;
      count = appState.openSessions.length;
      return <div />;
    }
    render(wrapper(<Probe />));
    await waitFor(() => expect(reported).toBe("s1"));

    // 缺字段：先证明这一帧真的被处理了（列表变了），再说当前会话没动 ——
    // 否则「仍然是 s1」也可能只是帧没到。
    m.emit({
      type: "open_sessions",
      data: { openSessions: [{ sid: "s1", projectName: "p" }, { sid: "s3", projectName: "q" }] },
      sid: null,
    });
    await waitFor(() => expect(count).toBe(2));
    expect(reported).toBe("s1");

    m.emit({
      type: "open_sessions",
      data: { openSessions: [{ sid: "s3", projectName: "q" }], activeSid: null },
      sid: null,
    });
    await waitFor(() => expect(count).toBe(1));
    await waitFor(() => expect(reported).toBeNull());
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

  it("rant 2026-11-20T09:54:50 第 5 条：登记的通道少于桥声明要用的通道时必红", () => {
    // 一个通道的完整链条是 preload → main → daemon → 桥 → 这里的对象字面量。前四段各有
    // 自己的测试，唯独这一段没有：字面量漏掉一项时，桥拿到的是 undefined，于是它按
    // 「这一版不会问」处理，而下面每一段都还是绿的。状态带的三个 `—` 就是这么来的。
    const declared = memberNames(objectBody(bridgeSource, /^\s{2}emrg: \{/m));
    const registered = memberNames(objectBody(providerSource, /^\s{6}emrg: \{/m));

    // 先量主体：两个空集合相差为零，那是「什么都没比」，不是「全都登记了」。
    expect(declared.length).toBeGreaterThan(5);
    expect(registered.length).toBeGreaterThan(5);
    expect(declared).toContain("usageWindow");

    const missing = declared.filter((name) => !registered.includes(name));
    expect(missing).toEqual([]);
  });
});
