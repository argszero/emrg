import { afterEach, describe, expect, it, vi } from "vitest";
import { createRef, type RefObject } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DialogHost, type DialogHostHandle } from "./DialogHost";
import { I18nProvider } from "../lib/i18n";
import { createTranscriptStore, type TranscriptStore } from "../lib/transcript";
import type { DaemonAppState, SessionSummary } from "../lib/daemonBridge";

/**
 * DialogHost.test.tsx — Batch 5 slice 4：对话框宿主 + /指令路由落地测试。
 * 模拟 window.emrg 的列表/会话/记忆/技能/历史 IPC，验证：
 * - 各对话框打开与数据加载（help/memory/skills/rewind/sessions/newSession）；
 * - 需要会话的指令在无激活会话时给出 app.needSession 系统消息且不弹框；
 * - confirm/rename 动作真实调用 IPC（deleteSession/renameSession/rewindSession）；
 * - 直接执行类指令（/version /clear /image）在 transcript 落地。
 */

function appState(over: Partial<DaemonAppState> = {}): DaemonAppState {
  return {
    connected: true,
    authFailed: false,
    reconnecting: false,
    installing: false,
    serverId: "srv-1",
    model: "deepseek-v3",
    currentVersion: "0.2.81",
    evolutionCount: 115,
    sessions: [],
    openSessions: [],
    busyBySid: {},
    ownStreamRidBySid: {},
    disconnectedBySid: {},
    ...over,
  };
}

/** 模拟 preload 桥（DialogHost 用到的通道子集） */
function mockEmrg(over: Record<string, unknown> = {}) {
  const calls: Record<string, unknown[][]> = {};
  const fn = (name: string, result: unknown) => {
    const mock = vi.fn().mockResolvedValue(result);
    calls[name] = [];
    mock.mockImplementation(async (...args: unknown[]) => {
      calls[name].push(args);
      return result;
    });
    return mock;
  };
  const bridge = {
    listProjects: fn("listProjects", [{ name: "emrg", path: "/p/emrg" }, { name: "demo", path: "/p/demo", latest_session_at: "2026-08-27T00:00:00Z" }]),
    listProjectSessions: fn("listProjectSessions", { sessions: [{ session_id: "s9", title: "Demo session" }] }),
    newSession: fn("newSession", { session_id: "s-new" }),
    pickProjectDir: fn("pickProjectDir", { path: "/p/new" }),
    registerProject: fn("registerProject", { ok: true, path: "/p/new" }),
    switchSession: fn("switchSession", { ok: true }),
    deleteSession: fn("deleteSession", { ok: true }),
    renameSession: fn("renameSession", { ok: true }),
    clearSession: fn("clearSession", { ok: true }),
    compactSession: fn("compactSession", { ok: true }),
    listHistory: fn("listHistory", { messages: [{ record_index: 3, preview: "hello world" }, { record_index: 2, preview: "older" }] }),
    rewindSession: fn("rewindSession", { ok: true }),
    listMemories: fn("listMemories", [{ id: "m1", title: "记忆一", summary: "摘要内容" }]),
    readMemory: fn("readMemory", { id: "m1", title: "记忆一", content: "详情正文" }),
    listSkills: fn("listSkills", [{ name: "browser-harness", description: "web automation", source: "user" }]),
    removeProject: fn("removeProject", { ok: true }),
    ...over,
  };
  (window as unknown as { emrg?: unknown }).emrg = bridge;
  return { bridge, calls };
}

function setup(opts: { sid?: string | null; sessions?: SessionSummary[]; over?: Record<string, unknown> } = {}) {
  const { bridge, calls } = mockEmrg(opts.over);
  const ref: RefObject<DialogHostHandle | null> = createRef();
  const store = createTranscriptStore();
  const onSwitchSession = vi.fn();
  const { sid = null, sessions = [] } = opts;
  render(
    <I18nProvider lang="en">
      <DialogHost
        ref={ref}
        sid={sid}
        sessions={sessions}
        transcript={store}
        appState={appState()}
        onSwitchSession={onSwitchSession}
      />
    </I18nProvider>,
  );
  return { bridge, calls, ref, store, onSwitchSession };
}

function sysMsgs(store: TranscriptStore, sid: string | null): string[] {
  return store
    .getEntries(sid)
    .filter((e) => e.kind === "system")
    .map((e) => (e as { text: string }).text);
}

afterEach(() => {
  delete (window as unknown as { emrg?: unknown }).emrg;
});

describe("DialogHost (Batch 5 slice 4)", () => {
  it("/help 打开帮助对话框并列出全部指令行", async () => {
    const { ref } = setup();
    ref.current?.openHelp();
    await waitFor(() => expect(screen.getByTestId("help-dialog")).toBeInTheDocument());
    expect(screen.getAllByTestId("help-row").length).toBeGreaterThanOrEqual(16);
  });

  it("/memory 加载列表；点击行 readMemory 并显示详情", async () => {
    const { ref, calls } = setup();
    ref.current?.openMemory();
    await waitFor(() => expect(screen.getByTestId("memory-dialog")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("记忆一")).toBeInTheDocument());
    fireEvent.click(screen.getByText("记忆一"));
    await waitFor(() => expect(screen.getByTestId("memory-detail")).toBeInTheDocument());
    expect(calls.readMemory[0][0]).toMatchObject({ memoryId: "m1", scope: "project" });
  });

  it("/memory session 传 scope=session", async () => {
    const { ref, calls } = setup({ sid: "s1" });
    ref.current?.openMemory("session");
    await waitFor(() => expect(calls.listMemories.length).toBeGreaterThan(0));
    expect(calls.listMemories[0][0]).toMatchObject({ scope: "session", sessionId: "s1" });
  });

  it("/skills 加载技能列表", async () => {
    const { ref } = setup();
    ref.current?.openSkills();
    await waitFor(() => expect(screen.getByTestId("skills-dialog")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("browser-harness")).toBeInTheDocument());
  });

  it("/rewind 无激活会话 → needSession 提示且不弹框", async () => {
    const { ref, store } = setup();
    ref.current?.openRewind();
    await waitFor(() => expect(sysMsgs(store, null)).toContain("Start a conversation first."));
    expect(screen.queryByTestId("rewind-dialog")).not.toBeInTheDocument();
  });

  it("/rewind 有会话 → 加载历史点；点选调用 rewindSession", async () => {
    const { ref, calls } = setup({ sid: "s1" });
    ref.current?.openRewind();
    await waitFor(() => expect(screen.getByTestId("rewind-dialog")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("#3")).toBeInTheDocument());
    fireEvent.click(screen.getByText("#3"));
    await waitFor(() => expect(calls.rewindSession.length).toBe(1));
    expect(calls.rewindSession[0][0]).toMatchObject({ sessionId: "s1", recordIndex: 3 });
  });

  it("/rename 无会话 → needSession；有会话 → 提交 renameSession", async () => {
    const { ref, store, calls } = setup({ sid: null });
    ref.current?.openRename();
    await waitFor(() => expect(sysMsgs(store, null)).toContain("Start a conversation first."));

    const { ref: ref2, calls: calls2 } = setup({ sid: "s1", sessions: [{ session_id: "s1", title: "旧标题" }] });
    ref2.current?.openRename();
    await waitFor(() => expect(screen.getByTestId("rename-dialog")).toBeInTheDocument());
    const input = screen.getByTestId("rename-input") as HTMLInputElement;
    expect(input.value).toBe("旧标题");
    fireEvent.change(input, { target: { value: "新标题" } });
    fireEvent.click(screen.getByTestId("rename-ok"));
    await waitFor(() => expect(calls2.renameSession.length).toBe(1));
    expect(calls2.renameSession[0][0]).toMatchObject({ sessionId: "s1", title: "新标题" });
  });

  it("/delete 确认后 deleteSession；删除激活会话 → onSwitchSession(null)", async () => {
    const { ref, calls, onSwitchSession } = setup({ sid: "s1" });
    ref.current?.openDelete();
    await waitFor(() => expect(screen.getByTestId("confirm-dialog")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("confirm-ok"));
    await waitFor(() => expect(calls.deleteSession.length).toBe(1));
    expect(calls.deleteSession[0][0]).toMatchObject({ sessionId: "s1" });
    expect(onSwitchSession).toHaveBeenCalledWith(null);
  });

  it("打开会话两步流：项目 → 会话 → switchSession + onSwitchSession", async () => {
    const { ref, calls, onSwitchSession } = setup({ sid: "s1" });
    ref.current?.openSessions();
    await waitFor(() => expect(screen.getByTestId("open-session-dialog")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("demo")).toBeInTheDocument());
    fireEvent.click(screen.getByText("demo"));
    await waitFor(() => expect(calls.listProjectSessions.length).toBe(1));
    await waitFor(() => expect(screen.getByText("Demo session")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Demo session"));
    await waitFor(() => expect(calls.switchSession.length).toBe(1));
    expect(calls.switchSession[0][0]).toMatchObject({ sessionId: "s9" });
    expect(onSwitchSession).toHaveBeenCalledWith("s9");
  });

  it("删除受保护项目 → 拒绝确认框", async () => {
    const { ref } = setup();
    ref.current?.openSessions();
    await waitFor(() => expect(screen.getByText("emrg")).toBeInTheDocument());
    const del = screen.getAllByTestId("open-session-delete");
    fireEvent.click(del[0]); // emrg 项目
    await waitFor(() => expect(screen.getByTestId("confirm-dialog")).toBeInTheDocument());
    expect(screen.getByText(/system project/i)).toBeInTheDocument();
  });

  it("新建会话：点选项目 → newSession + onSwitchSession(新会话)", async () => {
    const { ref, calls, onSwitchSession } = setup();
    ref.current?.openNewSession();
    await waitFor(() => expect(screen.getByTestId("new-session-dialog")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("demo")).toBeInTheDocument());
    fireEvent.click(screen.getByText("demo"));
    await waitFor(() => expect(calls.newSession.length).toBe(1));
    expect(calls.newSession[0][0]).toMatchObject({ projectPath: "/p/demo" });
    expect(onSwitchSession).toHaveBeenCalledWith("s-new");
  });

  it("/version 在 transcript 写入版本信息", async () => {
    const { ref, store } = setup({ sid: "s1" });
    await ref.current?.runDirect("/version", []);
    const msgs = sysMsgs(store, "s1");
    expect(msgs.some((m) => m.includes("0.2.81") && m.includes("srv-1") && m.includes("deepseek-v3"))).toBe(true);
  });

  it("/clear 调用 clearSession + 清空 transcript + cleared 消息；无会话 → needSession", async () => {
    const { ref, store, calls } = setup({ sid: "s1" });
    await ref.current?.runDirect("/clear", []);
    expect(calls.clearSession[0][0]).toMatchObject({ sessionId: "s1" });
    expect(sysMsgs(store, "s1")).toContain("Current conversation cleared.");

    const { ref: ref2, store: store2 } = setup({ sid: null });
    await ref2.current?.runDirect("/clear", []);
    expect(sysMsgs(store2, null)).toContain("Start a conversation first.");
  });

  it("window.emrg 缺失时降级：help 可开、数据加载 no-op 不崩溃", async () => {
    delete (window as unknown as { emrg?: unknown }).emrg;
    const ref: RefObject<DialogHostHandle | null> = createRef();
    const store = createTranscriptStore();
    render(
      <I18nProvider lang="en">
        <DialogHost ref={ref} sid={null} sessions={[]} transcript={store} appState={appState()} onSwitchSession={vi.fn()} />
      </I18nProvider>,
    );
    ref.current?.openHelp();
    await waitFor(() => expect(screen.getByTestId("help-dialog")).toBeInTheDocument());
    ref.current?.openMemory();
    await waitFor(() => expect(screen.getByTestId("memory-dialog")).toBeInTheDocument());
    await ref.current?.runDirect("/version", []);
    expect(sysMsgs(store, null).length).toBeGreaterThan(0);
  });
});
