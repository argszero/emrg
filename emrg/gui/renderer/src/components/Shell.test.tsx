import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { Shell } from "./Shell";
import { DaemonBridgeProvider } from "./DaemonBridgeProvider";
import { I18nProvider } from "../lib/i18n";
import type { DaemonEventFrame } from "../lib/daemonBridge";

/**
 * Shell.test.tsx — Batch 5 slice 3：聊天回路接线测试。
 * 模拟 preload 的 window.emrg（多订阅者通道），通过 emit 投递 daemon 事件帧，
 * 验证：会话自动选择、侧边栏切换、transcript 按 sid 切片、连接状态指示、
 * 断连横幅、无会话发送提示。
 */

/** 模拟 preload 暴露的 window.emrg（与 ipcRenderer.on 语义一致） */
function mockEmrg() {
  const listeners = new Set<(evt: DaemonEventFrame) => void>();
  const onEvent = vi.fn((cb: (evt: DaemonEventFrame) => void) => {
    listeners.add(cb);
    return () => listeners.delete(cb);
  });
  const sendMessage = vi.fn().mockResolvedValue({ requestId: "req-9" });
  // DialogHost 用到的 IPC 通道（Batch 5 slice 4）：对话框数据加载 + 直接执行类指令
  const clearSession = vi.fn().mockResolvedValue({ ok: true });
  const compactSession = vi.fn().mockResolvedValue({ ok: true });
  const listAgents = vi.fn().mockResolvedValue([{ name: "emrg", path: "/p/emrg" }]);
  const listTasks = vi.fn().mockResolvedValue([{ name: "evo", type: "evolution", enabled: true, session_id: "evo", agent_path: "/p/emrg" }]);
  const listRants = vi.fn().mockResolvedValue([{ timestamp: "2026-08-26T12:00:00+08:00", agent: "emrg", status: "pending", message: "x" }]);
  const listMemories = vi.fn().mockResolvedValue([]);
  const listSkills = vi.fn().mockResolvedValue({ skills: [], shadows: [] });
  const listHistory = vi.fn().mockResolvedValue({ messages: [] });
  // Issue #2349 part 2/2: the prompts the host typed in this session, as the daemon
  // holds them (oldest first) — the list the composer's ↑ walks.
  const listInputHistory = vi.fn().mockResolvedValue({ entries: ["first", "second"], hasMore: false });
  // Batch 5 slice 8：任务 CRUD + Rant 提交（window.emrg 桥已暴露，main.js IPC 在列）
  const taskCreate = vi.fn().mockResolvedValue({ ok: true });
  const taskUpdate = vi.fn().mockResolvedValue({ ok: true });
  const taskDelete = vi.fn().mockResolvedValue({ ok: true });
  const taskTemplateList = vi.fn().mockResolvedValue([{ name: "journal" }]);
  // issue #2490：间隔下限由 daemon 上报 —— 这里刻意给 120（不是渲染层曾写死的 60），
  // 于是「表单拿到的就是上报值」这件事在接线层可断言。
  const taskLimits = vi.fn().mockResolvedValue({ minInterval: 120 });
  const sendRant = vi.fn().mockResolvedValue({ ok: true, count: 11 });
  const restartDaemon = vi.fn().mockResolvedValue({ ok: true });
  const setModel = vi.fn().mockResolvedValue({ ok: true });
  const setSandbox = vi.fn().mockResolvedValue({ ok: true });
  const setSandboxRoots = vi.fn().mockResolvedValue({ ok: true });
  const triggerTask = vi.fn().mockResolvedValue({ ok: true });
  const switchSession = vi.fn().mockResolvedValue({ ok: true });
  // Rant 2026-10-29T14:21:57: the stop button must reach the daemon for a turn this
  // client did not start, so the bridge it calls has to exist here.
  const cancel = vi.fn().mockResolvedValue({ ok: true });
  (window as unknown as { emrg?: unknown }).emrg = {
    onEvent,
    sendMessage,
    clearSession,
    compactSession,
    listAgents,
    listTasks,
    listRants,
    listMemories,
    listSkills,
    listHistory,
    listInputHistory,
    taskCreate,
    taskUpdate,
    taskDelete,
    taskTemplateList,
    taskLimits,
    sendRant,
    restartDaemon,
    setModel,
    setSandbox,
    setSandboxRoots,
    triggerTask,
    switchSession,
    cancel,
  };
  return {
    listeners,
    onEvent,
    sendMessage,
    clearSession,
    listAgents,
    listTasks,
    listRants,
    listHistory,
    listInputHistory,
    taskCreate,
    taskUpdate,
    taskDelete,
    taskTemplateList,
    taskLimits,
    sendRant,
    restartDaemon,
    setModel,
    setSandbox,
    setSandboxRoots,
    triggerTask,
    switchSession,
    cancel,
    emit: (evt: DaemonEventFrame) => listeners.forEach((cb) => cb(evt)),
  };
}

function wrapper(children: ReactNode) {
  return (
    <I18nProvider lang="en">
      <DaemonBridgeProvider>{children}</DaemonBridgeProvider>
    </I18nProvider>
  );
}

function openSessionsFrame(entries: { sid: string; title?: string }[]): DaemonEventFrame {
  return { type: "open_sessions", data: { openSessions: entries } };
}

function sessionsFrame(sessions: { session_id: string; title?: string }[]): DaemonEventFrame {
  return { type: "sessions", data: { sessions } };
}

function deltaFrame(sid: string, text: string): DaemonEventFrame {
  return { type: "message_delta", sid, data: { chunks: [{ request_id: `req-${sid}`, content: text }] } };
}

/** 向 tiptap 编辑器输入文本（Stage 1：textarea → contenteditable，fireEvent.change 失效） */
async function typeIntoComposer(text: string) {
  const input = await screen.findByTestId("composer-input");
  await userEvent.type(input, text);
}

describe("Shell (Batch 5 slice 3 chat wiring)", () => {
  it("the session's sandbox tier is the daemon's: a broadcast moves the chip, a click asks (rant 2026-09-30T09:30:16, GUI half)", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(1));
    // Nothing has reported a tier yet → the chip shows its own default, which is not
    // a claim about the daemon (the store key is absent, not "workspace-write").
    await waitFor(() => expect(screen.getByTestId("sandbox-workspace-write").getAttribute("aria-pressed")).toBe("true"));
    // A tier set in the TUI arrives as this frame — the chip follows the daemon.
    m.emit({ type: "sandbox_set", sid: "s1", data: { type: "sandbox_set", session_id: "s1", mode: "read-only" } });
    await waitFor(() => expect(screen.getByTestId("sandbox-read-only").getAttribute("aria-pressed")).toBe("true"));
    expect(screen.getByTestId("sandbox-workspace-write").getAttribute("aria-pressed")).toBe("false");
    // Clicking asks the daemon and moves nothing by itself.
    await userEvent.click(screen.getByTestId("sandbox-danger-full-access"));
    await waitFor(() => expect(m.setSandbox).toHaveBeenCalledWith({ sessionId: "s1", mode: "danger-full-access" }));
    expect(screen.getByTestId("sandbox-read-only").getAttribute("aria-pressed")).toBe("true");
  });

  it("the session's extra writable roots are the daemon's too: the frame moves the chip, and the click opens the one surface that can name one (rant 2026-10-09T09:43:39, GUI half)", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(1));
    // Nobody has asked yet → the chip is there but claims no count: `0` would be a
    // state the daemon never reported (same rule as the tier chip above it).
    await waitFor(() => expect(screen.getByTestId("sandbox-roots-open")).toHaveTextContent("Roots"));
    expect(screen.getByTestId("sandbox-roots-open")).not.toHaveTextContent("Roots 0");
    // A root added by the TUI (or another GUI) arrives as this frame and the chip follows.
    m.emit({
      type: "sandbox_roots",
      sid: "s1",
      data: { type: "sandbox_roots", session_id: "s1", op: "add", roots: ["/tmp/scratch"] },
    });
    await waitFor(() => expect(screen.getByTestId("sandbox-roots-open")).toHaveTextContent("Roots 1"));
    // The click sends nothing but the read the dialog needs on open — the whole point
    // of this test: preload → provider → bridge → dialog is a chain of four names, and
    // a name missing from any of them ends as a dialog that opens and says nothing.
    await userEvent.click(screen.getByTestId("sandbox-roots-open"));
    await waitFor(() => expect(screen.getByTestId("sandbox-roots-dialog")).toBeInTheDocument());
    await waitFor(() => expect(m.setSandboxRoots).toHaveBeenCalledWith({ sessionId: "s1", op: "list", path: "" }));
    expect(screen.getByTestId("sandbox-roots-row")).toHaveAttribute("data-root", "/tmp/scratch");
    // Type a path and add it: the op leaves on the same chain, and the list on screen
    // does **not** move by itself — only the daemon's next frame may move it.
    await userEvent.type(screen.getByTestId("sandbox-roots-path"), "/tmp/other");
    await userEvent.click(screen.getByTestId("sandbox-roots-add"));
    await waitFor(() => expect(m.setSandboxRoots).toHaveBeenCalledWith({ sessionId: "s1", op: "add", path: "/tmp/other" }));
    expect(screen.getAllByTestId("sandbox-roots-row")).toHaveLength(1);
  });

  afterEach(() => {
    delete (window as unknown as { emrg?: unknown }).emrg;
  });

  it("renders the chat loop (transcript + composer) without window.emrg (degradation)", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("transcript-view")).toBeInTheDocument());
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.getByTestId("react-shell-sidebar")).toBeInTheDocument();
    // 无 emrg → 连接状态点灰色（未连接）
    expect(screen.getByTestId("conn-status").querySelector(".conn-dot")?.className).toContain("gray");
  });

  it("auto-selects the first open session and renders its transcript from broadcasts", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }, { sid: "s2", title: "Beta" }]));
    m.emit(sessionsFrame([{ session_id: "s1", title: "Alpha" }, { session_id: "s2", title: "Beta" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(2));
    // 自动选中第一个会话 → s1 条目带 active
    expect(screen.getAllByTestId("open-session-item")[0].className).toContain("active");
    // s1 的 delta 到达 → transcript 显示
    m.emit(deltaFrame("s1", "Hello from s1"));
    await waitFor(() => expect(screen.getByText("Hello from s1")).toBeInTheDocument());
  });

  it("clicking a sidebar session switches the transcript to that session (sid-scoped)", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }, { sid: "s2", title: "Beta" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(2));
    // 两个会话都有内容，但 s1 激活 → 只显示 s1
    m.emit(deltaFrame("s1", "content-one"));
    m.emit(deltaFrame("s2", "content-two"));
    await waitFor(() => expect(screen.getByText("content-one")).toBeInTheDocument());
    expect(screen.queryByText("content-two")).not.toBeInTheDocument();
    // 点击 s2 → 激活切换，transcript 显示 s2 内容
    const s2 = screen.getAllByTestId("open-session-item").find((el) => el.dataset.sid === "s2");
    expect(s2).toBeTruthy();
    fireEvent.click(s2 as HTMLElement);
    await waitFor(() => expect(screen.getByText("content-two")).toBeInTheDocument());
    expect(screen.queryByText("content-one")).not.toBeInTheDocument();
    expect((screen.getAllByTestId("open-session-item").find((el) => el.dataset.sid === "s2"))?.className).toContain("active");
  });

  it("#2351：rewind_result 让 Shell 从落盘历史重建该会话，而不是删掉手里最后 N 行", async () => {
    // The defect this pins: `rewind_result` used to be the requesting client's own
    // business, and a client that answered it by deleting its last N rows stayed right
    // only while it still held them. The daemon now broadcasts the frame to every
    // connection subscribed to the session — so the rule is "whoever is told the history
    // was rewritten rebuilds from the file", and the rebuild is this effect. Without it
    // the transcript is cleared and never refilled: measured, deleting the effect left
    // all 686 renderer tests green.
    const m = mockEmrg();
    let rewritten = false;
    m.listHistory.mockImplementation(() =>
      Promise.resolve(
        rewritten
          ? { messages: [{ record_index: 0, kind: "message", role: "user", content: "kept row" }], hasMore: false }
          : { messages: [], hasMore: false },
      ),
    );
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(m.listHistory).toHaveBeenCalledTimes(1));
    // A row that arrived live — the rewind removes it, and the file after the rewrite
    // no longer holds it.
    m.emit(deltaFrame("s1", "the row the rewind removed"));
    await waitFor(() => expect(screen.getByText("the row the rewind removed")).toBeInTheDocument());

    rewritten = true;
    m.emit({
      type: "rewind_result",
      sid: "s1",
      data: {
        type: "rewind_result",
        session_id: "s1",
        ok: true,
        record_index: 1,
        kept_count: 1,
        removed_count: 1,
        removed: [{ record_index: 1, kind: "message", role: "assistant" }],
      },
    });
    // ① the file is read again — this is the assertion the effect alone satisfies
    await waitFor(() => expect(m.listHistory).toHaveBeenCalledTimes(2));
    // ② what the rebuilt transcript shows comes from the file, not from the rows the
    //    client happened to hold
    await waitFor(() => expect(screen.getByText("kept row")).toBeInTheDocument());
    expect(screen.queryByText("the row the rewind removed")).not.toBeInTheDocument();
  });

  it("loads a session's history as records and replays them like the live stream (rant 2026-09-20T18:58:44)", async () => {
    const m = mockEmrg();
    // 一条远超 80 字的助手消息 + 一轮工具 + 收尾文本：旧路径取 preview 只会显示 81 字，
    // 且工具活动整条不显示（list_history 当时不发 tool_result）。
    const long = `L${"o".repeat(600)}ng`;
    const records = [
      { record_index: 0, kind: "message", role: "user", content: "看看这个仓库" },
      {
        record_index: 1,
        kind: "message",
        role: "assistant",
        content: long,
        tool_calls: [{ id: "c1", function: { name: "bash", arguments: '{"intent":"list the files"}' } }],
      },
      { record_index: 2, kind: "tool_result", tool_call_id: "c1", tool_name: "bash", content: "a\nb\n" },
      { record_index: 3, kind: "message", role: "assistant", content: "结论：一切正常。" },
    ];
    // 桥的替身照 daemon 的两种回答来回答（缺省模式 = preview 截断、只有消息；records 模式
    // = 完整记录序列）——于是「GUI 改回缺省模式」这个真实退化会在这里变红，而不是只断言
    // 一个参数名。
    m.listHistory.mockImplementation((p: { includeRecords?: boolean }) =>
      Promise.resolve(
        p?.includeRecords
          ? { messages: records, hasMore: false }
          : {
              messages: [
                { record_index: 0, role: "user", content: "看看这个仓库", preview: "看看这个仓库" },
                { record_index: 1, role: "assistant", content: long, preview: `${long.slice(0, 80)}…` },
              ],
              hasMore: false,
            },
      ),
    );
    const { container } = render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(m.listHistory).toHaveBeenCalledTimes(1));

    // 1) 问的是完整记录序列（不是 preview 截断的那一版）
    expect(m.listHistory).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "s1", includeRecords: true }),
    );
    // 2) 正文完整（截断时这里只会出现 81 字）
    await waitFor(() => expect(screen.getByText(long)).toBeInTheDocument());
    // 3) 工具活动与 intent 在历史里也看得见
    await waitFor(() => expect(container.querySelectorAll(".tool-row")).toHaveLength(1));
    expect(container.querySelector(".tool-intent")?.textContent).toBe("list the files");
    expect(screen.getByText("结论：一切正常。")).toBeInTheDocument();
  });

  /**
   * 两页记录（更新的一页 + 更早的一页），桥的替身照 `before_index` 回答：带游标 = 更早那一页，
   * 不带 = 又一份最新页。于是「游标丢了」这个真实退化在断言里可辨，而不只是断言一个参数名。
   */
  function mockTwoHistoryPages(m: ReturnType<typeof mockEmrg>) {
    const newest = [
      { record_index: 4, kind: "message", role: "user", content: "newest-1" },
      { record_index: 5, kind: "message", role: "assistant", content: "newest-2" },
      { record_index: 6, kind: "message", role: "user", content: "newest-3" },
    ];
    const older = [
      { record_index: 1, kind: "message", role: "user", content: "older-1" },
      { record_index: 2, kind: "message", role: "assistant", content: "older-2" },
      { record_index: 3, kind: "message", role: "user", content: "older-3" },
    ];
    m.listHistory.mockImplementation((p: { beforeIndex?: number } = {}) =>
      Promise.resolve(
        p.beforeIndex != null
          ? { messages: older, hasMore: false }
          : { messages: newest, hasMore: true },
      ),
    );
    return { newest, older };
  }

  /** 打开一个会话，等第一页落定，再把滚动位置带到顶部（TranscriptView 的 capture 监听在 scrollTop<=2 时上翻）。 */
  async function openSessionAndScrollToTop(
    m: ReturnType<typeof mockEmrg>,
    container: HTMLElement,
    firstPageText: string,
  ) {
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(m.listHistory).toHaveBeenCalledTimes(1));
    // 第一页真的渲染出来（此时 hasMore 已落定 → canLoadOlder(hasMore, loading) 为真），再模拟上翻。
    await waitFor(() => expect(screen.getByText(firstPageText)).toBeInTheDocument());
    const viewport = container.querySelector<HTMLElement>('[data-testid="transcript-view"]');
    expect(viewport).not.toBeNull();
    Object.defineProperty(viewport, "scrollTop", { value: 0, configurable: true, writable: true });
    Object.defineProperty(viewport, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(viewport, "clientHeight", { value: 400, configurable: true });
    act(() => {
      viewport!.dispatchEvent(new Event("scroll", { bubbles: false }));
    });
  }

  it("asks for the records BEFORE the oldest loaded one when scrolled to the top (rant 2026-09-20T18:58:44)", async () => {
    // 该 rant 的第三条缺陷是「分页会重复、会漏、会让更早内容不可达」，修法是游标改成
    // 最早已加载记录的绝对 record_index。游标一旦丢失，页 2 会再取一次最新页，而
    // `loaded` 会把整页滤空 —— 视图永不前进，且套件全绿（issue #1493 的变异臂实测）。
    const m = mockEmrg();
    const { older } = mockTwoHistoryPages(m);
    const { container } = render(wrapper(<Shell />));
    await openSessionAndScrollToTop(m, container, "newest-1");

    await waitFor(() => expect(m.listHistory).toHaveBeenCalledTimes(2));
    expect(m.listHistory).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ sessionId: "s1", includeRecords: true, beforeIndex: 4 }),
    );
    // 更早一页确实进了视图，而不是被 `loaded` 去重滤空。
    await waitFor(() => expect(screen.getByText(older[0].content)).toBeInTheDocument());
  });

  it("clicking the load bar asks for the older page, same cursor as the scroll path (host report 2026-10-08)", async () => {
    // 宿主报障：「点击加载更早消息，没有任何效果」。断在接缝上：条形件的 onClick → Shell 的
    // onScrollTop（150ms 防抖）→ loadOlderHistory。滚动那条路一直是好的，两条路必须落到同一个
    // 游标上——所以这里断言的不是「回调被调用」，而是 daemon 真收到了 beforeIndex=4 那一页。
    const m = mockEmrg();
    const { older } = mockTwoHistoryPages(m);
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(m.listHistory).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("newest-1")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("history-load-bar"));

    await waitFor(() => expect(m.listHistory).toHaveBeenCalledTimes(2));
    expect(m.listHistory).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ sessionId: "s1", includeRecords: true, beforeIndex: 4 }),
    );
    await waitFor(() => expect(screen.getByText(older[0].content)).toBeInTheDocument());
  });

  it("prepends an older page as one block, keeping record order (rant 2026-09-20T18:58:44)", async () => {
    const m = mockEmrg();
    const { newest, older } = mockTwoHistoryPages(m);
    const { container } = render(wrapper(<Shell />));
    await openSessionAndScrollToTop(m, container, "newest-1");
    await waitFor(() => expect(screen.getByText(older[2].content)).toBeInTheDocument());

    // 文本在容器里的先后 = DOM 顺序：更早一页整块在当前页之前，且页内仍是记录顺序
    // （倒序 unshift 会让页内反而颠倒，这一条同时钉住「前插」和「不翻转」）。
    const text = container.querySelector('[data-testid="transcript-view"]')?.textContent ?? "";
    const at = (s: string) => text.indexOf(s);
    expect(at(older[0].content)).toBeGreaterThanOrEqual(0);
    expect(at(older[2].content)).toBeLessThan(at(newest[0].content));
    expect(at(older[0].content)).toBeLessThan(at(older[2].content));
  });

  it("the load bar stays silent on the first page when there is no earlier page (rant 2026-10-09T09:25:00)", async () => {
    // 「没有更早的页」这句话是**回答**，而首屏没人问过：vanilla `app.js:838-841` 首屏
    // 只有一个 `if (hasMore)` 分支，else 在翻页那条路径上。规则现在只有一处
    // （`lib/history.ts:loadBarKey`），这条断言钉住它在用户眼前的那一半。
    const m = mockEmrg();
    m.listHistory.mockResolvedValue({
      messages: [{ record_index: 1, kind: "message", role: "user", content: "only-1" }],
      hasMore: false,
    });
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getByText("only-1")).toBeInTheDocument());
    expect(screen.queryByTestId("history-load-bar")).toBeNull();
  });

  it("after paging to the end the bar says so — and is a status line, not a control", async () => {
    const m = mockEmrg();
    const { older } = mockTwoHistoryPages(m); // 更早那页 hasMore:false = 翻到头了
    const { container } = render(wrapper(<Shell />));
    await openSessionAndScrollToTop(m, container, "newest-1");
    await waitFor(() => expect(screen.getByText(older[0].content)).toBeInTheDocument());
    const bar = screen.getByTestId("history-load-bar");
    expect(bar.tagName).toBe("DIV");
    expect(bar.textContent).toBe("No more history");
  });

  it("while an older page is in flight the bar is already not a control (rant 2026-10-09T09:25:00)", async () => {
    // 钉的是**生产读取点**，不是组件契约：`canLoadOlder(hasMore, loading)` 的 `loading`
    // 那一半到底会不会被渲染出来。只在每个 loader 的 `finally` 里 publish 时，`loading =
    // true` 从未到达渲染 —— 翻页途中顶部条仍是可点的 BUTTON，点下去在 `st.loading` 早退处
    // 什么也不做（宿主报障里「长得像能点、点下去是死的」那一类，换了一个分支）。复查
    // cyc20261009-151044 的否决就断在这里：把更早那页换成一个**未兑现**的 promise，
    // 在它还没落定时问 DOM 要标签名。`TranscriptView.test.tsx` 传 prop 看不见这一条。
    const m = mockEmrg();
    let release: (v: unknown) => void = () => {};
    const pending = new Promise((res) => { release = res; });
    const newest = [{ record_index: 4, kind: "message", role: "user", content: "newest-1" }];
    const older = [{ record_index: 1, kind: "message", role: "user", content: "older-1" }];
    m.listHistory.mockImplementation((p: { beforeIndex?: number } = {}) =>
      p.beforeIndex != null
        ? (pending as Promise<{ messages: unknown[]; hasMore: boolean }>)
        : Promise.resolve({ messages: newest, hasMore: true }),
    );
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(m.listHistory).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("newest-1")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("history-load-bar"));
    await waitFor(() => expect(m.listHistory).toHaveBeenCalledTimes(2));
    // 此刻更早那页还没落定：条形件必须已经退化成状态文字。
    expect(screen.getByTestId("history-load-bar").tagName).toBe("DIV");

    // 落定之后仍然不是控件（翻到头了），并且更早那页真的进来了。
    act(() => release({ messages: older, hasMore: false }));
    await waitFor(() => expect(screen.getByText(older[0].content)).toBeInTheDocument());
    expect(screen.getByTestId("history-load-bar").tagName).toBe("DIV");
  });

  it("shows the connection status + model from the status broadcast", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit({ type: "status", data: { connected: true, model: "claude-3.7" } });
    await waitFor(() => expect(screen.getByTestId("conn-status").textContent).toContain("claude-3.7"));
    expect(screen.getByTestId("conn-status").querySelector(".conn-dot")?.className).toContain("green");
  });

  it("shows the daemon's effective image capability, not a declaration (rant 2026-09-17T16:53:02)", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    // 没有读数 → 没有标记：null 既不是 true 也不是 false（宣告"支持图片"要靠 daemon 说）
    m.emit({ type: "status", data: { connected: true, model: "claude-3.7" } });
    await waitFor(() => expect(screen.getByTestId("conn-status").textContent).toContain("claude-3.7"));
    expect(screen.queryByTestId("conn-vision")).not.toBeInTheDocument();

    m.emit({ type: "pong", data: { identity: { instance_id: "sv1" }, model: "claude-3.7", vision: true } });
    await waitFor(() => expect(screen.getByTestId("conn-vision").textContent).toBe("images"));
    expect(screen.getByTestId("conn-vision").className).toContain("on");

    // daemon 移动了生效值（/model 切到无视觉模型）→ 标记跟着变，不必等重连
    m.emit({ type: "config_applied", data: { model: "kimi-k2", vision: false, applied: ["vision"] } });
    await waitFor(() => expect(screen.getByTestId("conn-vision").textContent).toBe("no images"));
    expect(screen.getByTestId("conn-vision").className).toContain("off");

    // 断连后不再宣称任何能力（模型名也退回 disconnected 文案）
    m.emit({ type: "status", data: { connected: false } });
    await waitFor(() => expect(screen.queryByTestId("conn-vision")).not.toBeInTheDocument());
  });

  it("shows the disconnected banner when the active session broadcasts disconnected", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(1));
    m.emit({ type: "disconnected", sid: "s1", data: {} });
    await waitFor(() => expect(screen.getByTestId("conn-banner")).toBeInTheDocument());
  });

  it("composer send with no active session shows the need-session hint", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    await typeIntoComposer("hello");
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(screen.getByText("Start a conversation first.")).toBeInTheDocument());
  });

  // ── Batch 5 slice 4：/指令路由 + 侧边栏按钮 ──

  it("/help command opens the help dialog", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    await typeIntoComposer("/help");
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(screen.getByTestId("help-dialog")).toBeInTheDocument());
    expect(screen.getAllByTestId("help-row").length).toBeGreaterThanOrEqual(16);
  });

  it("/rename with an active session opens the rename dialog", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    m.emit(sessionsFrame([{ session_id: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(1));
    await typeIntoComposer("/rename");
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(screen.getByTestId("rename-dialog")).toBeInTheDocument());
  });

  it("sidebar new-chat button opens the new-session dialog", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("new-chat-btn")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("new-chat-btn"));
    await waitFor(() => expect(screen.getByTestId("new-session-dialog")).toBeInTheDocument());
  });

  it("sidebar open-chat button opens the open-session dialog", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("open-chat-btn")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("open-chat-btn"));
    await waitFor(() => expect(screen.getByTestId("open-session-dialog")).toBeInTheDocument());
  });

  it("/clear with an active session calls clearSession and shows the cleared message", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(1));
    await typeIntoComposer("/clear");
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(screen.getByText("Current conversation cleared.")).toBeInTheDocument());
  });

  // ── Batch 5 slice 5：workspace 视图切换（vanilla switchView 语义） ──

  it("默认 sessions 视图：显示会话区（transcript/composer），不显示面板", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("transcript-view")).toBeInTheDocument());
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.queryByTestId("workspace-view")).not.toBeInTheDocument();
    expect(screen.queryByTestId("panel-agents")).not.toBeInTheDocument();
  });

  it("点击 nav-agents → 面板视图：显示项目面板，隐藏会话 chrome", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-agents"));
    await waitFor(() => expect(screen.getByTestId("panel-agents")).toBeInTheDocument());
    expect(screen.getByTestId("nav-agents").className).toContain("active");
    // 面板视图隐藏会话 chrome（vanilla setWorkspaceChrome("panel")）
    expect(screen.queryByTestId("transcript-view")).not.toBeInTheDocument();
    expect(screen.queryByTestId("composer")).not.toBeInTheDocument();
    expect(screen.queryByTestId("result-panel")).not.toBeInTheDocument();
  });

  it("点击当前激活面板 → toggle 回会话视图（vanilla 语义）", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-agents"));
    await waitFor(() => expect(screen.getByTestId("panel-agents")).toBeInTheDocument());
    // 再点同一面板 → 关闭回会话
    fireEvent.click(screen.getByTestId("nav-agents"));
    await waitFor(() => expect(screen.queryByTestId("panel-agents")).not.toBeInTheDocument());
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.getByTestId("nav-agents").className).not.toContain("active");
  });

  it("面板间切换：agents → tasks → settings", async () => {
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-agents"));
    await waitFor(() => expect(screen.getByTestId("panel-agents")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("panel-tasks")).toBeInTheDocument());
    expect(screen.queryByTestId("panel-agents")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("nav-settings"));
    await waitFor(() => expect(screen.getByTestId("panel-settings")).toBeInTheDocument());
    expect(screen.queryByTestId("panel-tasks")).not.toBeInTheDocument();
  });

  it("面板视图下点击会话条目 → 回会话视图并切换 sid", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }, { sid: "s2", title: "Beta" }]));
    await waitFor(() => expect(screen.getAllByTestId("open-session-item")).toHaveLength(2));
    // 打开面板
    fireEvent.click(screen.getByTestId("nav-rants"));
    await waitFor(() => expect(screen.getByTestId("panel-rants")).toBeInTheDocument());
    // 点击 s2 → 回会话视图，s2 激活
    const s2 = screen.getAllByTestId("open-session-item").find((el) => el.dataset.sid === "s2");
    expect(s2).toBeTruthy();
    fireEvent.click(s2 as HTMLElement);
    await waitFor(() => expect(screen.queryByTestId("panel-rants")).not.toBeInTheDocument());
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect((screen.getAllByTestId("open-session-item").find((el) => el.dataset.sid === "s2"))?.className).toContain("active");
    expect(screen.getByTestId("nav-sessions").className).toContain("active");
  });

  // ── Batch 5 slice 7：workspace 面板数据接线（真实 listAgents/listTasks/listRants） ──

  it("进入任务面板 → 加载 listTasks 并渲染任务行", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(m.listTasks).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    expect(screen.getByTestId("task-row")).toHaveTextContent("evo");
  });

  it("任务行「打开会话」→ switchSession(sessionId, projectPath) + 回到会话视图（vanilla #924）", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    // 打开会话按钮存在且可点（有 session_id）
    const openBtn = screen.getByTitle("Open session");
    expect(openBtn).toBeEnabled();
    fireEvent.click(openBtn);
    await waitFor(() =>
      expect(m.switchSession).toHaveBeenCalledWith({ sessionId: "evo", projectPath: "/p/emrg" }),
    );
    // 切回会话视图（transcript + composer 可见）
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    expect(screen.queryByTestId("panel-tasks")).not.toBeInTheDocument();
  });

  it("切换被拒（issue #2637）：任务面板原地不动，拒绝出现在人操作的面板上，不写进没人看的转录", async () => {
    const m = mockEmrg();
    m.switchSession.mockRejectedValueOnce(new Error("boom-guard"));
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    fireEvent.click(screen.getByTitle("Open session"));
    // 拒绝可见：横幅就在任务面板上方
    const banner = await screen.findByTestId("switch-error");
    expect(banner).toHaveTextContent("boom-guard");
    // 人没有被搬走：仍在任务面板，会话视图没有被切换过去
    expect(screen.getByTestId("panel-tasks")).toBeInTheDocument();
    expect(screen.queryByTestId("composer")).not.toBeInTheDocument();
  });

  it("切换被拒后再次成功 → 横幅退场并进入会话视图（issue #2637）", async () => {
    const m = mockEmrg();
    m.switchSession.mockRejectedValueOnce(new Error("boom-guard"));
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    fireEvent.click(screen.getByTitle("Open session"));
    await screen.findByTestId("switch-error");
    // 第二次点击：mock 已回落到默认 resolve
    fireEvent.click(screen.getByTitle("Open session"));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    expect(screen.queryByTestId("switch-error")).not.toBeInTheDocument();
  });

  it("进入 Rant 面板 → 加载 listRants 并渲染 rant 行", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-rants"));
    await waitFor(() => expect(m.listRants).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("rant-row")).toBeInTheDocument());
  });

  it("展开 Rant 行 → 详情走真实 markdown 渲染器（Shell 把渲染器传进 WorkspaceView，rant 2026-09-30T09:06:16）", async () => {
    // 守卫点：Shell → WorkspaceView → RantList 的 renderer 接线（回归正是断在这里）。
    // 走真实 createProdMarkdownRenderer（marked/DOMPurify 经 vendorMarkdown 注入），
    // 因此断言的是真渲染产物：#### 【现象】 → <h4>。
    const m = mockEmrg();
    m.listRants.mockResolvedValue([
      { timestamp: "2026-09-30T09:06:16+08:00", agent: "emrg", status: "pending", message: "【现象】\n**加粗**正文" },
    ]);
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-rants"));
    await waitFor(() => expect(screen.getByTestId("rant-row")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("rant-row"));
    await waitFor(() => expect(document.querySelector(".rant-md h4")).not.toBeNull());
    expect(document.querySelector(".rant-md strong")).not.toBeNull();
  });

  it("进入项目面板 → 加载 listAgents；查看会话 → listAgentSessions 加载会话行", async () => {
    const m = mockEmrg();
    m.listAgents.mockResolvedValue([{ name: "p1", path: "/p/p1" }]);
    (window as unknown as { emrg: { listAgentSessions?: unknown } }).emrg.listAgentSessions =
      vi.fn().mockResolvedValue({ sessions: [{ session_id: "s1", title: "会话一" }] });
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-agents"));
    await waitFor(() => expect(m.listAgents).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId("agent-row")).toBeInTheDocument());
    // 查看会话 → 会话子视图加载（en 标题 "Sessions"；限定在项目行内避免与侧栏撞名）
    const row = screen.getByTestId("agent-row");
    fireEvent.click(row.querySelector('button[title="Sessions"]') as HTMLButtonElement);
    await waitFor(() =>
      expect((window as unknown as { emrg: { listAgentSessions?: unknown } }).emrg.listAgentSessions).toHaveBeenCalled(),
    );
    await waitFor(() => expect(screen.getByTestId("agent-session-row")).toBeInTheDocument());
    expect(screen.getByTestId("agent-session-row")).toHaveTextContent("会话一");
  });

  // ── Batch 5 slice 8：任务表单 + Rant 对话框接线 ──

  it("任务面板激活时每 5s 轮询 listTasks（vanilla startTaskPoll 语义）", async () => {
    vi.useFakeTimers();
    try {
      const m = mockEmrg();
      render(wrapper(<Shell />));
      await vi.waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
      fireEvent.click(screen.getByTestId("nav-tasks"));
      await vi.waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
      const callsAfterActivation = m.listTasks.mock.calls.length;
      expect(callsAfterActivation).toBeGreaterThanOrEqual(1);
      // 快进 5s → 再次轮询；再快进 10s → 至少再两次
      await act(async () => {
        vi.advanceTimersByTime(5000);
      });
      await act(async () => {
        vi.advanceTimersByTime(10_000);
      });
      expect(m.listTasks.mock.calls.length).toBeGreaterThanOrEqual(callsAfterActivation + 2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("任务面板「＋ 添加任务」→ 打开表单 → 保存 → taskCreate + 刷新列表", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    // 打开表单（任务行按钮 + 底部添加按钮均触发 openTaskForm）
    fireEvent.click(screen.getByText("＋ Add task"));
    await waitFor(() => expect(screen.getByTestId("task-form-dialog")).toBeInTheDocument());
    // 类型下拉来自 taskTemplateList（journal）+ evolution 兜底
    const typeSel = screen.getByTestId("task-form-type") as HTMLSelectElement;
    await waitFor(() => expect(m.taskTemplateList).toHaveBeenCalled());
    expect(typeSel.options.length).toBeGreaterThanOrEqual(2);
    // 填表保存
    fireEvent.change(screen.getByTestId("task-form-name"), { target: { value: "t2" } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    await waitFor(() => expect(m.taskCreate).toHaveBeenCalledTimes(1));
    const payload = m.taskCreate.mock.calls[0][0];
    expect(payload.name).toBe("t2");
    expect(payload.interval).toBe(1800);
    await waitFor(() => expect(m.listTasks).toHaveBeenCalledTimes(2)); // 初始 + 保存后刷新
    expect(screen.queryByTestId("task-form-dialog")).not.toBeInTheDocument();
  });

  it("表单的间隔下限来自 daemon 上报（taskLimits），不是渲染层写死的数", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    fireEvent.click(screen.getByText("＋ Add task"));
    await waitFor(() => expect(screen.getByTestId("task-form-dialog")).toBeInTheDocument());
    // 接线本身：表单打开时问过 daemon 的下限
    await waitFor(() => expect(m.taskLimits).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("task-form-name"), { target: { value: "t3" } });
    // 上报值是 120：写成 60（渲染层曾经写死的那个数）必须被拒，且错误文案带 120
    fireEvent.change(screen.getByTestId("task-form-interval"), { target: { value: "60" } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    expect(screen.getByTestId("task-form-error")).toHaveTextContent("120");
    expect(m.taskCreate).not.toHaveBeenCalled();

    fireEvent.change(screen.getByTestId("task-form-interval"), { target: { value: "120" } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    await waitFor(() => expect(m.taskCreate).toHaveBeenCalledTimes(1));
    expect(m.taskCreate.mock.calls[0][0].interval).toBe(120);
  });

  it("任务行「Edit」→ 表单预填 + name 只读 → 保存 → taskUpdate（不改名）", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    fireEvent.click(screen.getByTitle("Edit"));
    await waitFor(() => expect(screen.getByTestId("task-form-dialog")).toBeInTheDocument());
    // 预填由 TaskFormDialog 的 useEffect 在 mount 后执行（vanilla openTaskForm 语义，
    // 见 #1031 同类 flaky 修复）。用 waitFor 等待 effect 填充值，避免在 effect 跑完前
    // 读到空 name.value（#1035 轮询测试的 fake-timer/async 交错曾偶发暴露此竞态）。
    const name = screen.getByTestId("task-form-name") as HTMLInputElement;
    await waitFor(() => expect(name.value).toBe("evo"));
    expect(name.disabled).toBe(true);
    fireEvent.change(screen.getByTestId("task-form-interval"), { target: { value: "7200" } });
    fireEvent.click(screen.getByTestId("task-form-save"));
    await waitFor(() => expect(m.taskUpdate).toHaveBeenCalledTimes(1));
    expect(m.taskUpdate.mock.calls[0][0]).toMatchObject({ name: "evo", interval: 7200 });
    expect(m.taskCreate).not.toHaveBeenCalled();
  });

  it("任务行「Delete」→ 确认弹窗 → taskDelete + 刷新", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-tasks"));
    await waitFor(() => expect(screen.getByTestId("task-row")).toBeInTheDocument());
    fireEvent.click(screen.getByTitle("Delete"));
    await waitFor(() => expect(screen.getByTestId("confirm-dialog")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("confirm-ok"));
    await waitFor(() => expect(m.taskDelete).toHaveBeenCalledWith({ name: "evo" }));
    await waitFor(() => expect(m.listTasks).toHaveBeenCalledTimes(2));
  });

  it("Rant 面板「＋ New rant」→ 对话框 → 提交 → sendRant + 刷新列表", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(screen.getByTestId("composer")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("nav-rants"));
    await waitFor(() => expect(screen.getByTestId("rant-row")).toBeInTheDocument());
    fireEvent.click(screen.getByText("＋ New rant"));
    await waitFor(() => expect(screen.getByTestId("rant-dialog")).toBeInTheDocument());
    // 项目下拉含已注册项目 emrg
    const sel = screen.getByTestId("rant-agent") as HTMLSelectElement;
    expect(Array.from(sel.options).some((o) => o.value === "emrg")).toBe(true);
    fireEvent.change(screen.getByTestId("rant-message"), { target: { value: "测试 rant" } });
    fireEvent.change(screen.getByTestId("rant-agent"), { target: { value: "emrg" } });
    fireEvent.click(screen.getByTestId("rant-submit"));
    await waitFor(() => expect(m.sendRant).toHaveBeenCalledWith({ message: "测试 rant", agent: "emrg" }));
    await waitFor(() => expect(m.listRants).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("rant-dialog")).not.toBeInTheDocument();
  });

  it("upgrade 事件 → 渲染升级横幅；重启按钮调 restartDaemon（完整重启链，修版本号死循环）；dismiss 后同版本不重现", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    // 初始无横幅
    expect(screen.queryByTestId("upgrade-banner")).not.toBeInTheDocument();
    // 心跳 upgrade 帧（installed 0.2.84 ≠ current 0.2.83）
    act(() => {
      m.emit({ type: "upgrade", data: { current_version: "0.2.83", installed_version: "0.2.84" } });
    });
    const banner = await screen.findByTestId("upgrade-banner");
    expect(banner).toBeInTheDocument();
    // from→to 文案（en 词典）
    expect(screen.getByText(/upgraded from 0\.2\.83 to 0\.2\.84/i)).toBeInTheDocument();
    // 重启按钮 → restartDaemon（rant 2026-09-01T20:09:41：完整 stop 链 + GUI relaunch，
    // 否则 daemon 内存版本不更新 → 心跳持续报版本差 → 横幅死循环）
    fireEvent.click(screen.getByTestId("upgrade-banner-restart"));
    await waitFor(() => expect(m.restartDaemon).toHaveBeenCalledTimes(1));
    // dismiss → 隐藏；同版本心跳重发不重现（vanilla lastKnownVersion 语义）
    fireEvent.click(screen.getByTestId("upgrade-banner-dismiss"));
    expect(screen.queryByTestId("upgrade-banner")).not.toBeInTheDocument();
    act(() => {
      m.emit({ type: "upgrade", data: { current_version: "0.2.83", installed_version: "0.2.84" } });
    });
    expect(screen.queryByTestId("upgrade-banner")).not.toBeInTheDocument();
  });
});

describe("Shell — 会话信息行 + /指令接线（rant 2026-09-01T20:16:55 / 20:22:00）", () => {
  afterEach(() => {
    delete (window as unknown as { emrg?: unknown }).emrg;
  });

  it("header 显示会话信息：title (sid) · agent · N msgs（对齐 TUI 状态栏）", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }, { sid: "s2", title: "Beta" }]));
    m.emit({
      type: "sessions",
      data: {
        sessions: [
          { session_id: "s1", title: "Alpha", message_count: 5 },
          { session_id: "s2", title: "Beta", message_count: 9 },
        ],
      },
    });
    await waitFor(() => expect(screen.getByTestId("session-info")).toBeInTheDocument());
    const info = screen.getByTestId("session-info");
    expect(info.textContent).toContain("Alpha");
    expect(info.textContent).toContain("(s1)");
    expect(info.textContent).toContain("5 msgs");
  });

  it("中途打开一个已在跑的会话：计时从 daemon 的快照时刻起算（rant 2026-09-27T18:41:52 requirement 2）", async () => {
    // 这一轮不是本客户端起的（另一个客户端，或者定时任务），`turn_start` 已经过去了，
    // 所以打开时只有 `resume_result` 的 `meta.turn` 带着它。计时必须显示**已经**跑了多久，
    // 而不是从 00:00 起算 —— 分钟数是确定的，秒不必断言（只在断言与渲染之间会走一两秒）。
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    const startedAt = Math.floor(Date.now() / 1000) - 305; // 5 分 5 秒前开始
    m.emit({
      type: "command_result",
      data: { type: "resume_result", session_id: "s1", meta: { turn: { running: true, started_at: startedAt } } },
      sid: "s1",
    });
    await waitFor(() =>
      expect(screen.getByTestId("session-info").textContent).toMatch(/\[05:\d\d\]/),
    );
  });

  it("停止按钮跟着会话走，而不是跟着本端发没发过请求（rant 2026-10-29T14:21:57）", async () => {
    // 这一轮不是本端发起的：定时任务的一次 tick，或者另一个客户端。本端从未调用
    // `sendMessage`，只收到 daemon 的 `turn_start` 广播。
    //
    // TUI 的 Esc 曾经只看「本端有请求在飞」（`busy`），于是这种轮在屏幕上跑着、
    // 计时在走，按 Esc 却什么都没发生，只能去开 GUI 或者杀掉 daemon。GUI 这边
    // 读的是 `busyBySid[activeSid]` —— daemon 广播的会话级事实 —— 所以本来是好的；
    // 这条测试把「本来是好的」变成「再也不能变坏」：删掉 Shell 里那一行
    // `busy = appState.busyBySid[activeSid]`，停止按钮就不会出现，本测试变红。
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    m.emit({ type: "turn_start", data: { started_at: Math.floor(Date.now() / 1000) }, sid: "s1" });

    const stop = await screen.findByTestId("composer-stop");
    expect(m.sendMessage).not.toHaveBeenCalled(); // 本端确实没发起过这一轮
    fireEvent.click(stop);
    await waitFor(() => expect(m.cancel).toHaveBeenCalledWith("s1"));
  });

  it("快照说没有轮在跑 → 会话信息行不显示计时（不得留下上一轮的时刻）", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    m.emit({ type: "turn_start", data: { started_at: Math.floor(Date.now() / 1000) - 305 }, sid: "s1" });
    await waitFor(() => expect(screen.getByTestId("session-info").textContent).toMatch(/\[05:\d\d\]/));
    m.emit({
      type: "command_result",
      data: { type: "resume_result", session_id: "s1", meta: { turn: { running: false, started_at: null } } },
      sid: "s1",
    });
    await waitFor(() =>
      expect(screen.getByTestId("session-info").textContent).not.toMatch(/\[\d\d:\d\d\]/),
    );
  });

  it("turn_end → 头部计时消失（rant 2026-09-30T09:47:11 requirement 5，与 done/cancelled 幂等）", async () => {
    // 上一支只走快照清空；这一支走**实时**关闭路径：轮结束时 daemon 广播 `turn_end`，
    // 头部 [m:ss] 必须随之消失（不得停住、不得残留）。这条与下面那条 live 起点配对，
    // 一起覆盖「起得来 + 停得掉」——中间层丢掉这两帧时它们一起变红。
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    m.emit({ type: "turn_start", data: { started_at: Math.floor(Date.now() / 1000) - 305 }, sid: "s1" });
    await waitFor(() => expect(screen.getByTestId("session-info").textContent).toMatch(/\[05:\d\d\]/));
    m.emit({ type: "turn_end", data: {}, sid: "s1" });
    await waitFor(() =>
      expect(screen.getByTestId("session-info").textContent).not.toMatch(/\[\d\d:\d\d\]/),
    );
  });

  it("/model <name> → setModel 直切（对齐 TUI）；无参 → 设置面板", async () => {    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    await typeIntoComposer("/model deepseek-chat");
    const input = await screen.findByTestId("composer-input");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await waitFor(() => expect(m.setModel).toHaveBeenCalledWith({ model: "deepseek-chat" }));
  });

  it("/trigger <name> → triggerTask 直发（对齐 TUI）", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    await typeIntoComposer("/trigger evo");
    const input = await screen.findByTestId("composer-input");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await waitFor(() => expect(m.listTasks).toHaveBeenCalled());
  });

  it("/rant @emrg hello → sendRant 直发（对齐 TUI @agent 前缀）", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    await typeIntoComposer("/rant @emrg hello world");
    const input = await screen.findByTestId("composer-input");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await waitFor(() =>
      expect(m.sendRant).toHaveBeenCalledWith({ message: "hello world", agent: "emrg" }),
    );
  });

  // ── 输入历史召回（issue #2349 part 2/2）：三件分开测过的东西在**这里**被要求接上 ──
  // bridge 有自己的测试（listInputHistory 写 store、notePromptSubmitted 追加），Composer
  // 有自己的测试（方向键怎么走），Shell 两者都不测 —— 而那几条接线恰恰是上一张 ❌ 点名的
  // 东西：删掉任意一条，其余全套仍然绿。这两条测试就是那些接线的看守。

  /** 编辑器当前文本（tiptap contenteditable 的 textContent） */
  const composerText = () => screen.getByTestId("composer-input").textContent;

  /** 在输入框上按一个键（Composer 的 handleKeyDown 挂在同一个元素上） */
  function pressInComposer(key: string) {
    screen.getByTestId("composer-input").dispatchEvent(
      new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }),
    );
  }

  /** 等 bridge 把一次 IPC 答复落进 store（答复在微任务里落地） */
  async function settle() {
    await act(async () => {
      await Promise.resolve();
    });
  }

  it("会话一打开就去 daemon 取这个会话的输入历史，取到的那份直接给方向键用（issue #2349 part 2/2）", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeInTheDocument());
    // ① 按**会话**读：列表的权威在 daemon，客户端手上没有自己的副本。
    //    这条断言红 ⇒ 真机上方向键永远空着。
    await waitFor(() => expect(m.listInputHistory).toHaveBeenCalledWith({ sessionId: "s1" }));
    await settle();
    // ② 读到的列表必须真的交到 Composer 手上（Shell → provider → prop → 方向键）：
    //    任何一环缺名，IPC 通了、store 有了，键盘上仍然什么也不发生。
    pressInComposer("ArrowUp");
    await waitFor(() => expect(composerText()).toBe("second"));
    pressInComposer("ArrowUp");
    await waitFor(() => expect(composerText()).toBe("first"));
    pressInComposer("ArrowDown");
    await waitFor(() => expect(composerText()).toBe("second"));
  });

  it("刚发出去的那一条立刻进召回列表：↑ 先撞见它，再往回才是 daemon 交来的旧条目（issue #2349 part 2/2）", async () => {
    const m = mockEmrg();
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }]));
    await waitFor(() => expect(m.listInputHistory).toHaveBeenCalledWith({ sessionId: "s1" }));
    await settle();
    await typeIntoComposer("hello there");
    pressInComposer("Enter");
    await waitFor(() => expect(m.sendMessage).toHaveBeenCalled());
    await settle();
    // ③ 发送成功后 Shell 要把这一条接进列表（onPromptSent → notePromptSubmitted）。
    //    删掉那条接线 ⇒ 这里只翻得到 "second"，也就是上一张 ❌ 描述的那个缺陷：
    //    在刚打了五句话的会话里按 ↑，列表停在会话打开那一刻。
    pressInComposer("ArrowUp");
    await waitFor(() => expect(composerText()).toBe("hello there"));
    pressInComposer("ArrowUp");
    await waitFor(() => expect(composerText()).toBe("second"));
  });

  it("发送途中切会话：这条 prompt 记进它发出去的那个会话，不进宿主切过去的会话（veto cyc20261019-023629，issue #2349）", async () => {
    // 实测形状：send `typed in s1` → 在 send 解析之前切到 s2 → 在 s2 按 ↑。
    // 若上报闭包住「渲染时 activeSid」，s2 的召回列表会凭空多出一条它的 daemon 文件里
    // 不存在的 prompt —— 正是 issue #2349 要消灭的那件事（新会话翻出上一个会话的输入）。
    const m = mockEmrg();
    let release: (v: { requestId: string }) => void = () => {};
    m.sendMessage.mockImplementation(() => new Promise<{ requestId: string }>((r) => { release = r; }));
    m.listInputHistory.mockImplementation((p: { sessionId: string }) =>
      Promise.resolve({ entries: p.sessionId === "s1" ? ["first"] : ["second"], hasMore: false }),
    );
    render(wrapper(<Shell />));
    await waitFor(() => expect(m.onEvent).toHaveBeenCalledTimes(1));
    m.emit(openSessionsFrame([{ sid: "s1", title: "Alpha" }, { sid: "s2", title: "Beta" }]));
    await waitFor(() => expect(m.listInputHistory).toHaveBeenCalledWith({ sessionId: "s1" }));
    await settle();

    await typeIntoComposer("typed in s1");
    pressInComposer("Enter");
    await waitFor(() => expect(m.sendMessage).toHaveBeenCalledTimes(1));

    // 发送还没解析就先切走：现在激活的是 s2，并已读到 s2 自己的列表 ["second"]。
    await userEvent.click(screen.getAllByTestId("open-session-item")[1]);
    await waitFor(() => expect(m.listInputHistory).toHaveBeenCalledWith({ sessionId: "s2" }));
    await settle();
    // 放行那次发送：它属于 s1，必须落在 s1 名下。
    await act(async () => {
      release({ requestId: "req-9" });
    });
    await settle();

    // s2 的列表仍是它自己的：↑ 得到 "second"，不是 "typed in s1"。
    pressInComposer("ArrowUp");
    await waitFor(() => expect(composerText()).toBe("second"));
  });
});
