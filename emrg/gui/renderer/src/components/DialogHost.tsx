import { forwardRef, useEffect, useImperativeHandle, useReducer, useState } from "react";
import { useI18n } from "../lib/i18n";
import { COMMANDS, hintText } from "../lib/commands";
import { helpRows, type MemoryRow, type SkillRow } from "../lib/dialogLists";
import { rewindPointRow, type RewindPoint } from "../lib/rewind";
import {
  isProtectedProject,
  projectRowView,
  sessionRowView,
  type ProjectRow,
  type SessionRow,
} from "../lib/openSession";
import { dialogReducer, initialDialogState } from "../lib/dialog";
import type { TranscriptStore } from "../lib/transcript";
import type { DaemonAppState, SessionSummary } from "../lib/daemonBridge";
import { ConfirmDialog } from "./ConfirmDialog";
import { RenameDialog } from "./RenameDialog";
import { HelpDialog } from "./HelpDialog";
import { MemoryDialog, type MemoryDetailView } from "./MemoryDialog";
import { SkillsDialog } from "./SkillsDialog";
import { RewindDialog } from "./RewindDialog";
import { OpenSessionDialog, type OpenSessionStep } from "./OpenSessionDialog";
import { NewSessionDialog } from "./NewSessionDialog";

/**
 * DialogHost — Batch 5 slice 4：对话框宿主 + /指令路由落地层。
 *
 * 职责（镜像 vanilla `js/dialogs.js` + `app.js handleCommand`）：
 * - 持有全部对话框的打开状态与列表数据（help/memory/skills/rewind/rename/confirm/
 *   open-session/new-session），数据经 window.emrg 加载（构造注入的组件只收 props，
 *   本层是唯一接线方）；
 * - 经 forwardRef 暴露命令式句柄 DialogHostHandle，Shell 的 onCommand 路由到这里
 *   （/help /memory /skills /rewind /rename /delete /sessions /open /resume +
 *   直接执行类 /clear /compact /version /image）；
 * - 需要会话的指令在无激活会话时经 transcript.addSystemMessage 提示（vanilla
 *   app.needSession 语义）。
 *
 * 降级：window.emrg 缺失（jsdom / 未接 preload）时对话框不崩溃——数据加载为 no-op，
 * 直接类指令（/version /image）仍可在 transcript 中给出反馈。
 */

/** DialogHost 用到的 preload 桥方法（与 preload.js 的 52 通道子集对齐） */
interface EmrgBridge {
  listProjects(): Promise<ProjectRow[]>;
  listProjectSessions(p: { projectPath: string }): Promise<{ sessions: SessionRow[] }>;
  newSession(p: { projectPath: string }): Promise<{ session_id: string }>;
  pickProjectDir(): Promise<{ path?: string } | null>;
  registerProject(p: { path: string }): Promise<{ ok: boolean; path: string }>;
  switchSession(p: { sessionId: string }): Promise<unknown>;
  deleteSession(p: { sessionId: string }): Promise<unknown>;
  renameSession(p: { sessionId: string; title: string }): Promise<unknown>;
  clearSession(p: { sessionId: string }): Promise<unknown>;
  compactSession(p: { sessionId: string }): Promise<unknown>;
  listHistory(p: { sessionId: string }): Promise<{ messages: RewindPoint[]; hasMore?: boolean }>;
  rewindSession(p: { sessionId: string; recordIndex: number }): Promise<unknown>;
  listMemories(p: { scope: "session" | "project"; sessionId?: string }): Promise<MemoryRow[]>;
  readMemory(p: { memoryId: string; scope: "session" | "project"; sessionId?: string }): Promise<{
    id?: string;
    title?: string;
    content?: string;
  }>;
  listSkills(): Promise<SkillRow[]>;
  removeProject(p: { name: string; path: string }): Promise<{ ok: boolean; error?: string }>;
}

function bridge(): EmrgBridge | undefined {
  return (window as unknown as { emrg?: EmrgBridge }).emrg;
}

/** 列表式对话框（各自持有 open 布尔 + 数据加载 effect） */
type ListDialogKind = "help" | "memory" | "skills" | "rewind" | "sessions" | "newSession";
type ListDialogOpen = Record<ListDialogKind, boolean>;

const CLOSED: ListDialogOpen = { help: false, memory: false, skills: false, rewind: false, sessions: false, newSession: false };

export interface DialogHostProps {
  /** 激活会话（null = 未激活） */
  sid: string | null;
  /** 已知会话列表（重命名当前标题用；bridge.store.sessions） */
  sessions: SessionSummary[];
  /** 聊天状态机（无会话提示 / /clear /compact 系统消息） */
  transcript: TranscriptStore;
  /** 应用级状态（/version 的版本/实例/模型/进化次数） */
  appState: DaemonAppState;
  /** 会话切换（Shell setActiveSid；删除激活会话时传 null 让 Shell 自动选相邻） */
  onSwitchSession: (sid: string | null) => void;
}

export interface DialogHostHandle {
  openHelp(): void;
  openMemory(scopeArg?: string): void;
  openSkills(): void;
  openRewind(): void;
  openRename(sid?: string): void;
  openDelete(sid?: string): void;
  openSessions(): void;
  openNewSession(): void;
  /** /resume <id>：直接切换会话 */
  resumeSession(sid: string): void;
  /** /clear /compact /version /image：无需对话框的直接执行类指令 */
  runDirect(cmd: string, args: string[]): Promise<void>;
}

export const DialogHost = forwardRef<DialogHostHandle, DialogHostProps>(function DialogHost(
  { sid, sessions, transcript, appState, onSwitchSession },
  ref,
) {
  const { t } = useI18n();
  // confirm/rename 走 lib/dialog 的 reducer（vanilla showConfirm/showRename 请求槽）
  const [dialogState, dispatch] = useReducer(dialogReducer, initialDialogState);
  const [listDialog, setListDialog] = useState<ListDialogOpen>(CLOSED);
  const [memoryScope, setMemoryScope] = useState<"session" | "project">("project");

  // 列表数据（null = loading）
  const [projects, setProjects] = useState<ProjectRow[] | null>(null);
  const [openStep, setOpenStep] = useState<OpenSessionStep>("projects");
  const [openSessions, setOpenSessions] = useState<SessionRow[] | null>(null);
  const [openProjectName, setOpenProjectName] = useState("");
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [memories, setMemories] = useState<MemoryRow[] | null>(null);
  const [memoryDetail, setMemoryDetail] = useState<MemoryDetailView | null>(null);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const [skills, setSkills] = useState<SkillRow[] | null>(null);
  const [skillsError, setSkillsError] = useState<string | null>(null);
  const [points, setPoints] = useState<RewindPoint[] | null>(null);
  const [rewindError, setRewindError] = useState<string | null>(null);

  const openDialog = (kind: ListDialogKind) => setListDialog((s) => ({ ...s, [kind]: true }));
  const closeDialog = (kind: ListDialogKind) => setListDialog((s) => ({ ...s, [kind]: false }));

  /** 无激活会话 → 系统提示并返回 false（vanilla needSession 守卫） */
  function needSession(): boolean {
    if (sid) return true;
    transcript.addSystemMessage(t("app.needSession"), sid);
    return false;
  }

  // ── 数据加载（打开即拉取；bridge 缺失时 no-op 降级） ──

  // projects：open-session / new-session 共用
  useEffect(() => {
    if (!listDialog.sessions && !listDialog.newSession) return;
    const b = bridge();
    if (!b) return;
    let cancelled = false;
    setProjects(null);
    setSessionsError(null);
    b.listProjects()
      .then((rows) => {
        if (!cancelled) setProjects(rows);
      })
      .catch((e) => {
        if (!cancelled) setSessionsError((e as Error)?.message ?? String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [listDialog.sessions, listDialog.newSession]);

  // memories（/memory [session|project]）
  useEffect(() => {
    if (!listDialog.memory) return;
    const b = bridge();
    if (!b) return;
    let cancelled = false;
    setMemories(null);
    setMemoryDetail(null);
    setMemoryError(null);
    b.listMemories({ scope: memoryScope, sessionId: sid ?? undefined })
      .then((rows) => {
        if (!cancelled) setMemories(rows);
      })
      .catch((e) => {
        if (!cancelled) setMemoryError((e as Error)?.message ?? String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [listDialog.memory, memoryScope, sid]);

  // skills（/skills）
  useEffect(() => {
    if (!listDialog.skills) return;
    const b = bridge();
    if (!b) return;
    let cancelled = false;
    setSkills(null);
    setSkillsError(null);
    b.listSkills()
      .then((rows) => {
        if (!cancelled) setSkills(rows);
      })
      .catch((e) => {
        if (!cancelled) setSkillsError((e as Error)?.message ?? String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [listDialog.skills]);

  // rewind（/rewind — 需要激活会话）
  useEffect(() => {
    if (!listDialog.rewind) return;
    const b = bridge();
    if (!b || !sid) return;
    let cancelled = false;
    setPoints(null);
    setRewindError(null);
    b.listHistory({ sessionId: sid })
      .then(({ messages }) => {
        if (!cancelled) setPoints(messages);
      })
      .catch((e) => {
        if (!cancelled) setRewindError((e as Error)?.message ?? String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [listDialog.rewind, sid]);

  // ── 命令路由动作 ──

  function openMemory(scopeArg?: string): void {
    setMemoryScope(scopeArg === "session" ? "session" : "project");
    openDialog("memory");
  }

  function openRename(sidOverride?: string): void {
    const target = sidOverride ?? sid;
    if (!target) {
      if (!sid) transcript.addSystemMessage(t("app.needSession"), sid);
      return;
    }
    const cur = sessions.find((s) => s.session_id === target);
    dispatch({ type: "open-rename", payload: { sessionId: target, currentTitle: cur?.title } });
  }

  function openDelete(sidOverride?: string): void {
    const target = sidOverride ?? sid;
    if (!target) {
      if (!sid) transcript.addSystemMessage(t("app.needSession"), sid);
      return;
    }
    dispatch({
      type: "open-confirm",
      payload: {
        title: t("copy.deleteConfirmTitle"),
        message: t("copy.deleteConfirmBody"),
        okText: "dlg.delete",
        danger: true,
        onOk: async () => {
          const b = bridge();
          if (b) await b.deleteSession({ sessionId: target });
          if (target === sid) onSwitchSession(null); // 删除激活会话 → Shell 自动选相邻
        },
      },
    });
  }

  /** 打开会话两步流：项目 → 会话（vanilla showOpenSessionDialog） */
  async function pickProject(p: ProjectRow): Promise<void> {
    const b = bridge();
    if (!b || !p.path) return;
    setOpenStep("sessions");
    setOpenProjectName(p.name || p.path);
    setOpenSessions(null);
    setSessionsError(null);
    try {
      const res = await b.listProjectSessions({ projectPath: p.path });
      setOpenSessions(res.sessions ?? []);
    } catch (e) {
      setSessionsError((e as Error)?.message ?? String(e));
    }
  }

  /** 删除项目：受保护 → 拒绝提示；否则确认后 removeProject + 重载列表 */
  function deleteProject(p: ProjectRow): void {
    if (isProtectedProject(p)) {
      dispatch({
        type: "open-confirm",
        payload: {
          title: t("deleteProject.title"),
          message: t("deleteProject.protectedBody", { name: String(p.name || p.path || "") }),
          okText: "dlg.gotIt",
          danger: false,
        },
      });
      return;
    }
    dispatch({
      type: "open-confirm",
      payload: {
        title: t("deleteProject.title"),
        message: t("deleteProject.body", { name: String(p.name || p.path || "") }),
        okText: "dlg.delete",
        danger: true,
        onOk: async () => {
          const b = bridge();
          if (!b) return;
          const res = await b.removeProject({ name: p.name || "", path: p.path || "" });
          if (res && res.error) {
            transcript.addSystemMessage(t("deleteProject.failed", { msg: String(res.error) }), sid);
          } else {
            transcript.addSystemMessage(t("deleteProject.removed", { name: String(p.name || p.path || "") }), sid);
            setProjects(null); // 重载项目列表
          }
        },
      },
    });
  }

  /** 新建项目 = 选目录 → 注册 → 重载项目列表（vanilla new-session 的 new-project 路径） */
  async function newProject(): Promise<void> {
    const b = bridge();
    if (!b) return;
    try {
      const res = await b.pickProjectDir();
      if (!res?.path) return;
      await b.registerProject({ path: res.path });
      setProjects(null);
      const rows = await b.listProjects();
      setProjects(rows);
    } catch (e) {
      setSessionsError((e as Error)?.message ?? String(e));
    }
  }

  /** 新建会话：在选中项目创建 → 切到新会话并关闭 */
  async function createSession(projectPath: string): Promise<void> {
    const b = bridge();
    if (!b) return;
    const res = await b.newSession({ projectPath });
    if (res?.session_id) onSwitchSession(res.session_id);
    closeDialog("newSession");
  }

  /** /clear /compact /version /image：直接执行（无需对话框） */
  async function runDirect(cmd: string, _args: string[]): Promise<void> {
    const b = bridge();
    switch (cmd) {
      case "/clear":
        if (!needSession()) return;
        if (b) await b.clearSession({ sessionId: sid as string });
        transcript.clear(sid);
        transcript.addSystemMessage(t("app.cleared"), sid);
        break;
      case "/compact":
        if (!needSession()) return;
        if (b) await b.compactSession({ sessionId: sid as string });
        transcript.addSystemMessage(t("app.compacted"), sid);
        break;
      case "/version":
        transcript.addSystemMessage(
          t("app.versionInfo", {
            ver: appState.currentVersion || "0.2.8",
            id: appState.serverId || t("app.unknown"),
            model: appState.model || t("app.unknown"),
            n: appState.evolutionCount ?? 0,
          }),
          sid,
        );
        break;
      case "/image":
        transcript.addSystemMessage(t("app.imagePaste"), sid);
        break;
      default:
        transcript.addSystemMessage(t("app.cmdUnknown", { cmd }), sid);
    }
  }

  useImperativeHandle(ref, () => ({
    openHelp: () => openDialog("help"),
    openMemory,
    openSkills: () => openDialog("skills"),
    openRewind: () => {
      if (!needSession()) return;
      openDialog("rewind");
    },
    openRename,
    openDelete,
    openSessions: () => {
      setOpenStep("projects");
      openDialog("sessions");
    },
    openNewSession: () => openDialog("newSession"),
    resumeSession: (target) => onSwitchSession(target),
    runDirect,
  }));

  return (
    <>
      <ConfirmDialog request={dialogState.confirm} onDismiss={() => dispatch({ type: "close-confirm" })} />
      <RenameDialog
        request={dialogState.rename}
        onSubmit={async (sessionId, title) => {
          const b = bridge();
          if (b) await b.renameSession({ sessionId, title });
          dispatch({ type: "close-rename" });
        }}
        onDismiss={() => dispatch({ type: "close-rename" })}
      />
      <HelpDialog
        open={listDialog.help}
        rows={helpRows(COMMANDS, (cmd) => hintText(cmd, t))}
        onDismiss={() => closeDialog("help")}
      />
      <MemoryDialog
        open={listDialog.memory}
        memories={memories}
        scope={memoryScope}
        error={memoryError}
        detail={memoryDetail}
        onSelect={async (id) => {
          const b = bridge();
          if (!b) return;
          try {
            const m = await b.readMemory({ memoryId: id, scope: memoryScope, sessionId: sid ?? undefined });
            setMemoryDetail({ title: m.title || id, body: m.content || "" });
          } catch (e) {
            setMemoryError((e as Error)?.message ?? String(e));
          }
        }}
        onDismiss={() => closeDialog("memory")}
      />
      <SkillsDialog open={listDialog.skills} skills={skills} error={skillsError} onDismiss={() => closeDialog("skills")} />
      <RewindDialog
        open={listDialog.rewind}
        points={points}
        error={rewindError}
        onPick={async (index) => {
          const b = bridge();
          if (!b || !sid) return;
          try {
            await b.rewindSession({ sessionId: sid, recordIndex: index });
            closeDialog("rewind");
          } catch (e) {
            setRewindError((e as Error)?.message ?? String(e));
          }
        }}
        onDismiss={() => closeDialog("rewind")}
      />
      <OpenSessionDialog
        open={listDialog.sessions}
        step={openStep}
        projects={openStep === "projects" ? projects : null}
        sessions={openStep === "sessions" ? openSessions : null}
        projectName={openProjectName}
        currentSid={sid}
        error={sessionsError}
        onPickProject={pickProject}
        onPickSession={async (s) => {
          const b = bridge();
          if (b) await b.switchSession({ sessionId: s.session_id });
          onSwitchSession(s.session_id);
          closeDialog("sessions");
        }}
        onDeleteProject={deleteProject}
        onNewProject={newProject}
        onNewSession={() => {
          closeDialog("sessions");
          openDialog("newSession");
        }}
        onDismiss={() => closeDialog("sessions")}
      />
      <NewSessionDialog
        open={listDialog.newSession}
        projects={projects}
        error={sessionsError}
        onPickProject={(p) => {
          if (p.path) void createSession(p.path);
        }}
        onNewProject={newProject}
        onDismiss={() => closeDialog("newSession")}
      />
    </>
  );
});

// 类型 re-export（测试/调用方构造行视图用）
export { projectRowView, sessionRowView, rewindPointRow };
