import type { TranslateFn } from "./utils";

/**
 * transcript.ts — 聊天区纯状态机（Batch 2，设计 §5 Batch 2 项 1–3）。
 * 源：vanilla renderer/js/chat.js（471 行）。chat.js 直接操作 DOM；本模块把
 * 消息/工具行/合并组建模为数据，React TranscriptView 消费渲染（D3：旧 vanilla
 * 保持不动直到 Batch 5 一次性切换，故本模块独立存在、可脱离 DOM 测试）。
 *
 * 迁移保真的关键行为（逐条对齐 chat.js）：
 * - P3（rant 15:07:19）会话级状态隔离：每 sid 一份 entries/groupIndex/toolRowIndex/doneRids；
 *   sid=null 为旧版单会话桶（无 sid 事件 → 行为与改造前完全一致）。
 * - 流式 delta（G122）：按 rid 累加进当前文本段；rid 已 done → 丢弃残留 delta（rant 14:11）。
 * - 文本段封存（rant 21:57:10）：助手文本段之后来工具 → 封存当前段（移除 typing），
 *   后续 delta 开新段，保持 TUI 交错顺序。
 * - 工具合并组（rant 21:28:49）：前一个工具已完成（.tool-row:not(.running)）→ 建组收编；
 *   已是组 → 新行并入；组收起后新工具 start 自动展开显示 running 行；全部完成且 ≥2 行
 *   → bar 显示摘要（数量 + 总耗时）并自动收起（除非手动展开 user-expanded，rule 5）。
 * - done 收尾：typing 停止、doneRids 记入（>500 清空防长期运行增长）、timeout/maxRounds
 *   提示系统消息（文案经注入 t 解析，与 copywriting.ts 同模式）。
 *
 * 本模块不含 DOM/滚动/欢迎屏副作用（append/scrollToBottom/App.updateEmptyState 属组件层）。
 */

/** 工具行（chat.js .tool-row 的数据化） */
export interface ToolRow {
  callId: string;
  toolName: string;
  status: "running" | "done" | "failed";
  intent?: string;
  /** 仅成功且提供 elapsed 时记录（chat.js 只在 ok 时写 dataset.elapsed，供合并组摘要求和） */
  elapsed?: number;
  content?: string;
  /** 点击行切换输出可见性（.tool-output hidden） */
  outputExpanded: boolean;
  /** 展开全文按钮（G91/G131：content >2000 字符截断 + 展开） */
  fullExpanded: boolean;
}

/** 工具合并组（rant 21:28:49）——摘要 + 收起/展开状态 */
export interface ToolGroup {
  rows: ToolRow[];
  collapsed: boolean;
  /** 手动展开后不再自动收起（rule 5）；折叠后再手动收起清除 */
  userExpanded: boolean;
  /** 组内 <2 行或有 running 行 → bar 隐藏（进行中工具实时可见） */
  barHidden: boolean;
  /** ≥2 行全部完成 → 摘要（数量 + 总耗时）；否则 null */
  summary: { count: number; totalElapsed: number } | null;
}

/** 助手消息文本段（同 rid 流式分段：段被工具封存后新 delta 开新段） */
export interface AssistantSegment {
  text: string;
  hasText: boolean;
  sealed: boolean;
  typing: boolean;
}

export interface AssistantEntry {
  kind: "assistant";
  rid: string;
  isOwn: boolean;
  segments: AssistantSegment[];
}

export interface UserEntry {
  kind: "user";
  text: string;
}
export interface SystemEntry {
  kind: "system";
  text: string;
}
export interface HistoryEntry {
  kind: "history";
  text: string;
}
export interface ToolRowEntry {
  kind: "tool-row";
  row: ToolRow;
}
export interface ToolGroupEntry {
  kind: "tool-group";
  group: ToolGroup;
}

/** 消息列表条目（渲染层按 kind 分发组件） */
export type TranscriptEntry =
  | UserEntry
  | SystemEntry
  | HistoryEntry
  | AssistantEntry
  | ToolRowEntry
  | ToolGroupEntry;

/** 单会话状态桶（P3：每 sid 一份） */
export interface SessionTranscript {
  sid: string | null;
  entries: TranscriptEntry[];
  /** 顶部历史加载条（rant 14:15:12；text=null 移除） */
  loadBar: string | null;
  /** rid → entries 下标（助手条目映射；done 后删除，迟到 delta 由 doneRids 拦截） */
  groupIndex: Map<string, number>;
  /** callId → { entry: 条目下标, row: 组内行号（独立行 = null） } */
  toolRowIndex: Map<string, { entry: number; row: number | null }>;
  doneRids: Set<string>;
}

/* ── 事件入参（与 main/daemon 协议字段对齐，仅取 chat.js 用到的） ── */
export interface DeltaChunk {
  request_id?: string;
  content?: string;
}
export interface DoneData {
  request_id?: string;
  timeout?: boolean;
  content?: string;
}
export interface ToolStartData {
  request_id?: string;
  tool_call_id: string;
  tool_name: string;
  intent?: string;
}
export interface ToolEndData {
  tool_call_id: string;
  tool_name: string;
  elapsed?: number;
  content?: string;
  error?: unknown;
}

/** 订阅接口（React useSyncExternalStore 消费） */
export interface TranscriptStore {
  subscribe(listener: () => void): () => void;
  /** 单调版本号：每次变更 +1（getSnapshot 缓存值，稳定引用） */
  getVersion(): number;
  registerSession(sid: string | null): void;
  unregisterSession(sid?: string | null): void;
  st(sid?: string | null): SessionTranscript;
  getEntries(sid?: string | null): TranscriptEntry[];
  getLoadBar(sid?: string | null): string | null;
  /** 设置 own stream request id（决定助手消息是否显示“来自其他客户端”标签） */
  setOwnStream(rid: string | null): void;
  handleDelta(chunks: DeltaChunk[], sid?: string | null): void;
  handleDone(data: DoneData, sid?: string | null): void;
  handleToolStart(data: ToolStartData, sid?: string | null): void;
  handleToolEnd(data: ToolEndData, sid?: string | null): void;
  clearTyping(sid?: string | null): void;
  clear(sid?: string | null): void;
  addUserMessage(text: string, sid?: string | null): void;
  addSystemMessage(text: string, sid?: string | null): void;
  addHistoryMessage(text: string, sid?: string | null): void;
  setLoadBar(text: string | null, sid?: string | null): void;
  toggleRowOutput(sid: string | null, callId: string): void;
  expandRowContent(sid: string | null, callId: string): void;
  toggleGroup(sid: string | null, entryIndex: number): void;
}

const SID_NULL = "__emrg_null_sid__";

function assistantHasText(entry: AssistantEntry): boolean {
  return entry.segments.some((seg) => seg.hasText);
}

/** 更新工具合并组展示状态（chat.js updateToolGroup 数据化） */
function updateToolGroup(group: ToolGroup): void {
  const running = group.rows.some((r) => r.status === "running");
  if (group.rows.length >= 2 && !running) {
    const total = group.rows.reduce((acc, r) => acc + (r.elapsed ?? 0), 0);
    group.summary = { count: group.rows.length, totalElapsed: total };
    group.barHidden = false;
    group.collapsed = !group.userExpanded;
  } else {
    // 组内仅 1 行 或 存在 running 行 → bar 隐藏、rows 展开（进行中工具实时可见）
    group.summary = null;
    group.barHidden = true;
    group.collapsed = false;
  }
}

export function createTranscriptStore(opts: { t?: TranslateFn } = {}): TranscriptStore {
  const t = opts.t ?? ((key: string): string => key);
  const sessions = new Map<string, SessionTranscript>();
  let ownStreamRequestId: string | null = null;
  let version = 0;
  const listeners = new Set<() => void>();

  function notify(): void {
    version++;
    for (const listener of [...listeners]) listener();
  }

  function key(sid?: string | null): string {
    return sid || SID_NULL;
  }

  function st(sid?: string | null): SessionTranscript {
    const k = key(sid);
    let s = sessions.get(k);
    if (!s) {
      s = {
        sid: sid || null,
        entries: [],
        loadBar: null,
        groupIndex: new Map(),
        toolRowIndex: new Map(),
        doneRids: new Set(),
      };
      sessions.set(k, s);
    }
    return s;
  }

  /** 变更包装：所有 mutation 走这里统一通知订阅者 */
  function mutate(fn: () => void): void {
    fn();
    notify();
  }

  function findRow(
    s: SessionTranscript,
    callId: string,
  ): { row: ToolRow; entry: number; rowIdx: number | null } | undefined {
    const loc = s.toolRowIndex.get(callId);
    if (!loc) return undefined;
    const entry = s.entries[loc.entry];
    if (!entry) return undefined;
    if (entry.kind === "tool-row" && entry.row.callId === callId) {
      return { row: entry.row, entry: loc.entry, rowIdx: null };
    }
    if (entry.kind === "tool-group") {
      const row = entry.group.rows[loc.row ?? -1];
      if (row && row.callId === callId) return { row, entry: loc.entry, rowIdx: loc.row };
    }
    return undefined;
  }

  function handleDelta(chunks: DeltaChunk[], sid?: string | null): void {
    mutate(() => {
      const s = st(sid);
      for (const chunk of chunks) {
        const rid = chunk.request_id;
        if (!rid || s.doneRids.has(rid)) continue; // rant 14:11：已 done 的流丢弃残留 delta
        let entryIndex = s.groupIndex.get(rid);
        let entry = entryIndex !== undefined ? s.entries[entryIndex] : undefined;
        if (!entry || entry.kind !== "assistant") {
          const e: AssistantEntry = { kind: "assistant", rid, isOwn: ownStreamRequestId === rid, segments: [] };
          s.entries.push(e);
          s.groupIndex.set(rid, s.entries.length - 1);
        }
        const as = s.entries[s.groupIndex.get(rid)!] as AssistantEntry;
        const active = as.segments[as.segments.length - 1];
        if (!active || active.sealed) {
          // rant 21:57:10：上一文本段被工具行“封存”→ 新文本段开新节点
          as.segments.push({ text: "", hasText: false, sealed: false, typing: true });
        }
        const seg = as.segments[as.segments.length - 1];
        const content = chunk.content || "";
        if (content) seg.hasText = true;
        seg.text += content;
      }
    });
  }

  function handleDone(data: DoneData, sid?: string | null): void {
    mutate(() => {
      const s = st(sid);
      const rid = data.request_id;
      if (rid) {
        s.doneRids.add(rid);
        if (s.doneRids.size > 500) s.doneRids.clear(); // UUID 不复用，超限即清
        const entryIndex = s.groupIndex.get(rid);
        if (entryIndex !== undefined) {
          const entry = s.entries[entryIndex];
          if (entry && entry.kind === "assistant") {
            for (const seg of entry.segments) seg.typing = false;
          }
          s.groupIndex.delete(rid); // 渲染完成 → 移除映射（chat.js groupNodes.delete）
        }
      }
      if (data.timeout) {
        s.entries.push({ kind: "system", text: t("chat.timeoutWarn") });
      }
      // 工具调用次数上限中断（跨项目教训：截断的工作不提示 = 用户拿半成品）
      if (data.content && /exceeded/i.test(data.content) && /max|limit|round/i.test(data.content)) {
        s.entries.push({ kind: "system", text: t("chat.maxRoundsHint") });
      }
    });
  }

  function handleToolStart(data: ToolStartData, sid?: string | null): void {
    mutate(() => {
      const s = st(sid);
      const rid = data.request_id;
      if (rid) {
        let entryIndex = s.groupIndex.get(rid);
        let entry = entryIndex !== undefined ? s.entries[entryIndex] : undefined;
        if (!entry || entry.kind !== "assistant") {
          // G104：tool_start 也建组（LLM 先出 tool_calls 后出文本）
          const e: AssistantEntry = { kind: "assistant", rid, isOwn: ownStreamRequestId === rid, segments: [] };
          s.entries.push(e);
          s.groupIndex.set(rid, s.entries.length - 1);
        } else if (assistantHasText(entry)) {
          // rant 21:57:10：已有文本段之后来了工具 → 封存当前段
          const active = entry.segments[entry.segments.length - 1];
          active.sealed = true;
          // rant 21:09：已结束的文本段不再闪烁——封存时移除 typing
          active.typing = false;
        }
      }
      const row: ToolRow = {
        callId: data.tool_call_id,
        toolName: data.tool_name,
        status: "running",
        intent: data.intent,
        outputExpanded: false,
        fullExpanded: false,
      };
      // rant 21:28:49：连续工具合并 —— 最后条目判定
      // 1) 前一个工具已完成（.tool-row:not(.running)）→ 新建 .tool-group，把旧行移入 rows，新行也入 rows
      // 2) 已是 .tool-group → 新行直接入其 rows（组收起时新工具 start 自动展开显示 running）
      // 3) 其他（文本/用户消息/无条目）→ 独立 .tool-row（文本穿插不合并）
      const last = s.entries[s.entries.length - 1];
      if (last && last.kind === "tool-row" && last.row.status !== "running") {
        const group: ToolGroup = {
          rows: [last.row, row],
          collapsed: false,
          userExpanded: false,
          barHidden: true,
          summary: null,
        };
        s.entries[s.entries.length - 1] = { kind: "tool-group", group };
        s.toolRowIndex.set(last.row.callId, { entry: s.entries.length - 1, row: 0 });
        s.toolRowIndex.set(row.callId, { entry: s.entries.length - 1, row: 1 });
        updateToolGroup(group);
      } else if (last && last.kind === "tool-group") {
        const g = last.group;
        g.rows.push(row);
        g.collapsed = false; // 组收起后新工具 start → 自动展开显示 running 行
        s.toolRowIndex.set(row.callId, { entry: s.entries.length - 1, row: g.rows.length - 1 });
        updateToolGroup(g);
      } else {
        s.entries.push({ kind: "tool-row", row });
        s.toolRowIndex.set(row.callId, { entry: s.entries.length - 1, row: null });
      }
    });
  }

  function handleToolEnd(data: ToolEndData, sid?: string | null): void {
    mutate(() => {
      const s = st(sid);
      const found = findRow(s, data.tool_call_id);
      if (!found) return;
      const ok = !data.error;
      found.row.status = ok ? "done" : "failed";
      // rant 21:08：spinner 停止（数据层 = status 切换）；耗时只在 ok 时记录
      if (ok && data.elapsed !== undefined) found.row.elapsed = data.elapsed;
      if (data.content) found.row.content = data.content;
      // 该行在合并组内 → 更新组摘要（数量 + 总耗时）与收起状态
      if (found.rowIdx !== null) {
        const entry = s.entries[found.entry];
        if (entry && entry.kind === "tool-group") updateToolGroup(entry.group);
      }
    });
  }

  function clearTyping(sid?: string | null): void {
    mutate(() => {
      const s = st(sid);
      for (const e of s.entries) {
        if (e.kind === "assistant") {
          for (const seg of e.segments) seg.typing = false;
        }
      }
    });
  }

  function clear(sid?: string | null): void {
    mutate(() => {
      const s = st(sid);
      // 清空消息但保留容器（app.js 的 .session-header 属组件层 DOM，不在此模型内）
      s.entries = [];
      s.groupIndex.clear();
      s.toolRowIndex.clear();
      s.doneRids.clear();
      s.loadBar = null;
    });
  }

  return {
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getVersion: () => version,
    registerSession: (sid) => {
      st(sid);
    },
    unregisterSession: (sid) => {
      mutate(() => {
        sessions.delete(key(sid));
      });
    },
    st,
    getEntries: (sid) => st(sid).entries,
    getLoadBar: (sid) => st(sid).loadBar,
    setOwnStream: (rid) => {
      mutate(() => {
        ownStreamRequestId = rid;
      });
    },
    handleDelta,
    handleDone,
    handleToolStart,
    handleToolEnd,
    clearTyping,
    clear,
    addUserMessage: (text, sid) => {
      mutate(() => {
        st(sid).entries.push({ kind: "user", text });
      });
    },
    addSystemMessage: (text, sid) => {
      mutate(() => {
        st(sid).entries.push({ kind: "system", text });
      });
    },
    addHistoryMessage: (text, sid) => {
      mutate(() => {
        st(sid).entries.push({ kind: "history", text });
      });
    },
    setLoadBar: (text, sid) => {
      mutate(() => {
        st(sid).loadBar = text;
      });
    },
    toggleRowOutput: (sid, callId) => {
      mutate(() => {
        const found = findRow(st(sid), callId);
        if (found) found.row.outputExpanded = !found.row.outputExpanded;
      });
    },
    expandRowContent: (sid, callId) => {
      mutate(() => {
        const found = findRow(st(sid), callId);
        if (found) found.row.fullExpanded = true;
      });
    },
    toggleGroup: (sid, entryIndex) => {
      mutate(() => {
        const entry = st(sid).entries[entryIndex];
        if (!entry || entry.kind !== "tool-group") return;
        if (entry.group.collapsed) {
          entry.group.collapsed = false;
          entry.group.userExpanded = true;
        } else {
          entry.group.collapsed = true;
          entry.group.userExpanded = false;
        }
      });
    },
  };
}
