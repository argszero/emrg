import { useEffect, useState } from "react";
import { useI18n } from "../lib/i18n";
import { useSnapshotStore } from "../hooks/useSnapshotStore";
import { useDaemonBridge } from "./DaemonBridgeProvider";
import { Sidebar } from "./Sidebar";
import { ResultPanel } from "./ResultPanel";
import { WorkspaceView } from "./WorkspaceView";
import { TranscriptView } from "./TranscriptView";
import { Composer } from "./Composer";

/**
 * Shell — React 布局（Batch 0 骨架 + Batch 3 Sidebar/ResultPanel/WorkspaceView
 * + Batch 5 slice 3 核心聊天回路接线）。
 *
 * Batch 5 slice 3：把 daemon 实时数据接到聊天回路——
 * - TranscriptView 消费共享 TranscriptStore（bridge.transcript），按 activeSid 切片
 *   （一切 store 按 sid 键控，会话串线防护 #977）；
 * - Composer 发送走 window.emrg.sendMessage（默认注入），busy 受控于 daemon 广播的
 *   busyBySid（done/cancelled/error 释放锁，#655 队列注入协议）；
 * - Sidebar 点击切换 activeSid；open_sessions 广播到达且无激活会话时自动选第一个
 *   （vanilla app.js 同语义）；
 * - ResultPanel 绑定 activeSid（per-session Tab/产物隔离）。
 * 后续 slice：/指令路由到对话框、workspace 视图切换、index.html 切换（Batch 5 final）。
 */
export function Shell() {
  const { t } = useI18n();
  const { bridge, transcript } = useDaemonBridge();
  const appState = useSnapshotStore(bridge.store);
  const [activeSid, setActiveSid] = useState<string | null>(null);

  // open_sessions 广播到达且尚无激活会话 → 自动选第一个（vanilla 同语义）
  useEffect(() => {
    if (activeSid === null && appState.openSessions.length > 0) {
      setActiveSid(appState.openSessions[0].sid);
    }
  }, [appState.openSessions, activeSid]);

  const busy = activeSid ? (appState.busyBySid[activeSid] ?? false) : false;
  const disconnected = activeSid ? (appState.disconnectedBySid[activeSid] ?? false) : false;

  return (
    <div className="react-shell" data-testid="react-shell">
      <header className="react-shell-header">
        <span className="react-shell-brand">✦ EMRG</span>
        <span className="react-shell-badge" data-testid="react-shell-badge">
          {t("shell.batch0Notice")}
        </span>
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
            onSelect={setActiveSid}
          />
        </aside>
        <main className="react-shell-main" data-testid="react-shell-main">
          <WorkspaceView activeView="projects" />
          {disconnected ? (
            <div className="conn-banner" role="alert" data-testid="conn-banner">
              {t("app.sessionDisconnected")}
            </div>
          ) : null}
          <TranscriptView store={transcript} sid={activeSid} />
          <Composer store={transcript} sid={activeSid} busy={busy} />
        </main>
        <ResultPanel sid={activeSid} workspaceRoot="" />
      </div>
    </div>
  );
}
