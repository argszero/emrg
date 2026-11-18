import { describe, expect, it } from "vitest";
import { formatActionElapsed, turnActions, turnFiles } from "./inspector";
import type { ToolRow, TranscriptEntry } from "./transcript";

/**
 * inspector.test.ts — this turn's calls and files, read off the transcript.
 *
 * The point of the module is that it invents nothing: an order comes from the entries,
 * a duration comes from the daemon's `elapsed`, and "this turn" is bounded by the last
 * message the host sent. Each of those is asserted here, including the shapes that read
 * the same but are not: a running call has no duration yet, and a call with no path is
 * not a file.
 */

function row(over: Partial<ToolRow> = {}): ToolRow {
  return {
    callId: "c1",
    toolName: "read",
    status: "done",
    outputExpanded: false,
    fullExpanded: false,
    ...over,
  };
}

function toolRow(r: ToolRow): TranscriptEntry {
  return { kind: "tool-row", id: r.callId, row: r };
}

function group(rows: ToolRow[]): TranscriptEntry {
  return {
    kind: "tool-group",
    id: "g1",
    group: { rows, collapsed: false, barHidden: rows.length < 2, summary: null },
  };
}

const user = (text: string): TranscriptEntry => ({ kind: "user", id: `u-${text}`, text });

describe("turnActions", () => {
  it("按执行顺序列出，耗时取 daemon 报的值", () => {
    const entries = [
      user("go"),
      toolRow(row({ callId: "c1", toolName: "read", elapsed: 0.4 })),
      group([row({ callId: "c2", toolName: "grep" }), row({ callId: "c3", toolName: "bash", elapsed: 2.2 })]),
    ];
    expect(turnActions(entries)).toEqual([
      { callId: "c1", toolName: "read", status: "done", elapsed: 0.4 },
      { callId: "c2", toolName: "grep", status: "done", elapsed: null },
      { callId: "c3", toolName: "bash", status: "done", elapsed: 2.2 },
    ]);
  });

  it("本轮从宿主最后一条消息之后算起，上一轮的调用不算", () => {
    const entries = [
      user("first"),
      toolRow(row({ callId: "old", toolName: "read" })),
      user("second"),
      toolRow(row({ callId: "new", toolName: "write" })),
    ];
    expect(turnActions(entries).map((a) => a.callId)).toEqual(["new"]);
  });

  it("还没发过消息（中途打开一个在跑的会话）→ 屏幕上的调用都算本轮", () => {
    const entries = [toolRow(row({ callId: "c1", toolName: "read" }))];
    expect(turnActions(entries)).toHaveLength(1);
  });

  it("没报耗时是 null，不是 0", () => {
    expect(turnActions([toolRow(row({ elapsed: undefined }))])[0].elapsed).toBeNull();
  });
});

describe("turnFiles", () => {
  it("一条路径一行，重复的只出现一次，保留第一次的调用名", () => {
    const entries = [
      user("go"),
      toolRow(row({ callId: "c1", toolName: "read", arguments: { file_path: "/p/a.py" } })),
      toolRow(row({ callId: "c2", toolName: "edit", arguments: { file_path: "/p/a.py" } })),
      toolRow(row({ callId: "c3", toolName: "write", arguments: { path: "/p/b.py" } })),
    ];
    expect(turnFiles(entries)).toEqual([
      { path: "/p/a.py", toolName: "read" },
      { path: "/p/b.py", toolName: "write" },
    ]);
  });

  it("不点名的调用（搜索、命令、消息）不产生文件行", () => {
    const entries = [
      user("go"),
      toolRow(row({ callId: "c1", toolName: "grep", arguments: { pattern: "x" } })),
      toolRow(row({ callId: "c2", toolName: "bash", arguments: { command: "ls" } })),
      toolRow(row({ callId: "c3", toolName: "read" })),
    ];
    expect(turnFiles(entries)).toEqual([]);
  });
});

describe("formatActionElapsed", () => {
  it("十秒以下到十分之一秒，以上取整，没报就是 —", () => {
    expect(formatActionElapsed(0.4)).toBe("0.4s");
    expect(formatActionElapsed(9.94)).toBe("9.9s");
    expect(formatActionElapsed(42.6)).toBe("43s");
    expect(formatActionElapsed(null)).toBe("—");
    expect(formatActionElapsed(undefined)).toBe("—");
  });
});
