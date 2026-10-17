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
 *   已是组 → 新行并入；≥2 行 → bar 显示摘要（数量 + 总耗时）。**展开状态只在宿主点 bar 时
 *   改变**（rant 2026-10-17T19:23:08）：工具开始、结束、追加、摘要变大都不动它。这条规则
 *   取代了原先的「有 running 行就展开、全部完成且 ≥2 行就收起（除非 user-expanded）」。
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
  /**
   * 工具入参（rant 2026-09-30T09:17:54 → #1787）。实时是 daemon 解析好的对象；回放那一路
   * 记录里存的是 `function.arguments` 的 **JSON 字符串**，但 `historyReplay.toolCallArguments`
   * 照 daemon 的规矩（解析失败落 `{}`）先归一，所以两条路存下来的是同一形状——这正是
   * 「打开旧会话与一直开着逐项一致」那条断言能直接比对象的原因。类型仍写 unknown：
   * 呈现统一走 `formatToolArguments`，它是最终的兜底。
   */
  arguments?: unknown;
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
  /**
   * 组内 <2 行 → bar 隐藏（还不成一个可折叠的组）。
   *
   * 「或有 running 行」那半句随自动收起一起去掉了：现在 ≥2 行就显示 bar，**包括还在跑的
   * 时候**——这是本改动的第二个可见后果，摘要会在运行中就出现。
   */
  barHidden: boolean;
  /** ≥2 行 → 摘要（数量 + 总耗时），不再要求全部完成；否则 null */
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
  segments: AssistantSegment[];
  /**
   * 这条回复的时刻 —— **daemon 记下的那一个**，取自收尾帧的 `timestamp`
   * （rant 2026-10-17T10:03:08）。只有该 rid 的**最后**一个助手段带它：一轮回复被工具
   * 切成多段时，时刻属于这条消息，不属于每一段。收尾帧不带时刻（取消/超轮次/出错，
   * 那些结局本就不落盘回复）时留空，行上就不显示任何时间。
   */
  timestamp?: string;
}

export interface UserEntry {
  kind: "user";
  text: string;
  /**
   * 这一行的时刻 —— daemon 写记录的那一刻（`user_message` / `steer_committed` 帧带来）。
   * 客户端自己点击发送时并不知道它，所以先留空、由帧补上；补不上的行（老记录、别的
   * 客户端发的）宁可没有时间，也不能由本地时钟编一个。
   */
  timestamp?: string;
  /** 发起这一行的 request id —— 帧按它认领自己那一行（见 `stampUserMessage`）。 */
  rid?: string;
}
export interface SystemEntry {
  kind: "system";
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
  | AssistantEntry
  | ToolRowEntry
  | ToolGroupEntry;

/** 单会话状态桶（P3：每 sid 一份） */
export interface SessionTranscript {
  sid: string | null;
  entries: TranscriptEntry[];
  /** 顶部历史加载条（rant 14:15:12；text=null 移除） */
  loadBar: string | null;
  /** 输入框草稿（rant 2026-09-01T20:28:31：按 sid 隔离，切视图/切会话不丢） */
  draft: string;
  /** rid → entries 下标（助手条目映射；done 后删除，迟到 delta 由 doneRids 拦截） */
  groupIndex: Map<string, number>;
  /** callId → { entry: 条目下标, row: 组内行号（独立行 = null） } */
  toolRowIndex: Map<string, { entry: number; row: number | null }>;
  doneRids: Set<string>;
  /**
   * `prependEntries` 的单调计数（每会话）。渲染层靠它区分「更早一页前插」与「新内容追加」：
   * 只有前插会改变**已有内容在文档中的位置**，因而只有前插需要滚差补偿（`lib/history.ts`
   * 的 `scrollCompensation`）。
   *
   * 为什么不看 `entries.length`：追加流式文本不增加条目数；而一页若整页都是重叠记录
   * （游标被压缩夹回时的去重），前插的条目数是 0 —— 两种情况下条数变化都不代表
   * 「有内容插到了前面」。
   */
  prepends: number;
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
  /**
   * 这条回复的时刻，与落盘记录里的是**同一个字符串**（`daemon.py` 收尾帧）。
   * 缺席 = 这一轮没有回复落盘（取消 / 超轮次 / 出错），行上就该没有时间。
   */
  timestamp?: string;
}
export interface ToolStartData {
  request_id?: string;
  tool_call_id: string;
  tool_name: string;
  intent?: string;
  /** daemon 的 `tool_start` 帧本就带 `arguments`（解析好的对象）——此前在这里被丢掉。 */
  arguments?: unknown;
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
  /** 草稿独立变更通道（#1100 次要项：击键不 bump 主版本 → TranscriptView 不因打字重渲染） */
  subscribeDraft(listener: () => void): () => void;
  /** 草稿版本号：仅 setComposerDraft 递增，与主版本互不干扰 */
  getDraftVersion(): number;
  registerSession(sid: string | null): void;
  unregisterSession(sid?: string | null): void;
  st(sid?: string | null): SessionTranscript;
  getEntries(sid?: string | null): TranscriptEntry[];
  getLoadBar(sid?: string | null): string | null;
  /**
   * 该会话发生过多少次前插（单调递增，见 `SessionTranscript.prepends`）。渲染层用它触发
   * 滚差补偿：前插把已有内容整体下移，补偿必须发生在**同一次提交内**，所以需要一个能被
   * `useSyncExternalStore` 读到的、与 entries 分开的信号。
   */
  getPrepends(sid?: string | null): number;
  handleDelta(chunks: DeltaChunk[], sid?: string | null): void;
  handleDone(data: DoneData, sid?: string | null): void;
  handleToolStart(data: ToolStartData, sid?: string | null): void;
  handleToolEnd(data: ToolEndData, sid?: string | null): void;
  clearTyping(sid?: string | null): void;
  clear(sid?: string | null): void;
  /**
   * 追加一条用户消息。`meta.requestId` 让后来到达的 `user_message` / `steer_committed`
   * 帧认领这一行（`stampUserMessage`）；`meta.timestamp` 只有回放那一路用得上 —— 那是
   * 记录里 daemon 写下的时刻，不是客户端时钟（rant 2026-10-17T10:03:08）。
   */
  addUserMessage(text: string, sid?: string | null, meta?: { requestId?: string; timestamp?: string }): void;
  /**
   * 给 `rid` 那一行用户消息补上 daemon 的时刻。找不到该行（例如别的客户端发的消息）
   * 就什么也不做 —— 绝不退化成「给最后一行盖章」。
   */
  stampUserMessage(rid: string, timestamp?: string | null, sid?: string | null): void;
  addSystemMessage(text: string, sid?: string | null): void;
  /**
   * 把一段回放出来的条目整块插到最前（rant 2026-09-20T18:58:44：更早一页由实时 handler
   * 回放成条目，落点在这里）。record→entry 的映射不在本方法里 —— 它只负责落点。
   */
  prependEntries(entries: TranscriptEntry[], sid?: string | null): void;
  setLoadBar(text: string | null, sid?: string | null): void;
  /** 输入框草稿读写（rant 2026-09-01T20:28:31：按 sid 隔离，切视图/切会话不丢） */
  getComposerDraft(sid?: string | null): string;
  setComposerDraft(text: string, sid?: string | null): void;
  toggleRowOutput(sid: string | null, callId: string): void;
  expandRowContent(sid: string | null, callId: string): void;
  toggleGroup(sid: string | null, entryIndex: number): void;
}

const SID_NULL = "__emrg_null_sid__";

function assistantHasText(entry: AssistantEntry): boolean {
  return entry.segments.some((seg) => seg.hasText);
}

/**
 * 工具入参的可读呈现（rant 2026-09-30T09:17:54 → #1787）。
 *
 * 两条来路都要能渲染，所以入参类型是 unknown：
 * - **实时**：`tool_start.arguments` 是 daemon 解析好的对象 → 缩进 JSON；
 * - **回放**：记录里的 `function.arguments` 是 JSON **字符串**（回放已按 daemon 的规矩归一成
 *   对象，这里是第二道保险）→ 先试解析（成功则缩进，失败原样返回，绝不让一条坏记录把整屏拖垮）。
 *
 * 无参（undefined/null/空串/空对象）→ 空串，调用方据此不渲染「输入」段：daemon 对空载荷
 * 与解析失败都发 `{}`（它自己的 `try/except` 落 `{}`），把它画成「输入 {}」是噪音，
 * 而「没有输入」这件事由「没有这一段」表达得更准确 —— 与 TUI 的 `_format_args` 同口径
 * （`if not args: return ""`），也与实时那一路一致（回放同样得 `{}`、同样不渲染）。
 */
export function formatToolArguments(value: unknown): string {
  if (value === undefined || value === null) return "";
  let payload: unknown = value;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return "";
    try {
      payload = JSON.parse(trimmed);
    } catch {
      // 坏载荷（截断/非 JSON）：原样返回，能看见多少是多少
      return value;
    }
  }
  if (payload === null || payload === undefined) return "";
  if (typeof payload === "object" && !Array.isArray(payload) && Object.keys(payload as object).length === 0) {
    return "";
  }
  try {
    const rendered = JSON.stringify(payload, null, 2);
    return rendered === undefined ? String(payload) : rendered;
  } catch {
    return String(payload);
  }
}

/** 更新工具合并组的**摘要**——只数数，不动展开状态（rant 2026-10-17T19:23:08）。
 *
 * 这里曾经按「有没有 running 行」决定收起：`collapsed = !userExpanded`（≥2 行且全部完成）
 * 与 `collapsed = false`（有 running 行）。于是**每一个工具边界都翻转一次**——`tool_end`
 * 收起、`tool_start` 展开——组的高度在 1 行与 N 行之间来回摆，五个工具摆五次，滚动位置跟着跳。
 * `userExpanded` 只保护「宿主亲手展开过」的组，没被点过的组照样每调一次翻一次；而旧测试
 * 恰恰把这个翻转断言为正确行为，所以套件一直是绿的。
 *
 * 规则：**组的展开状态只在宿主点它的 bar 时改变**（`toggleGroup`），别的时候——工具开始、
 * 结束、追加、摘要变大——都不动。新建的组初始是展开的（进行中的工具必须看得见），且此后
 * 不会自己收起：轨道允许一次性的收尾收起，但那是有意的设计选择，不是"每调一次顺手收一下"。
 */
function updateToolGroup(group: ToolGroup): void {
  if (group.rows.length >= 2) {
    const total = group.rows.reduce((acc, r) => acc + (r.elapsed ?? 0), 0);
    group.summary = { count: group.rows.length, totalElapsed: total };
    group.barHidden = false;
  } else {
    // 组内仅 1 行 → 还没有可折叠的东西，bar 不显示
    group.summary = null;
    group.barHidden = true;
  }
}

/**
 * 往 entries 前面插入条目之后，把两张「按 entries 下标索引」的表整体后移。
 *
 * `groupIndex`（rid → 该流当前的助手段）与 `toolRowIndex`（tool_call_id → 条目下标/行号）
 * 存的都是下标；前插而不移位，后续到达的实时 delta / tool_end 就会命中错位的条目。
 * 加载更早一页正是往最前面插入，所以这不是理论问题（rant 2026-09-20T18:58:44 的回放
 * 把它变成必然）。
 */
function shiftIndexes(s: SessionTranscript, delta: number): void {
  for (const [rid, i] of s.groupIndex) s.groupIndex.set(rid, i + delta);
  for (const [callId, loc] of s.toolRowIndex) {
    s.toolRowIndex.set(callId, { entry: loc.entry + delta, row: loc.row });
  }
}

export function createTranscriptStore(opts: { t?: TranslateFn } = {}): TranscriptStore {
  const t = opts.t ?? ((key: string): string => key);
  const sessions = new Map<string, SessionTranscript>();
  let version = 0;
  let draftVersion = 0;
  const listeners = new Set<() => void>();
  const draftListeners = new Set<() => void>();

  function notify(): void {
    version++;
    for (const listener of [...listeners]) listener();
  }

  /** 草稿通道通知：只 bump draftVersion，不碰主版本（打字只重渲染草稿订阅者） */
  function notifyDraft(): void {
    draftVersion++;
    for (const listener of [...draftListeners]) listener();
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
        draft: "",
        groupIndex: new Map(),
        toolRowIndex: new Map(),
        doneRids: new Set(),
        prepends: 0,
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

  /** 草稿变更包装：只通知草稿订阅者（不触发聊天区重渲染） */
  function mutateDraft(fn: () => void): void {
    fn();
    notifyDraft();
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
          const e: AssistantEntry = { kind: "assistant", rid, segments: [] };
          s.entries.push(e);
          s.groupIndex.set(rid, s.entries.length - 1);
        }
        let as = s.entries[s.groupIndex.get(rid)!] as AssistantEntry;
        const active = as.segments[as.segments.length - 1];
        if (!active || active.sealed) {
          if (active && active.sealed) {
            // rant 2026-08-31T12:30:33：上一文本段被工具行“封存”后，本 rid 的后续
            // 文本必须开【新的独立 AssistantEntry】push 到 entries 尾部并更新
            // groupIndex[rid]，而不是在原 entry 内追加 segment —— 否则工具后产生的
            // 文本会经 groupIndex 回挂到工具上方的旧 entry，被渲染在工具上方
            // （“文本→工具”错序：工具后文本永远在工具之上）。新 entry 排在工具行
            // 之后，与 TUI 的到达顺序语义对齐（文本1→工具→文本2）。
            const e: AssistantEntry = { kind: "assistant", rid, segments: [] };
            s.entries.push(e);
            s.groupIndex.set(rid, s.entries.length - 1);
            as = e;
          }
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
            // 收尾帧带来的时刻写到**这一段回复的行**上 —— groupIndex[rid] 指的正是该 rid
            // 最后一个助手条目，工具把一轮回复切成多段时，时刻属于消息而非每段。
            // 帧不带时刻（取消/超轮次/出错，本就没有回复落盘）→ 行上留空。
            if (data.timestamp) entry.timestamp = data.timestamp;
          }
          s.groupIndex.delete(rid); // 渲染完成 → 移除映射（chat.js groupNodes.delete）
        }
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
        // rant 2026-08-28T22:40:33：不再预建空 AssistantEntry。tool_start 不含任何文本，
        // 若在此建一个空 assistant 节点，它会固定在 entries 中被推到所有工具之前，
        // 后续 message_delta 经 groupIndex 找到这个空节点把文本塞进去 → 文本被渲染在
        // 工具上方（「文本 → 全部工具」）。文本只在真正的 message_delta 到达时
        // （handleDelta）才新建 AssistantEntry，使工具行按真实到达顺序进入 entries，
        // 顺序恢复为「工具1→工具2→…→文本」。
        if (entry && entry.kind === "assistant" && assistantHasText(entry)) {
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
        arguments: data.arguments,
        outputExpanded: false,
        fullExpanded: false,
      };
      // rant 21:28:49：连续工具合并 —— 最后条目判定
      // 1) 前一个工具已完成（.tool-row:not(.running)）→ 新建 .tool-group，把旧行移入 rows，新行也入 rows
      // 2) 已是 .tool-group → 新行直接入其 rows（**不**动展开状态：组收起时新工具也留在
      //    收起里；展开只由宿主点 bar 决定，rant 2026-10-17T19:23:08）
      // 3) 其他（文本/用户消息/无条目）→ 独立 .tool-row（文本穿插不合并）
      const last = s.entries[s.entries.length - 1];
      if (last && last.kind === "tool-row" && last.row.status !== "running") {
        const group: ToolGroup = {
          rows: [last.row, row],
          collapsed: false,
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
        // 不展开、不收起了：宿主收起过的组，新工具也保持收起（rant 2026-10-17T19:23:08）。
        // 正在跑的那一行如果要一直看得见，那是另一条"常显"行的事，不是把上面的行翻开的理由。
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
    subscribeDraft: (listener) => {
      draftListeners.add(listener);
      return () => {
        draftListeners.delete(listener);
      };
    },
    getDraftVersion: () => draftVersion,
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
    getPrepends: (sid) => st(sid).prepends,
    getComposerDraft: (sid) => st(sid).draft,
    handleDelta,
    handleDone,
    handleToolStart,
    handleToolEnd,
    clearTyping,
    clear,
    addUserMessage: (text, sid, meta) => {
      mutate(() => {
        const entry: UserEntry = { kind: "user", text };
        if (meta?.requestId) entry.rid = meta.requestId;
        if (meta?.timestamp) entry.timestamp = meta.timestamp;
        st(sid).entries.push(entry);
      });
    },
    stampUserMessage: (rid, timestamp, sid) => {
      if (!rid || !timestamp) return;
      const s = st(sid);
      // 从后往前找：同一个 rid 只可能有一行，但重发过的行可能重复，取最近的那一行。
      for (let i = s.entries.length - 1; i >= 0; i--) {
        const e = s.entries[i];
        if (e.kind === "user" && e.rid === rid) {
          mutate(() => {
            e.timestamp = timestamp;
          });
          return;
        }
      }
      // 找不到就不动：`st(sid)` 已经在上面建过桶，不该为了补一个时刻而触发一次渲染。
    },
    addSystemMessage: (text, sid) => {
      mutate(() => {
        st(sid).entries.push({ kind: "system", text });
      });
    },
    prependEntries: (entries, sid) => {
      mutate(() => {
        const s = st(sid);
        if (!entries.length) return;
        s.prepends++;
        shiftIndexes(s, entries.length);
        s.entries.unshift(...entries);
      });
    },
    setLoadBar: (text, sid) => {
      mutate(() => {
        st(sid).loadBar = text;
      });
    },
    setComposerDraft: (text, sid) => {
      // #1100 次要项：草稿写走独立通道，不 bump 主版本 → 打字不重渲染 TranscriptView
      mutateDraft(() => {
        st(sid).draft = text;
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
        // 唯一会改变展开状态的地方（rant 2026-10-17T19:23:08）：一个布尔量，一个写入点。
        entry.group.collapsed = !entry.group.collapsed;
      });
    },
  };
}
