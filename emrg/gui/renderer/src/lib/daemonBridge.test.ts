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

  it("done → transcript.handleDone + 该会话的 busy 收敛（daemon 的回合终局为准）", () => {
    const { emit, bridge } = setup();
    bridge.store.update((s) => ({ ...s, busyBySid: { ...s.busyBySid, s1: true } }));
    bridge.handleFrame({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "x" }] }, sid: "s1" });
    bridge.handleFrame({ type: "done", data: { request_id: "r1" }, sid: "s1" });
    expect(bridge.store.get().busyBySid["s1"]).toBe(false);
  });

  it("渲染状态里没有「谁的消息」这张表（rant 2026-09-29T15:52:49 要求 3）", () => {
    // 本连接在途的 request id 是连接内簿记，不是可渲染状态：它一旦回到 store，
    // 渲染器就能再据此给消息打「来自其他客户端」标——正是本条要求要拆掉的东西。
    const { bridge } = setup();
    expect("ownStreamRidBySid" in bridge.store.get()).toBe(false);
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

  it("cancelled → clearTyping + 释放锁（无 request_id 全清）+ 打中断那一行", () => {
    const { emit, bridge, transcript } = setup();
    bridge.handleFrame({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "partial" }] }, sid: "s1" });
    bridge.handleFrame({ type: "cancelled", data: {}, sid: "s1" });
    expect(bridge.store.get().busyBySid["s1"]).toBe(false);
    // Rant 2026-09-20T12:50:13：结束这轮的唯一陈述是这条回执。曾经这行由 Composer.stop()
    // 本地打（于是「按 Esc 的一端已中断，真正跑着这轮的另一端服务端毫无反应」）。
    expect(entriesText(transcript, "s1")).toContain("s:chat.interrupted");
  });

  it("cancelled 是会话级回执：旁观的一端也打那一行，且只打进它订阅的那个会话", () => {
    const { bridge, transcript } = setup();
    for (const sid of ["sA", "sB"]) {
      bridge.store.update((s) => ({ ...s, busyBySid: { ...s.busyBySid, [sid]: true } }));
    }
    // A 端发起的取消广播到所有订阅该会话的连接：这里扮演「看着 sA 的另一端」。
    bridge.handleFrame({ type: "cancelled", data: { type: "cancelled", session_id: "sA" }, sid: "sA" });
    expect(entriesText(transcript, "sA")).toContain("s:chat.interrupted");
    expect(bridge.store.get().busyBySid["sA"]).toBe(false);
    // 不在看 sA 的那个会话既不打字、也不被清 busy——它这轮还在跑。
    expect(entriesText(transcript, "sB")).not.toContain("s:chat.interrupted");
    expect(bridge.store.get().busyBySid["sB"]).toBe(true);
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

  it("生效的图片能力来自 daemon 的 pong / status / config_applied，且缺字段时不改口", () => {
    // Rant 2026-09-17T16:53:02：界面要显示 daemon 实际依据的 vision，而不是
    // config.toml 的声明值。三个报告点：pong（连接/心跳）、status（main 的
    // pong 广播）、config_applied（热重载帧）。缺字段 ≠ false：没有读数时保持
    // 上一个已知值，从未有过读数时为 null。
    const { emit, bridge } = setup();
    expect(bridge.store.get().vision).toBeNull();

    emit({ type: "pong", data: { identity: { instance_id: "sv1" }, model: "m1", vision: true }, sid: null });
    expect(bridge.store.get().vision).toBe(true);

    emit({ type: "status", data: { connected: true, model: "m1", vision: false }, sid: null });
    expect(bridge.store.get().vision).toBe(false);

    emit({ type: "config_applied", data: { model: "m2", vision: true, applied: ["vision"] }, sid: null });
    let st = bridge.store.get();
    expect(st.model).toBe("m2");
    expect(st.vision).toBe(true);

    // 不带 vision 的帧不动它（旧的 config_applied/model_set 载荷形态）
    emit({ type: "config_applied", data: { model: "m3", applied: ["max_tokens"] }, sid: null });
    st = bridge.store.get();
    expect(st.model).toBe("m3");
    expect(st.vision).toBe(true);
    emit({ type: "pong", data: { identity: { instance_id: "sv1" }, model: "m3" }, sid: null });
    expect(bridge.store.get().vision).toBe(true);
  });

  it("task_queued → 排队系统消息（sid 路由）", () => {
    const { emit, transcript } = setup();
    emit({ type: "task_queued", data: { position: 3 }, sid: "s2" });
    expect(entriesText(transcript, "s2")).toContain("s:queued:3");
  });

  it("turn_start → busy + turnStartBySid（epoch ms 换算，含后台/其他客户端 turn）", () => {
    const { emit, bridge } = setup();
    emit({ type: "turn_start", data: { started_at: 1756785600.5 }, sid: "s1" });
    const st = bridge.store.get();
    expect(st.busyBySid["s1"]).toBe(true);
    expect(st.turnStartBySid["s1"]).toBe(1756785600500);
  });

  it("turn_end → 清 busy + turnStartBySid（幂等，与 done 重复不炸）", () => {
    const { emit, bridge } = setup();
    emit({ type: "turn_start", data: { started_at: 1756785600.5 }, sid: "s1" });
    emit({ type: "turn_end", data: {}, sid: "s1" });
    let st = bridge.store.get();
    expect(st.busyBySid["s1"]).toBe(false);
    expect(st.turnStartBySid["s1"]).toBeUndefined();
    // 幂等：done 后再 turn_end 不抛错、状态保持
    emit({ type: "turn_end", data: {}, sid: "s1" });
    st = bridge.store.get();
    expect(st.busyBySid["s1"]).toBe(false);
    expect(st.turnStartBySid["s1"]).toBeUndefined();
  });

  it("resume_result 快照 → 中途打开一个已在跑的会话，计时从 daemon 的时刻起算（requirement 2）", () => {
    // rant 2026-09-27T18:41:52：`turn_start` 只在轮开始那一刻广播，中途打开的会话收不到它，
    // 所以快照里的 `meta.turn` 是唯一来源。这里的断言落在 store 上而不是渲染上：
    // 时刻必须是 daemon 给的那一个（1756785600.5s → 1756785600500ms），
    // 而不是「客户端见到它时」的 Date.now()。
    const { emit, bridge } = setup();
    emit({
      type: "command_result",
      data: { type: "resume_result", session_id: "s1", meta: { turn: { running: true, started_at: 1756785600.5 } } },
      sid: "s1",
    } as DaemonEventFrame);
    const st = bridge.store.get();
    expect(st.turnStartBySid["s1"]).toBe(1756785600500);
    expect(st.busyBySid["s1"]).toBe(true);
  });

  it("resume_result 快照说没有轮 → 清掉上一条连接留下的时刻（不能假装还在跑）", () => {
    const { emit, bridge } = setup();
    emit({ type: "turn_start", data: { started_at: 1756785600 }, sid: "s1" });
    expect(bridge.store.get().turnStartBySid["s1"]).toBe(1756785600000);
    emit({
      type: "command_result",
      data: { type: "resume_result", session_id: "s1", meta: { turn: { running: false, started_at: null } } },
      sid: "s1",
    } as DaemonEventFrame);
    const st = bridge.store.get();
    expect(st.turnStartBySid["s1"]).toBeUndefined();
    expect(st.busyBySid["s1"]).toBe(false);
  });

  it("command_result 里不是 resume_result 的帧 → 不碰轮状态（别把它当快照读）", () => {
    const { emit, bridge } = setup();
    emit({
      type: "command_result",
      data: { type: "model_set", meta: { turn: { running: true, started_at: 1756785600 } } },
      sid: "s1",
    } as DaemonEventFrame);
    expect(bridge.store.get().turnStartBySid["s1"]).toBeUndefined();
  });

  it("done / cancelled → 顺带清 turn 计时（幂等兜底）", () => {    const { emit, bridge } = setup();
    emit({ type: "turn_start", data: { started_at: 1756785600 }, sid: "s1" });
    emit({ type: "done", data: { request_id: "r1" }, sid: "s1" });
    let st = bridge.store.get();
    expect(st.turnStartBySid["s1"]).toBeUndefined();
    emit({ type: "turn_start", data: { started_at: 1756785601 }, sid: "s1" });
    emit({ type: "cancelled", data: {}, sid: "s1" });
    st = bridge.store.get();
    expect(st.busyBySid["s1"]).toBe(false);
    expect(st.turnStartBySid["s1"]).toBeUndefined();
  });

  it("error → 错误系统消息 + 释放锁", () => {
    // 载荷形状是**线上真实的那个**：daemon 的错误帧把文本放在 `error` 键
    // （`daemon_client.js` 的 `_emit("error", frame)` 原样转发）。这条测试曾经喂
    // `{message: "boom"}` —— 一个**没有任何产出者**的拼写，于是它一直绿着，而真实
    // 帧在界面上渲染成「出了点问题：」（后面什么都没有）。见下面两条腿。
    const { emit, bridge, transcript } = setup();
    bridge.handleFrame({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "x" }] }, sid: "s1" });
    bridge.handleFrame({ type: "done", data: { request_id: "r1" }, sid: "s1" });
    emit({ type: "error", data: { error: "boom" }, sid: "s1" });
    expect(entriesText(transcript, "s1")).toContain("s:err:boom");
  });

  it("error 帧的文本真的到达界面（线上键是 `error`，不是 `message`）（2026-10-02）", () => {
    // 一个键名两个拼写：产出者（daemon → daemon_client.js）写 `error`，读者
    // （本文件的 `case "error"`）读 `message`。`ErrorData` 声明了 `message`，于是
    // 类型检查通过、单测（喂 message）也通过 —— 只有真实链路是空的。
    const { emit, transcript } = setup();
    emit({ type: "error", data: { error: "Turn ended without reporting: Boom" }, sid: "s1" });
    const rows = entriesText(transcript, "s1");
    expect(rows).toContain("s:err:Turn ended without reporting: Boom");
    expect(rows).not.toContain("s:err:"); // 空文本那一格：缺陷的形状
  });

  it("点名了某一轮的 error 不是本连接的终局——不释放本端的锁（2026-10-02）", () => {
    // 工具循环的四处失败广播是 `{request_id, error}`，广播给会话**每个**订阅者。
    // 它说的是**那一轮**结束了，不是本连接这一轮。而 `case "error"` 原来无条件
    // `releaseOwnStream(sid, null, true)`：别人的 turn 失败会把本端还在跑的流解锁
    // （界面停止按钮消失、typing 收掉），而本端那一轮其实还在流式输出。
    const { emit, bridge } = setup();
    bridge.store.update((s) => ({ ...s, busyBySid: { ...s.busyBySid, s1: true } }));
    emit({ type: "error", data: { error: "Turn ended without reporting: Boom", request_id: "turn-1" }, sid: "s1" });
    expect(bridge.store.get().busyBySid["s1"]).toBe(true);
  });

  it("没有点名任何一轮的 error 仍然释放锁（控制腿：daemon 对本连接命令的直接答复）", () => {
    // 与上一条同一条规则的另一半：`{"error": "unknown message type"}` 这种无名无 type
    // 的帧只可能是 daemon 对本连接某条命令的直接答复（客户端侧的配对逻辑因此已经把它
    // 收窄到「既无 type 也无 request_id」）。两棵树都绿——它的价值由变异臂证明。
    const { emit, bridge } = setup();
    bridge.store.update((s) => ({ ...s, busyBySid: { ...s.busyBySid, s1: true } }));
    emit({ type: "error", data: { error: "unknown message type" }, sid: "s1" });
    expect(bridge.store.get().busyBySid["s1"]).toBe(false);
  });

  it("disconnected → 按 sid 标记 + 清锁 + 清队列；无 sid → 全局 connected=false", () => {
    const { emit, bridge } = setup();
    emit({ type: "status", data: { connected: true }, sid: null });
    bridge.store.update((s) => ({ ...s, busyBySid: { ...s.busyBySid, s1: true } }));
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
    // 模拟 s1 流式 + s2 流式（P3：每会话独立 busy 锁）
    bridge.store.update((s) => ({ ...s, busyBySid: { ...s.busyBySid, s1: true, s2: true } }));
    bridge.handleFrame({ type: "message_delta", data: { chunks: [{ request_id: "r1", content: "a" }] }, sid: "s1" });
    bridge.handleFrame({ type: "message_delta", data: { chunks: [{ request_id: "r2", content: "b" }] }, sid: "s2" });
    bridge.handleFrame({ type: "done", data: { request_id: "r1" }, sid: "s1" });
    const st = bridge.store.get();
    expect(st.busyBySid["s1"]).toBe(false);
    expect(st.busyBySid["s2"]).toBe(true); // s2 锁保留
  });

  it("applyInit 融合 init 返回 → store（connected/会话/model，vanilla boot 语义）", () => {
    const { bridge } = setup();
    bridge.applyInit({
      config_exists: true,
      api_key_configured: true,
      server_id: "inst-1",
      model: "gpt-4o",
      evolution_count: 42,
      current_version: "0.2.81",
      sessions: [{ session_id: "s1", title: "hello" }],
      open_sessions: [{ sid: "s1", projectName: "p" }],
    });
    let st = bridge.store.get();
    expect(st.connected).toBe(true);
    expect(st.serverId).toBe("inst-1");
    expect(st.model).toBe("gpt-4o");
    expect(st.evolutionCount).toBe(42);
    expect(st.currentVersion).toBe("0.2.81");
    expect(st.sessions).toHaveLength(1);
    expect(st.openSessions).toHaveLength(1);
  });

  it("applyInit 在 config/key 缺失时保持 connected=false（未配置降级，不崩）", () => {
    const { bridge } = setup();
    bridge.applyInit({ config_exists: false, api_key_configured: false });
    const st = bridge.store.get();
    expect(st.connected).toBe(false);
    expect(st.sessions).toHaveLength(0);
  });

  it("upgrade → store.upgradeBanner；同 installed 心跳重发不重建（dedupe）", () => {
    const { emit, bridge } = setup();
    emit({ type: "upgrade", data: { current_version: "0.2.83", installed_version: "0.2.84" } });
    expect(bridge.store.get().upgradeBanner).toEqual({ current: "0.2.83", installed: "0.2.84" });
    // 心跳每 15s 重发同一版本 → 引用不变（Object.is no-op，不触发 re-render）
    const before = bridge.store.get();
    emit({ type: "upgrade", data: { current_version: "0.2.83", installed_version: "0.2.84" } });
    expect(bridge.store.get()).toBe(before);
    // 新 installed 版本 → 更新
    emit({ type: "upgrade", data: { current_version: "0.2.84", installed_version: "0.2.85" } });
    expect(bridge.store.get().upgradeBanner).toEqual({ current: "0.2.84", installed: "0.2.85" });
    // 空 installed → 忽略（dev 运行无版本数据）
    emit({ type: "upgrade", data: { current_version: "0.2.85", installed_version: "" } });
    expect(bridge.store.get().upgradeBanner).toEqual({ current: "0.2.84", installed: "0.2.85" });
  });

  // Rant 2026-09-29T15:52:38.987951+08:00 后续：提问被 daemon 单方面终结（拒绝/超时），
  // 客户端必须据此关闭对话框——否则弹窗在提问早已结束后仍宣称"已答复"。
  it("approval_request → store.pendingApproval（提问承载问题与会话）", () => {
    const { emit, bridge } = setup();
    emit({ type: "approval_request", data: { request_id: "appr-1", question: "widen?" }, sid: "s1" });
    expect(bridge.store.get().pendingApproval).toEqual({
      requestId: "appr-1", question: "widen?", sessionId: "s1",
    });
  });

  it("approval_resolved → pendingApproval 清空（超时也算一种结局）", () => {
    const { emit, bridge } = setup();
    emit({ type: "approval_request", data: { request_id: "appr-2", question: "widen?" }, sid: "s1" });
    emit({ type: "approval_resolved", data: { request_id: "appr-2", outcome: "timed_out" }, sid: "s1" });
    expect(bridge.store.get().pendingApproval).toBeNull();
  });

  it("approval_resolved 只关掉它命名的那一问（别问的解析帧不得关本窗）", () => {
    const { emit, bridge } = setup();
    emit({ type: "approval_request", data: { request_id: "appr-3", question: "widen?" }, sid: "s1" });
    emit({ type: "approval_resolved", data: { request_id: "appr-OTHER", outcome: "refused" }, sid: "s1" });
    expect(bridge.store.get().pendingApproval?.requestId).toBe("appr-3");
    emit({ type: "approval_resolved", data: { request_id: "appr-3", outcome: "refused" }, sid: "s1" });
    expect(bridge.store.get().pendingApproval).toBeNull();
  });

  it("approval_resolved 的第三种结局（随轮取消）同样关窗——结局名不参与判定", () => {
    // The daemon announces a `cancelled` ending too, because ESC cancels the
    // handle waiting on the answer. Which word a resolution carries must not
    // decide whether the dialog closes: the two clients only need to know that
    // the question the frame names is over. A future outcome therefore cannot
    // reintroduce the stuck dialog this fix removes.
    const { emit, bridge } = setup();
    emit({ type: "approval_request", data: { request_id: "appr-4", question: "widen?" }, sid: "s1" });
    emit({ type: "approval_resolved", data: { request_id: "appr-4", outcome: "cancelled" }, sid: "s1" });
    expect(bridge.store.get().pendingApproval).toBeNull();
  });

  // Issue #1757 的要求 2，落到渲染器一侧：客户端必须能自己收尾。TUI 的自有期限
  // （`approval_question_is_still_live`）是同一规则的另一半；GUI 从前只在收到帧时
  // 关窗，于是一旦帧没到（连接断、daemon 死在提问中途、写入被打断）对话框就永远留着，
  // 而 i18n 早就写着「超时也算拒绝」。期限来自 daemon 的 `timeout_seconds`，客户端
  // 不自造数字。
  it("提问自带期限：帧没来也会到点关窗（GUI 不再无限持有）", () => {
    vi.useFakeTimers();
    try {
      const { emit, bridge } = setup();
      emit({
        type: "approval_request",
        data: { request_id: "appr-5", question: "widen?", timeout_seconds: 120 },
        sid: "s1",
      });
      expect(bridge.store.get().pendingApproval?.requestId).toBe("appr-5");
      vi.advanceTimersByTime(119_000);
      expect(bridge.store.get().pendingApproval?.requestId).toBe("appr-5");
      vi.advanceTimersByTime(1_000);
      expect(bridge.store.get().pendingApproval).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("帧先到就不留计时器：它不得在之后关掉别的一问", () => {
    vi.useFakeTimers();
    try {
      const { emit, bridge } = setup();
      emit({
        type: "approval_request",
        data: { request_id: "appr-6", question: "first?", timeout_seconds: 120 },
        sid: "s1",
      });
      emit({ type: "approval_resolved", data: { request_id: "appr-6", outcome: "approved" }, sid: "s1" });
      emit({
        type: "approval_request",
        data: { request_id: "appr-7", question: "second?", timeout_seconds: 300 },
        sid: "s1",
      });
      // 越过第一问原本的期限：它已经结束，不能顺手关掉第二问。
      vi.advanceTimersByTime(120_000);
      expect(bridge.store.get().pendingApproval?.requestId).toBe("appr-7");
      vi.advanceTimersByTime(180_000);
      expect(bridge.store.get().pendingApproval).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("daemon 没声明期限就不计时（客户端不自造数字）", () => {
    vi.useFakeTimers();
    try {
      const { emit, bridge } = setup();
      emit({ type: "approval_request", data: { request_id: "appr-8", question: "widen?" }, sid: "s1" });
      vi.advanceTimersByTime(10 * 60_000);
      expect(bridge.store.get().pendingApproval?.requestId).toBe("appr-8");
    } finally {
      vi.useRealTimers();
    }
  });

  it("host 自己答复后不留计时器（问已结束）", async () => {
    vi.useFakeTimers();
    try {
      const { emit, bridge } = setup();
      emit({
        type: "approval_request",
        data: { request_id: "appr-9", question: "widen?", timeout_seconds: 120 },
        sid: "s1",
      });
      await bridge.respondApproval(true);
      emit({
        type: "approval_request",
        data: { request_id: "appr-10", question: "next?", timeout_seconds: 300 },
        sid: "s1",
      });
      // 越过第一问原本的期限：它已被 host 答复，不能顺手关掉第二问。
      vi.advanceTimersByTime(120_000);
      expect(bridge.store.get().pendingApproval?.requestId).toBe("appr-10");
      vi.advanceTimersByTime(180_000);
      expect(bridge.store.get().pendingApproval).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
