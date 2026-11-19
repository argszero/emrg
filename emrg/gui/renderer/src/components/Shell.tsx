import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import { useSnapshotStore } from "../hooks/useSnapshotStore";
import { useDaemonBridge } from "./DaemonBridgeProvider";
import { createProdMarkdownRenderer } from "../lib/vendorMarkdown";
import { dialogReducer, initialDialogState } from "../lib/dialog";
import { ConfirmDialog } from "./ConfirmDialog";
import { ApprovalDialog } from "./ApprovalDialog";
import { TaskFormDialog, type TaskFormPayload } from "./TaskFormDialog";
import { RantDialog } from "./RantDialog";
import type { AgentRec, RantRec, TaskRec } from "../lib/workspaceView";
import type { SessionRow } from "../lib/openSession";
import type { SessionInfo } from "../lib/sidebar";
import { SessionColumn } from "./SessionColumn";
import { IconRail } from "./IconRail";
import { StatusBand } from "./StatusBand";
import { SearchOverlay } from "./SearchOverlay";
import { ResizeHandle } from "./ResizeHandle";
import {
  HANDLE_WIDTH,
  LEFT_COLUMN_DEFAULT,
  LEFT_COLUMN_MIN,
  LS_LEFT_WIDTH,
  LS_RIGHT_WIDTH,
  RAIL_WIDTH,
  RIGHT_COLUMN_DEFAULT,
  RIGHT_COLUMN_MIN,
  clampWidth,
  columnBounds,
  mergeSessionRows,
  persistColumnWidth,
  sessionHits,
  storedColumnWidth,
  type SidebarViewId,
} from "../lib/cockpit";
import { InspectorPanel } from "./InspectorPanel";
import { WorkspaceView, type WorkspaceViewId } from "./WorkspaceView";
import { TranscriptView, type ScrollMemory } from "./TranscriptView";
import { Composer, type CommandRouting } from "./Composer";
import { DialogHost, type DialogHostHandle } from "./DialogHost";
import {
  HISTORY_PAGE,
  type HistoryPageState,
  applyHistoryPage,
  createHistoryPages,
  historyPageState,
  loadBarKey,
  unloadedRecords,
} from "../lib/history";
import { replayHistoryRecords, replayHistoryRecordsPrepend, type HistoryRecord } from "../lib/historyReplay";

/**
 * Shell — the page's five sections (issue #2969).
 *
 * 横向五段：IconRail(60) → 会话栏(218) → 拖拉把手(6) → 正文(flex:1, min-width:0) →
 * 拖拉把手(6) → 检查栏(262)。两栏的宽度与开合都住在这里：一个会卸载的组件记不住
 * 自己的宽度，而拖动之所以能移动宽度，正是因为宽度写在行内而不是样式表里。
 *
 * 旧的 Sidebar（分组 + 历史列表）与旧的 ResultPanel（自带 resizer / 产物 tab /
 * 英文空态）没有兼容层，整块删除；新做的东西只挂在这套骨架上。
 *
 * 早期 Batch 备注（Sidebar/ResultPanel 的接线）：
 * + Batch 5 slice 3 核心聊天回路接线 + slice 4 对话框路由）。
 *
 * Batch 5 slice 3：把 daemon 实时数据接到聊天回路——
 * - TranscriptView 消费共享 TranscriptStore（bridge.transcript），按 activeSid 切片
 *   （一切 store 按 sid 键控，会话串线防护 #977）；
 * - Composer 发送走 window.emrg.sendMessage（默认注入），busy 受控于 daemon 广播的
 *   busyBySid（done/cancelled/error 释放锁，#655 队列注入协议）；
 * - Sidebar 点击切换 activeSid；open_sessions 广播到达且无激活会话时自动选第一个
 *   （vanilla app.js 同语义）；
 * - ResultPanel 绑定 activeSid（per-session Tab/产物隔离）。
 *
 * Batch 5 slice 4：/指令路由到 DialogHost——
 * - Composer onCommand → Shell 路由（/help /memory /skills /rewind /rename /delete
 *   /sessions /open /resume → DialogHost；/clear /compact /version /image → runDirect）；
 * - Sidebar 新对话/打开会话按钮 → NewSessionDialog / OpenSessionDialog；
 * - DialogHost 统一持有对话框状态 + window.emrg 数据加载（vanilla dialogs.js 语义）。
 *
 * Batch 5 slice 5：workspace 视图切换——
 * - activeView 状态（"sessions" | agents | tasks | rants | settings），vanilla
 *   app.js switchView 语义：点当前面板再点一次 → toggle 回会话视图；点其他面板 →
 *   切换；点 💬 → 回会话视图；
 * - 面板视图激活时隐藏会话 chrome（transcript/composer/检查栏 —— vanilla
 *   setWorkspaceChrome("panel")），sessions 视图显示完整会话区；
 * - Sidebar nav rail（#side-nav）高亮当前视图 + onSwitchView 接线；
 * - WorkspaceView 数据接线：面板激活加载 listAgents/listTasks/listRants，项目会话子视图
 *   listAgentSessions，动作 switch-session / add-agent / delete-agent / trigger-task。
 */
/** Shell 用到的 workspace 数据桥方法（与 preload.js 通道对齐） */
interface WorkspaceBridge {
  listAgents(): Promise<AgentRec[]>;
  listAgentSessions(p: { projectPath: string }): Promise<{ sessions: SessionRow[] }>;
  /**
   * Every registered agent's sessions, in one answer (issue #2978). Optional because a
   * packaged client from before the message existed answers `undefined`, and the
   * overlay then shows what the app state already holds instead of nothing at all.
   */
  listAllSessions?(): Promise<{ sessions: SessionInfo[] }>;
  listTasks(): Promise<TaskRec[]>;
  listRants(p: { status?: string }): Promise<RantRec[]>;
  switchSession(p: { sessionId: string; projectPath?: string }): Promise<unknown>;
  /**
   * 关掉一条打开的会话（`emrg:closeSession`；磁盘数据保留）。
   *
   * 可选：这条消息比某些已装客户端新，未接线时 Shell 退回「什么都不做」而不是清屏 ——
   * 清掉一份还归那条会话所有的内容，等于凭空丢掉它。
   */
  closeSession?(p: { sessionId: string }): Promise<unknown>;
  /**
   * 拖拽调序（`emrg:reorderOpenSessions`；issue #3022）：把整份新顺序交给主进程。
   *
   * 顺序的唯一真源在主进程 —— 它收下、落盘、广播，列表照那次广播画。这里再排一次
   * 就是第二个真源，而两个真源打架的样子就是「列表跳一下」。
   *
   * 可选：这条消息比某些已装客户端新，未接线时那次拖拽落空，列表不动。
   */
  reorderOpenSessions?(p: { sids: string[] }): Promise<unknown>;
  pickAgentDir(): Promise<{ path?: string } | null>;
  registerAgent(p: { path: string }): Promise<{ ok?: boolean; path?: string }>;
  removeAgent(p: { name: string; path?: string }): Promise<{ ok?: boolean; error?: string; protected?: boolean }>;
  triggerTask(p: { name: string }): Promise<unknown>;
  taskCreate(p: TaskFormPayload): Promise<unknown>;
  taskUpdate(p: TaskFormPayload): Promise<unknown>;
  /**
   * 间隔下限（issue #2490）：daemon 的 `MIN_INTERVAL`，由 `tasks_list` 帧上报。
   * 渲染层不保留第二份数字 —— 拿不到就没有预检，由 daemon 权威校验。
   */
  taskLimits?(): Promise<{ minInterval?: number }>;
  taskDelete(p: { name: string }): Promise<unknown>;
  taskTemplateList(): Promise<{ name: string }[]>;
  sendRant(p: { message: string; agent?: string }): Promise<{ ok?: boolean; count?: number }>;
  /** 删除一条 rant（issue #2959 item 7）——`ok: false` 表示账本里没有这一行 */
  deleteRant?(p: { timestamp: string }): Promise<{ ok?: boolean; notice?: string }>;
  /**
   * 升级横幅重启（rant 2026-09-01T20:09:41）：调 emrg:restartDaemon —— 完整 stop
   * 链（TUI→daemon，--skip-gui 跳过 GUI 防自杀）→ GUI 自己 relaunch → 新 GUI
   * ensureDaemon 用新安装代码 spawn 新 daemon。此前仅重启 GUI 外壳导致版本号死循环
   * （daemon 内存 _run_version 不更新 → 心跳持续报版本差 → 横幅永不消失）。
   */
  restartDaemon?(): Promise<unknown>;
  /** /model 直切模型（rant 2026-09-01T20:22:00：对齐 TUI，preload.js 已暴露 emrg:setModel） */
  setModel?(p: { model: string }): Promise<unknown>;
  /** 历史按需加载（rant 2026-09-01T20:19:40：GUI 打开会话不显示历史——链路从未接线） */
  listHistory?(p: {
    sessionId: string;
    limit?: number;
    /** @deprecated 距最新的 offset；历史加载已改游标（rant 2026-09-20T18:58:44） */
    offset?: number;
    /** rant 2026-09-02T10:03:29：true → daemon 同时返回 user + assistant 消息 */
    includeAssistant?: boolean;
    /**
     * rant 2026-09-20T18:58:44：true → daemon 返回**完整有序的记录序列**——含
     * `tool_result`、含只带 `tool_calls` 的助手记录，每条带绝对 `record_index`，
     * 且不再附 `preview`（回放优先取 preview 曾把 5064 字的回答显示成 81 字）。
     */
    includeRecords?: boolean;
    /** 稳定游标：只取 `record_index < beforeIndex` 的记录（分页不再用 offset 计数） */
    beforeIndex?: number;
  }): Promise<{
    messages?: HistoryRecord[];
    hasMore?: boolean;
  }>;
}

function wsBridge(): WorkspaceBridge | undefined {
  return (window as unknown as { emrg?: WorkspaceBridge }).emrg;
}

export function Shell() {
  const { t } = useI18n();
  const { bridge, transcript } = useDaemonBridge();
  const appState = useSnapshotStore(bridge.store);
  const [activeSid, setActiveSid] = useState<string | null>(null);
  const [activeView, setActiveView] = useState<"sessions" | WorkspaceViewId>("sessions");
  // ── The cockpit's four pieces of shell state (issue #2959) ──
  //
  // Column widths and what is open live here rather than in the components they size:
  // a column that unmounts takes its own state with it, so a width held inside the
  // sidebar would reset every time the host hid and showed it. The width is written
  // inline (never in a stylesheet), which is what makes a drag able to move it, and it
  // is remembered under one key per column so closing one never forgets the other.
  const [leftWidth, setLeftWidth] = useState(() => storedColumnWidth(LS_LEFT_WIDTH, LEFT_COLUMN_DEFAULT));
  const [rightWidth, setRightWidth] = useState(() => storedColumnWidth(LS_RIGHT_WIDTH, RIGHT_COLUMN_DEFAULT));
  const [leftOpen, setLeftOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(true);
  const [searchOpen, setSearchOpen] = useState(false);
  // The window width is read once and then tracked: the left column's ceiling is what
  // the window leaves the body, so it has to be a live number, not a constant.
  const [windowWidth, setWindowWidth] = useState(() =>
    typeof window === "undefined" ? 1440 : window.innerWidth,
  );
  const dialogHost = useRef<DialogHostHandle>(null);
  // `/rewind` picked a message and that message's text goes back into the composer
  // (issue #2384). A request, not a controlled value: the input's live content belongs
  // to the host, so each pick carries a fresh id and the composer applies it once.
  const [composerInsert, setComposerInsert] = useState<{ id: number; text: string } | null>(null);
  const composerInsertSeq = useRef(0);
  // workspace 面板数据（Batch 5 接线：项目/任务/Rant 真实列表 + 项目会话）
  const [agents, setAgents] = useState<AgentRec[]>([]);
  const [tasks, setTasks] = useState<TaskRec[]>([]);
  const [rants, setRants] = useState<RantRec[]>([]);
  const [projectSessions, setAgentSessions] = useState<SessionRow[] | null>(null);
  const [projectSessionsError, setAgentSessionsError] = useState<string | null>(null);
  // 切会话被拒时的可见提示（issue #2637）：拒绝必须落在人操作的那个面板上，
  // 而不是一个不在屏幕上的转录或一个未处理的 Promise。
  const [switchError, setSwitchError] = useState<string | null>(null);
  const [dialogState, dispatch] = useReducer(dialogReducer, initialDialogState);
  // Batch 5 slice 8：任务表单 + Rant 对话框（vanilla openTaskForm/openRantForm 语义）
  const [taskForm, setTaskForm] = useState<{ task: TaskRec | null } | null>(null);
  /** 间隔下限：daemon 上报的值（undefined = 还没问到，表单就不预检） */
  const [minInterval, setMinInterval] = useState<number | undefined>(undefined);
  const [rantOpen, setRantOpen] = useState(false);
  const [taskTypes, setTaskTypes] = useState<string[]>([]);
  // 升级横幅（rant 2026-08-27T21:54:51：React 迁移丢 upgrade 事件）：dismissed 记录
  // 已提示过的 installed 版本（vanilla state.lastKnownVersion 语义——心跳每 15s 重发
  // 同一版本，不重复弹）；restarting 防重复点击（relaunch 后进程即退出）。
  const [dismissedUpgrade, setDismissedUpgrade] = useState<string | null>(null);
  const [restarting, setRestarting] = useState(false);
  // 会话信息行（rant 2026-09-01T20:16:55：对齐 TUI 状态栏——id/name/agent/消息数/轮计时）
  //
  // 计时基准是 **daemon 的轮开始时刻**（`turnStartBySid`，由 `turn_start` 帧、或打开会话时的
  // `resume_result` 快照 `meta.turn` 写入），不是本客户端见到 busy 的那一刻（rant
  // 2026-09-27T18:41:52，requirement 2「计时由 daemon 给」）：中途打开一个已经跑了 5 分钟的轮
  // ——别的客户端起的，或定时任务起的——必须显示 05:00 并继续每秒 +1，而不是从 00:00 起算。
  // 基准与 TUI 状态栏、侧边栏的 `[m:ss]` 同一来源（同一个 `started_at`），三处不可能对不上。
  const activeTurnStart = activeSid ? appState.turnStartBySid[activeSid] : undefined;

  // Rant 2026-09-02T10:36:26：每个运行会话的 [m:ss] 计时——任意会话有轮在跑时共享一个 1s
  // tick 触发重渲染（Sidebar 与上面的会话信息行都在渲染时读 Date.now() 自行推导 elapsed，
  // 所以多会话复用同一定时器，没有第二个只服务一处的 interval）。
  const [, setSidebarTick] = useState(0);
  const hasRunningSessions = Object.keys(appState.turnStartBySid).length > 0;
  useEffect(() => {
    if (!hasRunningSessions) return;
    const iv = setInterval(() => setSidebarTick((t) => t + 1), 1000);
    return () => clearInterval(iv);
  }, [hasRunningSessions]);

  // The window is what a column's ceiling is measured against (issue #2959 item 4), so
  // a resized window has to move that ceiling rather than leave a stale one behind.
  useEffect(() => {
    const onResize = () => setWindowWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  const activeSessionEntry = appState.openSessions.find((o) => o.sid === activeSid);
  const activeKnown = appState.sessions.find((s) => s.session_id === activeSid);
  const activeTitle = activeSessionEntry?.title || activeKnown?.title || t("app.unnamed");
  const activeAgent = activeSessionEntry?.projectName || "";
  const activeMsgCount = (activeKnown as { message_count?: number } | undefined)?.message_count ?? 0;
  // The turn's elapsed time is no longer formatted here: the status band does it from
  // `startedAtMs` (`formatElapsed`, the same reader the sidebar's stopwatch uses), so
  // the shell keeps no second copy of that arithmetic to drift from it.

  /**
   * 当前会话以**后台广播的那条**为准（rant 2026-11-18T18:41:06 清单 2，issue #2985 遗留项）。
   *
   * 以前这里只有一条规则：没有激活会话就取 `openSessions[0]` —— 也就是渲染层自己按
   * 列表顺序挑一条，而列表顺序是「最近活跃」，不是「后台当前」。三条取值必须分开读：
   *   - `undefined` 后台没说过（旧主进程、测试帧）：不动，沿用旧规则；
   *   - `string`    就是这条：采用它，不许再按列表挑；
   *   - `null`      后台刚关掉它的当前会话：按规则挑一条仍然有效的，**并且告诉后台**
   *                （清单 9），否则下一条广播还把 `null` 送回来，页面停在空白。
   */
  useEffect(() => {
    const reported = appState.reportedSid;
    if (reported === undefined) {
      if (activeSid === null && appState.openSessions.length > 0) {
        const sid = appState.openSessions[0].sid;
        setActiveSid(sid);
        void loadHistory(sid);
      }
      return;
    }
    if (typeof reported === "string") {
      if (reported === activeSid) return;
      setActiveSid(reported);
      setActiveView("sessions");
      void loadHistory(reported);
      return;
    }
    // reported === null：当前会话没了。挑一条仍然有效的（列表按 lastActive 降序），
    // 没有任何一条就退回「没有会话」——留着旧的 sid 只会让页面看着有会话却是空的。
    const next = appState.openSessions[0]?.sid ?? null;
    if (next === null) {
      if (activeSid !== null) setActiveSid(null);
      return;
    }
    if (next !== activeSid) void selectSession(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appState.reportedSid, appState.openSessions, activeSid]);

  /** 重新装配一个会话的 transcript：丢掉分页状态与「已加载」标记，从头回放一次。 */
  async function reloadHistory(sid: string) {
    historyLoadedRef.current.delete(sid);
    historyPagesRef.current.delete(sid);
    transcript.clear(sid);
    await loadHistory(sid);
  }

  /**
   * 一次 rewind 之后重建这个会话的 transcript（#2351）。
   *
   * daemon 把 `rewind_result` 广播给订阅该会话的每个连接，改写的是同一份落盘历史；
   * 本客户端自己做的那次也经主进程转发成同一个事件（`main.js:emrg:rewindSession`），
   * 于是这里只有一条规则：**谁被告知历史被改写了，谁的屏幕就从那份历史重新长出来**，
   * 而不是各自猜着删掉最后 N 行。计数器只比较变化，值本身没有含义。
   */
  const rewoundCount = activeSid ? appState.rewoundBySid[activeSid] : undefined;
  useEffect(() => {
    if (!activeSid || rewoundCount === undefined) return;
    void reloadHistory(activeSid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rewoundCount, activeSid]);

  /**
   * 会话的输入历史（issue #2349 part 2/2，GUI 半边）。
   *
   * 列表属于**会话**、由 daemon 落盘（每接受一条 prompt 就写 `<session_dir>/
   * input_history.jsonl`），所以每次切到某个会话都重新问一次，而不是让 Composer
   * 自己记：换会话、重开客户端、rewind、compact 之后它都还是对的，因为唯一的
   * 写入者不是这个进程。读失败时 store 里保持"没有这个键"（= 还没读到），Composer
   * 于是把方向键留给光标——一份读不到的列表和一个空列表在键盘上不该有两种表现。
   */
  useEffect(() => {
    if (!activeSid) return;
    void bridge.listInputHistory(activeSid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSid]);

  // ── workspace 面板数据加载（vanilla loadTaskMeta/renderRantList 语义） ──
  async function loadAgents() {
    const b = wsBridge();
    if (!b?.listAgents) return;
    try {
      setAgents(await b.listAgents());
    } catch {
      setAgents([]);
    }
  }

  async function loadTasks() {
    const b = wsBridge();
    if (!b?.listTasks) return;
    try {
      setTasks(await b.listTasks());
    } catch {
      setTasks([]);
    }
  }

  async function loadRants() {
    const b = wsBridge();
    if (!b?.listRants) return;
    try {
      setRants(await b.listRants({}));
    } catch {
      setRants([]);
    }
  }

  async function loadProjectSessions(p: AgentRec) {
    const b = wsBridge();
    if (!b?.listAgentSessions || !p.path) return;
    setAgentSessions(null);
    setAgentSessionsError(null);
    try {
      const res = await b.listAgentSessions({ projectPath: p.path });
      setAgentSessions(res.sessions ?? []);
    } catch (e) {
      setAgentSessionsError((e as Error)?.message ?? String(e));
    }
  }

  // 面板激活 → 加载对应数据（每次进入刷新，vanilla 面板激活时拉取）
  useEffect(() => {
    if (activeView === "agents") void loadAgents();
    else if (activeView === "tasks") void loadTasks();
    else if (activeView === "rants") void loadRants();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeView]);

  // 任务面板激活时每 5s 轮询任务状态（vanilla startTaskPoll，rant 2026-08-22T07:18:35 方案 B：
  // 宿主确认轮询、可接受几秒滞后）——让"运行中→待运行"切换与下次倒计时 ≤5s 内自动更新，
  // 不引入事件推送。幂等：重复启动先清旧定时器；离开任务视图清除防泄漏；轮询失败静默下轮重试。
  useEffect(() => {
    if (activeView !== "tasks") return;
    const id = setInterval(() => {
      void loadTasks();
    }, 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeView]);

  // ── workspace 面板动作（vanilla dialogs.js 语义） ──
  async function addProject() {
    const b = wsBridge();
    if (!b?.pickAgentDir || !b?.registerAgent) return;
    try {
      const res = await b.pickAgentDir();
      if (!res?.path) return;
      await b.registerAgent({ path: res.path });
      await loadAgents();
    } catch (e) {
      transcript.addSystemMessage(t("deleteAgent.failed", { msg: (e as Error)?.message ?? String(e) }), activeSid);
    }
  }

  /**
   * Remove an agent — no confirmation dialog, because the row already asked.
   *
   * Issue #2959 item 7 moved the question onto the row itself (`WorkspaceView`'s
   * `deleteControl`): the first click turns the button into "delete?", the second
   * answers it. Keeping a modal on top of that would ask twice for one deletion, and
   * the modal is the worse of the two askings — it covers the list, so the host can no
   * longer see which row they are answering about.
   */
  async function deleteAgent(p: AgentRec) {
    const b = wsBridge();
    if (!b?.removeAgent) return;
    try {
      await b.removeAgent({ name: p.name || "", path: p.path || "" });
      await loadAgents();
    } catch (e) {
      transcript.addSystemMessage(t("deleteAgent.failed", { msg: (e as Error)?.message ?? String(e) }), activeSid);
    }
  }

  async function selectProjectSession(p: Pick<AgentRec, "path">, sid: string) {
    const b = wsBridge();
    if (!b?.switchSession) return;
    try {
      // The row's own agent travels with the switch (issue #2978). Without it the main
      // process has to work the directory out afterwards, and a history row belonging
      // to another agent could open under whichever agent happened to be active — the
      // messages then read out of the wrong directory, or out of an empty one.
      await b.switchSession({ sessionId: sid, ...(p.path ? { projectPath: p.path } : {}) });
    } catch (e) {
      // 切换失败：人没有离开当前视图，所以拒绝要出现在**这里**（issue #2637）——
      // 以前这一行没有 try，拒绝变成未处理的 Promise rejection，点一下像没反应。
      setSwitchError((e as Error)?.message ?? String(e));
      return;
    }
    setSwitchError(null);
    setActiveSid(sid);
    setActiveView("sessions");
    void loadHistory(sid);
  }

  /** 任务面板「打开会话」：switchSession 到任务所属会话（vanilla #924 行为迁移） */
  async function openTaskSession(task: TaskRec) {
    if (!task.session_id) return;
    const b = wsBridge();
    if (!b?.switchSession) return;
    try {
      await b.switchSession({ sessionId: task.session_id, projectPath: task.agent_path });
      setSwitchError(null);
      setActiveSid(task.session_id);
      setActiveView("sessions");
      void loadHistory(task.session_id);
    } catch (e) {
      // 以前这条拒绝被写进 `activeSid` 的会话记录，而人正看着任务面板 —— 消息落在
      // 一个不在屏幕上的转录里。面板视图上方的横幅才是人操作的地方（issue #2637）。
      setSwitchError((e as Error)?.message ?? String(e));
    }
  }

  async function triggerTask(task: TaskRec) {
    const b = wsBridge();
    if (!b?.triggerTask || !task.name) return;
    try {
      await b.triggerTask({ name: task.name });
      transcript.addSystemMessage(t("app.triggered", { n: task.name }), activeSid);
      await loadTasks();
    } catch (e) {
      transcript.addSystemMessage(t("app.triggerFailed", { msg: (e as Error)?.message ?? String(e) }), activeSid);
    }
  }

  // ── Batch 5 slice 8：任务表单 + Rant 对话框（vanilla openTaskForm/openRantForm 语义） ──
  /** 打开任务表单：加载类型下拉（taskTemplateList）+ 项目列表 → 预填 */
  async function openTaskForm(task: TaskRec) {
    const b = wsBridge();
    const isEdit = Boolean(task?.name);
    if (b?.taskTemplateList && taskTypes.length === 0) {
      try {
        const tpls = await b.taskTemplateList();
        const names = (tpls || []).map((tp) => tp.name);
        // 内置类型兜底（taskTemplateList 只返回自定义；vanilla 用 taskTypes=内置+自定义）
        if (!names.includes("evolution")) names.unshift("evolution");
        setTaskTypes(names);
      } catch {
        setTaskTypes(["evolution"]);
      }
    }
    if (taskTypes.length === 0 && !isEdit) {
      // 首次打开且模板加载失败 → 仍可用 evolution 内置类型
      setTaskTypes((prev) => (prev.length ? prev : ["evolution"]));
    }
    if (!isEdit) await loadAgents(); // 新建需要项目下拉
    // 下限先问 daemon（issue #2490）：问不到保持 undefined，表单不预检
    if (b?.taskLimits) {
      try {
        const limits = await b.taskLimits();
        if (typeof limits?.minInterval === "number") setMinInterval(limits.minInterval);
      } catch {
        /* 拿不到就交给 daemon 权威校验 */
      }
    }
    setTaskForm({ task: isEdit ? task : null });
  }

  async function saveTask(payload: TaskFormPayload) {
    const b = wsBridge();
    const editing = taskForm?.task?.name ? taskForm.task.name : null;
    if (!b) return;
    try {
      if (editing) await b.taskUpdate({ ...payload, name: editing });
      else await b.taskCreate(payload);
      transcript.addSystemMessage(t("settings.taskSaved"), activeSid);
      await loadTasks();
    } catch (e) {
      transcript.addSystemMessage(t("app.tasksFailed", { msg: (e as Error)?.message ?? String(e) }), activeSid);
    }
  }

  /** Same rule as `deleteAgent`: the row asked, so this only answers. */
  async function deleteTask(task: TaskRec) {
    const b = wsBridge();
    if (!b?.taskDelete || !task.name) return;
    try {
      await b.taskDelete({ name: task.name });
      transcript.addSystemMessage(t("settings.taskDeleted"), activeSid);
      await loadTasks();
    } catch (e) {
      transcript.addSystemMessage(t("app.tasksFailed", { msg: (e as Error)?.message ?? String(e) }), activeSid);
    }
  }

  async function submitRant(payload: { message: string; agent: string }) {
    const b = wsBridge();
    if (!b?.sendRant) return;
    try {
      const res = await b.sendRant(payload);
      transcript.addSystemMessage(t("rants.sent", { count: res?.count ?? "" }), activeSid);
      await loadRants();
    } catch (e) {
      transcript.addSystemMessage(t("rants.sendFailed", { msg: (e as Error)?.message ?? String(e) }), activeSid);
    }
  }

  /**
   * Remove a rant row (issue #2959 item 7).
   *
   * The daemon owns the ledger, so this is a request rather than an edit: it reports
   * `ok: false` when no row carried that timestamp, and the panel is reloaded from the
   * daemon either way. Reloading on refusal matters — a list that keeps a row the
   * ledger no longer has is a list that will lie until the next refresh.
   */
  async function deleteRant(r: RantRec) {
    const b = wsBridge();
    if (!b?.deleteRant || !r.timestamp) return;
    try {
      const res = await b.deleteRant({ timestamp: String(r.timestamp) });
      if (res?.ok === false) {
        transcript.addSystemMessage(
          t("rants.deleteFailed", { msg: res.notice || "" }),
          activeSid,
        );
      }
    } catch (e) {
      transcript.addSystemMessage(
        t("rants.deleteFailed", { msg: (e as Error)?.message ?? String(e) }),
        activeSid,
      );
    }
    await loadRants();
  }

  /** 打开 Rant 对话框：vanilla openRantForm 语义 —— 显式 listAgents 填充项目下拉 */
  async function newRant() {
    if (agents.length === 0) await loadAgents();
    setRantOpen(true);
  }

  const busy = activeSid ? (appState.busyBySid[activeSid] ?? false) : false;
  const disconnected = activeSid ? (appState.disconnectedBySid[activeSid] ?? false) : false;

  // ── The status band's two daemon readings: the running round, and the spend ──
  //
  // Both come from `turn_progress`, the frame the daemon broadcasts at the top of every
  // round. Neither is derived here: a client that counted rounds, or added up usage it
  // happened to see, would answer with its own arithmetic the moment a stream restarts
  // or another window watches the same turn. `null` is "not reported", which the band
  // renders as `—` rather than as round 1 or as zero tokens.
  const round = activeSid ? (appState.roundBySid[activeSid] ?? null) : null;
  const turnTokens = activeSid ? (appState.turnTokensBySid[activeSid] ?? null) : null;

  /**
   * Where the inspector's two trees are rooted (issue #3026).
   *
   * Both roots come from the main process, because it is the only layer holding the
   * open-session ledger, the global session index and the daemon's own reading of a
   * session's working directory. `null` means "not measured" — no session, or a build
   * that cannot answer — and the tree says so rather than rooting at a guess.
   *
   * The turn's tool calls are no longer this column's business: they are already on
   * screen as transcript rows, with the duration the daemon measured, and a second list
   * of them here was a copy that could disagree with the one being watched.
   */
  const [roots, setRoots] = useState<{ agentRoot: string | null; sessionRoot: string | null }>({
    agentRoot: null,
    sessionRoot: null,
  });
  useEffect(() => {
    let live = true;
    if (!activeSid) {
      setRoots({ agentRoot: null, sessionRoot: null });
      return () => {
        live = false;
      };
    }
    // A build whose preload has no such channel **rejects** rather than answering, and
    // an unhandled rejection here is a console error in the app and a red suite in CI
    // (measured: run 44472804661, where a test removed `window.emrg` while this call was
    // in flight). `null` is the answer the trees already render as "not measured", so a
    // failure keeps both roots null instead of escaping.
    void bridge
      .sessionRoots(activeSid)
      .then((r) => {
        if (live && r) setRoots(r);
      })
      .catch(() => {
        if (live) setRoots({ agentRoot: null, sessionRoot: null });
      });
    return () => {
      live = false;
    };
  }, [bridge, activeSid]);
  const loadDir = useCallback((p: string) => bridge.listDir(p), [bridge]);

  /**
   * The whole register's sessions, fetched each time the overlay opens (issue #2978).
   *
   * `appState.sessions` is the active agent's listing — what the left column shows —
   * and the overlay's subject is every registered agent, so it asks once on open
   * rather than once per agent. Read on open rather than on mount for the reason the
   * daemon reads its register live: an agent registered while the app is running must
   * be findable next time the box opens.
   *
   * A daemon that cannot answer leaves what the app state already holds. The host
   * opened this box to find a session, and an error surface where the results go is
   * not an answer to that.
   */
  const [allSessions, setAllSessions] = useState<SessionInfo[]>([]);
  useEffect(() => {
    if (!searchOpen) return;
    const ask = wsBridge()?.listAllSessions;
    if (!ask) return;
    let live = true;
    void (async () => {
      try {
        const res = await ask();
        if (live) setAllSessions(res.sessions ?? []);
      } catch {
        if (live) setAllSessions([]);
      }
    })();
    return () => {
      live = false;
    };
  }, [searchOpen]);

  /** What the search overlay can reach: the open sessions plus every agent's listing. */
  const searchHits = useMemo(
    () => sessionHits(appState.openSessions, mergeSessionRows(appState.sessions, allSessions)),
    [appState.openSessions, appState.sessions, allSessions],
  );

  /** The left column's bounds for this window, and the width it renders at today. */
  const leftReserved =
    RAIL_WIDTH + HANDLE_WIDTH + (rightOpen ? rightWidth + HANDLE_WIDTH : 0);
  const leftBounds = columnBounds(windowWidth, leftReserved, LEFT_COLUMN_MIN);
  const effectiveLeftWidth = clampWidth(leftWidth, leftBounds);

  /**
   * The inspector column's bounds, the same rule as the session column's: what the
   * window leaves once the rail, both handles and the session column are taken. The
   * body reserves nothing (issue #2969) — a column may be dragged to the window's own
   * edge, and the only floor is the column's own minimum, which keeps it retrievable.
   */
  const rightReserved =
    RAIL_WIDTH + HANDLE_WIDTH + (leftOpen ? effectiveLeftWidth + HANDLE_WIDTH : 0);
  const rightBounds = columnBounds(windowWidth, rightReserved, RIGHT_COLUMN_MIN);
  const effectiveRightWidth = clampWidth(rightWidth, rightBounds);

  /** Remember a column width and the drag that produced it, in one place each. */
  function resizeLeft(px: number) {
    setLeftWidth(px);
    persistColumnWidth(LS_LEFT_WIDTH, px);
  }

  function resizeRight(px: number) {
    setRightWidth(px);
    persistColumnWidth(LS_RIGHT_WIDTH, px);
  }

  /** 工作区视图切换（vanilla app.js switchView 语义）：
   *  点当前面板 → toggle 回会话视图；点其他面板 → 切换；点 sessions → 回会话视图 */
  function switchView(view: SidebarViewId): void {
    // 换视图即离开那次失败的操作现场，提示随之退场（issue #2637）
    setSwitchError(null);
    if (view === "sessions") {
      setActiveView("sessions");
      return;
    }
    setActiveView((cur) => (cur === view ? "sessions" : view));
  }

  /**
   * A session picked in the search overlay.
   *
   * Two cases, and the difference is not cosmetic: a session already open in this
   * client is just a change of focus, while one the daemon merely listed has to be
   * resumed — `switch_session` is what makes the daemon adopt it, so its cwd and its
   * history arrive with it. Picking the second kind with the first kind's path leaves
   * a window pointed at a session the server never opened.
   */
  async function pickSession(sid: string): Promise<void> {
    if (appState.openSessions.some((o) => o.sid === sid)) {
      selectSession(sid);
      return;
    }
    // The overlay row carries the agent it belongs to (issue #2978), so the session
    // opens under that agent. A row whose listing named no directory passes none, and
    // the daemon's own session index answers instead of any default here.
    const row = searchHits.find((h) => h.sid === sid);
    await selectProjectSession({ path: row?.cwd }, sid);
  }

  /**
   * 选择会话：对话区、输入框、**后台当前会话**在同一次操作里换过去
   * （rant 2026-11-18T18:41:06 清单 1「三处同一」）。
   *
   * 以前这里只改本地的 `activeSid`，后台当前会话原地不动 —— 屏幕在 B、后台在 A，
   * 之后所有「以当前会话为准」的操作都落在 A 上，正是这条 rant 的形状。
   *
   * 顺序不是风格：先问后台，成功才换屏幕。反过来在后台拒绝时留下半切状态（屏幕 B、
   * 后台 A、输入框发到 B）。后台没有这条通道（jsdom / 未接 preload）时退回纯本地切换，
   * 因为那时「后台当前会话」这个量不存在，而不是它拒绝了。
   */
  async function selectSession(sid: string | null): Promise<void> {
    if (!sid) {
      setActiveSid(null);
      setActiveView("sessions");
      return;
    }
    const b = wsBridge();
    if (b?.switchSession) {
      try {
        await b.switchSession({ sessionId: sid });
      } catch (e) {
        // 人没有离开当前会话，所以拒绝必须出现在这里，而不是变成一次没反应的点击。
        setSwitchError((e as Error)?.message ?? String(e));
        return;
      }
    }
    setSwitchError(null);
    setActiveSid(sid);
    setActiveView("sessions");
    void loadHistory(sid);
  }

  /**
   * 关闭一条打开的会话：断开它的连接、清掉为它渲染的内容，**磁盘上的数据保留**
   * （rant 2026-11-18T18:41:06 清单 8）。
   *
   * 三件事必须一起做，少一件就落回这条 rant 报的那类「看着有会话、页面却是空的」：
   * 主进程断开并移出列表、这里清掉渲染内容、**把「已加载」标记一并删掉**。
   * 第三条最容易漏 —— 只清内容不删标记，之后重开这条会话时 `loadHistory` 会因为
   * 它还在集合里直接返回，历史一条都回放不出来，而页面看起来就像那条会话本来就是空的。
   *
   * 关闭失败时什么都不清：主进程说没关掉，内容就还是那条会话的，清掉等于凭空丢内容。
   * 主进程的拒绝是抛出来的（非法 session id），所以这里的失败就是那次 `catch`。
   */
  /**
   * 拖拽调序（issue #3022）：把新顺序交给主进程，顺序的唯一真源在那里。
   *
   * 这里不动本地数组：主进程收下、落盘、广播 `open_sessions`，新顺序从那次广播回来。
   * 失败就什么都不做 —— 列表照旧显示，下一次广播仍是真相。
   */
  async function reorderOpenSessionList(sids: string[]): Promise<void> {
    const b = wsBridge();
    if (!b?.reorderOpenSessions) return;
    await b.reorderOpenSessions({ sids });
  }

  async function closeOpenSession(sid: string): Promise<void> {
    const b = wsBridge();
    if (!b?.closeSession) return;
    try {
      await b.closeSession({ sessionId: sid });
    } catch {
      return;
    }
    transcript.clear(sid);
    scrollMemoryRef.current.delete(sid); // 清单第 9 条：关掉再打开 = 首次打开
    // 「已加载」标记和分页游标一起丢掉，与 `reloadHistory` 清的是同一对：留下游标，
    // 重开时第一页会从旧游标往前取，最新那些记录一页都不在里面。
    historyLoadedRef.current.delete(sid);
    historyPagesRef.current.delete(sid);
    // 关掉的若是当前会话，交给 `reportedSid === null` 那条规则去挑下一条仍然有效的，
    // 而不是在这里自己按列表顺序选 —— 那正是清单 2 禁止的写法。
    if (activeSid === sid) setActiveSid(null);
  }

  // ── 历史按需加载（rant 2026-09-01T20:19:40：GUI 打开会话不显示历史——链路从未接线）──
  // 切会话加载最近一页（limit 50），滚动到顶加载更早一页；游标是绝对 record_index
  // （daemon before_index），不再是「距最新的 offset」（rant 2026-09-20T18:58:44：offset
  // 在翻页间来了新消息时会平移 → 重复 + 更早内容不可达）。回放复用实时那一套 handler
  // （lib/historyReplay.ts），不再走「preview 优先」的旧路径。historyLoaded 集合防重复加载
  // （切换走/回来不重复 append）。
  const historyPagesRef = useRef<ReturnType<typeof createHistoryPages>>(createHistoryPages());
  const historyLoadedRef = useRef<Set<string>>(new Set());
  /**
   * 每条会话的滚动位置（issue #3024 清单第 6、7 条）。归 Shell 而不是 TranscriptView：
   * 切到别的页面时整个聊天区被卸载，位置必须活过那一次卸载。关闭会话时一并删掉
   * （清单第 9 条：关掉再打开按首次打开处理，落在底部）。
   */
  const scrollMemoryRef = useRef<ScrollMemory>(new Map());
  const historyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /**
   * 分页状态住在 ref 的 Map 里（游标与去重集合也住在那里，它是**唯一真源**）。Shell 在
   * 渲染时读它，于是**每落定一页就必须重渲染一次** —— 否则 `hasMore`/`loading` 会停在
   * 上一页的取值上：翻到头之后顶部条仍旧是「可点的按钮」（点下去 `loadOlderHistory`
   * 因为 `!st.hasMore` 立刻 return），正是宿主报障里「长得像能点、点下去是死的」那一类。
   * 这里只做「叫一次重渲染」，不把那两个事实再抄进 state —— 第二个家就是下一份会漂移的副本。
   */
  const [, publishPaging] = useReducer((n: number) => n + 1, 0);

  /**
   * 加载状态的**唯一写入点**：改了就叫一次重渲染。
   *
   * 只在每个 loader 的 `finally` 里发布是不够的 —— 那样 `st.loading = true` 从未到达
   * 渲染：翻页途中顶部条仍是**可点的 BUTTON**，点下去在 `st.loading` 早退处什么也不做
   * （宿主报障里「长得像能点、点下去是死的」那一类，只是换了一个分支），而
   * `canLoadOlder(hasMore, loading)` 的 `loading` 那一半在生产里恒为 false。两个写入点
   * 各写一遍 `publishPaging()` 也不对：同一条规则的两份副本，漏一处不会有任何信号
   * ——`TranscriptView.test.tsx` 是**传 prop** 钉的组件契约，看不见生产读取点。
   * 所以收成一个函数，进入与落定都走它（issue #1979，评审 cyc20261009-151044）。
   */
  function setPagingLoading(st: HistoryPageState, loading: boolean) {
    const changed = st.loading !== loading;
    st.loading = loading;
    if (changed) publishPaging();
  }

  /**
   * 顶部条文案：规则在 `lib/history.ts:loadBarKey` 一处，两个写入点都走这里。
   * `paged` = 这是一次翻页的结果（首屏那次传 false —— 问的问题不同，见该函数的注释）。
   */
  function loadBarText(hasMore: boolean, paged: boolean): string | null {
    const key = loadBarKey(hasMore, paged);
    return key ? t(key) : null;
  }

  /** 切会话：加载最近一页历史（回放进 transcript）。 */
  async function loadHistory(sid: string) {
    if (historyLoadedRef.current.has(sid)) return;
    const b = wsBridge();
    if (!b?.listHistory) return;
    const st = historyPageState(historyPagesRef.current, sid);
    if (st.loading) return;
    setPagingLoading(st, true);
    try {
      const res = await b.listHistory({ sessionId: sid, limit: HISTORY_PAGE, includeRecords: true });
      const page = res.messages || [];
      replayHistoryRecords(unloadedRecords(st, page), sid, transcript);
      applyHistoryPage(st, page, !!res.hasMore);
      transcript.setLoadBar(loadBarText(st.hasMore, false), sid);
      historyLoadedRef.current.add(sid);
    } catch (e) {
      transcript.addSystemMessage(
        t("app.historyFailed", { msg: (e as Error)?.message ?? String(e) }),
        sid,
      );
    } finally {
      // 落定也走同一个写入点：`hasMore` / `oldestIndex` 是在 try 里改的，这一句把它们一起推出去。
      setPagingLoading(st, false);
    }
  }

  /** 滚动到顶：加载更早一页（游标 = 最早已加载记录的 record_index；整块前插 + 滚差补偿由渲染层处理）。 */
  async function loadOlderHistory(sid: string | null) {
    if (!sid) return;
    const b = wsBridge();
    if (!b?.listHistory) return;
    const st = historyPageState(historyPagesRef.current, sid);
    if (!st.hasMore || st.loading || st.oldestIndex === null) return;
    setPagingLoading(st, true);
    try {
      const res = await b.listHistory({
        sessionId: sid,
        limit: HISTORY_PAGE,
        includeRecords: true,
        beforeIndex: st.oldestIndex,
      });
      const page = res.messages || [];
      // 更早一页整体插到最前（回放在独立缓冲里跑同一套 handler，落点由 prependEntries 决定）
      replayHistoryRecordsPrepend(unloadedRecords(st, page), sid, transcript);
      applyHistoryPage(st, page, !!res.hasMore);
      transcript.setLoadBar(loadBarText(st.hasMore, true), sid);
    } catch (e) {
      transcript.addSystemMessage(
        t("app.historyFailed", { msg: (e as Error)?.message ?? String(e) }),
        sid,
      );
    } finally {
      // 落定也走同一个写入点：`hasMore` / `oldestIndex` 是在 try 里改的，这一句把它们一起推出去。
      setPagingLoading(st, false);
    }
  }

  /** 滚动到顶触发（TranscriptView onScroll → 这里防抖 150ms，vanilla 同）。 */
  function onScrollTop() {
    if (historyTimerRef.current) clearTimeout(historyTimerRef.current);
    historyTimerRef.current = setTimeout(() => void loadOlderHistory(activeSid), 150);
  }

  // 分页状态 → 判定的两条事实。此前这里先把两者合成一个 `canLoadOlder` 布尔再传下去，
  // 容器从此分不清「没有更早的页」与「正在取」——而判定本可以只有一处（issue #1979）。
  const activePaging = activeSid ? historyPageState(historyPagesRef.current, activeSid) : null;

  const isPanelView = activeView !== "sessions";
  // 生产 markdown 渲染器（真实 vendor marked/DOMPurify/hljs，Batch 5 承诺项）——
  // 一次构造复用；缺省降级在 TranscriptView 内部仍兜底
  const mdRenderer = useMemo(() => createProdMarkdownRenderer(), []);

  /** Composer 的 /指令路由（vanilla handleCommand 的 React 版；无对话框的指令进 runDirect） */
  function handleCommand(routing: CommandRouting): void {
    const h = dialogHost.current;
    if (!h) return;
    switch (routing.cmd) {
      case "/help":
        h.openHelp();
        break;
      case "/memory":
        h.openMemory(routing.args?.[0]);
        break;
      case "/skills":
        h.openSkills();
        break;
      // rant 2026-10-09T09:43:39（GUI 半边）：额外可写根的入口也是 /sandbox（与 TUI
      // 同名同子命令），因为宿主 2026-10-09 已拍板「并入现有的 /sandbox」。
      case "/sandbox":
        h.openSandbox(routing.args ?? []);
        break;
      case "/rewind":
        h.openRewind();
        break;
      case "/rename":
        h.openRename();
        break;
      case "/delete":
        h.openDelete();
        break;
      case "/sessions":
      case "/open":
        h.openSessions();
        break;
      case "/resume":
        if (routing.args && routing.args.length > 0) h.resumeSession(routing.args[0]);
        else h.openSessions();
        break;
      case "/clear":
      case "/compact":
      case "/version":
      case "/image":
        void h.runDirect(routing.cmd, routing.args ?? []);
        break;
      // rant 2026-09-01T20:22:00：/model /rant /trigger 接线（此前落 cmdUnknown）
      case "/model":
        void handleModelCommand(routing.args ?? []);
        break;
      case "/rant":
        void handleRantCommand(routing.args ?? []);
        break;
      case "/trigger":
        void handleTriggerCommand(routing.args ?? []);
        break;
      default:
        transcript.addSystemMessage(t("app.cmdUnknown", { cmd: routing.cmd }), activeSid);
    }
  }

  /** /model：有参 → 直切模型（对齐 TUI /model <name>）；无参 → 设置面板。 */
  async function handleModelCommand(args: string[]) {
    const name = args[0]?.trim();
    if (name) {
      const b = wsBridge();
      if (!b?.setModel) return;
      try {
        await b.setModel({ model: name });
        transcript.addSystemMessage(t("app.modelSwitched", { model: name }), activeSid);
      } catch (e) {
        transcript.addSystemMessage(
          t("app.modelSwitchFailed", { msg: (e as Error)?.message ?? String(e) }),
          activeSid,
        );
      }
    } else {
      switchView("settings");
    }
  }

  /** /rant：有参 → 直发（/@name 前缀指定目标，对齐 TUI）；无参/空内容 → 对话框。 */
  async function handleRantCommand(args: string[]) {
    const first = args[0] ?? "";
    let agent = "";
    let message = args.join(" ");
    if (first.startsWith("@")) {
      agent = first.slice(1);
      message = args.slice(1).join(" ");
    }
    if (!message.trim()) {
      await newRant();
      return;
    }
    await submitRant({ message, agent });
  }

  /** /trigger：有参 → 直发任务；无参 → 任务面板。 */
  async function handleTriggerCommand(args: string[]) {
    const name = args[0]?.trim();
    if (name) {
      const b = wsBridge();
      if (!b?.triggerTask) return;
      try {
        await b.triggerTask({ name });
        transcript.addSystemMessage(t("app.triggered", { n: name }), activeSid);
        await loadTasks();
      } catch (e) {
        transcript.addSystemMessage(
          t("app.triggerFailed", { msg: (e as Error)?.message ?? String(e) }),
          activeSid,
        );
      }
    } else {
      switchView("tasks");
    }
  }

  /** 升级横幅重启（rant 2026-09-01T20:09:41）：调 emrg:restartDaemon 完整重启链
   *  （stop_all --skip-gui → GUI relaunch → 新 daemon）。TUI 会被 stop 链杀掉，
   *   用户需手动重开（设计如此）。失败 → 系统消息。 */
  async function handleUpgradeRestart() {
    const b = wsBridge();
    if (!b?.restartDaemon) return;
    setRestarting(true);
    try {
      await b.restartDaemon();
      // 成功：进程即将退出重启，无需清理（restarting 状态随进程消亡）
    } catch (e) {
      setRestarting(false);
      transcript.addSystemMessage(
        t("app.upgradeRestartFailed", { msg: e instanceof Error ? e.message : String(e) }),
        activeSid,
      );
    }
  }

  const upgradeBanner = appState.upgradeBanner;
  const showUpgradeBanner =
    upgradeBanner && upgradeBanner.installed !== "" && upgradeBanner.installed !== dismissedUpgrade;

  return (
    <div className="react-shell" data-testid="react-shell">
      {showUpgradeBanner ? (
        <div id="upgrade-banner" role="status" data-testid="upgrade-banner">
          <span id="upgrade-banner-msg">
            {upgradeBanner.current && upgradeBanner.current !== upgradeBanner.installed
              ? t("app.upgradeBannerMsgFromTo", { from: upgradeBanner.current, to: upgradeBanner.installed })
              : t("app.upgradeBannerMsg", { version: upgradeBanner.installed })}
          </span>
          <button
            type="button"
            id="upgrade-banner-restart"
            className="btn btn-primary"
            data-testid="upgrade-banner-restart"
            disabled={restarting}
            onClick={() => void handleUpgradeRestart()}
          >
            {t("app.upgradeRestartBtn")}
          </button>
          <button
            type="button"
            id="upgrade-banner-dismiss"
            className="btn btn-ghost"
            title="✕"
            data-testid="upgrade-banner-dismiss"
            onClick={() => setDismissedUpgrade(upgradeBanner.installed)}
          >
            ✕
          </button>
        </div>
      ) : null}
      <StatusBand
        running={busy}
        round={round}
        startedAtMs={activeTurnStart}
        tokens={turnTokens}
        leftOpen={leftOpen}
        rightOpen={rightOpen}
        onToggleLeft={() => setLeftOpen((v) => !v)}
        onToggleRight={() => setRightOpen((v) => !v)}
      />
      <div className="react-shell-body">
        <IconRail activeView={activeView} onSwitchView={switchView} />
        {leftOpen ? (
          <>
            <aside
              className="react-shell-session"
              data-testid="session-column-aside"
              style={{ width: effectiveLeftWidth, flex: "none" }}
            >
              <SessionColumn
                openSessions={appState.openSessions}
                knownSessions={appState.sessions}
                activeSid={activeSid}
                turnStartBySid={appState.turnStartBySid}
                onSelect={selectSession}
                onClose={(sid) => void closeOpenSession(sid)}
                onReorder={(sids) => void reorderOpenSessionList(sids)}
                onSearch={() => setSearchOpen(true)}
                onNew={() => dialogHost.current?.openNewSession()}
              />
            </aside>
            {/* Both handles are siblings of the columns they resize, never children: a
                6px strip inside the thing it sizes cannot be hit once that thing is 0. */}
            <ResizeHandle
              side="left"
              width={effectiveLeftWidth}
              bounds={leftBounds}
              onResize={resizeLeft}
              onReset={() => resizeLeft(LEFT_COLUMN_DEFAULT)}
            />
          </>
        ) : null}
        <main className="react-shell-main" data-testid="react-shell-main">
          {isPanelView ? (
            <>
              {switchError ? (
                <div className="switch-banner" role="alert" data-testid="switch-error">
                  {t("app.switchFailed", { msg: switchError })}
                </div>
              ) : null}
              <WorkspaceView
                activeView={activeView}
                onSwitch={switchView}
                agents={agents}
                tasks={tasks}
                rants={rants}
                projectSessions={projectSessions}
                projectSessionsError={projectSessionsError}
                currentSid={activeSid}
                version={appState.currentVersion}
                evolutionCount={appState.evolutionCount}
                onViewAgentSessions={(p) => void loadProjectSessions(p)}
                onSelectAgentSession={(p, sid) => void selectProjectSession(p, sid)}
                onAddAgent={() => void addProject()}
                onDeleteAgent={(p) => void deleteAgent(p)}
                onTriggerTask={(task) => void triggerTask(task)}
                onOpenSessionTask={(task) => void openTaskSession(task)}
                onEditTask={(task) => void openTaskForm(task)}
                onDeleteTask={(task) => void deleteTask(task)}
                onNewRant={() => void newRant()}
                onDeleteRant={(r) => void deleteRant(r)}
                renderer={mdRenderer}
              />
            </>
          ) : (
            <>
              {disconnected ? (
                <div className="conn-banner" role="alert" data-testid="conn-banner">
                  {t("app.sessionDisconnected")}
                </div>
              ) : null}
              <TranscriptView
                store={transcript}
                sid={activeSid}
                renderer={mdRenderer}
                hasMore={activePaging?.hasMore ?? false}
                loading={activePaging?.loading ?? false}
                onLoadOlder={onScrollTop}
                // 会话各自的滚动位置（issue #3024 清单第 6、7 条）：这一层活过聊天区的卸载
                scrollMemory={scrollMemoryRef}
              />
              <Composer
                store={transcript}
                sid={activeSid}
                busy={busy}
                onCommand={handleCommand}
                // The session's sandbox tier, as the daemon reported it (rant
                // 2026-09-30T09:30:16, GUI half): `sandbox_set` for a change made
                // anywhere, `resume_result.meta.sandbox` for the session this client
                // just opened. `null` = the daemon has not said, and the chip shows
                // its own default rather than pretending to know.
                sandbox={activeSid ? (appState.sandboxBySid[activeSid] ?? null) : null}
                onSandboxChange={(mode) => void bridge.setSandbox(activeSid, mode)}
                // 会话的额外可写根（rant 2026-10-09T09:43:39, GUI half）：同一个
                // 权威规则——列表是 daemon 的，`sandbox_roots` 帧写它，
                // `resume_result.meta.sandbox_roots` 是后开会话唯一的来源；这里只显示，
                // 并在有人问过之前保持 `null`（= 未知，不是 0）。
                sandboxRoots={activeSid ? (appState.sandboxRootsBySid[activeSid] ?? null) : null}
                // 会话的输入历史（issue #2349 part 2/2）：同一份权威——列表是 daemon 的，
                // 由上面的 effect 按会话读取；`null` = 还没问过（方向键因此照常移动光标）。
                inputHistory={activeSid ? (appState.inputHistoryBySid[activeSid] ?? null) : null}
                // 刚刚发出去的那条也进召回列表（issue #2349 part 2/2）：daemon 已经接受了
                // 它，所以它的文件现在就以这段文字结尾；不跟上，↑ 在「刚打完五句话的新
                // 会话」里就只能翻到会话打开那一刻为止——而那正是宿主最常按 ↑ 的时候。
                onPromptSent={(sid, text) => bridge.notePromptSubmitted(sid, text)}
                onManageSandboxRoots={() => dialogHost.current?.openSandbox([])}
                // /rewind 的选中结果（issue #2384）：文本由 DialogHost 交上来，这里只
                // 负责给它一个新 id——同一个 id 只应用一次，宿主之后打的字不会被盖掉。
                insertRequest={composerInsert}
                // The band carries only what moves; the model and the connection do
                // not, so they live where the model is switched — the composer's chip
                // row (issue #2969). Same readings as before, one row lower.
                modelChip={
                  <span className="conn-chip" data-testid="conn-status" title={t("sidebar.statusTitle")}>
                    <span className={`conn-dot ${appState.connected ? "green" : "gray"}`} />
                    {appState.connected ? appState.model : t("copy.disconnected")}
                    {appState.connected && appState.vision !== null ? (
                      <span
                        className={`conn-vision ${appState.vision ? "on" : "off"}`}
                        data-testid="conn-vision"
                        title={t("app.visionTitle")}
                      >
                        {appState.vision ? t("app.visionOn") : t("app.visionOff")}
                      </span>
                    ) : null}
                  </span>
                }
              />
            </>
          )}
        </main>
        {rightOpen ? (
          <>
            <ResizeHandle
              side="right"
              width={effectiveRightWidth}
              bounds={rightBounds}
              onResize={resizeRight}
              onReset={() => resizeRight(RIGHT_COLUMN_DEFAULT)}
            />
            <aside
              className="react-shell-inspector"
              data-testid="inspector-aside"
              style={{ width: effectiveRightWidth, flex: "none" }}
            >
              <InspectorPanel
                sid={activeSid}
                agentRoot={roots.agentRoot}
                sessionRoot={roots.sessionRoot}
                loadDir={loadDir}
                onOpenMemory={() => dialogHost.current?.openMemory()}
              />
            </aside>
          </>
        ) : null}
        {/* The history browser (item 6): the sessions the left column no longer lists.
            It sits above the dialogs because it is a reading surface, not a question —
            picking a row switches sessions and closes it. */}
        <SearchOverlay
          open={searchOpen}
          hits={searchHits}
          onClose={() => setSearchOpen(false)}
          onPick={(sid) => {
            setSearchOpen(false);
            void pickSession(sid);
          }}
        />
        <DialogHost
          ref={dialogHost}
          sid={activeSid}
          sessions={appState.sessions}
          transcript={transcript}
          appState={appState}
          onSwitchSession={selectSession}
          onSandboxRootsOp={(op, path) => void bridge.setSandboxRoots(activeSid, op, path)}
          // 回退选中的那条的**原文**交回输入框（issue #2384）：与 TUI 同一个行为，
          // 两端在 rewind 之后的差别只剩下渲染方式。
          onRewindPicked={(text) => {
            composerInsertSeq.current += 1;
            setComposerInsert({ id: composerInsertSeq.current, text });
          }}
        />
        <ConfirmDialog request={dialogState.confirm} onDismiss={() => dispatch({ type: "close-confirm" })} />
        {/*
          daemon 的提权提问（rant 2026-09-29T15:52:38）。它渲染在 DialogHost 之外、
          与 ConfirmDialog 并列：提问属于**连接**而不属于某个对话框状态机，daemon
          正在阻塞等它，任何一层「稍后打开」都会把答复推到一个已超时的提问上。
        */}
        <ApprovalDialog
          request={appState.pendingApproval}
          onAnswer={(approved) => void bridge.respondApproval(approved)}
        />
        <TaskFormDialog
          request={taskForm}
          types={taskTypes}
          agents={agents}
          minInterval={minInterval}
          onSubmit={(payload) => void saveTask(payload)}
          onDismiss={() => setTaskForm(null)}
        />
        <RantDialog
          open={rantOpen}
          agents={agents}
          onSubmit={(payload) => void submitRant(payload)}
          onDismiss={() => setRantOpen(false)}
        />
      </div>
    </div>
  );
}
