import { afterEach, describe, expect, it, vi } from "vitest";
import { createRef, type RefObject } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    // Nothing has reported the effective image capability to this fixture, which is
    // the state a real renderer is in until a frame arrives (rant 2026-09-17T16:53:02).
    vision: null,
    currentVersion: "0.2.81",
    evolutionCount: 115,
    sessions: [],
    openSessions: [],
    busyBySid: {},
    turnStartBySid: {},
    disconnectedBySid: {},
    // No frame has reported a session's sandbox tier to this fixture (rant
    // 2026-09-30T09:30:16, GUI half) — absent is "the daemon has not said".
    sandboxBySid: {},
    sandboxRootsBySid: {},
    sandboxRootsNoticeBySid: {},
    upgradeBanner: null,
    pendingApproval: null,
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

function setup(opts: {
  sid?: string | null;
  sessions?: SessionSummary[];
  over?: Record<string, unknown>;
  /** 会话额外可写根的上报通道（Shell 由 bridge.setSandboxRoots 接上） */
  onSandboxRootsOp?: (op: "add" | "remove" | "list", path: string) => void;
  /** appState 覆盖（store 里已有的根/notice 由它注入） */
  state?: Partial<DaemonAppState>;
} = {}) {
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
        appState={appState(opts.state)}
        onSwitchSession={onSwitchSession}
        onSandboxRootsOp={opts.onSandboxRootsOp}
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

  it("记忆详情按 vanilla 截断：title 80 / body 2000", async () => {
    const longBody = "x".repeat(5000);
    const longTitle = "T".repeat(200);
    const { ref } = setup({
      over: {
        readMemory: vi.fn().mockResolvedValue({ id: "m1", title: longTitle, content: longBody }),
      },
    });
    ref.current?.openMemory();
    await waitFor(() => expect(screen.getByText("记忆一")).toBeInTheDocument());
    fireEvent.click(screen.getByText("记忆一"));
    await waitFor(() => expect(screen.getByTestId("memory-detail")).toBeInTheDocument());
    const detail = screen.getByTestId("memory-detail");
    expect(detail.textContent).toContain("T".repeat(80));
    expect(detail.textContent).not.toContain("T".repeat(81));
    expect(detail.textContent).toContain("x".repeat(2000));
    expect(detail.textContent).not.toContain("x".repeat(2001));
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
    // RenameDialog fills the input in a useEffect after mount — waitFor (flake
    // fix: CI raced the effect and observed '' instead of the prefill).
    await waitFor(() => expect(input.value).toBe("旧标题"));
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

/**
 * /sandbox 路由（rant 2026-10-09T09:43:39, GUI half）。
 *
 * 四条路各测一次，因为它们的**去向**不同：无参与 `list` 去对话框（列表只在那里
 * 看得见），`add`/`remove` 直接发一次写，其余落在用法提示上——而所有这些都要先有会话。
 */
describe("DialogHost /sandbox 路由", () => {
  it("无参 → 打开对话框，并把它问来的列表渲染出来", async () => {
    const onOp = vi.fn();
    const { ref } = setup({ sid: "s1", onSandboxRootsOp: onOp, state: { sandboxRootsBySid: { s1: ["/tmp/a"] } } });
    act(() => ref.current?.openSandbox([]));
    await waitFor(() => expect(screen.getByTestId("sandbox-roots-dialog")).toBeInTheDocument());
    // 打开即问：列表只回答发问的那条连接。
    expect(onOp).toHaveBeenCalledWith("list", "");
    expect(screen.getByTestId("sandbox-roots-row")).toHaveAttribute("data-root", "/tmp/a");
  });

  it("list → 同一条路（它是刷新，不是第二种界面）", async () => {
    const onOp = vi.fn();
    const { ref } = setup({ sid: "s1", onSandboxRootsOp: onOp });
    act(() => ref.current?.openSandbox(["list"]));
    await waitFor(() => expect(screen.getByTestId("sandbox-roots-dialog")).toBeInTheDocument());
    expect(onOp).toHaveBeenCalledWith("list", "");
  });

  it("add / remove 带路径 → 直接发那一次写，不必先开对话框", async () => {
    const onOp = vi.fn();
    const { ref } = setup({ sid: "s1", onSandboxRootsOp: onOp });
    act(() => ref.current?.openSandbox(["add", "/tmp/scratch"]));
    expect(onOp).toHaveBeenCalledWith("add", "/tmp/scratch");
    act(() => ref.current?.openSandbox(["remove", "/tmp/scratch"]));
    expect(onOp).toHaveBeenCalledWith("remove", "/tmp/scratch");
    expect(screen.queryByTestId("sandbox-roots-dialog")).toBeNull();
  });

  it("带路径的 add 里路径有空格 → 整段都算路径（parser 已按空白切过一次）", () => {
    const onOp = vi.fn();
    const { ref } = setup({ sid: "s1", onSandboxRootsOp: onOp });
    act(() => ref.current?.openSandbox(["add", "/tmp/my", "folder"]));
    expect(onOp).toHaveBeenCalledWith("add", "/tmp/my folder");
  });

  it("add / remove 缺路径 → 一行能照抄的用法，不发命令", () => {
    const onOp = vi.fn();
    const { ref, store } = setup({ sid: "s1", onSandboxRootsOp: onOp });
    act(() => ref.current?.openSandbox(["add"]));
    expect(onOp).not.toHaveBeenCalled();
    expect(sysMsgs(store, "s1").some((m) => m.includes("/sandbox add /tmp/scratch"))).toBe(true);
  });

  it("不认识子命令（含 TUI 的 `/sandbox <mode>`）→ 用法提示，不是静默无事发生", () => {
    const onOp = vi.fn();
    const { ref, store } = setup({ sid: "s1", onSandboxRootsOp: onOp });
    act(() => ref.current?.openSandbox(["workspace-write"]));
    expect(onOp).not.toHaveBeenCalled();
    expect(sysMsgs(store, "s1").some((m) => m.includes("Usage: /sandbox"))).toBe(true);
  });

  it("没有激活会话 → 提示先开会话，且不发任何命令、不弹框", () => {
    const onOp = vi.fn();
    const { ref, store } = setup({ sid: null, onSandboxRootsOp: onOp });
    act(() => ref.current?.openSandbox(["add", "/tmp/a"]));
    expect(onOp).not.toHaveBeenCalled();
    expect(sysMsgs(store, null)).toContain("Start a conversation first.");
    expect(screen.queryByTestId("sandbox-roots-dialog")).toBeNull();
  });
});
