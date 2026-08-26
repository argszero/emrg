import { describe, expect, it, vi } from "vitest";
import { createDaemonBridge, type DaemonEventFrame } from "./daemonBridge";
import { createTranscriptStore } from "./transcript";

/**
 * daemonBridge.test.ts — daemon 事件桥测试（Batch 5 slice 1）。
 * 镜像 vanilla App.handleEvent（app.js 1411-1600）：按 type 路由到 transcript/app store。
 * 严格 sid 路由（#977）：每会话独立 busy/ownStream/disconnected 状态。
 */

function setup() {
  const t = vi.fn((key: string, params?: Record<string, unknown>) => {
    if (key === "app.queued") return `queued:${params?.pos}`;
    if (key === "app.queuedResent") return `resent:${params?.n}`;
    if (key === "app.queuedCancelled") return "cancelled";
    if (key === "app.error") return `err:${params?.msg}`;
    return key;
  });
  const transcript = createTranscriptStore({ t });
  const sendMessage = vi.fn().mockResolvedValue({ requestId: "req-1" });
  let cb: ((evt: DaemonEventFrame) => void) | null = null;
  const disposeCb = vi.fn();
  const onEvent = vi.fn((handler: (evt: DaemonEventFrame) => void) => {
    cb = handler;
    return disposeCb;
  });
  const bridge = createDaemonBridge({ onEvent, emrg: { sendMessage }, transcript, t });
  return { bridge, transcript, t, sendMessage, onEvent, disposeCb, emit: (f: DaemonEventFrame) => cb?.(f) };
}

function entriesText(store: ReturnType<typeof createTranscriptStore>, sid: string | null = null): string[] {
  return store.getEntries(sid).map((e) =>
    e.kind === "user" ? `u:${e.text}` : e.kind === "assistant" ? `a:${e.segments.map((s) => s.text).join("")}` : e.kind === "system" ? `s:${e.text}` : `t:${e.kind}`,
  );
}

describe("createDaemonBridge", () => {
  it("订阅 onEvent 并返回 dispose（取消订阅）", () => {
    const { bridge, onEvent, disposeCb } = setup();
    expect(onEvent).toHaveBeenCalledTimes(1);
    bridge.dispose();
    expect(disposeCb).toHaveBeenCalledTimes(1);
  });

  it("message_delta → transcript.handleDelta（chunks 数组与单帧两种形态）", () => {
    const { emit, transcript } = setup();
    emit({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "Hel" }] }, sid: "s1" });
    emit({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "lo" }] }, sid: "s1" });
    const txt = entriesText(transcript, "s1");
    expect(txt.some((x) => x === "a:Hello")).toBe(true);
  });

  it("done → transcript.handleDone + 释放 own stream 锁（request 匹配）", () => {
    const { emit, bridge } = setup();
    bridge.store.update((s) => ({ ...s, busyBySid: { ...s.busyBySid, s1: true }, ownStreamRidBySid: { ...s.ownStreamRidBySid, s1: "r1" } }));
    bridge.handleFrame({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "x" }] }, sid: "s1" });
    bridge.handleFrame({ type: "done", data: { request_id: "r1" }, sid: "s1" });
    const st = bridge.store.get();
    expect(st.busyBySid["s1"]).toBe(false);
    expect(st.ownStreamRidBySid["s1"] ?? null).toBeNull();
  });

  it("tool_started / tool_finished → 工具行状态", () => {
    const { emit, transcript } = setup();
    emit({ type: "tool_started", data: { request_id: "r1", tool_call_id: "c1", tool_name: "bash", intent: "run" }, sid: "s1" });
    const rows = transcript.getEntries("s1").filter((e) => e.kind === "tool-row");
    expect(rows).toHaveLength(1);
    emit({ type: "tool_finished", data: { tool_call_id: "c1", tool_name: "bash", elapsed: 5, content: "out" }, sid: "s1" });
    const after = transcript.getEntries("s1").filter((e) => e.kind === "tool-row");
    expect((after[0] as { row: { status: string } }).row.status).toBe("done");
  });

  it("cancelled → clearTyping + 释放锁（无 request_id 全清）", () => {
    const { emit, bridge } = setup();
    bridge.handleFrame({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "partial" }] }, sid: "s1" });
    bridge.handleFrame({ type: "cancelled", data: {}, sid: "s1" });
    expect(bridge.store.get().busyBySid["s1"]).toBe(false);
  });

  it("sessions / open_sessions → store 更新", () => {
    const { emit, bridge } = setup();
    emit({ type: "sessions", data: { sessions: [{ session_id: "s1", title: "T" }] }, sid: null });
    expect(bridge.store.get().sessions).toEqual([{ session_id: "s1", title: "T" }]);
    emit({ type: "open_sessions", data: { openSessions: [{ sid: "s1", projectName: "p" }] }, sid: null });
    expect(bridge.store.get().openSessions).toEqual([{ sid: "s1", projectName: "p" }]);
  });

  it("status → 连接状态 + serverId/model/version；pong → serverId/model/evolutionCount", () => {
    const { emit, bridge } = setup();
    emit({ type: "status", data: { connected: true, server_id: "sv1", model: "m1", current_version: "v1" }, sid: null });
    let st = bridge.store.get();
    expect(st.connected).toBe(true);
    expect(st.serverId).toBe("sv1");
    expect(st.model).toBe("m1");
    expect(st.currentVersion).toBe("v1");
    emit({ type: "pong", data: { identity: { instance_id: "sv2" }, model: "m2", evolution_count: 7 }, sid: null });
    st = bridge.store.get();
    expect(st.serverId).toBe("sv2");
    expect(st.model).toBe("m2");
    expect(st.evolutionCount).toBe(7);
  });

  it("task_queued → 排队系统消息（sid 路由）", () => {
    const { emit, transcript } = setup();
    emit({ type: "task_queued", data: { position: 3 }, sid: "s2" });
    expect(entriesText(transcript, "s2")).toContain("s:queued:3");
  });

  it("error → 错误系统消息 + 释放锁", () => {
    const { emit, bridge, transcript } = setup();
    bridge.handleFrame({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "x" }] }, sid: "s1" });
    bridge.handleFrame({ type: "done", data: { request_id: "r1" }, sid: "s1" });
    emit({ type: "error", data: { message: "boom" }, sid: "s1" });
    expect(entriesText(transcript, "s1")).toContain("s:err:boom");
  });

  it("disconnected → 按 sid 标记 + 清锁 + 清队列；无 sid → 全局 connected=false", () => {
    const { emit, bridge } = setup();
    emit({ type: "status", data: { connected: true }, sid: null });
    bridge.store.update((s) => ({ ...s, busyBySid: { ...s.busyBySid, s1: true }, ownStreamRidBySid: { ...s.ownStreamRidBySid, s1: "r1" } }));
    emit({ type: "disconnected", data: {}, sid: "s1" });
    let st = bridge.store.get();
    expect(st.disconnectedBySid["s1"]).toBe(true);
    expect(st.connected).toBe(true); // 后台会话断连不触发全局
    emit({ type: "disconnected", data: {}, sid: null });
    st = bridge.store.get();
    expect(st.connected).toBe(false);
  });

  it("未知事件类型静默忽略", () => {
    const { emit, transcript } = setup();
    emit({ type: "future_type", data: {}, sid: null });
    expect(entriesText(transcript)).toEqual([]);
  });

  it("sid 隔离：s1 的 done 不释放 s2 的锁", () => {
    const { emit, bridge } = setup();
    // 模拟 s1 流式 + s2 流式（P3：每会话独立 ownStreamRid）
    bridge.store.update((s) => ({ ...s, busyBySid: { ...s.busyBySid, s1: true, s2: true }, ownStreamRidBySid: { ...s.ownStreamRidBySid, s1: "r1", s2: "r2" } }));
    bridge.handleFrame({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "a" }] }, sid: "s1" });
    bridge.handleFrame({ type: "message_delta", data: { chunks: [{ request_id: "r2", content: "b" }] }, sid: "s2" });
    bridge.handleFrame({ type: "done", data: { request_id: "r1" }, sid: "s1" });
    const st = bridge.store.get();
    expect(st.busyBySid["s1"]).toBe(false);
    expect(st.busyBySid["s2"]).toBe(true); // s2 锁保留
    expect(st.ownStreamRidBySid["s2"]).toBe("r2"); // s2 的 rid 未被误清
  });
});
