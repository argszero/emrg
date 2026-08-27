import { useEffect, useMemo, useState } from "react";
import { useI18n } from "../lib/i18n";
import { SettingsPanel } from "./SettingsPanel";
import { sessionRowView, type SessionRow } from "../lib/openSession";
import {
  buildEvolveProjects,
  preprocessRantMarkdown,
  projectHint,
  rantFirstLine,
  rantStatusMeta,
  rantTimestamp,
  taskHint,
  taskMeta,
  taskStatusBadge,
  type ProjectRec,
  type RantFilter,
  type RantRec,
  type TaskRec,
} from "../lib/workspaceView";

/**
 * WorkspaceView — Batch 3: 工作区面板（项目 / 任务 / Rant / 设置 视图切换）。
 * 从 vanilla `js/dialogs.js` 的列表区（renderProjectList / renderTaskList /
 * renderRantList）迁移为 React 组件（设计文档 §5 Batch 3 项 3）。
 *
 * - 数据注入（不直接碰 daemon/IPC，Batch 5 接线）；类名与 vanilla CSS 一致
 *   （.workspace-view/.task-list/.task-row/.task-badge/.rant-head/.rant-col-*），Batch 5 复用
 * - 设置面板仅渲染 tab 壳（Batch 4 对话框迁移时填充）
 * - 项目/任务/Rant 数据经 props 注入；操作经回调上抛（onSelectSession / onTriggerTask / …）
 */
export type WorkspaceViewId = "projects" | "tasks" | "rants" | "settings";

export interface WorkspaceViewProps {
  projects?: ProjectRec[];
  tasks?: TaskRec[];
  rants?: RantRec[];
  /** 当前项目会话列表（Batch 5 接线：Shell 经 listProjectSessions 加载） */
  projectSessions?: SessionRow[] | null;
  projectSessionsError?: string | null;
  /** 激活会话（会话行 "当前" 标记，vanilla App.state.sessionId 语义） */
  currentSid?: string | null;
  activeView: WorkspaceViewId;
  onSwitch?: (view: WorkspaceViewId) => void;
  /** 设置面板关于 tab 数据（Shell appState 注入，Batch 5 slice 6） */
  version?: string;
  evolutionCount?: number | null;
  onSelectProjectSession?: (project: ProjectRec, sessionId: string) => void;
  onViewProjectSessions?: (project: ProjectRec) => void;
  onAddProject?: () => void;
  onDeleteProject?: (project: ProjectRec) => void;
  onTriggerTask?: (task: TaskRec) => void;
  onEditTask?: (task: TaskRec) => void;
  onDeleteTask?: (task: TaskRec) => void;
  onNewRant?: () => void;
}

const RANT_FILTERS: { value: RantFilter; labelKey: string }[] = [
  { value: "", labelKey: "rants.filterAll" },
  { value: "pending", labelKey: "rants.filterPending" },
  { value: "in_progress", labelKey: "rants.filterInProgress" },
  { value: "completed", labelKey: "rants.filterCompleted" },
];

export function WorkspaceView({
  projects = [],
  tasks = [],
  rants = [],
  projectSessions = null,
  projectSessionsError = null,
  currentSid = null,
  activeView,
  onSwitch,
  version,
  evolutionCount,
  onSelectProjectSession,
  onViewProjectSessions,
  onAddProject,
  onDeleteProject,
  onTriggerTask,
  onEditTask,
  onDeleteTask,
  onNewRant,
}: WorkspaceViewProps) {
  const { t } = useI18n();
  const [rantFilter, setRantFilter] = useState<RantFilter>("");
  const [expandedRant, setExpandedRant] = useState<string | null>(null);
  const [sessionsProject, setSessionsProject] = useState<ProjectRec | null>(null);
  // 任务面板激活时每秒 tick → 倒计时/运行时长实时更新（vanilla taskPollTimer 行为；
  // 评审 #1008：静态快照导致运行时长恒 0s）
  const [, setTick] = useState(0);
  useEffect(() => {
    if (activeView !== "tasks") return;
    const id = setInterval(() => setTick((v) => v + 1), 1000);
    return () => clearInterval(id);
  }, [activeView]);

  const evolveProjects = useMemo(() => buildEvolveProjects(tasks), [tasks]);
  const filteredRants = useMemo(
    () => (rantFilter ? rants.filter((r) => (r.status || "pending") === rantFilter) : rants),
    [rants, rantFilter],
  );

  function switchView(v: WorkspaceViewId) {
    // 离开项目会话子视图 → 重置回项目列表
    if (v !== "projects" && sessionsProject) setSessionsProject(null);
    onSwitch?.(v);
  }

  return (
    <div className="workspace" data-testid="workspace-view">
      {activeView === "projects" && (
        <section className="workspace-view" data-view="projects" data-testid="panel-projects">
          <div className="workspace-view-body">
            <h2 className="workspace-view-title">{t("projects.title")}</h2>
            {sessionsProject ? (
              <ProjectSessions
                project={sessionsProject}
                sessions={projectSessions}
                error={projectSessionsError}
                currentSid={currentSid}
                onBack={() => setSessionsProject(null)}
                onSelect={(sid) => onSelectProjectSession?.(sessionsProject, sid)}
              />
            ) : (
              <ProjectList
                projects={projects}
                evolveProjects={evolveProjects}
                t={t}
                onViewSessions={(p) => {
                  setSessionsProject(p);
                  onViewProjectSessions?.(p);
                }}
                onDelete={onDeleteProject}
              />
            )}
            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <button
                type="button"
                className="btn btn-ghost"
                style={{ flex: 1 }}
                onClick={onAddProject}
              >
                {t("projects.add")}
              </button>
            </div>
          </div>
        </section>
      )}

      {activeView === "tasks" && (
        <section className="workspace-view" data-view="tasks" data-testid="panel-tasks">
          <div className="workspace-view-body">
            <h2 className="workspace-view-title">{t("tasks.title")}</h2>
            <div className="hint" style={{ marginBottom: 6 }}>{t("settings.tasksHint")}</div>
            <TaskList
              tasks={tasks}
              t={t}
              onTrigger={onTriggerTask}
              onEdit={onEditTask}
              onDelete={onDeleteTask}
            />
            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <button type="button" className="btn btn-ghost" style={{ flex: 1 }} onClick={() => onEditTask?.({} as TaskRec)}>
                {t("settings.taskAdd")}
              </button>
            </div>
          </div>
        </section>
      )}

      {activeView === "rants" && (
        <section className="workspace-view" data-view="rants" data-testid="panel-rants">
          <div className="workspace-view-body">
            <h2 className="workspace-view-title">{t("rants.title")}</h2>
            <div className="panel-tabs" role="tablist" style={{ marginBottom: "var(--sp-2)" }}>
              {RANT_FILTERS.map((f) => (
                <button
                  key={f.value || "all"}
                  type="button"
                  className={`panel-tab${rantFilter === f.value ? " active" : ""}`}
                  data-rant-filter={f.value}
                  onClick={() => setRantFilter(f.value)}
                >
                  {t(f.labelKey)}
                </button>
              ))}
            </div>
            <div className="rant-head">
              <span className="rant-col-time">{t("rants.colTime")}</span>
              <span className="rant-col-project">{t("rants.colProject")}</span>
              <span className="rant-col-status">{t("rants.colStatus")}</span>
              <span className="rant-col-progress">{t("rants.colProgress")}</span>
              <span className="rant-col-content">{t("rants.colContent")}</span>
            </div>
            <RantList
              rants={filteredRants}
              filter={rantFilter}
              t={t}
              expanded={expandedRant}
              onToggle={(key) => setExpandedRant(expandedRant === key ? null : key)}
            />
            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <button type="button" className="btn btn-ghost" style={{ flex: 1 }} onClick={onNewRant}>
                {t("rants.new")}
              </button>
            </div>
          </div>
        </section>
      )}

      {activeView === "settings" && (
        <SettingsPanel version={version} evolutionCount={evolutionCount} />
      )}
    </div>
  );
}

/* ── 项目列表 ────────────────────────────────────────────── */

function ProjectList({
  projects,
  evolveProjects,
  t,
  onViewSessions,
  onDelete,
}: {
  projects: ProjectRec[];
  evolveProjects: Set<string>;
  t: (k: string, v?: Record<string, unknown>) => string;
  onViewSessions: (p: ProjectRec) => void;
  onDelete?: (p: ProjectRec) => void;
}) {
  if (!projects.length) {
    return <div className="task-empty" data-testid="projects-empty">{t("projects.empty")}</div>;
  }
  return (
    <div className="task-list" data-testid="project-list">
      {projects.map((p) => {
        const name = p.name || p.path || "?";
        const hint = projectHint(p, t);
        return (
          <div className="task-row" key={name} data-testid="project-row">
            <span className="task-name">{name}</span>
            {evolveProjects.has(name) && (
              <span className="task-badge" data-testid="project-evolve-badge">⚡ {t("projects.autoEvolve")}</span>
            )}
            {hint && <span className="task-hint">{hint}</span>}
            <span className="task-actions">
              <button
                type="button"
                className="model-action-btn"
                title={t("projects.viewSessions")}
                onClick={() => onViewSessions(p)}
              >
                {t("projects.viewSessions")}
              </button>
              {onDelete && (
                <button
                  type="button"
                  className="model-action-btn danger"
                  title={t("deleteProject.delete")}
                  onClick={() => onDelete(p)}
                >
                  {t("deleteProject.delete")}
                </button>
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** 项目会话子视图（点击项目"查看会话"后展示） */
function ProjectSessions({
  project,
  sessions,
  error,
  currentSid,
  onBack,
  onSelect,
}: {
  project: ProjectRec;
  sessions: SessionRow[] | null;
  error?: string | null;
  currentSid?: string | null;
  onBack: () => void;
  onSelect: (sid: string) => void;
}) {
  const { t } = useI18n();
  return (
    <div className="task-list" data-testid="project-sessions">
      <div className="task-row">
        <button type="button" className="model-action-btn" style={{ flexShrink: 0 }} onClick={onBack}>
          {t("projects.back")}
        </button>
        <span className="task-name">{t("projects.sessionsOf", { project: project.name || project.path || "" })}</span>
      </div>
      {error ? (
        <div className="task-empty" data-testid="project-sessions-error">{t("openSession.loadFailed", { msg: error })}</div>
      ) : sessions === null ? (
        <div className="task-empty" data-testid="project-sessions-loading">{t("dlg.loading")}</div>
      ) : sessions.length === 0 ? (
        <div className="task-empty" data-testid="project-sessions-empty">{t("projects.noSessions")}</div>
      ) : (
        sessions.map((s) => {
          const v = sessionRowView(s, currentSid ?? null, t);
          return (
            <button
              type="button"
              className="help-row"
              key={s.session_id}
              data-testid="project-session-row"
              style={{ width: "100%", textAlign: "left", cursor: "pointer", background: "none", border: "none" }}
              onClick={() => onSelect(s.session_id)}
            >
              <span className="help-cmd">{v.label}</span>
              <span className="help-hint">{v.marks.join(" · ")}</span>
            </button>
          );
        })
      )}
    </div>
  );
}

/* ── 任务列表 ────────────────────────────────────────────── */

function TaskList({
  tasks,
  t,
  onTrigger,
  onEdit,
  onDelete,
}: {
  tasks: TaskRec[];
  t: (k: string, v?: Record<string, unknown>) => string;
  onTrigger?: (task: TaskRec) => void;
  onEdit?: (task: TaskRec) => void;
  onDelete?: (task: TaskRec) => void;
}) {
  if (!tasks.length) {
    return <div className="task-empty" data-testid="tasks-empty">{t("settings.taskEmpty")}</div>;
  }
  return (
    <div className="task-list" data-testid="task-list">
      {tasks.map((task) => {
        const badge = taskStatusBadge(task, t);
        const hint = taskHint(task, t);
        const meta = taskMeta(task, t);
        return (
          <div className="task-row" key={task.name || "?"} data-testid="task-row">
            <span className="task-name">{task.name || "?"}</span>
            <span className="task-badge">{task.type || "evolution"}</span>
            {badge && (
              <span className={`task-badge ${badge.cls}`} data-testid="task-status-badge">
                {badge.text}
              </span>
            )}
            {badge?.countdown && <span className="task-next-run">{badge.countdown}</span>}
            {hint && <span className="task-hint">{hint}</span>}
            <div className="task-meta">
              {meta.map((m) => (
                <span
                  key={m.key}
                  className={m.badgeCls ? `task-badge ${m.badgeCls}` : "task-meta-item"}
                >
                  {m.text}
                </span>
              ))}
            </div>
            <span className="task-actions">
              {onTrigger && (
                <button
                  type="button"
                  className="model-action-btn"
                  title={t("settings.taskTrigger")}
                  disabled={!!task.running}
                  onClick={() => onTrigger(task)}
                >
                  {t("settings.taskTrigger")}
                </button>
              )}
              {onEdit && (
                <button type="button" className="model-action-btn" title={t("settings.taskEdit")} onClick={() => onEdit(task)}>
                  {t("settings.taskEdit")}
                </button>
              )}
              {onDelete && (
                <button type="button" className="model-action-btn danger" title={t("settings.taskDelete")} onClick={() => onDelete(task)}>
                  {t("settings.taskDelete")}
                </button>
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/* ── Rant 列表 ───────────────────────────────────────────── */

function RantList({
  rants,
  filter,
  t,
  expanded,
  onToggle,
}: {
  rants: RantRec[];
  filter: RantFilter;
  t: (k: string, v?: Record<string, unknown>) => string;
  expanded: string | null;
  onToggle: (key: string) => void;
}) {
  if (!rants.length) {
    return (
      <div className="task-empty" data-testid="rants-empty">
        {t(filter ? "rants.emptyFiltered" : "rants.empty")}
      </div>
    );
  }
  return (
    <div className="task-list" data-testid="rant-list">
      {rants.map((r) => {
        const key = String(r.timestamp || r.project || r.message || Math.random());
        const ts = rantTimestamp(r.timestamp);
        const st = rantStatusMeta(r.status, t);
        const firstLine = rantFirstLine(r.message);
        const isExpanded = expanded === key;
        return (
          <div key={key}>
            <div
              className="task-row rant-row"
              data-testid="rant-row"
              onClick={() => onToggle(key)}
            >
              <span className="rant-col-time">{ts}</span>
              <span className="rant-col-project">{r.project || "—"}</span>
              <span className={`task-badge ${st.badgeCls}`} data-testid="rant-status-badge">{st.text}</span>
              <span className="rant-col-progress">{r.progress ? String(r.progress) : "—"}</span>
              <span className="rant-col-content">{firstLine}</span>
            </div>
            {isExpanded && (
              <div className="rant-detail" style={{ padding: "6px 8px", borderTop: "1px solid var(--border)", fontSize: "var(--fs-secondary)" }}>
                <div className="rant-meta">{ts} · {r.project || "—"} · {st.text}</div>
                <div className="msg-body rant-md">{preprocessRantMarkdown(r.message)}</div>
                <div className="rant-progress">
                  {r.progress ? `${t("rants.statusInProgress")}: ${r.progress}` : t("rants.noProgress")}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
