/**
 * daemonBridge.ts — daemon 事件桥（Batch 5 slice 1：final switch 的地基）。
 * 源：vanilla renderer/js/app.js App.handleEvent（1411-1600）+ Chat.handleDelta 等。
 *
 * 职责：订阅 `window.emrg.onEvent`，按事件类型把帧路由到：
 * - TranscriptStore（聊天区状态机，transcript.ts 已具备全部 handler）
 * - DaemonAppStore（会话列表/打开会话/连接状态/serverId/model/evolutionCount）
 * 严格按 sid 路由（#977 教训：会话串线根因 = 无 sid 隔离；P3 后每帧带 sid）。
 *
 * 纯逻辑、零 DOM：注入 onEvent/emrg/transcript/t，可脱离 Electron 单测。
 * Shell 接线（Batch 5 后续 slice）消费 store 渲染；本模块不碰 window.emrg 默认值
 * （测试/接线层负责注入）。
 */

import type { TranslateFn } from "./utils";
import { createSnapshotStore, type SnapshotStore } from "./snapshot-store";
import type { TranscriptStore, DeltaChunk, DoneData, ToolStartData, ToolEndData } from "./transcript";
import type { OpenSessionEntry } from "./sidebar";
import type { ImageAttach } from "./composer";

/* ── 事件帧（与 main.js 广播契约一致：{ type, data, sid }） ── */

/** sessions 事件载荷（daemon sessions_list → main 重组） */
export interface SessionsData {
  sessions?: SessionSummary[];
}
export interface SessionSummary {
  session_id: string;
  title?: string;
  [k: string]: unknown;
}

/** status 事件载荷（main 连接状态广播） */
export interface StatusData {
  connected?: boolean;
  server_id?: string;
  model?: string;
  /** 生效的图片能力（daemon pong 报的值，不是 config.toml 的声明值） */
  vision?: boolean | null;
  current_version?: string;
  evolution_count?: number | null;
  auth_failed?: boolean;
  reconnecting?: boolean;
  installing?: boolean;
  /**
   * The main process gave up on re-spawning the daemon and says so once.
   *
   * It travels with `error`, which on this path is **the tail of `emrgd.log`**
   * (`daemon_client.js`'s `startDaemon` builds the message and includes it) —
   * so the sentence is the only thing that tells a host *why* the daemon is
   * gone. Both keys went unread here after #1024 deleted the vanilla renderer
   * that printed them (`Chat.addSystemMessage(_t("app.daemonStopped", …))`), and
   * the React renderer that replaced it kept the dictionary entry and dropped
   * the reader: the GUI said "Disconnected" and nothing else (measured
   * 2026-10-02).
   */
  daemon_stopped?: boolean;
  /** Why the connection is down: the `emrgd.log` tail on a spawn failure, the
   *  auth error otherwise. Paired with whichever flag above is set. */
  error?: string;
}

/** pong 事件载荷（daemon 心跳） */
export interface PongData {
  identity?: { instance_id?: string };
  model?: string;
  /** 生效的图片能力（rant 2026-09-17T16:53:02：pong / model_set / config_applied 都报它） */
  vision?: boolean;
  evolution_count?: number | null;
}

/** 队列注入协议帧（#655：busy 排队） */
export interface QueuedData {
  position?: number;
  request_id?: string;
  request_ids?: string[];
}
export interface ErrorData {
  message?: string;
}

/** upgrade 事件载荷（main.js 心跳检测 installed_version ≠ current_version → 专用事件） */
export interface UpgradeData {
  current_version?: string;
  installed_version?: string;
}

/** config_applied 事件载荷（daemon 热重载广播，issue #1380/#1387） */
export interface ConfigAppliedData {
  model?: string;
  /** 重载后的生效值；`applied` 是这次真的动了的键 */
  vision?: boolean;
  applied?: string[];
}

/** 统一事件帧（onEvent 回调入参） */
export interface DaemonEventFrame {
  type: string;
  data: Record<string, unknown> & { chunks?: DeltaChunk[] } & Partial<DoneData & ToolStartData & ToolEndData & StatusData & PongData & ConfigAppliedData & SessionsData & QueuedData & ErrorData & UpgradeData>;
  sid?: string | null;
}

/* ── App 级状态 store（桥维护，Shell 消费） ── */

export interface DaemonAppState {
  /** 连接状态（status.connected / disconnected 事件驱动） */
  connected: boolean;
  reconnecting: boolean;
  installing: boolean;
  /**
   * Why the connection is down, when the main process said why — or null, which
   * is also the state after a `connected: true` frame retires it. The notice
   * describes *this* outage, not a property of the app.
   *
   * A variant carries the surface's whole input: `daemon-stopped` owns the
   * `emrgd.log` tail its sentence interpolates, `auth` needs no detail because
   * its sentence says what to do about it. The shape is a union rather than two
   * flags plus a string because those three can disagree (both set, or a reason
   * left over from the outage before) — and the vanilla renderer's reader for
   * them was deleted with it in #1024, so nothing has been reading the flags
   * since (measured 2026-10-02).
   */
  connectionFailure:
    | { kind: "auth" }
    | { kind: "daemon-stopped"; detail: string }
    | null;
  serverId: string;
  model: string;
  /**
   * The daemon's **effective** image capability, or null when no frame has said.
   *
   * Three states, and the null is not padding (rant 2026-09-17T16:53:02): `true` /
   * `false` are what the daemon reported it acts on, `null` means nothing has been
   * reported yet — so a surface can say "images: yes/no" instead of claiming a
   * capability from `config.toml`'s declaration, which is a *different* value
   * (the running daemon resolved the entry-key → top-level-default priority at
   * startup, and both a `/model` switch and an `[llm] vision` reload move it).
   */
  vision: boolean | null;
  currentVersion: string;
  evolutionCount: number | null;
  sessions: SessionSummary[];
  openSessions: OpenSessionEntry[];
  /** 每会话 busy 锁（P3 slice 1：done/cancelled 按 sid 释放，不误清激活会话） */
  busyBySid: Record<string, boolean>;
  /** 每会话 turn 开始时刻（epoch ms；rant 2026-09-02T10:36:26 daemon turn_start 权威广播） */
  turnStartBySid: Record<string, number>;
  /** 每会话断线标记（P3 finalize：后台会话断线不触发全局 UI） */
  disconnectedBySid: Record<string, boolean>;
  /** upgrade 事件（心跳检测 installed ≠ current → "重启生效"横幅；null=无待重启提示） */
  upgradeBanner: { current: string; installed: string } | null;
  /**
   * The daemon's open approval question, or null when it is not asking one.
   *
   * The daemon is *blocked* on this: a confined command it was asked to run
   * waits for an answer, and no answer at all is a refusal (rant
   * 2026-09-29T15:52:38.987951+08:00, requirement 1). So the frame is not a
   * notification to decorate the transcript with — it is a state the GUI has to
   * surface, which is why it lives in the store rather than in a local ref.
   */
  pendingApproval: PendingApproval | null;
}

/** `approval_request` 帧的载荷（daemon `request_approval`）。 */
export interface PendingApproval {
  requestId: string;
  question: string;
  /** 发出提问的会话；回答要写回同一条连接。 */
  sessionId: string | null;
}

const SID_NULL = "__emrg_null_sid__";
const KEY = (sid?: string | null): string => sid || SID_NULL;

/** `resume_result` 帧的载荷（daemon `_handle_resume_session` → `meta.turn`）。 */
export interface ResumeResultData {
  type?: string;
  session_id?: string;
  meta?: { turn?: { running?: boolean; started_at?: number | null } | null } | null;
}

/**
 * `resume_result` 快照说「这个会话的轮正在跑」时，那个轮的开始时刻（epoch ms）。
 *
 * requirement 1 给了 `meta["turn"]` 会话的轮状态（`{"running": bool, "started_at": epoch}`）
 * ——事实归 daemon，客户端只渲染它。打开一个轮已经在跑的会话，必须显示它**已经**跑了多久：
 * 所以这里返回一个**时刻**而不是时长，由读的人拿同一个时钟相减。TUI 侧的同一条规则是
 * `resume_turn_instant`（`emrg/client/app.py`），两个客户端刻意用同一个判据，免得漂移。
 *
 * `null` 覆盖所有「没什么可渲染」的形状：没有快照、会话没有轮、running 但没有可用时刻。
 */
export function resumeTurnInstantMs(meta: unknown): number | null {
  const turn = (meta as { turn?: unknown } | null | undefined)?.turn;
  if (typeof turn !== "object" || turn === null) return null;
  const t = turn as { running?: unknown; started_at?: unknown };
  if (t.running !== true) return null;
  const started = t.started_at;
  if (typeof started !== "number" || !(started > 0)) return null;
  return started * 1000;
}

export function createDaemonAppStore(): SnapshotStore<DaemonAppState> {
  return createSnapshotStore<DaemonAppState>({
    connected: false,
    reconnecting: false,
    installing: false,
    connectionFailure: null,
    serverId: "",
    model: "",
    // Nothing has reported it yet — not `false`, which would be a claim about the
    // daemon's capability made before any frame arrived (see `DaemonAppState.vision`).
    vision: null,
    currentVersion: "",
    evolutionCount: null,
    sessions: [],
    openSessions: [],
    busyBySid: {},
    turnStartBySid: {},
    disconnectedBySid: {},
    upgradeBanner: null,
    pendingApproval: null,
  });
}

/* ── 桥 ── */

export interface SendMessagePayload {
  sessionId: string | null;
  text: string;
  requestId?: string;
  sandbox?: string;
  /** 图片附件（rant 2026-09-02T15:23:53：透传 sendTask → daemon vision） */
  images?: ImageAttach[] | null;
}

/** window.emrg.init() 返回值（main.js emrg:init 处理器 → preload.init） */
export interface InitResult {
  config_exists?: boolean;
  api_key_configured?: boolean;
  /** 生效的图片能力（main.js 从 pong 透传；布尔才是读数，缺失=未知） */
  vision?: boolean | null;
  server_id?: string;
  model?: string;
  evolution_count?: number | null;
  current_version?: string;
  version?: string;
  sessions?: SessionSummary[];
  open_sessions?: OpenSessionEntry[];
  active_sid?: string | null;
}

/** 注入依赖（onEvent/emrg 不设默认，接线层显式传入） */
export interface DaemonBridgeDeps {
  onEvent: (cb: (evt: DaemonEventFrame) => void) => () => void;
  emrg: {
    sendMessage(p: SendMessagePayload): Promise<{ requestId?: string }>;
    init?(): Promise<InitResult>;
    /** 回答 daemon 的提权提问（preload.respondApproval；未接线时为 undefined） */
    respondApproval?(p: {
      sessionId: string;
      requestId: string;
      approved: boolean;
    }): Promise<{ ok?: boolean }>;
  };
  transcript: TranscriptStore;
  t?: TranslateFn;
}

export interface DaemonBridge {
  /** App 级状态（sessions/连接/openSessions/每会话锁） */
  store: SnapshotStore<DaemonAppState>;
  /** 取消订阅（组件卸载/断连清理时调用） */
  dispose(): void;
  /** 供接线层手动投递（测试/重放用） */
  handleFrame(frame: DaemonEventFrame): void;
  /** 把 window.emrg.init() 返回值融合进 store（对齐 vanilla boot 语义） */
  applyInit(result: InitResult): void;
  /**
   * Answer the daemon's open approval question, or do nothing when it is not
   * asking one.
   *
   * Clearing the store entry **first** is deliberate: a question that has been
   * answered must disappear even if the send itself fails, because a prompt that
   * stays on screen is one the host will answer twice — and the daemon's channel
   * takes the first answer only.
   */
  respondApproval(approved: boolean): Promise<boolean>;
}

export function createDaemonBridge(deps: DaemonBridgeDeps): DaemonBridge {
  const { onEvent, emrg, transcript, t } = deps;
  const tt: TranslateFn = t ?? ((key: string): string => key);
  const store = createDaemonAppStore();

  // P2 queue-injection（#655）：busy 时发送的消息入 daemon 队列，queued_requeue
  // 以原 requestId 重发（不重加用户行）。逐 sid 记录（后台会话独立跟踪）。
  const queuedSends = new Map<string, { requestId: string; text: string; sandbox?: string; images?: ImageAttach[] | null }[]>();

  function sidBusy(sid: string | null, busy: boolean): void {
    const k = KEY(sid);
    store.update((s) => ({ ...s, busyBySid: { ...s.busyBySid, [k]: busy } }));
  }

  /** 清除该会话的 turn 计时（turn_end/done/cancelled/disconnected 幂等调用）。 */
  function clearTurnTimer(sid: string | null): void {
    const k = KEY(sid);
    const { [k]: _drop, ...rest } = store.get().turnStartBySid;
    if (_drop !== undefined || store.get().busyBySid[k]) {
      store.update((s) => ({
        ...s,
        busyBySid: { ...s.busyBySid, [k]: false },
        turnStartBySid: rest,
      }));
    }
  }
  /**
   * Per-session request id **this connection** has in flight (the requeue path).
   *
   * Connection-local on purpose, and not part of `DaemonAppState` (rant 2026-09-29T15:52:49
   * requirement 3). It answers "when does this connection's stream end", which the lock
   * needs; it does not answer "whose message is this", which no client may decide. The
   * table used to live in the store, where the renderer read it to tag a message as
   * "from another client" — a per-client view of a session, when the daemon is the only
   * state source and a client is only a renderer. Nothing renders it now.
   */
  const inflightRidBySid = new Map<string, string | null>();

  /**
   * The bound on the question on screen: the timer and the request it belongs to.
   *
   * Issue #1757. `pendingApproval` used to be cleared only by a frame, so the
   * dialog outlived the question whenever the frame did not arrive — a dropped
   * connection, a daemon that died mid-question, a resolution whose write was
   * interrupted. The TUI's own bound is its half of the same rule
   * (`approval_question_is_still_live`, `emrg/client/app.py`); this is the
   * renderer's, and it is deliberately the *last* thing to close the dialog: a
   * frame that arrives first clears it and cancels the timer.
   *
   * The instant is the daemon's, never this client's — the value rides the
   * request frame (`timeout_seconds`). A frame that declares none arms nothing,
   * because a client that cannot measure the bound must not invent one: guessing
   * would either close a live question early or claim a deadline the daemon
   * never set.
   */
  let approvalBound: { requestId: string; handle: ReturnType<typeof setTimeout> } | null = null;

  function clearApprovalBound(): void {
    if (approvalBound) {
      clearTimeout(approvalBound.handle);
      approvalBound = null;
    }
  }

  function armApprovalBound(requestId: string, timeoutSeconds: unknown): void {
    clearApprovalBound();
    const seconds =
      typeof timeoutSeconds === "number" && Number.isFinite(timeoutSeconds) && timeoutSeconds > 0
        ? timeoutSeconds
        : null;
    if (seconds === null) return;
    approvalBound = {
      requestId,
      handle: setTimeout(() => {
        approvalBound = null;
        // Only the question this timer was armed for: a later question has its
        // own timer, and an earlier one must not dismiss it.
        const current = store.get().pendingApproval;
        if (current && current.requestId === requestId) {
          store.update((s) => ({ ...s, pendingApproval: null }));
        }
      }, seconds * 1000),
    };
  }

  function setInflightRid(sid: string | null, rid: string | null): void {
    inflightRidBySid.set(KEY(sid), rid);
  }
  function sidDisconnected(sid: string | null, v: boolean): void {
    const k = KEY(sid);
    store.update((s) => ({ ...s, disconnectedBySid: { ...s.disconnectedBySid, [k]: v } }));
  }

  /**
   * One session's connection is back, so its banner has to go.
   *
   * A disconnection ends when something travels over the connection again, and
   * the GUI's own re-open is that something: `conn-manager.js` `open()` sends
   * `resume_session` and its `resume_result` arrives on the freshly established
   * connection (forwarded as `command_result`). Without this the mark was
   * written and never read back — `sidDisconnected(sid, true)` sat in the
   * `disconnected` case and no call ever passed `false`, so the session banner
   * (`app.sessionDisconnected`) stayed up for the rest of the run and told the
   * host that a live session was about to reconnect.
   */
  function sessionConnectionBack(sid: string | null): void {
    const k = KEY(sid);
    // A session that was never marked gets no entry: this writes a state, not an
    // outage, and inventing a row per `resume_result` would turn the map into a
    // list of sessions instead of a list of disconnections.
    if (!store.get().disconnectedBySid[k]) return;
    sidDisconnected(sid, false);
  }

  /**
   * Every session is connected again — the daemon-level connection is up and the
   * GUI has re-opened and re-subscribed everything it had open (`main.js`
   * `onRecovered`, which sends `status { connected: true }` last).
   *
   * The vanilla renderer's rule, kept whole (`js/app.js:1608`, "重连成功 → 清全部
   * 会话断线标记"): it is one connection that carried them all, so they end
   * together. Pure, so the caller can fold it into the same store update the
   * status frame already produces.
   */
  function everySessionConnected(map: Record<string, boolean>): Record<string, boolean> {
    const next: Record<string, boolean> = {};
    for (const k of Object.keys(map)) next[k] = false;
    return next;
  }

  /** done/cancelled 释放该事件所属会话的锁（vanilla：仅当 request 匹配或 timeout） */
  function releaseOwnStream(sid: string | null, requestId?: string | null, force = false): void {
    const rid = inflightRidBySid.get(KEY(sid)) ?? null;
    if (force || (requestId && (rid === requestId))) {
      sidBusy(sid, false);
      setInflightRid(sid, null);
    }
  }

  /** 按 sid 重发排队消息（vanilla queued_requeue 协议，含 wasBusy 收敛修正 #695） */
  async function resendQueued(sid: string | null, ids: Set<string>): Promise<void> {
    const k = KEY(sid);
    const q = queuedSends.get(k);
    if (!q || q.length === 0) return;
    const toResend = q.filter((e) => ids.has(e.requestId));
    const remaining = q.filter((e) => !ids.has(e.requestId));
    if (toResend.length) {
      const wasBusy = store.get().busyBySid[k] ?? false;
      for (let i = 0; i < toResend.length; i++) {
        const item = toResend[i];
        sidBusy(sid, true);
        setInflightRid(sid, item.requestId);
        try {
          const res = await emrg.sendMessage({
            sessionId: sid,
            text: item.text,
            requestId: item.requestId,
            sandbox: item.sandbox,
            ...(item.images ? { images: item.images } : {}),
          });
          setInflightRid(sid, res?.requestId ?? item.requestId);
        } catch {
          sidBusy(sid, false);
          setInflightRid(sid, null);
        }
        if (wasBusy || i > 0) {
          remaining.push(item);
        }
      }
      if (remaining.length) queuedSends.set(k, remaining);
      else queuedSends.delete(k);
      transcript.addSystemMessage(tt("app.queuedResent", { n: toResend.length }), sid);
    }
  }

  function handleFrame(frame: DaemonEventFrame): void {
    const { type, data } = frame;
    const sid = frame.sid ?? null;
    switch (type) {
      case "command_result": {
        // 打开会话时 daemon 的轮状态快照（requirement 1：`resume_result` 的 `meta.turn`）。
        // `turn_start` 只在轮**开始的那一刻**广播，所以一个中途打开的会话永远收不到它 ——
        // 没有这一支，中途打开就只能从 00:00 起算（rant 2026-09-27T18:41:52 requirement 2）。
        // 快照说「没有轮在跑」时也要**清**：上一条连接留下的时刻不能假装这一轮还在跑，
        // 这正是 TUI 那一半已经做过的事（`emrg/client/app.py`，stage「requirement 3 TUI half」）。
        const resume = data as ResumeResultData;
        if (resume?.type !== "resume_result") break;
        // The frame arrived on this session's connection, which means that
        // connection exists — the banner it left behind has to go with it.
        sessionConnectionBack(resume.session_id ?? sid);
        const k = KEY(resume.session_id ?? sid);
        const startedMs = resumeTurnInstantMs(resume.meta);
        const { [k]: _drop, ...rest } = store.get().turnStartBySid;
        store.update((s) => ({
          ...s,
          turnStartBySid: startedMs === null ? rest : { ...s.turnStartBySid, [k]: startedMs },
          busyBySid: { ...s.busyBySid, [k]: startedMs !== null },
        }));
        break;
      }
      case "turn_start": {
        // Rant 2026-09-02T10:36:26：daemon 权威 turn 开始（含后台演化/其他客户端
        // turn）——记 start 时刻并置 busy；计时基准与 TUI 同一来源。
        const startedAt = (data as { started_at?: number }).started_at;
        const k = KEY(sid);
        if (typeof startedAt === "number" && startedAt > 0) {
          store.update((s) => ({
            ...s,
            busyBySid: { ...s.busyBySid, [k]: true },
            turnStartBySid: { ...s.turnStartBySid, [k]: startedAt * 1000 },
          }));
        }
        break;
      }
      case "turn_end": {
        // daemon 权威 turn 结束——清 busy + 计时（与 done/cancelled 幂等）。
        const k = KEY(sid);
        const { [k]: _drop, ...rest } = store.get().turnStartBySid;
        store.update((s) => ({
          ...s,
          busyBySid: { ...s.busyBySid, [k]: false },
          turnStartBySid: rest,
        }));
        break;
      }
      case "message_delta":
        transcript.handleDelta(data.chunks || [data], sid);
        break;
      case "done":
        transcript.handleDone(data as DoneData, sid);
        releaseOwnStream(sid, (data as DoneData).request_id, Boolean((data as DoneData).timeout));
        clearTurnTimer(sid);
        break;
      case "tool_started":
        transcript.handleToolStart(data as ToolStartData, sid);
        break;
      case "tool_finished":
        transcript.handleToolEnd(data as ToolEndData, sid);
        break;
      case "cancelled":
        // 结束这一轮的唯一陈述（rant 2026-09-20T12:50:13）。daemon 的 cancelled 是
        // **会话级回执**，广播给订阅该会话的每个客户端，所以问的一方与旁观的一方拿到的
        // 是同一条——曾经这条语句在 Composer.stop() 里本地生成（清 typing + 打一行 +
        // 置 busy=false），于是「按 Esc 的一端点停止、真正跑着这轮的另一端服务端毫无反应」
        // 而两边都显示已中断。本地那套已删除，保留这里一处。
        transcript.clearTyping(sid);
        transcript.addSystemMessage(tt("chat.interrupted"), sid);
        releaseOwnStream(sid, null, true);
        clearTurnTimer(sid);
        break;
      case "task_queued":
        transcript.addSystemMessage(tt("app.queued", { pos: (data as QueuedData).position ?? 0 }), sid);
        break;
      case "steer_committed": {
        // 已注入当前回合 → 从待重发记录移除
        const k = KEY(sid);
        const q = queuedSends.get(k);
        const rid = (data as QueuedData).request_id;
        if (q && rid) {
          const idx = q.findIndex((e) => e.requestId === rid);
          if (idx >= 0) q.splice(idx, 1);
          if (q.length === 0) queuedSends.delete(k);
        }
        break;
      }
      case "queued_requeue": {
        const ids = new Set((data as QueuedData).request_ids || []);
        void resendQueued(sid, ids);
        break;
      }
      case "queued_cancelled":
        if (queuedSends.delete(KEY(sid))) {
          transcript.addSystemMessage(tt("app.queuedCancelled"), sid);
        }
        break;
      case "error":
        transcript.addSystemMessage(tt("app.error", { msg: (data as ErrorData).message ?? "" }), sid);
        releaseOwnStream(sid, null, true);
        break;
      case "pong": {
        const pong = data as PongData;
        store.update((s) => ({
          ...s,
          serverId: pong.identity?.instance_id || s.serverId,
          model: pong.model || s.model,
          // A pong that carries no boolean says nothing about the capability, so the
          // last reported value stands rather than being reset to unknown.
          vision: typeof pong.vision === "boolean" ? pong.vision : s.vision,
          evolutionCount: pong.evolution_count ?? s.evolutionCount,
        }));
        break;
      }
      case "config_applied": {
        // daemon 热重载广播（issue #1374/#1380/#1387）：改 `[llm] vision` 或 model
        // 后，客户端只会从 pong / model_set 学到新值，而重载既不是两者之一 —— 这个
        // 帧就是为"客户端展示的字段动了"而发的。与 pong 同语义：非布尔不动。
        const ca = data as ConfigAppliedData;
        store.update((s) => ({
          ...s,
          model: ca.model || s.model,
          vision: typeof ca.vision === "boolean" ? ca.vision : s.vision,
        }));
        break;
      }
      case "status": {
        const st = data as StatusData;
        // This is the port of the reader #1024 deleted along with the vanilla
        // renderer (`js/app.js:1604`), read in its order: `connected` retires
        // the notice, then `auth_failed`, then `daemon_stopped`. The two
        // producers are mutually exclusive anyway (`main.js` `ensureConnected`
        // sends exactly one per frame), so the order is fidelity rather than
        // precedence — and a port sharing it is one a reviewer can diff against
        // the original. What is *new* is the last arm: vanilla fell through to a
        // red dot, which is the same rule — a frame naming no cause does not
        // clear the cause. Both `reconnecting` (every backoff step) and
        // `installing` are sent mid-outage and name none.
        store.update((s) => {
          const failure: DaemonAppState["connectionFailure"] =
            st.connected === true
              ? null
              : st.auth_failed === true
                ? { kind: "auth" }
                : st.daemon_stopped === true
                  ? { kind: "daemon-stopped", detail: st.error ?? "" }
                  : // Neither named: the notice stands. The previous value is
                    // read here, where the previous state lives.
                    s.connectionFailure;
          return {
            ...s,
            connected: st.connected ?? s.connected,
            reconnecting: st.reconnecting ?? s.reconnecting,
            installing: st.installing ?? s.installing,
            connectionFailure: failure,
            // A connection that came back is what ends every session's outage:
            // `connected: true` is sent by `main.js` after the GUI re-opened and
            // re-subscribed what it had open, so every mark it set is about the
            // connection that now exists again.
            disconnectedBySid:
              st.connected === true ? everySessionConnected(s.disconnectedBySid) : s.disconnectedBySid,
            serverId: st.server_id || s.serverId,
            model: st.model || s.model,
            vision: typeof st.vision === "boolean" ? st.vision : s.vision,
            currentVersion: st.current_version || s.currentVersion,
          };
        });
        break;
      }
      case "sessions": {
        const sd = data as SessionsData;
        store.update((s) => ({ ...s, sessions: sd.sessions || [] }));
        break;
      }
      case "open_sessions": {
        const os = data as { openSessions?: OpenSessionEntry[] };
        store.update((s) => ({ ...s, openSessions: os.openSessions || [] }));
        break;
      }
      case "disconnected": {
        sidBusy(sid, false);
        setInflightRid(sid, null);
        sidDisconnected(sid, true);
        clearTurnTimer(sid);
        queuedSends.delete(KEY(sid));
        if (!sid) {
          store.update((s) => ({ ...s, connected: false }));
        }
        break;
      }
      case "approval_request": {
        // Rant 2026-09-29T15:52:38.987951+08:00, requirement 1: the daemon asks a
        // client before it widens a confined call, and a client that stays silent
        // is a refusal. The frame carries the question, so it is stored whole —
        // and `sid` is kept beside it because the answer has to travel back on the
        // same session's connection.
        const ap = data as { request_id?: string; question?: string; timeout_seconds?: number };
        const requestId = String(ap?.request_id || "");
        if (requestId) {
          store.update((s) => ({
            ...s,
            pendingApproval: {
              requestId,
              question: String(ap?.question || ""),
              sessionId: sid,
            },
          }));
          // The daemon declares how long it will wait; this dialog is bounded by
          // that number, not by the arrival of a frame that may never come
          // (issue #1757: "no timer, no dismissal" is the defect).
          armApprovalBound(requestId, ap?.timeout_seconds);
        }
        break;
      }
      case "approval_resolved": {
        // Rant 2026-09-29T15:52:38.987951+08:00 follow-up: the daemon refuses a
        // confined call at its own timeout and says so here. Without this the
        // dialog outlived the question — it stayed up reporting an answer nobody
        // accepted, while i18n already promised "a timeout counts as a denial".
        // Only a frame naming the question on screen closes it: a resolved
        // request from another session must not dismiss this one.
        const ar = data as { request_id?: string };
        const resolvedId = String(ar?.request_id || "");
        const current = store.get().pendingApproval;
        if (current && resolvedId && current.requestId === resolvedId) {
          // The frame closed it first, so its timer must not fire later: it is
          // armed for a request id this store no longer holds, but a timer that
          // outlives its question is a leak with a state write attached.
          clearApprovalBound();
          store.update((s) => ({ ...s, pendingApproval: null }));
        }
        break;
      }
      case "upgrade": {
        // 心跳每 15s 检测到 installed ≠ current 都会重发；同一 installed 版本只
        // 写一次 store（vanilla lastKnownVersion 语义：不重复弹，dismiss 后不再出现）。
        const up = data as UpgradeData;
        const installed = up.installed_version || "";
        if (installed && installed !== store.get().upgradeBanner?.installed) {
          store.update((s) => ({
            ...s,
            upgradeBanner: { current: up.current_version || s.currentVersion, installed },
          }));
        }
        break;
      }
      default:
        // 未知事件类型：忽略（vanilla 同语义，未来类型静默兼容）
        break;
    }
  }

  const unsubscribe = onEvent((evt) => handleFrame(evt));
  // Teardown takes the bound with it: a timer armed for a question this bridge
  // will never render again is a write into a dead store (issue #1757).
  const dispose = (): void => {
    clearApprovalBound();
    unsubscribe();
  };

  function applyInit(result: InitResult): void {
    if (!result) return;
    store.update((s) => ({
      ...s,
      // init 成功=配置存在+key 已配置（main.js 仅在两者均满足时才走到 ensureConnected）
      connected: Boolean(result.config_exists && result.api_key_configured),
      serverId: result.server_id || s.serverId,
      model: result.model || s.model,
      vision: typeof result.vision === "boolean" ? result.vision : s.vision,
      evolutionCount: result.evolution_count ?? s.evolutionCount,
      currentVersion: result.current_version || s.currentVersion,
      sessions: result.sessions || s.sessions,
      openSessions: result.open_sessions || s.openSessions,
    }));
  }

  async function respondApproval(approved: boolean): Promise<boolean> {
    const pending = store.get().pendingApproval;
    // The host answered, so the question is over on this side too: the bound was
    // armed for a question that no longer exists (issue #1757).
    clearApprovalBound();
    store.update((s) => ({ ...s, pendingApproval: null }));
    if (!pending) return false;
    const responder = emrg.respondApproval;
    if (!responder) return false;
    try {
      await responder({
        sessionId: pending.sessionId || "",
        requestId: pending.requestId,
        approved,
      });
    } catch {
      // The answer did not reach the daemon. It is not retried: the daemon's
      // channel is fail-closed and its timeout is the fallback, so a silent
      // command is reported by the daemon, not guessed at here.
      return false;
    }
    return true;
  }

  return { store, dispose, handleFrame, applyInit, respondApproval };
}
