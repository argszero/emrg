import { useEffect, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import { useSnapshotStore } from "../hooks/useSnapshotStore";
import { useDaemonBridge } from "./DaemonBridgeProvider";
import { Sidebar, type SidebarViewId } from "./Sidebar";
import { ResultPanel } from "./ResultPanel";
import { WorkspaceView, type WorkspaceViewId } from "./WorkspaceView";
import { TranscriptView } from "./TranscriptView";
import { Composer, type CommandRouting } from "./Composer";
import { DialogHost, type DialogHostHandle } from "./DialogHost";

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
 * - WorkspaceView 数据仍为空数组注入（Batch 5 后续 slice 接 listProjects 等 IPC）。
 */
export function Shell() {
  const { t } = useI18n();
  const { bridge, transcript } = useDaemonBridge();
  const appState = useSnapshotStore(bridge.store);
  const [activeSid, setActiveSid] = useState<string | null>(null);
  const [activeView, setActiveView] = useState<"sessions" | WorkspaceViewId>("sessions");
  const dialogHost = useRef<DialogHostHandle>(null);

  // open_sessions 广播到达且尚无激活会话 → 自动选第一个（vanilla 同语义）
  useEffect(() => {
    if (activeSid === null && appState.openSessions.length > 0) {
      setActiveSid(appState.openSessions[0].sid);
    }
  }, [appState.openSessions, activeSid]);

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
  }

  const isPanelView = activeView !== "sessions";

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
      default:
        // /model /rant /trigger — 待 workspace/settings slice 接线
        transcript.addSystemMessage(t("app.cmdUnknown", { cmd: routing.cmd }), activeSid);
    }
  }

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
            activeView={activeView}
            onSelect={selectSession}
            onSwitchView={switchView}
            onNewChat={() => dialogHost.current?.openNewSession()}
            onOpenChat={() => dialogHost.current?.openSessions()}
          />
        </aside>
        <main className="react-shell-main" data-testid="react-shell-main">
          {isPanelView ? (
            <WorkspaceView activeView={activeView} onSwitch={switchView} />
          ) : (
            <>
              {disconnected ? (
                <div className="conn-banner" role="alert" data-testid="conn-banner">
                  {t("app.sessionDisconnected")}
                </div>
              ) : null}
              <TranscriptView store={transcript} sid={activeSid} />
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
      </div>
    </div>
  );
}
