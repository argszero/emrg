import { describe, expect, it, vi } from "vitest";
import { createTranscriptStore, formatToolArguments, type AssistantEntry, type TranscriptEntry, type TranscriptStore } from "./transcript";
import { replayHistoryRecords, replayHistoryRecordsPrepend, toolCallIntent, type HistoryRecord } from "./historyReplay";
import { t } from "./i18n";

/**
 * historyReplay.test.ts — 回放等价性测试（rant 2026-09-20T18:58:44 的验收）。
 *
 * 同一回合喂两路：一路是**实时帧**（按 daemon 的发送顺序调实时 handler），一路是
 * **对应落盘记录**（list_history include_records 的样子）经 `replayHistoryRecords`
 * 回放。两路产出的条目数组必须逐项相等（顺序、角色、文本、工具行、合并组）。
 *
 * 变异验证（本文件必须变红的方向，人工执行）：
 *  - 把回放里的 `content` 换回 `preview`（截断）→ 长正文那条断言变红；
 *  - 去掉工具记录的配对（照落盘顺序 start/start/end/end）→ 合并组那条断言变红；
 *  - 回放里不传 `arguments`（只抽 intent，即 #1787 修复前的样子）→ 入参一致性那条变红。
 */

const SID = "s1";

/**
 * 条目数组的逐项比较值：去掉 `rid`——它是**流的标识**，实时那一路由 daemon 的
 * `request_id` 给出、回放那一路由 record_index 合成，两者本就不必相同（也不该相同：
 * 回放取不到当初那个 request_id）。除此之外每个字段都比——正是 rant 列的顺序、角色、
 * 文本、工具行（callId/name/status/intent/content）、合并组（rows/summary/收起态）。
 *
 * `isOwn` 曾在这里被一并剥掉，因为它是「这条消息是不是本连接发的」——那条判定整个
 * 没有了（rant 2026-09-29T15:52:49 要求 3），条目形状里不再有可剥的东西。
 */
function comparable(entries: TranscriptEntry[]): unknown {
  return JSON.parse(JSON.stringify(entries, (k, v) => (k === "rid" ? undefined : v)));
}

/** 一条工具调用在两条来路上的公共来源：`args` 会以对象进实时帧、以 JSON 字符串进记录。 */
type FixtureTool = {
  id: string;
  name: string;
  intent: string;
  output: string;
  error?: boolean;
  /** 工具入参（除 intent 外的字段也要覆盖，否则这条等价性只验到 intent） */
  args?: Record<string, unknown>;
};

/** 实时帧里的 `arguments`（daemon 已解析）——与记录里的 JSON 字符串同源。 */
const liveArgs = (tool: FixtureTool): Record<string, unknown> =>
  tool.args ?? { intent: tool.intent };

/** 实时那一路：与 daemon 的发送顺序一致（一轮一个 request_id，done 收尾）。 */
function feedLive(
  store: TranscriptStore,
  sid: string,
  turn: {
    user: string;
    rounds: Array<{ text: string; tools: FixtureTool[] }>;
  },
): void {
  const rid = `req-${turn.user}`;
  store.addUserMessage(turn.user, sid);
  for (const round of turn.rounds) {
    if (round.text) store.handleDelta([{ request_id: rid, content: round.text }], sid);
    for (const tool of round.tools) {
      store.handleToolStart(
        {
          request_id: rid,
          tool_call_id: tool.id,
          tool_name: tool.name,
          intent: tool.intent,
          arguments: liveArgs(tool),
        },
        sid,
      );
      store.handleToolEnd({
        tool_call_id: tool.id,
        tool_name: tool.name,
        content: tool.output,
        error: tool.error ?? false,
      }, sid);
    }
  }
  store.handleDone({ request_id: rid }, sid);
}

/** 落盘那一路：与 daemon 写入 history.jsonl 的记录一致（助手记录带全部 tool_calls，随后才是结果）。 */
function toRecords(
  turn: {
    user: string;
    rounds: Array<{ text: string; tools: FixtureTool[] }>;
  },
  startIndex = 0,
): HistoryRecord[] {
  const records: HistoryRecord[] = [{ record_index: startIndex, kind: "message", role: "user", content: turn.user }];
  let i = startIndex + 1;
  for (const round of turn.rounds) {
    if (round.text || round.tools.length) {
      records.push({
        record_index: i++,
        kind: "message",
        role: "assistant",
        content: round.text,
        ...(round.tools.length
          ? {
              tool_calls: round.tools.map((tool) => ({
                id: tool.id,
                function: { name: tool.name, arguments: JSON.stringify(liveArgs(tool)) },
              })),
            }
          : {}),
      });
    }
    for (const tool of round.tools) {
      records.push({
        record_index: i++,
        kind: "tool_result",
        tool_call_id: tool.id,
        tool_name: tool.name,
        content: tool.output,
        error: tool.error ?? false,
      });
    }
  }
  return records;
}

const TURN = {
  user: "看看这个仓库",
  rounds: [
    {
      text: "先看看目录。",
      tools: [
        // c1 带真实入参（command 等），不只 intent —— 否则「入参一致」这条只验到 intent。
        { id: "c1", name: "bash", intent: "list the files", output: "a\nb\n", args: { command: "ls -a", intent: "list the files" } },
        { id: "c2", name: "read", intent: "read README", output: "# emrg\n" },
      ],
    },
    {
      text: "再读一个文件。",
      tools: [{ id: "c3", name: "grep", intent: "find the caller", output: "x.py:12\n" }],
    },
    { text: "结论：一切正常。", tools: [] },
  ],
};

describe("replayHistoryRecords is equivalent to the live stream", () => {
  it("produces the same entries item by item (order, role, text, tool rows, merged groups)", () => {
    const live = createTranscriptStore();
    feedLive(live, SID, TURN);

    const replay = createTranscriptStore();
    replayHistoryRecords(toRecords(TURN), SID, replay);

    expect(comparable(replay.getEntries(SID))).toEqual(comparable(live.getEntries(SID)));
  });

  it("renders the full assistant text, never a truncated preview (the measured 5064-char message)", () => {
    const long = `head ${"x".repeat(5064)} tail`;
    const turn = { user: "long", rounds: [{ text: long, tools: [] }] };
    const replay = createTranscriptStore();
    replayHistoryRecords(toRecords(turn), SID, replay);

    const entries = replay.getEntries(SID);
    const assistant = entries.filter((e) => e.kind === "assistant");
    expect(assistant).toHaveLength(1);
    const seg = (assistant[0] as { segments: Array<{ text: string }> }).segments[0];
    expect(seg.text).toBe(long);
    expect(seg.text).toHaveLength(5064 + "head ".length + " tail".length);
  });

  it("merges a round's consecutive tools into one group, as the live start/end/start/end order does", () => {
    const replay = createTranscriptStore();
    replayHistoryRecords(toRecords(TURN), SID, replay);

    const kinds = replay.getEntries(SID).map((e) => e.kind);
    // 用户 → 助手 → 工具组(c1+c2) → 助手 → 工具行(c3) → 助手
    expect(kinds).toEqual(["user", "assistant", "tool-group", "assistant", "tool-row", "assistant"]);
    const group = replay.getEntries(SID)[2] as { group: { rows: unknown[]; summary: { count: number } | null } };
    expect(group.group.rows).toHaveLength(2);
    expect(group.group.summary).toEqual({ count: 2, totalElapsed: 0 });
  });

  it("carries each tool row's intent out of tool_calls[].function.arguments", () => {
    const replay = createTranscriptStore();
    replayHistoryRecords(toRecords(TURN), SID, replay);
    const group = replay.getEntries(SID)[2] as { group: { rows: Array<{ intent?: string }> } };
    expect(group.group.rows.map((r) => r.intent)).toEqual(["list the files", "read README"]);
  });

  /** 所有工具行的 callLabel，按出现顺序——不假设是否已并进 group（取决于结果是否先到）。 */
  function rowsWithLabel(replay: ReturnType<typeof createTranscriptStore>): Array<string | undefined> {
    const out: Array<string | undefined> = [];
    for (const entry of replay.getEntries(SID)) {
      if (entry.kind === "tool-row") out.push((entry as { row: { callLabel?: string } }).row.callLabel);
      if (entry.kind === "tool-group") {
        for (const row of (entry as { group: { rows: Array<{ callLabel?: string }> } }).group.rows) {
          out.push(row.callLabel);
        }
      }
    }
    return out;
  }

  it("carries the daemon's batch label out of tool_calls[].call_label (rant 2026-10-20T12:22:21)", () => {
    // daemon 的历史投影在每一项 tool_calls 上算好 call_label；回放只透传，不自己数。
    const records: HistoryRecord[] = [
      { record_index: 0, kind: "message", role: "user", content: "q" },
      {
        record_index: 1,
        kind: "message",
        role: "assistant",
        content: "",
        tool_calls: [
          { id: "c1", function: { name: "bash", arguments: '{"command":"ls"}' }, call_label: "(1/2) " },
          { id: "c2", function: { name: "read", arguments: '{"path":"a"}' }, call_label: "(2/2) " },
        ],
      },
    ];
    const replay = createTranscriptStore();
    replayHistoryRecords(records, SID, replay);
    expect(rowsWithLabel(replay)).toEqual(["(1/2) ", "(2/2) "]);
  });

  it("leaves callLabel undefined when the record carries none — the client never counts", () => {
    const records: HistoryRecord[] = [
      { record_index: 0, kind: "message", role: "user", content: "q" },
      {
        record_index: 1,
        kind: "message",
        role: "assistant",
        content: "",
        tool_calls: [
          { id: "c1", function: { name: "bash", arguments: '{"command":"ls"}' } },
          { id: "c2", function: { name: "read", arguments: '{"path":"a"}' } },
        ],
      },
    ];
    const replay = createTranscriptStore();
    replayHistoryRecords(records, SID, replay);
    // 两条调用、没有字段 ⇒ 两条都不许冒出 (1/2)：客户端不自己算
    expect(rowsWithLabel(replay)).toEqual([undefined, undefined]);
  });

  it("does not invent an empty assistant bubble for a tool-only round", () => {
    const live = createTranscriptStore();
    feedLive(live, SID, { user: "q", rounds: [{ text: "", tools: [{ id: "c1", name: "bash", intent: "go", output: "ok" }] }, { text: "done", tools: [] }] });
    const replay = createTranscriptStore();
    replayHistoryRecords(toRecords({ user: "q", rounds: [{ text: "", tools: [{ id: "c1", name: "bash", intent: "go", output: "ok" }] }, { text: "done", tools: [] }] }), SID, replay);
    expect(comparable(replay.getEntries(SID))).toEqual(comparable(live.getEntries(SID)));
    expect(replay.getEntries(SID).map((e) => e.kind)).toEqual(["user", "tool-row", "assistant"]);
  });

  it("seals the turn's text segment at the turn's end (typing stops, like the done frame)", () => {
    const replay = createTranscriptStore();
    replayHistoryRecords(toRecords(TURN), SID, replay);
    for (const e of replay.getEntries(SID)) {
      if (e.kind !== "assistant") continue;
      for (const seg of (e as { segments: Array<{ typing: boolean }> }).segments) {
        expect(seg.typing).toBe(false);
      }
    }
  });

  it("keeps a result whose assistant record is missing (an unpaired tool_result is still delivered)", () => {
    const records: HistoryRecord[] = [
      { record_index: 0, kind: "message", role: "user", content: "q" },
      { record_index: 1, kind: "tool_result", tool_call_id: "orphan", tool_name: "bash", content: "out" },
    ];
    const replay = createTranscriptStore();
    replayHistoryRecords(records, SID, replay);
    // 实时那一路同样会播它的 tool_end（找不到行 = 无操作），所以两路都只留用户条目。
    expect(replay.getEntries(SID).map((e) => e.kind)).toEqual(["user"]);
  });

  it("records a failed tool as a failed row, exactly as the live tool_end does", () => {
    const turn = { user: "q", rounds: [{ text: "try", tools: [{ id: "c1", name: "bash", intent: "go", output: "boom", error: true }] }] };
    const live = createTranscriptStore();
    feedLive(live, SID, turn);
    const replay = createTranscriptStore();
    replayHistoryRecords(toRecords(turn), SID, replay);
    expect(comparable(replay.getEntries(SID))).toEqual(comparable(live.getEntries(SID)));
    const row = replay.getEntries(SID)[2] as { row: { status: string } };
    expect(row.row.status).toBe("failed");
  });
});

describe("replayHistoryRecordsPrepend", () => {
  it("puts an older page's entries in front, in order", () => {
    const store = createTranscriptStore();
    replayHistoryRecords(toRecords(TURN, 10), SID, store);
    const newer = store.getEntries(SID).length;
    replayHistoryRecordsPrepend(toRecords({ user: "更早的一轮", rounds: [{ text: "早", tools: [] }] }, 0), SID, store);
    const entries = store.getEntries(SID);
    expect(entries).toHaveLength(newer + 2);
    expect(entries[0]).toMatchObject({ kind: "user", text: "更早的一轮" });
    expect(entries[1]).toMatchObject({ kind: "assistant" });
    // 整块前插而不移位，会让后面那段的条目下标偏移——群组/工具行索引必须跟着挪。
    expect(entries[2]).toMatchObject({ kind: "user", text: TURN.user });
  });

  it("keeps a live tool row addressable after an older page is prepended (index shift)", () => {
    const store = createTranscriptStore();
    store.addUserMessage("q", SID);
    store.handleToolStart({ request_id: "r1", tool_call_id: "late", tool_name: "bash", intent: "still running" }, SID);
    replayHistoryRecordsPrepend(toRecords({ user: "older", rounds: [{ text: "old", tools: [] }] }, 0), SID, store);
    // 前插之后，晚到的 tool_end 必须仍然命中它自己的那一行。
    store.handleToolEnd({ tool_call_id: "late", tool_name: "bash", content: "done", error: false }, SID);
    const rows: Array<{ row: { callId: string; status: string } }> = [];
    for (const e of store.getEntries(SID)) {
      if (e.kind === "tool-row") rows.push(e as { row: { callId: string; status: string } });
    }
    expect(rows).toHaveLength(1);
    expect(rows[0].row).toMatchObject({ callId: "late", status: "done" });
  });

  it("is a no-op for an empty page (nothing to prepend)", () => {
    const store = createTranscriptStore();
    store.addUserMessage("q", SID);
    expect(replayHistoryRecordsPrepend([], SID, store)).toBe(0);
    expect(store.getEntries(SID)).toHaveLength(1);
  });
});

describe("toolCallIntent", () => {
  it("reads the intent the daemon logs", () => {
    expect(toolCallIntent('{"path":"x","intent":"read it"}')).toBe("read it");
  });

  it("returns empty for a missing or unparsable payload, never throwing", () => {
    expect(toolCallIntent(undefined)).toBe("");
    expect(toolCallIntent("")).toBe("");
    expect(toolCallIntent('{"path":"x"')).toBe("");
    expect(toolCallIntent('{"intent":42}')).toBe("");
  });

  it("does not throw on a whole page of broken arguments", () => {
    const records: HistoryRecord[] = [
      { record_index: 0, kind: "message", role: "assistant", content: "x", tool_calls: [{ id: "c1", function: { name: "bash", arguments: "{oops" } }] },
      { record_index: 1, kind: "tool_result", tool_call_id: "c1", tool_name: "bash", content: "out" },
    ];
    const replay = createTranscriptStore();
    expect(() => replayHistoryRecords(records, SID, replay)).not.toThrow();
    const row = replay.getEntries(SID).find((e) => e.kind === "tool-row") as { row: { intent?: string; status: string } };
    // 实时那一路同样发 intent: ""（daemon 的 `args.get("intent") or ""`），所以这里是
    // 空串而不是 undefined——两路一致，且不因一条坏参数整页失败。
    expect(row.row.intent).toBe("");
    expect(row.row.status).toBe("done");
  });
});

describe("the record shape the daemon actually sends", () => {
  it("accepts a page with no record_index at all (defensive: rid falls back)", () => {
    const sink = {
      addUserMessage: vi.fn(),
      handleDelta: vi.fn(),
      handleDone: vi.fn(),
      handleToolStart: vi.fn(),
      handleToolEnd: vi.fn(),
      addSystemMessage: vi.fn(),
    };
    replayHistoryRecords(
      [
        { kind: "message", role: "user", content: "q" },
        { kind: "message", role: "assistant", content: "a" },
      ],
      SID,
      sink,
    );
    // 第三个参数是「这一行是谁、什么时候」：id 由 record_index 合成（本页没有 record_index，
    // 于是落在 `hist-u`），记录没带时刻就不写这个键（rant 2026-10-17T10:03:08）。
    expect(sink.addUserMessage).toHaveBeenCalledWith("q", SID, { requestId: "hist-u" });
    expect(sink.handleDelta).toHaveBeenCalledWith([{ request_id: "hist-u", content: "a" }], SID);
    expect(sink.handleDone).toHaveBeenCalledWith({ request_id: "hist-u" }, SID);
  });
});

/**
 * #2351：回放里的系统提示与压缩边界。
 *
 * daemon 的 records 模式现在也投影两条既不是消息（显示路径投影的两种角色）也不是工具结果的
 * 记录：`Session.clear` 写下的 `role: "system"` 消息，和每次压缩写下的 `type: "summary"`。
 * 实时那一路两条都有对应的行（`/clear` 之后的系统提示、`compact_result` 之后的已压缩提示），
 * 所以回放必须调同一个入口写同一句话 —— 否则重开一个会话看到的就不是自己关掉的那一屏。
 *
 * 这三条是回放那一半唯一的钉：删掉 `replayHistoryRecords` 里那两个分支，此前整套渲染层
 * 测试仍然全绿（实测：686 条不变），于是两个客户端的一致性可以静默退回去。
 */
describe("系统提示与压缩边界的回放（#2351）", () => {
  const records: HistoryRecord[] = [
    { record_index: 0, kind: "message", role: "system", content: "[Session cleared]" },
    { record_index: 1, kind: "summary", content: "the earlier turns, summarised", compacted_message_count: 7 },
    { record_index: 2, kind: "message", role: "user", content: "after the compact" },
  ];

  function sinkOf() {
    return {
      addUserMessage: vi.fn(),
      handleDelta: vi.fn(),
      handleDone: vi.fn(),
      handleToolStart: vi.fn(),
      handleToolEnd: vi.fn(),
      addSystemMessage: vi.fn(),
    };
  }

  it("写一行系统提示，用的就是记录自己的文本（不翻译、不改写）", () => {
    const s = sinkOf();
    replayHistoryRecords(records, SID, s);
    // 第一条系统行：`Session.clear` 的那句话。实时那一路显示的也是它 —— 回放换一个说法，
    // 两条路就又分叉了，而这正是 #2351 要消掉的东西。
    expect(s.addSystemMessage).toHaveBeenNthCalledWith(1, "[Session cleared]", SID);
  });

  it("压缩边界写的是实时那一行的同一句话（同一个键，不是第二套文案）", () => {
    const s = sinkOf();
    replayHistoryRecords(records, SID, s);
    // `DialogHost` 在 `compact_result` 上写的是 `t("app.compacted")`；回放调同一个入口、
    // 同一个键，于是两路是同一行。断言精确相等：换成「已压缩 N 条」这种自造文案会变红。
    expect(s.addSystemMessage).toHaveBeenNthCalledWith(2, t("app.compacted"), SID);
    expect(s.addSystemMessage).toHaveBeenCalledTimes(2);
  });

  it("边界两侧的顺序与落盘一致：系统行在前，压缩行在中间，用户消息在最后", () => {
    const s = sinkOf();
    replayHistoryRecords(records, SID, s);
    const order = [
      ...s.addSystemMessage.mock.calls.map((c) => `sys:${c[0]}`),
      ...s.addUserMessage.mock.calls.map((c) => `user:${c[0]}`),
    ];
    expect(order).toEqual([
      "sys:[Session cleared]",
      `sys:${t("app.compacted")}`,
      "user:after the compact",
    ]);
  });
});

/**
 * rant 2026-09-30T09:17:54 → #1787（Part 2/2）：回放此前只从 `function.arguments` 里抽走
 * intent，把剩下的丢掉 —— 于是「打开旧会话」看到的工具行与实时不一致。这一组钉住
 * 「透传」与「两路呈现相同」。
 */
describe("工具入参的回放一致性（#1787）", () => {
  const RECORDS: HistoryRecord[] = [
    {
      record_index: 0,
      kind: "message",
      role: "assistant",
      content: "",
      tool_calls: [
        { id: "c1", function: { name: "bash", arguments: '{"command":"ls -la","intent":"check"}' } },
      ],
    },
    { record_index: 1, kind: "tool_result", tool_call_id: "c1", tool_name: "bash", content: "out" },
  ];

  it("把记录里的 arguments 归一成实时那一形状后交给 handleToolStart（不再只抽 intent）", () => {
    const sink = {
      addUserMessage: vi.fn(),
      handleDelta: vi.fn(),
      handleDone: vi.fn(),
      handleToolStart: vi.fn(),
      handleToolEnd: vi.fn(),
      addSystemMessage: vi.fn(),
    };
    replayHistoryRecords(RECORDS, SID, sink);
    expect(sink.handleToolStart).toHaveBeenCalledWith(
      expect.objectContaining({
        tool_call_id: "c1",
        tool_name: "bash",
        // daemon 的 tool_start.arguments 是解析好的对象，回放必须给同一种类型
        arguments: { command: "ls -la", intent: "check" },
      }),
      SID,
    );
  });

  it("坏载荷（截断/非 JSON）与实时同落 {}，不抛异常", () => {
    const broken: HistoryRecord[] = [
      {
        record_index: 0,
        kind: "message",
        role: "assistant",
        content: "",
        tool_calls: [{ id: "c1", function: { name: "bash", arguments: '{"command":"ls' } }],
      },
      { record_index: 1, kind: "tool_result", tool_call_id: "c1", tool_name: "bash", content: "out" },
    ];
    const store = createTranscriptStore();
    expect(() => replayHistoryRecords(broken, SID, store)).not.toThrow();
    const row = store.getEntries(SID).find((e) => e.kind === "tool-row") as { row: { arguments?: unknown } };
    // daemon 对同一串也是 json.loads 失败 → {}，回放给同一个结果（实时那一路同样不渲染输入段）
    expect(row.row.arguments).toEqual({});
    expect(formatToolArguments(row.row.arguments)).toBe("");
  });

  it("同一份参数：回放建出的行与实时建出的行给出同一段输入文本", () => {
    const live = createTranscriptStore();
    live.handleToolStart(
      {
        request_id: "r1",
        tool_call_id: "c1",
        tool_name: "bash",
        intent: "check",
        arguments: { command: "ls -la", intent: "check" }, // 实时这一路：daemon 已解析
      },
      SID,
    );
    const replayed = createTranscriptStore();
    replayHistoryRecords(RECORDS, SID, replayed);

    const rowOf = (s: TranscriptStore) =>
      (s.getEntries(SID).find((e) => e.kind === "tool-row") as { row: { arguments?: unknown } }).row;
    // 两路存的原生类型不同（对象 / JSON 字符串），经同一个 formatToolArguments 后必须逐字相同
    expect(formatToolArguments(rowOf(replayed).arguments)).toBe(
      formatToolArguments(rowOf(live).arguments),
    );
    expect(formatToolArguments(rowOf(live).arguments)).toContain('"command": "ls -la"');
  });
});

/**
 * 时刻在回放这一路的去向（rant 2026-10-17T10:03:08 → issue #2340）：`timestamp` 曾经
 * 「类型上有、映射处被丢掉」，于是重开一个会话永远看不到时间。这里断言两件事：
 * 记录里的时刻确实落到了条目上，且与实时那一轮落下的是**同一个值**。
 */
describe("时刻：回放与实时同值", () => {
  const USER_STAMP = "2026-10-17T10:27:55.123456+08:00";
  const REPLY_STAMP = "2026-10-17T10:28:09.987654+08:00";
  const records: HistoryRecord[] = [
    { kind: "message", record_index: 0, role: "user", content: "hi", timestamp: USER_STAMP },
    { kind: "message", record_index: 1, role: "assistant", content: "hello", timestamp: REPLY_STAMP },
  ];

  it("记录里的时刻落到用户行与助手行上", () => {
    const replayed = createTranscriptStore();
    replayHistoryRecords(records, SID, replayed);
    const entries = replayed.getEntries(SID);
    const user = entries.find((e) => e.kind === "user") as { timestamp?: string };
    const assistant = entries.find((e) => e.kind === "assistant") as AssistantEntry;
    expect(user.timestamp).toBe(USER_STAMP);
    expect(assistant.timestamp).toBe(REPLY_STAMP);
  });

  it("同一条消息：实时那一轮与重开会话后读到的是同一个时刻", () => {
    // 实时：用户行先由客户端插入（只带 request id），时刻由 user_message 帧补；回复的时刻
    // 由收尾帧带着。两处给的都是 daemon 写在记录里的那个字符串。
    const live = createTranscriptStore();
    live.addUserMessage("hi", SID, { requestId: "req-1" });
    live.stampUserMessage("req-1", USER_STAMP, SID);
    live.handleDelta([{ request_id: "req-1", content: "hello" }], SID);
    live.handleDone({ request_id: "req-1", timestamp: REPLY_STAMP }, SID);

    const replayed = createTranscriptStore();
    replayHistoryRecords(records, SID, replayed);

    const stamps = (s: TranscriptStore) =>
      s.getEntries(SID).map((e) =>
        e.kind === "user" || e.kind === "assistant" ? e.timestamp : undefined,
      );
    expect(stamps(replayed)).toEqual(stamps(live));
  });
});
