import { describe, expect, it, vi } from "vitest";
import { createTranscriptStore, type TranscriptEntry, type TranscriptStore } from "./transcript";
import { replayHistoryRecords, replayHistoryRecordsPrepend, toolCallIntent, type HistoryRecord } from "./historyReplay";

/**
 * historyReplay.test.ts — 回放等价性测试（rant 2026-09-20T18:58:44 的验收）。
 *
 * 同一回合喂两路：一路是**实时帧**（按 daemon 的发送顺序调实时 handler），一路是
 * **对应落盘记录**（list_history include_records 的样子）经 `replayHistoryRecords`
 * 回放。两路产出的条目数组必须逐项相等（顺序、角色、文本、工具行、合并组）。
 *
 * 变异验证（本文件必须变红的两个方向，人工执行）：
 *  - 把回放里的 `content` 换回 `preview`（截断）→ 长正文那条断言变红；
 *  - 去掉工具记录的配对（照落盘顺序 start/start/end/end）→ 合并组那条断言变红。
 */

const SID = "s1";

/**
 * 条目数组的逐项比较值：去掉 `rid`/`isOwn`——它们是**流的标识**，实时那一路由 daemon 的
 * `request_id` 给出、回放那一路由 record_index 合成，两者本就不必相同（也不该相同：
 * 回放取不到当初那个 request_id）。除此之外每个字段都比——正是 rant 列的顺序、角色、
 * 文本、工具行（callId/name/status/intent/content）、合并组（rows/summary/收起态）。
 */
function comparable(entries: TranscriptEntry[]): unknown {
  return JSON.parse(
    JSON.stringify(entries, (k, v) => (k === "rid" || k === "isOwn" ? undefined : v)),
  );
}

/** 实时那一路：与 daemon 的发送顺序一致（一轮一个 request_id，done 收尾）。 */
function feedLive(
  store: TranscriptStore,
  sid: string,
  turn: {
    user: string;
    rounds: Array<{ text: string; tools: Array<{ id: string; name: string; intent: string; output: string; error?: boolean }> }>;
  },
): void {
  const rid = `req-${turn.user}`;
  store.addUserMessage(turn.user, sid);
  for (const round of turn.rounds) {
    if (round.text) store.handleDelta([{ request_id: rid, content: round.text }], sid);
    for (const tool of round.tools) {
      store.handleToolStart({ request_id: rid, tool_call_id: tool.id, tool_name: tool.name, intent: tool.intent }, sid);
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
    rounds: Array<{ text: string; tools: Array<{ id: string; name: string; intent: string; output: string; error?: boolean }> }>;
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
                function: { name: tool.name, arguments: JSON.stringify({ intent: tool.intent }) },
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
        { id: "c1", name: "bash", intent: "list the files", output: "a\nb\n" },
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
    };
    replayHistoryRecords(
      [
        { kind: "message", role: "user", content: "q" },
        { kind: "message", role: "assistant", content: "a" },
      ],
      SID,
      sink,
    );
    expect(sink.addUserMessage).toHaveBeenCalledWith("q", SID);
    expect(sink.handleDelta).toHaveBeenCalledWith([{ request_id: "hist-u", content: "a" }], SID);
    expect(sink.handleDone).toHaveBeenCalledWith({ request_id: "hist-u" }, SID);
  });
});
