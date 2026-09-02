import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import { useSnapshotStore } from "../hooks/useSnapshotStore";
import { useDaemonBridge } from "./DaemonBridgeProvider";
import { createProdMarkdownRenderer } from "../lib/vendorMarkdown";
import { dialogReducer, initialDialogState } from "../lib/dialog";
import { ConfirmDialog } from "./ConfirmDialog";
import { TaskFormDialog, type TaskFormPayload } from "./TaskFormDialog";
import { RantDialog } from "./RantDialog";
import type { ProjectRec, RantRec, TaskRec } from "../lib/workspaceView";
import type { SessionRow } from "../lib/openSession";
import { Sidebar, type SidebarViewId } from "./Sidebar";
import { ResultPanel } from "./ResultPanel";
import { WorkspaceView, type WorkspaceViewId } from "./WorkspaceView";
import { TranscriptView } from "./TranscriptView";
import { Composer, type CommandRouting } from "./Composer";
import { DialogHost, type DialogHostHandle } from "./DialogHost";
import {
  HISTORY_PAGE,
  applyHistoryPage,
  createHistoryPages,
  historyPageState,
} from "../lib/history";

/**
 * Shell — React 布局（Batch 0 骨架 + Batch 3 Sidebar/ResultPanel/WorkspaceView
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
 * - activeView 状态（"sessions" | projects | tasks | rants | settings），vanilla
 *   app.js switchView 语义：点当前面板再点一次 → toggle 回会话视图；点其他面板 →
 *   切换；点 💬 → 回会话视图；
 * - 面板视图激活时隐藏会话 chrome（transcript/composer/result-panel —— vanilla
 *   setWorkspaceChrome("panel")），sessions 视图显示完整会话区；
 * - Sidebar nav rail（#side-nav）高亮当前视图 + onSwitchView 接线；
 * - WorkspaceView 数据接线：面板激活加载 listProjects/listTasks/listRants，项目会话子视图
 *   listProjectSessions，动作 switch-session / add-project / delete-project / trigger-task。
 */
/** Shell 用到的 workspace 数据桥方法（与 preload.js 通道对齐） */
interface WorkspaceBridge {
  listProjects(): Promise<ProjectRec[]>;
  listProjectSessions(p: { projectPath: string }): Promise<{ sessions: SessionRow[] }>;
  listTasks(): Promise<TaskRec[]>;
  listRants(p: { status?: string }): Promise<RantRec[]>;
  switchSession(p: { sessionId: string; projectPath?: string }): Promise<unknown>;
  pickProjectDir(): Promise<{ path?: string } | null>;
  registerProject(p: { path: string }): Promise<{ ok?: boolean; path?: string }>;
  removeProject(p: { name: string; path?: string }): Promise<{ ok?: boolean; error?: string; protected?: boolean }>;
  triggerTask(p: { name: string }): Promise<unknown>;
  taskCreate(p: TaskFormPayload): Promise<unknown>;
  taskUpdate(p: TaskFormPayload): Promise<unknown>;
  taskDelete(p: { name: string }): Promise<unknown>;
  taskTemplateList(): Promise<{ name: string }[]>;
  sendRant(p: { message: string; project?: string }): Promise<{ ok?: boolean; count?: number }>;
  /**
   * 升级横幅重启（rant 2026-09-01T20:09:41）：调 emrg:restartDaemon —— 完整 stop
   * 链（TUI→daemon，--skip-gui 跳过 GUI 防自杀）→ GUI 自己 relaunch → 新 GUI
   * ensureDaemon 用新安装代码 spawn 新 daemon。此前仅重启 GUI 外壳导致版本号死循环
   * （daemon 内存 _run_version 不更新 → 心跳持续报版本差 → 横幅永不消失）。
   */
  restartDaemon?(): Promise<unknown>;
  /** /model 直切模型（rant 2026-09-01T20:22:00：对齐 TUI，preload.js 已暴露 emrg:setModel） */
  setModel?(p: { model: string }): Promise<unknown>;
  /** 历史分页加载（rant 2026-09-01T20:19:40：切会话/滚动到顶；daemon list_history） */
  listHistory?(p: {
    sessionId: string;
    limit?: number;
    offset?: number;
    /** rant 2026-09-02T10:03:29：true → daemon 同时返回 user + assistant 消息 */
    includeAssistant?: boolean;
  }): Promise<{
    messages: Array<{ record_index?: number; role?: string; content?: string; preview?: string; timestamp?: string }>;
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
  const dialogHost = useRef<DialogHostHandle>(null);
  // workspace 面板数据（Batch 5 接线：项目/任务/Rant 真实列表 + 项目会话）
  const [projects, setProjects] = useState<ProjectRec[]>([]);
  const [tasks, setTasks] = useState<TaskRec[]>([]);
  const [rants, setRants] = useState<RantRec[]>([]);
  const [projectSessions, setProjectSessions] = useState<SessionRow[] | null>(null);
  const [projectSessionsError, setProjectSessionsError] = useState<string | null>(null);
  const [dialogState, dispatch] = useReducer(dialogReducer, initialDialogState);
  // Batch 5 slice 8：任务表单 + Rant 对话框（vanilla openTaskForm/openRantForm 语义）
  const [taskForm, setTaskForm] = useState<{ task: TaskRec | null } | null>(null);
  const [rantOpen, setRantOpen] = useState(false);
  const [taskTypes, setTaskTypes] = useState<string[]>([]);
  // 升级横幅（rant 2026-08-27T21:54:51：React 迁移丢 upgrade 事件）：dismissed 记录
  // 已提示过的 installed 版本（vanilla state.lastKnownVersion 语义——心跳每 15s 重发
  // 同一版本，不重复弹）；restarting 防重复点击（relaunch 后进程即退出）。
  const [dismissedUpgrade, setDismissedUpgrade] = useState<string | null>(null);
  const [restarting, setRestarting] = useState(false);
  // 会话信息行（rant 2026-09-01T20:16:55：对齐 TUI 状态栏——id/name/project/消息数/busy 计时）
  const [elapsed, setElapsed] = useState(0);
  const busyStartRef = useRef<number | null>(null);
  const activeBusy = activeSid ? (appState.busyBySid[activeSid] ?? false) : false;
  useEffect(() => {
    if (activeBusy) {
      busyStartRef.current = Date.now();
      setElapsed(0);
      const iv = setInterval(() => {
        const base = busyStartRef.current ?? Date.now();
        setElapsed(Math.floor((Date.now() - base) / 1000));
      }, 500);
      return () => clearInterval(iv);
    }
    busyStartRef.current = null;
    setElapsed(0);
  }, [activeBusy]);

  // Rant 2026-09-02T10:36:26：侧边栏每个运行会话的 [m:ss] 计时——任意会话 busy 时
  // 共享一个 1s tick 触发重渲染（Sidebar 读 Date.now() 计算 elapsed；多会话复用同一定时器）。
  const [, setSidebarTick] = useState(0);
  const hasRunningSessions = Object.keys(appState.turnStartBySid).length > 0;
  useEffect(() => {
    if (!hasRunningSessions) return;
    const iv = setInterval(() => setSidebarTick((t) => t + 1), 1000);
    return () => clearInterval(iv);
  }, [hasRunningSessions]);
  const activeSessionEntry = appState.openSessions.find((o) => o.sid === activeSid);
  const activeKnown = appState.sessions.find((s) => s.session_id === activeSid);
  const activeTitle = activeSessionEntry?.title || activeKnown?.title || t("app.unnamed");
  const activeProject = activeSessionEntry?.projectName || "";
  const activeMsgCount = (activeKnown as { message_count?: number } | undefined)?.message_count ?? 0;
  const mmss = `${String(Math.floor(elapsed / 60)).padStart(2, "0")}:${String(elapsed % 60).padStart(2, "0")}`;

  // open_sessions 广播到达且尚无激活会话 → 自动选第一个（vanilla 同语义）
  useEffect(() => {
    if (activeSid === null && appState.openSessions.length > 0) {
      const sid = appState.openSessions[0].sid;
      setActiveSid(sid);
      void loadHistory(sid);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appState.openSessions, activeSid]);

  // ── workspace 面板数据加载（vanilla loadTaskMeta/renderRantList 语义） ──
  async function loadProjects() {
    const b = wsBridge();
    if (!b?.listProjects) return;
    try {
      setProjects(await b.listProjects());
    } catch {
      setProjects([]);
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

  async function loadProjectSessions(p: ProjectRec) {
    const b = wsBridge();
    if (!b?.listProjectSessions || !p.path) return;
    setProjectSessions(null);
    setProjectSessionsError(null);
    try {
      const res = await b.listProjectSessions({ projectPath: p.path });
      setProjectSessions(res.sessions ?? []);
    } catch (e) {
      setProjectSessionsError((e as Error)?.message ?? String(e));
    }
  }

  // 面板激活 → 加载对应数据（每次进入刷新，vanilla 面板激活时拉取）
  useEffect(() => {
    if (activeView === "projects") void loadProjects();
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
    if (!b?.pickProjectDir || !b?.registerProject) return;
    try {
      const res = await b.pickProjectDir();
      if (!res?.path) return;
      await b.registerProject({ path: res.path });
      await loadProjects();
    } catch (e) {
      transcript.addSystemMessage(t("deleteProject.failed", { msg: (e as Error)?.message ?? String(e) }), activeSid);
    }
  }

  function deleteProject(p: ProjectRec) {
    dispatch({
      type: "open-confirm",
      payload: {
        title: t("deleteProject.title"),
        message: t("deleteProject.body", { name: String(p.name || p.path || "") }),
        okText: "dlg.delete",
        danger: true,
        onOk: async () => {
          const b = wsBridge();
          if (!b?.removeProject) return;
          try {
            await b.removeProject({ name: p.name || "", path: p.path || "" });
            await loadProjects();
          } catch (e) {
            transcript.addSystemMessage(t("deleteProject.failed", { msg: (e as Error)?.message ?? String(e) }), activeSid);
          }
        },
      },
    });
  }

  async function selectProjectSession(p: ProjectRec, sid: string) {
    const b = wsBridge();
    if (b?.switchSession) await b.switchSession({ sessionId: sid });
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
      await b.switchSession({ sessionId: task.session_id, projectPath: task.project_path });
      setActiveSid(task.session_id);
      setActiveView("sessions");
      void loadHistory(task.session_id);
    } catch (e) {
      transcript.addSystemMessage(
        t("openSession.loadFailed", { msg: (e as Error)?.message ?? String(e) }),
        activeSid,
      );
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
    if (!isEdit) await loadProjects(); // 新建需要项目下拉
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

  function deleteTask(task: TaskRec) {
    dispatch({
      type: "open-confirm",
      payload: {
        title: t("settings.taskDelete"),
        message: t("settings.taskDeleteConfirm", { name: String(task.name || "") }),
        okText: "dlg.delete",
        danger: true,
        onOk: async () => {
          const b = wsBridge();
          if (!b?.taskDelete || !task.name) return;
          try {
            await b.taskDelete({ name: task.name });
            transcript.addSystemMessage(t("settings.taskDeleted"), activeSid);
            await loadTasks();
          } catch (e) {
            transcript.addSystemMessage(t("app.tasksFailed", { msg: (e as Error)?.message ?? String(e) }), activeSid);
          }
        },
      },
    });
  }

  async function submitRant(payload: { message: string; project: string }) {
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

  /** 打开 Rant 对话框：vanilla openRantForm 语义 —— 显式 listProjects 填充项目下拉 */
  async function newRant() {
    if (projects.length === 0) await loadProjects();
    setRantOpen(true);
  }

  const busy = activeSid ? (appState.busyBySid[activeSid] ?? false) : false;
  const disconnected = activeSid ? (appState.disconnectedBySid[activeSid] ?? false) : false;

  /** 工作区视图切换（vanilla app.js switchView 语义）：
   *  点当前面板 → toggle 回会话视图；点其他面板 → 切换；点 sessions → 回会话视图 */
  function switchView(view: SidebarViewId): void {
    if (view === "sessions") {
      setActiveView("sessions");
      return;
    }
    setActiveView((cur) => (cur === view ? "sessions" : view));
  }

  /** 选择会话：切换 sid + 回会话视图（vanilla switchSession → activeView="sessions"） */
  function selectSession(sid: string | null): void {
    setActiveSid(sid);
    setActiveView("sessions");
    if (sid) void loadHistory(sid);
  }

  // ── 历史按需加载（rant 2026-09-01T20:19:40：GUI 打开会话不显示历史——链路从未接线）──
  // vanilla historyPages 移植：切会话加载最近一页（limit 50，offset 从最新往回数），
  // 滚动到顶加载更早一页。TranscriptView 滚动监听已在（capture），这里补加载函数 +
  // canLoadOlder/onLoadOlder 接线。historyLoaded 集合防重复加载（切换走/回来不重复 append）。
  const historyPagesRef = useRef<ReturnType<typeof createHistoryPages>>(createHistoryPages());
  const historyLoadedRef = useRef<Set<string>>(new Set());
  const historyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** 切会话：加载最近一页历史（用户消息气泡，只读；vanilla loadHistory）。 */
  async function loadHistory(sid: string) {
    if (historyLoadedRef.current.has(sid)) return;
    const b = wsBridge();
    if (!b?.listHistory) return;
    const st = historyPageState(historyPagesRef.current, sid);
    if (st.loading) return;
    st.loading = true;
    try {
      const res = await b.listHistory({ sessionId: sid, limit: HISTORY_PAGE, offset: st.offset, includeAssistant: true });
      const msgs = res.messages || [];
      for (const m of msgs) {
        const text = (m as { preview?: string; content?: string }).preview
          || (m as { preview?: string; content?: string }).content
          || "";
        // rant 2026-09-02T10:03:29：assistant 角色 → 助手气泡；user → history 气泡
        transcript.addHistoryMessage(text, sid, (m as { role?: string }).role === "assistant" ? "assistant" : undefined);
      }
      applyHistoryPage(st, msgs.length, !!res.hasMore);
      transcript.setLoadBar(st.hasMore ? t("app.historyLoadMore") : null, sid);
      historyLoadedRef.current.add(sid);
    } catch (e) {
      transcript.addSystemMessage(
        t("app.historyFailed", { msg: (e as Error)?.message ?? String(e) }),
        sid,
      );
    } finally {
      st.loading = false;
    }
  }

  /** 滚动到顶：加载更早一页（prepend + 滚差补偿由渲染层处理；vanilla loadOlderHistory）。 */
  async function loadOlderHistory(sid: string | null) {
    if (!sid) return;
    const b = wsBridge();
    if (!b?.listHistory) return;
    const st = historyPageState(historyPagesRef.current, sid);
    if (!st.hasMore || st.loading) return;
    st.loading = true;
    try {
      const res = await b.listHistory({ sessionId: sid, limit: HISTORY_PAGE, offset: st.offset, includeAssistant: true });
      const msgs = res.messages || [];
      // prepend：倒序 unshift 保持时间序（vanilla insertAfter(bar) 同序插入会页内倒序——
      // React 版修正为逐条 unshift，倒序迭代使页底 = 上一页顶部，无缝衔接）
      for (const m of [...msgs].reverse()) {
        const text = (m as { preview?: string; content?: string }).preview
          || (m as { preview?: string; content?: string }).content
          || "";
        // rant 2026-09-02T10:03:29：assistant 角色 → 助手气泡；user → history 气泡
        transcript.prependHistoryMessage(text, sid, (m as { role?: string }).role === "assistant" ? "assistant" : undefined);
      }
      applyHistoryPage(st, msgs.length, !!res.hasMore);
      transcript.setLoadBar(st.hasMore ? t("app.historyLoadMore") : t("app.historyNoMore"), sid);
    } catch (e) {
      transcript.addSystemMessage(
        t("app.historyFailed", { msg: (e as Error)?.message ?? String(e) }),
        sid,
      );
    } finally {
      st.loading = false;
    }
  }

  /** 滚动到顶触发（TranscriptView onScroll → 这里防抖 150ms，vanilla 同）。 */
  function onScrollTop() {
    if (historyTimerRef.current) clearTimeout(historyTimerRef.current);
    historyTimerRef.current = setTimeout(() => void loadOlderHistory(activeSid), 150);
  }

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

  /** /rant：有参 → 直发（/@project 前缀指定项目，对齐 TUI）；无参/空内容 → 对话框。 */
  async function handleRantCommand(args: string[]) {
    const first = args[0] ?? "";
    let project = "";
    let message = args.join(" ");
    if (first.startsWith("@")) {
      project = first.slice(1);
      message = args.slice(1).join(" ");
    }
    if (!message.trim()) {
      await newRant();
      return;
    }
    await submitRant({ message, project });
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
      <header className="react-shell-header">
        <span className="react-shell-brand">✦ EMRG</span>
        {activeSid ? (
          <span className="react-shell-session" data-testid="session-info" title={activeSid}>
            {activeTitle} ({activeSid})
            {activeProject ? ` · ${activeProject}` : ""}
            {" · "}
            {t("app.msgCount", { count: activeMsgCount })}
            {activeBusy ? ` [${mmss}]` : ""}
          </span>
        ) : null}
        <span className="react-shell-conn" data-testid="conn-status" title={t("sidebar.statusTitle")}>
          <span className={`conn-dot ${appState.connected ? "green" : "gray"}`} />
          {appState.connected ? appState.model : t("copy.disconnected")}
        </span>
      </header>
      <div className="react-shell-body">
        <aside className="react-shell-sidebar" data-testid="react-shell-sidebar">
          <Sidebar
            openSessions={appState.openSessions}
            knownSessions={appState.sessions}
            activeSid={activeSid}
            activeView={activeView}
            turnStartBySid={appState.turnStartBySid}
            onSelect={selectSession}
            onSwitchView={switchView}
            onNewChat={() => dialogHost.current?.openNewSession()}
            onOpenChat={() => dialogHost.current?.openSessions()}
          />
        </aside>
        <main className="react-shell-main" data-testid="react-shell-main">
          {isPanelView ? (
            <WorkspaceView
              activeView={activeView}
              onSwitch={switchView}
              projects={projects}
              tasks={tasks}
              rants={rants}
              projectSessions={projectSessions}
              projectSessionsError={projectSessionsError}
              currentSid={activeSid}
              version={appState.currentVersion}
              evolutionCount={appState.evolutionCount}
              onViewProjectSessions={(p) => void loadProjectSessions(p)}
              onSelectProjectSession={(p, sid) => void selectProjectSession(p, sid)}
              onAddProject={() => void addProject()}
              onDeleteProject={deleteProject}
              onTriggerTask={(task) => void triggerTask(task)}
              onOpenSessionTask={(task) => void openTaskSession(task)}
              onEditTask={(task) => void openTaskForm(task)}
              onDeleteTask={(task) => deleteTask(task)}
              onNewRant={() => void newRant()}
            />
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
                canLoadOlder={
                  activeSid
                    ? (() => {
                        const hs = historyPageState(historyPagesRef.current, activeSid);
                        return hs.hasMore && !hs.loading;
                      })()
                    : false
                }
                onLoadOlder={onScrollTop}
              />
              <Composer store={transcript} sid={activeSid} busy={busy} onCommand={handleCommand} />
            </>
          )}
        </main>
        {!isPanelView && <ResultPanel sid={activeSid} workspaceRoot="" />}
        <DialogHost
          ref={dialogHost}
          sid={activeSid}
          sessions={appState.sessions}
          transcript={transcript}
          appState={appState}
          onSwitchSession={selectSession}
        />
        <ConfirmDialog request={dialogState.confirm} onDismiss={() => dispatch({ type: "close-confirm" })} />
        <TaskFormDialog
          request={taskForm}
          types={taskTypes}
          projects={projects}
          onSubmit={(payload) => void saveTask(payload)}
          onDismiss={() => setTaskForm(null)}
        />
        <RantDialog
          open={rantOpen}
          projects={projects}
          onSubmit={(payload) => void submitRant(payload)}
          onDismiss={() => setRantOpen(false)}
        />
      </div>
    </div>
  );
}
