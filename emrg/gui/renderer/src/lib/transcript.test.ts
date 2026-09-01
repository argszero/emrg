import { describe, expect, it } from "vitest";
import {
  createTranscriptStore,
  type AssistantEntry,
  type ToolGroup,
  type TranscriptEntry,
  type ToolRow,
} from "./transcript";

/**
 * transcript.test.ts — 聊天区纯状态机测试（Batch 2）。
 * 镜像旧 emrg/gui/test/tool-group.test.js 的断言（工具合并组/文本穿插不合并），
 * 并覆盖 chat.js 的流式 delta / done / 会话隔离 / 清除逻辑。
 */
const identity = (k: string): string => k;

function store(opts?: { t?: (k: string) => string }) {
  return createTranscriptStore({ t: opts?.t ?? identity });
}

function assistantOf(entries: TranscriptEntry[], rid: string): AssistantEntry | undefined {
  const e = entries.find((x) => x.kind === "assistant" && x.rid === rid);
  return e?.kind === "assistant" ? e : undefined;
}

function groupOf(entries: TranscriptEntry[]): ToolGroup | undefined {
  const e = entries.find((x) => x.kind === "tool-group");
  return e?.kind === "tool-group" ? e.group : undefined;
}

function rowByCallId(entries: TranscriptEntry[], callId: string): ToolRow | undefined {
  for (const e of entries) {
    if (e.kind === "tool-row" && e.row.callId === callId) return e.row;
    if (e.kind === "tool-group") {
      const r = e.group.rows.find((x) => x.callId === callId);
      if (r) return r;
    }
  }
  return undefined;
}

describe("工具合并组（rant 21:28:49，镜像 tool-group.test.js）", () => {
  it("连续工具（无文本穿插）合并为 1 组 + 摘要 + 收起/展开", () => {
    const s = store();
    // 工具 1 完成
    s.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
    s.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 0.4, content: "" }, "s1");
    // 工具 2 完成
    s.handleToolStart({ tool_call_id: "t2", tool_name: "read", request_id: "r1" }, "s1");
    s.handleToolEnd({ tool_call_id: "t2", tool_name: "read", elapsed: 1.2, content: "" }, "s1");
    // 工具 3 完成
    s.handleToolStart({ tool_call_id: "t3", tool_name: "edit", request_id: "r1" }, "s1");
    s.handleToolEnd({ tool_call_id: "t3", tool_name: "edit", elapsed: 0.3, content: "" }, "s1");

    const entries = s.getEntries("s1");
    const groups = entries.filter((e) => e.kind === "tool-group");
    expect(groups).toHaveLength(1); // 应合并为 1 个组
    expect(entries.filter((e) => e.kind === "tool-row")).toHaveLength(0); // 无独立行
    const group = groupOf(entries)!;
    expect(group.rows).toHaveLength(3); // 组内 3 行
    // 组收起（全部完成且未手动展开）
    expect(group.collapsed).toBe(true);
    // 摘要：3 个工具执行 · 1.9s（0.4+1.2+0.3）
    expect(group.summary?.count).toBe(3);
    expect(group.summary?.totalElapsed.toFixed(1)).toBe("1.9");
    expect(group.barHidden).toBe(false);

    // bar 点击 → 展开（user-expanded，不再自动收起）
    const gi = entries.indexOf(groups[0]!);
    s.toggleGroup("s1", gi);
    expect(group.collapsed).toBe(false);
    expect(group.userExpanded).toBe(true);
    // 展开后再来新工具并完成 → 不自动收起（user-expanded）
    s.handleToolStart({ tool_call_id: "t4", tool_name: "bash", request_id: "r1" }, "s1");
    s.handleToolEnd({ tool_call_id: "t4", tool_name: "bash", elapsed: 0.5, content: "" }, "s1");
    expect(group.rows).toHaveLength(4); // 新工具应并入组
    expect(group.collapsed).toBe(false);
  });

  it("文本穿插不合并：工具前有文本 → 独立行", () => {
    const s = store();
    // 先来一个工具并完成
    s.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
    s.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 0.2, content: "" }, "s1");
    // 文本 delta（穿插）——旧测试传 text 字段，chat.js 只读 content，故不产生文本
    s.handleDelta([{ request_id: "r1", content: "" }], "s1");
    // 再来工具 → 因上一节点是助手文本，独立行
    s.handleToolStart({ tool_call_id: "t2", tool_name: "read", request_id: "r2" }, "s1");

    const entries = s.getEntries("s1");
    expect(entries.filter((e) => e.kind === "tool-group")).toHaveLength(0);
    expect(entries.filter((e) => e.kind === "tool-row")).toHaveLength(2);
  });

  it("工具→文本顺序正确（rant 2026-08-28T22:40:33）：不预建空助手条目，文本排在工具之后", () => {
    const s = store();
    // 同一轮：多个工具先到，最后才来流式文本
    s.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
    s.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 0.4, content: "" }, "s1");
    s.handleToolStart({ tool_call_id: "t2", tool_name: "read", request_id: "r1" }, "s1");
    s.handleToolEnd({ tool_call_id: "t2", tool_name: "read", elapsed: 1.2, content: "" }, "s1");
    s.handleToolStart({ tool_call_id: "t3", tool_name: "edit", request_id: "r1" }, "s1");
    s.handleToolEnd({ tool_call_id: "t3", tool_name: "edit", elapsed: 0.3, content: "" }, "s1");
    s.handleDelta([{ request_id: "r1", content: "汇总" }], "s1");

    const entries = s.getEntries("s1");
    // 最后一个条目是文本（assistant），工具（tool-group）在它之前 → 「工具1→工具2→…→文本」
    expect(entries[entries.length - 1].kind).toBe("assistant");
    expect(entries[entries.length - 1]).toMatchObject({ kind: "assistant", rid: "r1" });
    expect(entries.filter((e) => e.kind === "tool-group")).toHaveLength(1);
    // 首个非空文本段在工具组之后
    const asIdx = entries.findIndex((e) => e.kind === "assistant" && e.rid === "r1");
    const grpIdx = entries.findIndex((e) => e.kind === "tool-group");
    expect(asIdx).toBeGreaterThan(grpIdx);
  });
});

describe("流式 delta", () => {
  it("按 rid 累加文本到当前文本段，typing=true", () => {
    const s = store();
    s.handleDelta([{ request_id: "r1", content: "Hel" }, { request_id: "r1", content: "lo" }], "s1");
    const e = assistantOf(s.getEntries("s1"), "r1");
    expect(e).toBeDefined();
    expect(e!.segments).toHaveLength(1);
    expect(e!.segments[0].text).toBe("Hello");
    expect(e!.segments[0].typing).toBe(true);
    expect(e!.isOwn).toBe(false);
  });

  it("上一文本段被工具封存后，新 delta 开新 AssistantEntry（rant 2026-08-31T12:30:33）", () => {
    const s = store();
    s.handleDelta([{ request_id: "r1", content: "思考中…" }], "s1");
    s.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
    s.handleDelta([{ request_id: "r1", content: "继续输出" }], "s1");
    const entries = s.getEntries("s1");
    const assistants = entries.filter((x): x is AssistantEntry => x.kind === "assistant" && x.rid === "r1");
    // 工具后文本必须开新 entry（排在工具行之后），不能回挂到工具上方的旧 entry
    expect(assistants).toHaveLength(2);
    expect(assistants[0].segments).toHaveLength(1);
    expect(assistants[0].segments[0].text).toBe("思考中…");
    expect(assistants[0].segments[0].sealed).toBe(true);
    expect(assistants[0].segments[0].typing).toBe(false); // 封存时移除 typing（rant 21:09）
    expect(assistants[1].segments[0].text).toBe("继续输出");
    expect(assistants[1].segments[0].sealed).toBe(false);
    // 渲染顺序：文本1 → 工具行 → 文本2（与 TUI 到达顺序对齐）
    expect(entries.map((x) => x.kind)).toEqual(["assistant", "tool-row", "assistant"]);
  });

  it("多工具交错：文本→工具A→文本→工具B→文本 生成 A1→T_A→A2→T_B→A3（rant 2026-08-31T12:30:33）", () => {
    const s = store();
    s.handleDelta([{ request_id: "r1", content: "A1" }], "s1");
    s.handleToolStart({ tool_call_id: "ta", tool_name: "bash", request_id: "r1" }, "s1");
    s.handleDelta([{ request_id: "r1", content: "A2" }], "s1");
    s.handleToolStart({ tool_call_id: "tb", tool_name: "read", request_id: "r1" }, "s1");
    s.handleDelta([{ request_id: "r1", content: "A3" }], "s1");
    const kinds = s.getEntries("s1").map((x) => x.kind);
    expect(kinds).toEqual(["assistant", "tool-row", "assistant", "tool-row", "assistant"]);
    const as = s.getEntries("s1").filter((x): x is AssistantEntry => x.kind === "assistant");
    expect(as.map((a) => a.segments.map((seg) => seg.text).join(""))).toEqual(["A1", "A2", "A3"]);
    // 工具交错后同 rid 文本各自独立 entry，groupIndex 指向最新 entry → done 只停最新 typing
    s.handleDone({ request_id: "r1" }, "s1");
    expect(as[2].segments[0].typing).toBe(false);
  });

  it("done 之后到达的残留 delta 被丢弃（rant 14:11）", () => {
    const s = store();
    s.handleDelta([{ request_id: "r1", content: "done" }], "s1");
    s.handleDone({ request_id: "r1" }, "s1");
    s.handleDelta([{ request_id: "r1", content: "late" }], "s1");
    const e = assistantOf(s.getEntries("s1"), "r1");
    expect(e!.segments[0].text).toBe("done");
  });
});

describe("done 收尾", () => {
  it("done 停止 typing 且保留条目（渲染层负责 markdown）", () => {
    const s = store();
    s.handleDelta([{ request_id: "r1", content: "text" }], "s1");
    s.handleDone({ request_id: "r1" }, "s1");
    const e = assistantOf(s.getEntries("s1"), "r1");
    expect(e).toBeDefined();
    expect(e!.segments[0].typing).toBe(false);
    // done 后 groupIndex 移除：同 rid 再 tool_start 不建助手条目（rant 2026-08-28T22:40:33），
    // 助手条目只在真正的文本（delta）到达时才由 handleDelta 新建；且该 rid 已 done，
    // 后续迟到 delta 被 doneRids 丢弃（rant 14:11），故助手条目数保持 1。
    s.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
    expect(s.getEntries("s1").filter((x) => x.kind === "assistant")).toHaveLength(1);
    s.handleDelta([{ request_id: "r1", content: "new text" }], "s1");
    expect(s.getEntries("s1").filter((x) => x.kind === "assistant")).toHaveLength(1);
  });

  it("maxRounds 截断 → 系统提示", () => {
    const s = store();
    s.handleDone({ request_id: "r1", content: "exceeded max tool rounds" }, "s1");
    expect(s.getEntries("s1").some((e) => e.kind === "system" && e.text === "chat.maxRoundsHint")).toBe(true);
    // 不匹配 → 无提示
    const s2 = store();
    s2.handleDone({ request_id: "r1", content: "normal reply" }, "s1");
    expect(s2.getEntries("s1").filter((e) => e.kind === "system")).toHaveLength(0);
  });
});

describe("会话级状态隔离（P3）", () => {
  it("每 sid 独立桶；sid=null 为旧版单会话桶", () => {
    const s = store();
    s.handleDelta([{ request_id: "r1", content: "a" }], "s1");
    s.handleDelta([{ request_id: "r1", content: "b" }], "s2");
    s.handleDelta([{ request_id: "r1", content: "c" }]); // 无 sid → null 桶
    expect(assistantOf(s.getEntries("s1"), "r1")!.segments[0].text).toBe("a");
    expect(assistantOf(s.getEntries("s2"), "r1")!.segments[0].text).toBe("b");
    expect(assistantOf(s.getEntries(), "r1")!.segments[0].text).toBe("c");
    expect(s.getEntries("s1")).toHaveLength(1);
    expect(s.getEntries("s2")).toHaveLength(1);
    expect(s.getEntries()).toHaveLength(1);
  });

  it("unregisterSession 释放会话桶", () => {
    const s = store();
    s.handleDelta([{ request_id: "r1", content: "x" }], "s1");
    s.unregisterSession("s1");
    expect(s.getEntries("s1")).toHaveLength(0);
    // 再次使用自动重建（chat.js st() 惰性建桶）
    s.handleDelta([{ request_id: "r1", content: "y" }], "s1");
    expect(assistantOf(s.getEntries("s1"), "r1")!.segments[0].text).toBe("y");
  });
});

describe("clear / clearTyping / 行交互", () => {
  it("clear 重置条目、索引与加载条", () => {
    const s = store();
    s.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
    s.handleDelta([{ request_id: "r1", content: "hi" }], "s1");
    s.setLoadBar("加载历史中…", "s1");
    s.clear("s1");
    expect(s.getEntries("s1")).toHaveLength(0);
    expect(s.getLoadBar("s1")).toBeNull();
  });

  it("clearTyping 停止所有助手段 typing", () => {
    const s = store();
    s.handleDelta([{ request_id: "r1", content: "a" }], "s1");
    s.handleDelta([{ request_id: "r2", content: "b" }], "s1");
    s.clearTyping("s1");
    for (const e of s.getEntries("s1")) {
      if (e.kind === "assistant") for (const seg of e.segments) expect(seg.typing).toBe(false);
    }
  });

  it("toggleRowOutput 切换行输出可见性", () => {
    const s = store();
    s.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
    s.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 0.1, content: "out" }, "s1");
    expect(rowByCallId(s.getEntries("s1"), "t1")!.outputExpanded).toBe(false);
    s.toggleRowOutput("s1", "t1");
    expect(rowByCallId(s.getEntries("s1"), "t1")!.outputExpanded).toBe(true);
  });

  it("失败的工具行不记录耗时（供合并组摘要求和时为 0）", () => {
    const s = store();
    s.handleToolStart({ tool_call_id: "t1", tool_name: "bash", request_id: "r1" }, "s1");
    s.handleToolEnd({ tool_call_id: "t1", tool_name: "bash", elapsed: 9.9, content: "", error: "boom" }, "s1");
    const row = rowByCallId(s.getEntries("s1"), "t1")!;
    expect(row.status).toBe("failed");
    expect(row.elapsed).toBeUndefined();
  });

  it("订阅机制：变更通知 + 版本号递增", () => {
    const s = store();
    let calls = 0;
    const unsub = s.subscribe(() => calls++);
    s.addSystemMessage("x", "s1");
    expect(calls).toBe(1);
    expect(s.getVersion()).toBe(1);
    unsub();
    s.addSystemMessage("y", "s1");
    expect(calls).toBe(1);
  });
});
