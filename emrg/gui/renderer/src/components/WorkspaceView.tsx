import { useEffect, useMemo, useState } from "react";
import { useI18n } from "../lib/i18n";
import type { MarkdownRenderer } from "../lib/markdown";
import { countLabel, filterByName, sortByName } from "../lib/cockpit";
import { MarkdownText } from "./MarkdownText";
import { SettingsPanel } from "./SettingsPanel";
import { sessionRowView, type SessionRow } from "../lib/openSession";
import {
  buildEvolveAgents,
  preprocessRantMarkdown,
  projectHint,
  rantSummary,
  rantStatusMeta,
  rantTimestamp,
  taskHint,
  taskMeta,
  taskStatusBadge,
  type AgentRec,
  type RantFilter,
  type RantRec,
  type TaskRec,
} from "../lib/workspaceView";

/**
 * WorkspaceView — the three wide pages (issue #2959 item 7).
 *
 * What changed from the read-only lists it used to be:
 *
 * - each page carries a search box with a `hits / total` count beside it, because a
 *   list you can only scroll is a list you cannot find anything in;
 * - agents and tasks are ordered by name, so the same installation reads the same way
 *   twice — a *running* agent is not floated to the top, a dot marks it (item 9);
 * - the agent list reflows into more columns on a wide window and fewer on a narrow
 *   one, which is `grid-template-columns: repeat(auto-fill, …)`'s whole job;
 * - a delete is confirmed **inside the row**: the first click turns the button into
 *   the question, the second answers it, and nothing opens a window over the list the
 *   host is reading.
 *
 * Rants are the exception on deletion, and it is deliberate: the daemon exposes no
 * operation that removes a rant, and the ledger file has exactly one writer
 * (`emrg/server/rants.py`). A GUI button with nothing behind it would be a control
 * that looks alive and does nothing, which is the defect class this cockpit exists to
 * remove — so the row offers none until the daemon can answer one.
 */
export type WorkspaceViewId = "agents" | "tasks" | "rants" | "settings";

export interface WorkspaceViewProps {
  agents?: AgentRec[];
  tasks?: TaskRec[];
  rants?: RantRec[];
  /** 当前项目会话列表（Batch 5 接线：Shell 经 listAgentSessions 加载） */
  projectSessions?: SessionRow[] | null;
  projectSessionsError?: string | null;
  /** 激活会话（会话行 "当前" 标记，vanilla App.state.sessionId 语义） */
  currentSid?: string | null;
  activeView: WorkspaceViewId;
  onSwitch?: (view: WorkspaceViewId) => void;
  /** 设置面板关于 tab 数据（Shell appState 注入，Batch 5 slice 6） */
  version?: string;
  evolutionCount?: number | null;
  onSelectAgentSession?: (agent: AgentRec, sessionId: string) => void;
  onViewAgentSessions?: (agent: AgentRec) => void;
  onAddAgent?: () => void;
  onDeleteAgent?: (agent: AgentRec) => void;
  onTriggerTask?: (task: TaskRec) => void;
  onEditTask?: (task: TaskRec) => void;
  onDeleteTask?: (task: TaskRec) => void;
  /** 任务面板「打开会话」（vanilla #924：switchSession 到任务所属会话，无 session_id 时按钮禁用） */
  onOpenSessionTask?: (task: TaskRec) => void;
  onNewRant?: () => void;
  /** 删除一条 rant（issue #2959 item 7：三种行同样可删，确认在行内） */
  onDeleteRant?: (rant: RantRec) => void;
  /**
   * markdown 渲染器（Shell 注入 createProdMarkdownRenderer，与 TranscriptView 同一实例）。
   * 缺省 ⇒ Rant 详情退回纯文本（vanilla else 分支语义），不白屏、不抛错。
   */
  renderer?: MarkdownRenderer;
}

const RANT_FILTERS: { value: RantFilter; labelKey: string }[] = [
  { value: "", labelKey: "rants.filterAll" },
  { value: "pending", labelKey: "rants.filterPending" },
  { value: "in_progress", labelKey: "rants.filterInProgress" },
  { value: "completed", labelKey: "rants.filterCompleted" },
];

export function WorkspaceView({
  agents = [],
  tasks = [],
  rants = [],
  projectSessions = null,
  projectSessionsError = null,
  currentSid = null,
  activeView,
  onSwitch,
  version,
  evolutionCount,
  onSelectAgentSession,
  onViewAgentSessions,
  onAddAgent,
  onDeleteAgent,
  onTriggerTask,
  onEditTask,
  onDeleteTask,
  onOpenSessionTask,
  onNewRant,
  onDeleteRant,
  renderer,
}: WorkspaceViewProps) {
  const { t } = useI18n();
  const [rantFilter, setRantFilter] = useState<RantFilter>("");
  const [expandedRant, setExpandedRant] = useState<string | null>(null);
  const [sessionsProject, setSessionsProject] = useState<AgentRec | null>(null);
  // One query per page, so searching agents and then switching to tasks does not carry
  // a filter the host cannot see. A single shared box would hide that state change.
  const [agentQuery, setAgentQuery] = useState("");
  const [taskQuery, setTaskQuery] = useState("");
  const [rantQuery, setRantQuery] = useState("");
  /** The row whose delete button is asking, `${kind}:${name}` — never a dialog. */
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  // 任务面板激活时每秒 tick → 倒计时/运行时长实时更新（vanilla taskPollTimer 行为；
  // 评审 #1008：静态快照导致运行时长恒 0s）
  const [, setTick] = useState(0);
  useEffect(() => {
    if (activeView !== "tasks") return;
    const id = setInterval(() => setTick((v) => v + 1), 1000);
    return () => clearInterval(id);
  }, [activeView]);

  const evolveAgents = useMemo(() => buildEvolveAgents(tasks), [tasks]);
  /** Agents a running task belongs to — the green dot's only input (item 9). */
  const runningAgents = useMemo(() => {
    const s = new Set<string>();
    for (const task of tasks) {
      if (task.running) s.add(String(task.config?.agent || task.agent || ""));
    }
    return s;
  }, [tasks]);
  const agentRows = useMemo(
    () => filterByName(sortByName(agents), agentQuery),
    [agents, agentQuery],
  );
  const taskRows = useMemo(
    () => filterByName(sortByName(tasks), taskQuery),
    [tasks, taskQuery],
  );
  const filteredRants = useMemo(
    () => (rantFilter ? rants.filter((r) => (r.status || "pending") === rantFilter) : rants),
    [rants, rantFilter],
  );
  // A rant has no `name`, so its searchable text is spelled out: the time it was filed,
  // who it was filed against, and what it says. Searching a field the row does not have
  // would answer zero for every query and look like a working box.
  const rantRows = useMemo(
    () =>
      filterByName(
        filteredRants,
        rantQuery,
        (r) => `${r.timestamp ?? ""} ${r.agent ?? ""} ${r.message ?? ""} ${r.progress ?? ""}`,
      ),
    [filteredRants, rantQuery],
  );

  function switchView(v: WorkspaceViewId) {
    // 离开项目会话子视图 → 重置回项目列表
    if (v !== "agents" && sessionsProject) setSessionsProject(null);
    onSwitch?.(v);
  }

  /** First click asks, second click answers — on the same row. */
  function deleteControl(key: string, label: string, onConfirm: () => void) {
    if (pendingDelete === key) {
      return (
        <span className="row-confirm" data-testid="row-confirm">
          <button
            type="button"
            className="model-action-btn danger row-confirm-btn"
            data-testid="row-delete-confirm"
            title={label}
            onClick={() => {
              setPendingDelete(null);
              onConfirm();
            }}
          >
            {t("panels.deleteConfirm")}
          </button>
          <button
            type="button"
            className="model-action-btn"
            data-testid="row-delete-cancel"
            title={t("panels.cancel")}
            onClick={() => setPendingDelete(null)}
          >
            {t("panels.cancel")}
          </button>
        </span>
      );
    }
    return (
      <button
        type="button"
        className="model-action-btn danger"
        data-testid="row-delete"
        title={label}
        onClick={() => setPendingDelete(key)}
      >
        {label}
      </button>
    );
  }

  return (
    <div className="workspace" data-testid="workspace-view">
      {activeView === "agents" && (
        <section className="workspace-view" data-view="agents" data-testid="panel-agents">
          <div className="workspace-view-body">
            <h2 className="workspace-view-title">{t("agents.title")}</h2>
            {sessionsProject ? (
              <AgentSessions
                agent={sessionsProject}
                sessions={projectSessions}
                error={projectSessionsError}
                currentSid={currentSid}
                onBack={() => setSessionsProject(null)}
                onSelect={(sid) => onSelectAgentSession?.(sessionsProject, sid)}
              />
            ) : (
              <>
                <SearchRow
                  testId="agents"
                  value={agentQuery}
                  onChange={setAgentQuery}
                  hits={agentRows.length}
                  total={agents.length}
                />
                <AgentList
                  agents={agentRows}
                  evolveAgents={evolveAgents}
                  runningAgents={runningAgents}
                  t={t}
                  onViewSessions={(p) => {
                    setSessionsProject(p);
                    onViewAgentSessions?.(p);
                  }}
                  renderDelete={(p) =>
                    onDeleteAgent
                      ? deleteControl(`agent:${p.name || p.path || ""}`, t("deleteAgent.delete"), () =>
                          onDeleteAgent(p),
                        )
                      : null
                  }
                />
              </>
            )}
            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <button
                type="button"
                className="btn btn-ghost"
                style={{ flex: 1 }}
                onClick={onAddAgent}
              >
                {t("agents.add")}
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
            <SearchRow
              testId="tasks"
              value={taskQuery}
              onChange={setTaskQuery}
              hits={taskRows.length}
              total={tasks.length}
            />
            <TaskList
              tasks={taskRows}
              t={t}
              onTrigger={onTriggerTask}
              onOpenSession={onOpenSessionTask}
              onEdit={onEditTask}
              renderDelete={(task) =>
                onDeleteTask
                  ? deleteControl(`task:${task.name || ""}`, t("settings.taskDelete"), () =>
                      onDeleteTask(task),
                    )
                  : null
              }
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
            <SearchRow
              testId="rants"
              value={rantQuery}
              onChange={setRantQuery}
              hits={rantRows.length}
              total={filteredRants.length}
            />
            <div className="rant-head">
              <span className="rant-col-time">{t("rants.colTime")}</span>
              <span className="rant-col-agent">{t("rants.colAgent")}</span>
              <span className="rant-col-status">{t("rants.colStatus")}</span>
              <span className="rant-col-progress">{t("rants.colProgress")}</span>
              <span className="rant-col-content">{t("rants.colContent")}</span>
            </div>
            <RantList
              rants={rantRows}
              filter={rantFilter}
              t={t}
              renderer={renderer}
              expanded={expandedRant}
              onToggle={(key) => setExpandedRant(expandedRant === key ? null : key)}
              renderDelete={(r) =>
                onDeleteRant
                  ? deleteControl(`rant:${String(r.timestamp ?? "")}`, t("dlg.delete"), () =>
                      onDeleteRant(r),
                    )
                  : null
              }
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

/* ── 搜索行（item 7：搜索框 + hits/total） ───────────────── */

function SearchRow({
  testId,
  value,
  onChange,
  hits,
  total,
}: {
  testId: string;
  value: string;
  onChange: (v: string) => void;
  hits: number;
  total: number;
}) {
  const { t } = useI18n();
  return (
    <div className="panel-search-row">
      <input
        className="panel-search"
        data-testid={`${testId}-search`}
        placeholder={t("panels.searchPlaceholder")}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <span className="panel-hits" data-testid={`${testId}-hits`}>
        {countLabel(hits, total)}
      </span>
    </div>
  );
}

/* ── 项目列表 ────────────────────────────────────────────── */

function AgentList({
  agents,
  evolveAgents,
  runningAgents,
  t,
  onViewSessions,
  renderDelete,
}: {
  agents: AgentRec[];
  evolveAgents: Set<string>;
  runningAgents: Set<string>;
  t: (k: string, v?: Record<string, unknown>) => string;
  onViewSessions: (p: AgentRec) => void;
  renderDelete: (p: AgentRec) => React.ReactNode;
}) {
  if (!agents.length) {
    return <div className="task-empty" data-testid="agents-empty">{t("agents.empty")}</div>;
  }
  return (
    <div className="task-list agent-grid" data-testid="agent-list">
      {agents.map((p) => {
        const name = p.name || p.path || "?";
        const hint = projectHint(p, t);
        return (
          <div className="agent-card" key={name} data-testid="agent-row" title={p.path || name}>
            {runningAgents.has(name) ? (
              <span className="agent-dot" data-testid="agent-running-dot" title={t("tasks.running")} />
            ) : null}
            <span className="agent-name" title={p.path || name}>{name}</span>
            {evolveAgents.has(name) && (
              <span className="task-badge" data-testid="agent-evolve-badge">⚡ {t("agents.autoEvolve")}</span>
            )}
            {hint && <span className="task-hint">{hint}</span>}
            <span className="task-actions">
              <button
                type="button"
                className="model-action-btn"
                title={t("agents.viewSessions")}
                onClick={() => onViewSessions(p)}
              >
                {t("agents.viewSessions")}
              </button>
              {renderDelete(p)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** 项目会话子视图（点击项目"查看会话"后展示） */
function AgentSessions({
  agent,
  sessions,
  error,
  currentSid,
  onBack,
  onSelect,
}: {
  agent: AgentRec;
  sessions: SessionRow[] | null;
  error?: string | null;
  currentSid?: string | null;
  onBack: () => void;
  onSelect: (sid: string) => void;
}) {
  const { t } = useI18n();
  return (
    <div className="task-list" data-testid="agent-sessions">
      <div className="task-row">
        <button type="button" className="model-action-btn" style={{ flexShrink: 0 }} onClick={onBack}>
          {t("agents.back")}
        </button>
        <span className="task-name">{t("agents.sessionsOf", { agent: agent.name || agent.path || "" })}</span>
      </div>
      {error ? (
        <div className="task-empty" data-testid="agent-sessions-error">{t("openSession.loadFailed", { msg: error })}</div>
      ) : sessions === null ? (
        <div className="task-empty" data-testid="agent-sessions-loading">{t("dlg.loading")}</div>
      ) : sessions.length === 0 ? (
        <div className="task-empty" data-testid="agent-sessions-empty">{t("agents.noSessions")}</div>
      ) : (
        sessions.map((s) => {
          const v = sessionRowView(s, currentSid ?? null, t);
          return (
            <button
              type="button"
              className="help-row"
              key={s.session_id}
              data-testid="agent-session-row"
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
  onOpenSession,
  onEdit,
  renderDelete,
}: {
  tasks: TaskRec[];
  t: (k: string, v?: Record<string, unknown>) => string;
  onTrigger?: (task: TaskRec) => void;
  onOpenSession?: (task: TaskRec) => void;
  onEdit?: (task: TaskRec) => void;
  renderDelete: (task: TaskRec) => React.ReactNode;
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
              {onOpenSession && (
                <button
                  type="button"
                  className="model-action-btn"
                  title={t("settings.taskOpenSession")}
                  disabled={!task.session_id}
                  onClick={() => onOpenSession(task)}
                >
                  {t("settings.taskOpenSession")}
                </button>
              )}
              {onEdit && (
                <button type="button" className="model-action-btn" title={t("settings.taskEdit")} onClick={() => onEdit(task)}>
                  {t("settings.taskEdit")}
                </button>
              )}
              {renderDelete(task)}
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
  renderer,
  expanded,
  onToggle,
  renderDelete,
}: {
  rants: RantRec[];
  filter: RantFilter;
  t: (k: string, v?: Record<string, unknown>) => string;
  renderer?: MarkdownRenderer;
  expanded: string | null;
  onToggle: (key: string) => void;
  renderDelete: (rant: RantRec) => React.ReactNode;
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
        const key = String(r.timestamp || r.agent || r.message || Math.random());
        const ts = rantTimestamp(r.timestamp);
        const st = rantStatusMeta(r.status, t);
        const summary = rantSummary(r.message);
        const isExpanded = expanded === key;
        return (
          <div key={key}>
            <div
              className="task-row rant-row"
              data-testid="rant-row"
              onClick={() => onToggle(key)}
            >
              <span className="rant-col-time">{ts}</span>
              <span className="rant-col-agent">{r.agent || "—"}</span>
              <span className={`task-badge ${st.badgeCls}`} data-testid="rant-status-badge">{st.text}</span>
              <span className="rant-col-progress">{r.progress ? String(r.progress) : "—"}</span>
              <span className="rant-col-content">{summary}</span>
              {/* The delete control sits inside the row that toggles the detail, so its
                  clicks must not also expand the row — otherwise asking "delete?" would
                  open the rant the question is about. */}
              <span className="rant-col-actions" onClick={(e) => e.stopPropagation()}>
                {renderDelete(r)}
              </span>
            </div>
            {isExpanded && (
              <div className="rant-detail" style={{ padding: "6px 8px", borderTop: "1px solid var(--border)", fontSize: "var(--fs-secondary)" }}>
                <div className="rant-meta">{ts} · {r.agent || "—"} · {st.text}</div>
                {/* Markdown, then a box that scrolls rather than growing the page
                    (item 8): a long rant used to push everything below it off screen. */}
                <div className="rant-detail-scroll" data-testid="rant-detail-scroll">
                  <div className="rant-section">
                    <div className="rant-section-label">{t("rants.colContent")}</div>
                    <div className="msg-body rant-md" data-testid="rant-content">
                      {renderer ? (
                        <MarkdownText
                          text={preprocessRantMarkdown(r.message)}
                          md={renderer}
                          stripMark={false}
                          className="rant-md-html"
                          plainClassName="rant-md-plain"
                        />
                      ) : (
                        preprocessRantMarkdown(r.message)
                      )}
                    </div>
                  </div>
                  <div className="rant-section">
                    <div className="rant-section-label">{t("rants.colProgress")}</div>
                    <div className="rant-progress" data-testid="rant-progress">
                      {r.progress ? String(r.progress) : t("rants.noProgress")}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
