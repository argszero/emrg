import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { StatusBand } from "./StatusBand";
import { I18nProvider } from "../lib/i18n";
import { STATUS_BAND_HEIGHT } from "../lib/cockpit";
// 样式表走 Vite 的 `?raw`，不走 `node:fs`（`tests/test_renderer_node_types.py`）。
import cockpitCss from "../cockpit.css?raw";

/**
 * StatusBand.test.tsx — the one row that is always on screen (issue #2959 item 1).
 *
 * One cell is a reading of the daemon (the rolling 24-hour window) and one is derived
 * from a daemon timestamp (`startedAtMs`). Both are asserted with a number *and* with
 * `null`, because "the daemon has not reported" must render as `—` rather than as zero
 * tokens — an invented reading is the defect class this cockpit exists to remove. The
 * turn counter that used to sit between them is gone (issue #3039), and the last test
 * here is the one that says so.
 *
 * The window is three segments and never a total (rant 2026-11-19T09:31:00), and each
 * segment is `null` on its own: an endpoint that never reports cache hits must show `—`
 * there while input and output still read.
 *
 * One cell is not a reading at all: the open session's name and id (issue #3089), which
 * the window title used to carry where nothing could be selected. It is asserted here as
 * *text* — a span with no control inside, a hover title, no placeholder when no session is
 * open — and its two stylesheet rules are read as text, because jsdom has no layout to
 * truncate anything in.
 */

function setup(props: Partial<Parameters<typeof StatusBand>[0]> = {}) {
  const handlers = {
    onToggleLeft: vi.fn(),
    onToggleRight: vi.fn(),
  };
  render(
    <I18nProvider lang="en">
      <StatusBand
        running={false}
        usage={null}
        leftOpen
        rightOpen
        {...handlers}
        {...props}
      />
    </I18nProvider>,
  );
  return handlers;
}

describe("StatusBand", () => {
  it("44px 高（契约里的固定行高）", () => {
    setup();
    expect(screen.getByTestId("status-band").style.height).toBe(`${STATUS_BAND_HEIGHT}px`);
  });

  it("空闲/运行两态用 daemon 的 busy 说话，而不是本端的请求", () => {
    setup({ running: false });
    expect(screen.getByTestId("band-state")).toHaveTextContent("Idle");
    expect(screen.getByTestId("band-state")).toHaveClass("idle");
  });

  it("运行中 + 有窗口：两个格子都报读数", () => {
    setup({
      running: true,
      usage: {
        input: 12_300_000, cache: 11_100_000, output: 245_000,
        sessionInput: 1_500_000, sessionCache: 1_400_000, sessionOutput: 25_000,
        windowStart: 0, windowEnd: 1,
      },
    });
    expect(screen.getByTestId("band-state")).toHaveTextContent("Running");
    expect(screen.getByTestId("band-usage")).toHaveTextContent(
      "24h this session/machine in 1.5M/12.3M · cache 1.4M/11.1M · out 25.0k/245.0k",
    );
  });

  it("一段没测定就画 —，不画 0：三段各自独立", () => {
    setup({
      running: true,
      usage: { input: 1000, cache: null, output: 5, windowStart: 0, windowEnd: 1 },
    });
    expect(screen.getByTestId("band-usage")).toHaveTextContent(
      "24h this session/machine in —/1.0k · cache —/— · out —/5",
    );
  });

  it("issue #3072：本会话在前、本机在后，一格两个数", () => {
    // 宿主读的是两个问题：「这次对话花了多少」和「这台机器花了多少」。一格只给一个数，
    // 两个问题都答不了，而只换个说法、仍然只回本机那个数，正是这条要挡的改动。
    setup({
      running: true,
      usage: {
        input: 262_600_000, cache: 241_000_000, output: 4_100_000,
        sessionInput: 15_200_000, sessionCache: 14_100_000, sessionOutput: 245_000,
        windowStart: 0, windowEnd: 1,
      },
    });
    expect(screen.getByTestId("band-usage")).toHaveTextContent(
      "24h this session/machine in 15.2M/262.6M · cache 14.1M/241.0M · out 245.0k/4.1M",
    );
  });

  it("issue #3072：没测到本会话那一段就画 —，绝不拿本机数顶上", () => {
    // 本机那个数顶上去，等于替一个会话报出整台机器的花销。
    setup({
      running: true,
      usage: {
        input: 100, cache: 40, output: 7,
        sessionInput: 5, sessionCache: null, sessionOutput: 7,
        windowStart: 0, windowEnd: 1,
      },
    });
    const band = screen.getByTestId("band-usage");
    expect(band).toHaveTextContent("24h this session/machine in 5/100 · cache —/40 · out 7/7");
    expect(band.textContent).not.toContain("40/40");
  });

  it("issue #3072：悬停文字说出顺序，并保留「缓存是输入子集、不显示合计」", () => {
    setup({ running: true });
    const title = screen.getByTestId("band-usage").getAttribute("title") ?? "";
    expect(title).toContain("this session first and the machine second");
    expect(title).toContain("subset of input");
    expect(title).toContain("no total");
  });

  it("状态带不再说「累计」：窗口是三个独立段，不是一个当轮总数", () => {
    // rant 2026-11-19T09:31:00 的中文口径：宿主看到的必须是最近 24 小时，而不是「本轮累计」。
    render(
      <I18nProvider lang="zh">
        <StatusBand
          running
          usage={{
            input: 1000, cache: 900, output: 10,
            sessionInput: 500, sessionCache: 450, sessionOutput: 10,
            windowStart: 0, windowEnd: 1,
          }}
          leftOpen
          rightOpen
          onToggleLeft={vi.fn()}
          onToggleRight={vi.fn()}
        />
      </I18nProvider>,
    );
    expect(screen.getByTestId("status-band").textContent).not.toContain("累计");
    expect(screen.getByTestId("band-usage")).toHaveTextContent(
      "24h 本会话/本机 输入 500/1.0k · 缓存 450/900 · 输出 10/10",
    );
  });

  it("daemon 还没报 → 画 —，不画 0 token", () => {
    setup({ running: true, usage: null });
    expect(screen.getByTestId("band-usage")).toHaveTextContent(
      "24h this session/machine in —/— · cache —/— · out —/—",
    );
  });

  it("计时来自 daemon 的轮开始时刻：中途打开也显示已过去的时间", () => {
    const startedAt = Date.now() - 305_000; // 5 分 5 秒前
    setup({ running: true, startedAtMs: startedAt });
    expect(screen.getByTestId("band-elapsed").textContent).toMatch(/\[5:0\d\]/);
  });

  it("没有轮在跑 → 计时格子是 —（不留上一轮的时刻）", () => {
    setup({ running: false, startedAtMs: undefined });
    expect(screen.getByTestId("band-elapsed")).toHaveTextContent("—");
  });

  it("只有三项读数与两个栏开关：模型与消息条数不在这行里（issue #2969、#3089）", () => {
    setup({
      running: true,
      usage: { input: 3000, cache: 2000, output: 400, windowStart: 0, windowEnd: 1 },
      startedAtMs: Date.now() - 65_000,
      sessionId: "s1",
      sessionName: "Alpha",
    });
    const band = screen.getByTestId("status-band");
    // 三个变的读数：状态、计时、最近 24 小时。轮数那一格连同它右边的分隔符一起没了
    // （issue #3039）——这一行已经说了在跑、跑了多久，轮数没有第二个用处。
    //
    // 会话名与 id 不是第四个读数（issue #3089）：它不随时间变，它是**能拖走、能复制**
    // 的那段文字，所以它不占 `.band-item`，这一行的读数仍然只有三个。
    expect(band.querySelectorAll(".band-state, .band-item")).toHaveLength(3);
    expect(band.querySelectorAll(".band-session")).toHaveLength(1);
    // ……两个按钮，一个不多：搜索与新建会话的入口在会话栏（issue #3028）
    expect(band.querySelectorAll("button")).toHaveLength(2);
    // 一个不动的值出现在这一行，就是把状态带变回了顶栏：这两样仍是宿主点名移出的
    for (const gone of ["session-info", "conn-status", "conn-vision"]) {
      expect(screen.queryByTestId(gone)).not.toBeInTheDocument();
    }
  });

  it("轮数那一格端到端消失：没有 band-round，也没有它的文案（issue #3039）", () => {
    render(
      <I18nProvider lang="zh">
        <StatusBand
          running
          startedAtMs={Date.now()}
          usage={null}
          leftOpen
          rightOpen
          onToggleLeft={vi.fn()}
          onToggleRight={vi.fn()}
        />
      </I18nProvider>,
    );
    // 格子不在、dom 里找不到它的 testid
    expect(screen.queryByTestId("band-round")).toBeNull();
    // 文案也不在：中文界面里这一行再没有一个「轮」字（中英两条键都删了）
    const band = screen.getByTestId("status-band");
    expect(band.textContent).not.toContain("轮");
    expect(band.textContent).not.toContain("round");
  });

  it("两个按钮：两个栏开关，各自回自己的回调；搜索与新建不在状态带上（issue #3028）", () => {
    const h = setup({ leftOpen: true, rightOpen: false });
    fireEvent.click(screen.getByTestId("band-toggle-left"));
    fireEvent.click(screen.getByTestId("band-toggle-right"));
    // 一个动作只留一个入口：这两个在会话栏表头，状态带上删掉而不是藏起来
    expect(screen.queryByTestId("band-search")).toBeNull();
    expect(screen.queryByTestId("band-new")).toBeNull();
    expect(h.onToggleLeft).toHaveBeenCalledTimes(1);
    expect(h.onToggleRight).toHaveBeenCalledTimes(1);
    // 开着的栏按钮带 .on，关着的不带——一眼能看出哪一栏在屏幕上
    expect(screen.getByTestId("band-toggle-left")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("band-toggle-right")).toHaveAttribute("aria-pressed", "false");
  });
});

// ── 状态带上的当前会话（rant 2026-11-20T18:09:49 / issue #3089）───────────────

/**
 * 标题栏是系统画的：里面的会话名与 id 拖不动、复制不出来，而 id 恰恰是最要复制的东西
 * （贴进 issue、贴进日志、告诉另一个 agent 说的是哪个会话）。这一格把它挪成普通文字。
 *
 * 逻辑断言在这一层；「窄窗先截名字、id 一个字都不截」只能读样式表——jsdom 没有排版，
 * 拿不到真实的截断结果，读声明是唯一能变红的读法。
 */

/** 从样式表里取出一个选择器的声明块。取不到就断言失败，绝不让空串把断言放过去。 */
function cssRule(selector: string): string {
  const start = cockpitCss.indexOf(`\n${selector} {`);
  expect(start).toBeGreaterThanOrEqual(0);
  const end = cockpitCss.indexOf("}", start);
  const body = cockpitCss.slice(start, end);
  expect(body.length).toBeGreaterThan(20);
  return body;
}

describe("状态带上的当前会话（issue #3089）", () => {
  it("名字在前、id 在后，都是普通文字节点", () => {
    setup({ sessionId: "s_260727_1103_866d", sessionName: "emrg-main" });
    expect(screen.getByTestId("band-session").tagName).toBe("SPAN");
    const name = screen.getByTestId("band-session-name");
    const id = screen.getByTestId("band-session-id");
    expect(name).toHaveTextContent("emrg-main");
    expect(id).toHaveTextContent("s_260727_1103_866d");
    // 读的顺序：先「这是哪个会话」，再「它的号是多少」
    expect(name.compareDocumentPosition(id) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("这一格不是控件：里面没有按钮/链接/输入框，点一下也不触发任何回调", () => {
    const h = setup({ sessionId: "s1", sessionName: "Alpha" });
    const cell = screen.getByTestId("band-session");
    expect(
      cell.querySelectorAll("button, a, input, select, textarea, [role='button'], [tabindex]"),
    ).toHaveLength(0);
    fireEvent.click(cell);
    fireEvent.contextMenu(cell);
    expect(h.onToggleLeft).not.toHaveBeenCalled();
    expect(h.onToggleRight).not.toHaveBeenCalled();
  });

  it("会话没有名字时只显示 id：不画空括号，也不留一个孤立的分隔符", () => {
    const { unmount } = render(
      <I18nProvider lang="en">
        <StatusBand
          running={false}
          usage={null}
          sessionId="s_1"
          sessionName=""
          leftOpen
          rightOpen
          onToggleLeft={vi.fn()}
          onToggleRight={vi.fn()}
        />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("band-session-name")).toBeNull();
    expect(screen.getByTestId("band-session").textContent).toBe("s_1");
    unmount();
  });

  it("没有打开任何会话：整格不渲染，也不拿 — 占位", () => {
    // 占位符会读成「有个会话，只是号读不出来」。判据不是「找不到 band-session」，而是
    // 这一行的**结构**一个节点都不多：多出来的那个 —— 无论挂在哪个节点上、写的是 — 还是
    // 空串 —— 都会让它变红。
    //
    // 比的是 innerHTML 不是 textContent：两次渲染都没有会话，所以「无会话时多画一个占位」
    // 这个变异在两边都画了同一个占位，比文本会被它躲过去（实测：只比 textContent 时这个
    // 变异没被抓住）。结构之比才分得出「多了一个节点」。
    const props = {
      running: true,
      usage: null,
      leftOpen: true,
      rightOpen: true,
      onToggleLeft: vi.fn(),
      onToggleRight: vi.fn(),
    };
    const bare = render(
      <I18nProvider lang="en">
        <StatusBand {...props} />
      </I18nProvider>,
    );
    const withoutSessionProps = bare.container.innerHTML;
    bare.unmount();
    const withNull = render(
      <I18nProvider lang="en">
        <StatusBand {...props} sessionId={null} sessionName="Alpha" />
      </I18nProvider>,
    );
    expect(withNull.container.innerHTML).toBe(withoutSessionProps);
    expect(screen.queryByTestId("band-session")).toBeNull();
    // 三个读数、两个开关按钮——没有第四个格子
    const band = screen.getByTestId("status-band");
    expect(band.querySelectorAll(".band-state, .band-item")).toHaveLength(3);
    expect(band.querySelectorAll("button")).toHaveLength(2);
  });

  it("悬停提示只有一条：「当前会话」/ Current session", () => {
    setup({ sessionId: "s1", sessionName: "Alpha" });
    expect(screen.getByTestId("band-session").getAttribute("title")).toBe("Current session");
    render(
      <I18nProvider lang="zh">
        <StatusBand
          running={false}
          usage={null}
          sessionId="s1"
          sessionName="Alpha"
          leftOpen
          rightOpen
          onToggleLeft={vi.fn()}
          onToggleRight={vi.fn()}
        />
      </I18nProvider>,
    );
    // 两次渲染各一格：中文那条挂在第二次那一格上
    const cells = screen.getAllByTestId("band-session");
    expect(cells[cells.length - 1].getAttribute("title")).toBe("当前会话");
  });

  it("改名/换会话后这一格跟着换，不会多出第二格", () => {
    const props = {
      running: false,
      usage: null,
      leftOpen: true,
      rightOpen: true,
      onToggleLeft: vi.fn(),
      onToggleRight: vi.fn(),
    };
    const { rerender } = render(
      <I18nProvider lang="en">
        <StatusBand {...props} sessionId="s1" sessionName="Alpha" />
      </I18nProvider>,
    );
    expect(screen.getByTestId("band-session-name")).toHaveTextContent("Alpha");
    rerender(
      <I18nProvider lang="en">
        <StatusBand {...props} sessionId="s2" sessionName="Beta" />
      </I18nProvider>,
    );
    expect(screen.getAllByTestId("band-session")).toHaveLength(1);
    expect(screen.getByTestId("band-session-name")).toHaveTextContent("Beta");
    expect(screen.getByTestId("band-session-id")).toHaveTextContent("s2");
    expect(screen.queryByText("Alpha")).toBeNull();
    expect(screen.queryByText("s1")).toBeNull();
  });

  it("窄窗先短名字（末尾省略号），id 一个字都不截", () => {
    const name = cssRule(".band-session-name");
    expect(name).toMatch(/text-overflow:\s*ellipsis/);
    expect(name).toMatch(/overflow:\s*hidden/);
    expect(name).toMatch(/min-width:\s*0/);
    // id 那一半：不缩、不裁。缩了或裁了就是另一个 id
    const id = cssRule(".band-session-id");
    expect(id).toMatch(/flex:\s*none/);
    expect(id).not.toMatch(/text-overflow/);
    expect(id).not.toMatch(/overflow/);
    expect(id).not.toMatch(/min-width:\s*0/);
    // 等宽：id 是要一个字一个字对着看的
    expect(id).toMatch(/font-family:\s*var\(--font-mono/);
  });

  it("样式链上没有一条规则把这一格变成不可选：选中就是它存在的理由", () => {
    expect(cssRule(".band-session")).toMatch(/user-select:\s*text/);
    for (const selector of [".band-session", ".band-session-name", ".band-session-id", ".status-band"]) {
      expect(cssRule(selector)).not.toMatch(/user-select:\s*none/);
    }
  });
});
