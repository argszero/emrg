import { useEffect, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import { agentRowView, matchesQuery, type AgentRow } from "../lib/openSession";
import { Dialog } from "./Dialog";
import { DialogActions } from "./HelpDialog";

/**
 * NewSessionDialog — 新建会话对话框（Batch 4 slice 3）。
 * 镜像 vanilla `showNewSessionDialog()`：
 * - loading（agents=null）/ empty（noAgents）/ error（loadFailed）/ noMatches 五态
 * - 搜索框：位置与「打开会话」一致，复用同一个 matchesQuery 与同一份文案
 * - 项目行（name/path/最近活跃）→ 点击 onPickAgent(p)（vanilla: newSession({projectPath})）
 * - 底部 "＋ 新建项目…" → onNewAgent（vanilla: pickAgentDir → registerAgent → newSession）
 * 数据注入式：agents 与回调由调用方（Batch 5 接线 window.emrg.listAgents/pickAgentDir）提供。
 */
export interface NewSessionDialogProps {
  open: boolean;
  /** null = loading；[] = 空；有值 = 列表 */
  agents?: AgentRow[] | null;
  error?: string | null;
  onPickAgent?: (p: AgentRow) => void;
  onNewAgent?: () => void;
  onDismiss?: () => void;
}

export function NewSessionDialog({
  open,
  agents = null,
  error = null,
  onPickAgent,
  onNewAgent,
  onDismiss,
}: NewSessionDialogProps) {
  const { t } = useI18n();

  // 搜索框与「打开会话」共用一份匹配函数和一份文案：同一个 agent 列表不该有两套规则，
  // 也不该有两句意思相同的说明。
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement | null>(null);
  const searchLabel = t("openSession.searchAgents");

  // 每次打开都清空上一次的查询并把焦点放进搜索框——查询属于上一次的列表，
  // 带进这一次会让新列表看着是空的；焦点在里面则进去就能打字。
  useEffect(() => {
    if (!open) return;
    setQuery("");
    searchRef.current?.focus();
  }, [open]);

  const renderList = () => {
    if (error) {
      return (
        <div className="help-row" data-testid="new-session-error">
          <span className="help-hint">{t("newSession.loadFailed", { msg: error })}</span>
        </div>
      );
    }
    if (agents === null) {
      return (
        <div className="help-row" data-testid="new-session-loading">
          <span className="help-hint">{t("dlg.loading")}</span>
        </div>
      );
    }
    if (agents.length === 0) {
      return (
        <div className="help-row" data-testid="new-session-empty">
          <span className="help-hint">{t("openSession.noAgents")}</span>
        </div>
      );
    }
    // 「还没有 agent」只由列表本身是否为空决定（上面那条分支），查询管不到它；
    // 有 agent 但一条都不命中是另一种状态，两者的行不能互借。
    const shown = agents.filter((p) => matchesQuery(query, p.name, p.path));
    if (shown.length === 0) {
      return (
        <div className="help-row" data-testid="new-session-no-matches">
          <span className="help-hint">{t("openSession.noMatches")}</span>
        </div>
      );
    }
    return shown.map((p) => {
      const v = agentRowView(p, t);
      return (
        <button
          type="button"
          className="help-row"
          key={p.path || p.name || ""}
          data-testid="new-session-agent"
          onClick={() => onPickAgent?.(p)}
          style={{ width: "100%", textAlign: "left", cursor: "pointer", background: "none", border: "none" }}
        >
          <span className="help-cmd">{v.label}</span>
          <span className="help-hint">{v.hint}</span>
          {v.active ? <span className="help-hint">{v.active}</span> : null}
        </button>
      );
    });
  };

  if (!open) return null;
  return (
    <Dialog open title={t("newSession.title")} onClose={onDismiss} testId="new-session-dialog">
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-3)" }}>
        {t("newSession.desc")}
      </p>
      <input
        ref={searchRef}
        type="text"
        data-testid="new-session-search"
        aria-label={searchLabel}
        placeholder={searchLabel}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        style={{ width: "100%", boxSizing: "border-box", margin: "0 0 var(--sp-2)", padding: "4px 8px" }}
      />
      <div className="help-list" data-testid="new-session-list">
        {renderList()}
      </div>
      <div className="dialog-actions">
        <button type="button" className="btn btn-ghost" data-testid="new-session-new-agent" onClick={onNewAgent}>
          {t("newSession.newAgent")}
        </button>
      </div>
      <DialogActions onDismiss={onDismiss} />
    </Dialog>
  );
}
