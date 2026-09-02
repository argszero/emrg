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
  current_version?: string;
  evolution_count?: number | null;
  auth_failed?: boolean;
  reconnecting?: boolean;
  installing?: boolean;
}

/** pong 事件载荷（daemon 心跳） */
export interface PongData {
  identity?: { instance_id?: string };
  model?: string;
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

/** 统一事件帧（onEvent 回调入参） */
export interface DaemonEventFrame {
  type: string;
  data: Record<string, unknown> & { chunks?: DeltaChunk[] } & Partial<DoneData & ToolStartData & ToolEndData & StatusData & PongData & SessionsData & QueuedData & ErrorData & UpgradeData>;
  sid?: string | null;
}

/* ── App 级状态 store（桥维护，Shell 消费） ── */

export interface DaemonAppState {
  /** 连接状态（status.connected / disconnected 事件驱动） */
  connected: boolean;
  /** status 附带的诊断标记 */
  authFailed: boolean;
  reconnecting: boolean;
  installing: boolean;
  serverId: string;
  model: string;
  currentVersion: string;
  evolutionCount: number | null;
  sessions: SessionSummary[];
  openSessions: OpenSessionEntry[];
  /** 每会话 busy 锁（P3 slice 1：done/cancelled 按 sid 释放，不误清激活会话） */
  busyBySid: Record<string, boolean>;
  /** 每会话 turn 开始时刻（epoch ms；rant 2026-09-02T10:36:26 daemon turn_start 权威广播） */
  turnStartBySid: Record<string, number>;
  /** 每会话 own stream request id（决定"来自其他客户端"标签） */
  ownStreamRidBySid: Record<string, string | null>;
  /** 每会话断线标记（P3 finalize：后台会话断线不触发全局 UI） */
  disconnectedBySid: Record<string, boolean>;
  /** upgrade 事件（心跳检测 installed ≠ current → "重启生效"横幅；null=无待重启提示） */
  upgradeBanner: { current: string; installed: string } | null;
}

const SID_NULL = "__emrg_null_sid__";
const KEY = (sid?: string | null): string => sid || SID_NULL;

export function createDaemonAppStore(): SnapshotStore<DaemonAppState> {
  return createSnapshotStore<DaemonAppState>({
    connected: false,
    authFailed: false,
    reconnecting: false,
    installing: false,
    serverId: "",
    model: "",
    currentVersion: "",
    evolutionCount: null,
    sessions: [],
    openSessions: [],
    busyBySid: {},
    turnStartBySid: {},
    ownStreamRidBySid: {},
    disconnectedBySid: {},
    upgradeBanner: null,
  });
}

/* ── 桥 ── */

export interface SendMessagePayload {
  sessionId: string | null;
  text: string;
  requestId?: string;
  sandbox?: string;
}

/** window.emrg.init() 返回值（main.js emrg:init 处理器 → preload.init） */
export interface InitResult {
  config_exists?: boolean;
  api_key_configured?: boolean;
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
}

export function createDaemonBridge(deps: DaemonBridgeDeps): DaemonBridge {
  const { onEvent, emrg, transcript, t } = deps;
  const tt: TranslateFn = t ?? ((key: string): string => key);
  const store = createDaemonAppStore();

  // P2 queue-injection（#655）：busy 时发送的消息入 daemon 队列，queued_requeue
  // 以原 requestId 重发（不重加用户行）。逐 sid 记录（后台会话独立跟踪）。
  const queuedSends = new Map<string, { requestId: string; text: string; sandbox?: string }[]>();

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
  function sidOwnRid(sid: string | null, rid: string | null): void {
    const k = KEY(sid);
    store.update((s) => ({ ...s, ownStreamRidBySid: { ...s.ownStreamRidBySid, [k]: rid } }));
  }
  function sidDisconnected(sid: string | null, v: boolean): void {
    const k = KEY(sid);
    store.update((s) => ({ ...s, disconnectedBySid: { ...s.disconnectedBySid, [k]: v } }));
  }

  /** done/cancelled 释放该事件所属会话的锁（vanilla：仅当 request 匹配或 timeout） */
  function releaseOwnStream(sid: string | null, requestId?: string | null, force = false): void {
    const k = KEY(sid);
    const cur = store.get();
    const rid = cur.ownStreamRidBySid[k];
    if (force || (requestId && (rid === requestId))) {
      sidBusy(sid, false);
      sidOwnRid(sid, null);
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
        sidOwnRid(sid, item.requestId);
        try {
          const res = await emrg.sendMessage({
            sessionId: sid,
            text: item.text,
            requestId: item.requestId,
            sandbox: item.sandbox,
          });
          sidOwnRid(sid, res?.requestId ?? item.requestId);
        } catch {
          sidBusy(sid, false);
          sidOwnRid(sid, null);
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
        transcript.clearTyping(sid);
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
          evolutionCount: pong.evolution_count ?? s.evolutionCount,
        }));
        break;
      }
      case "status": {
        const st = data as StatusData;
        store.update((s) => ({
          ...s,
          connected: st.connected ?? s.connected,
          authFailed: st.auth_failed ?? s.authFailed,
          reconnecting: st.reconnecting ?? s.reconnecting,
          installing: st.installing ?? s.installing,
          serverId: st.server_id || s.serverId,
          model: st.model || s.model,
          currentVersion: st.current_version || s.currentVersion,
        }));
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
        sidOwnRid(sid, null);
        sidDisconnected(sid, true);
        clearTurnTimer(sid);
        queuedSends.delete(KEY(sid));
        if (!sid) {
          store.update((s) => ({ ...s, connected: false }));
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

  const dispose = onEvent((evt) => handleFrame(evt));

  function applyInit(result: InitResult): void {
    if (!result) return;
    store.update((s) => ({
      ...s,
      // init 成功=配置存在+key 已配置（main.js 仅在两者均满足时才走到 ensureConnected）
      connected: Boolean(result.config_exists && result.api_key_configured),
      serverId: result.server_id || s.serverId,
      model: result.model || s.model,
      evolutionCount: result.evolution_count ?? s.evolutionCount,
      currentVersion: result.current_version || s.currentVersion,
      sessions: result.sessions || s.sessions,
      openSessions: result.open_sessions || s.openSessions,
    }));
  }

  return { store, dispose, handleFrame, applyInit };
}
