/**
 * historyReplay.ts — 把 `list_history` 的落盘记录喂进**实时那一套 handler**。
 *
 * rant 2026-09-20T18:58:44：一个会话「打开后回放出来的历史」必须与它「一直开着时收到的
 * 实时展示」逐项一致。做法不是另写一套 record→entry 映射，而是复用实时的 handler
 * （`addUserMessage` / `handleDelta` / `handleToolStart` / `handleToolEnd` / `handleDone`），
 * 记录只负责把实时帧里由 daemon 提供、落盘记录里没有的那几样补出来：
 *
 * 1. **一轮对话的 rid**（实时 = daemon 的 `request_id`）：落盘记录不带它，这里按「一条用户
 *    消息开启一轮」合成 `hist-<record_index>`。同轮的所有助手文本必须共用一个 rid，
 *    `handleDelta` 才会把同一段文本续写进同一个条目。
 * 2. **工具 start/end 的交错顺序**：实时是逐个工具 `start→end→start→end`（daemon 每执行完
 *    一个工具就播一次 `tool_end`），而落盘顺序是「助手记录（带全部 tool_calls）+ 全部
 *    tool_result」。照落盘顺序直发会得到 `start c1 → start c2 → end c1 → end c2`，于是
 *    c2 的 start 撞上一条**仍在 running 的行** → 独立成行，而实时那一路会合并成一个组。
 *    所以这里按 `tool_call_id` 配对后交错投递。
 * 3. **一轮结尾的 done**（实时 = daemon 的 `done` 帧）：补一个 `handleDone`，让该轮的文本段
 *    封口（typing 停止），与实时完全同步。
 *
 * 不补的东西：`⏸ Interrupted` / 排队 / 达轮次上限这类系统提示**从不落盘**（宿主
 * 2026-09-20 裁定维持现状），回放路径也因此不会凭空造出它们。
 */

import { createTranscriptStore, type TranscriptStore } from "./transcript";
import { t } from "./i18n";

/** 落盘记录里的一条助手/用户消息（daemon `include_records` 的 `kind: "message"`）。 */
export interface HistoryMessageRecord {
  record_index?: number;
  /** daemon 给每条记录打的类别（判别字段，两个分支靠它收窄） */
  kind: "message";
  role?: string;
  content?: string;
  timestamp?: string;
  /**
   * `call_label` 不在落盘记录里：daemon 在 **`list_history` 读取时**把它算进每一项的副本
   * （`daemon._tool_call_labels`），所以这个字段只在「已被投影过的记录」上出现。类型放宽
   * 是因为这就是回放看到的东西，不是要写进磁盘（rant 2026-10-20T12:22:21）。
   */
  tool_calls?: Array<{
    id?: string;
    function?: { name?: string; arguments?: string };
    call_label?: string;
  }>;
}

/** 落盘记录里的一条工具结果（daemon `include_records` 的 `kind: "tool_result"`）。 */
export interface HistoryToolResultRecord {
  record_index?: number;
  kind: "tool_result";
  tool_call_id?: string;
  tool_name?: string;
  content?: string;
  error?: boolean;
}

export type HistoryRecord = HistoryMessageRecord | HistoryToolResultRecord | HistorySummaryRecord;

/**
 * 落盘记录里的一次压缩（daemon `include_records` 的 `kind: "summary"`，#2351）。
 *
 * 实时那一路是 `compact_result` → 一条系统提示；落盘记录里压缩边界是**一条记录**，
 * 若回放跳过它，重开一个会话就看不到自己亲眼见过的「已压缩」这一行。
 */
export interface HistorySummaryRecord {
  record_index?: number;
  kind: "summary";
  content?: string;
  timestamp?: string;
  compacted_message_count?: number;
}

/** 回放写入端：实时那一组 handler（TranscriptStore 直接满足）。 */
export interface ReplaySink {
  /**
   * `meta.timestamp` 是**记录里** daemon 写下的时刻（rant 2026-10-17T10:03:08）。回放把它
   * 原样交给 store，于是重开一个会话看到的时间与一直开着时同一条消息看到的一样 ——
   * 客户端的时钟从头到尾没有参与。
   */
  addUserMessage(text: string, sid?: string | null, meta?: { requestId?: string; timestamp?: string }): void;
  handleDelta(chunks: Array<{ request_id?: string; content?: string }>, sid?: string | null): void;
  handleDone(
    data: { request_id?: string; content?: string; timestamp?: string },
    sid?: string | null,
  ): void;
  handleToolStart(
    data: { request_id?: string; tool_call_id: string; tool_name: string; intent?: string; arguments?: unknown; call_label?: string },
    sid?: string | null,
  ): void;
  handleToolEnd(
    data: { tool_call_id: string; tool_name?: string; content?: string; error?: unknown },
    sid?: string | null,
  ): void;
  /**
   * 一条系统提示行（#2351）。实时那一路由 `DialogHost` 在 `compact_result` 上调用，
   * 回放那一路由 `replayHistoryRecords` 调用——两条路写的是同一个入口，所以重开一个
   * 会话看到的压缩提示与一直开着时那一行是同一行。
   */
  addSystemMessage(text: string, sid?: string | null): void;
}

/** 助手记录的 `tool_calls[].function.arguments` 里的 intent（daemon 记日志用的就是它）。 */
export function toolCallIntent(args?: string): string {
  if (!args) return "";
  try {
    const parsed = JSON.parse(args) as { intent?: unknown };
    return typeof parsed?.intent === "string" ? parsed.intent : "";
  } catch {
    // 参数不是合法 JSON（截断/非 JSON 载荷）：实时那一路同样拿不到 intent（daemon 也
    // 只在自己的 try 里取），这里同样留空，绝不让回放因一条坏记录整页失败。
    return "";
  }
}

/**
 * 助手记录的 `tool_calls[].function.arguments`（JSON 字符串）→ 实时帧里的那个形状。
 *
 * 实时那一路 daemon 已经解析好（`daemon.py`：`json.loads(tc_args_str) if tc_args_str else {}`，
 * 失败也落在 `{}`），所以 `tool_start.arguments` 是**对象**。回放若把记录里的**字符串**
 * 原样塞进行里，同一件事就有两种类型，「打开旧会话与一直开着逐项一致」这条不变式只能靠
 * 比较时放水来维持——那是把守卫改软，不是修好（#1787）。
 *
 * 因此这里照 daemon 的规矩归一：解析成功 → 解析值；空/坏载荷 → `{}`（与实时同一结果）。
 * 呈现仍统一走 `formatToolArguments`（它同时兜住字符串，多一层保险）。
 */
export function toolCallArguments(args?: string): unknown {
  if (!args) return {};
  try {
    return JSON.parse(args);
  } catch {
    // 截断/非 JSON 载荷：实时那一路 daemon 同样落在 {}，这里给同一个结果。
    return {};
  }
}

/**
 * 按记录顺序回放一页记录（顺序即 `record_index` 顺序；daemon 已保证切点不落在
 * 「助手记录 + 它的 tool_result」中间）。
 */
export function replayHistoryRecords(
  records: HistoryRecord[],
  sid: string | null,
  sink: ReplaySink,
): void {
  // 工具结果按 tool_call_id 配对：实时的 tool_end 紧跟它自己的 tool_start，
  // 而落盘顺序把全部结果排在助手记录之后（见文件头第 2 条）。
  const results = new Map<string, HistoryToolResultRecord>();
  for (const r of records) {
    if (r.kind === "tool_result" && r.tool_call_id) results.set(r.tool_call_id, r);
  }
  const delivered = new Set<string>();
  let rid: string | null = null;
  let turnOpen = false;

  let assistantStamp: string | undefined;

  const closeTurn = (): void => {
    if (rid && turnOpen) {
      sink.handleDone(
        assistantStamp ? { request_id: rid, timestamp: assistantStamp } : { request_id: rid },
        sid,
      );
    }
    turnOpen = false;
    assistantStamp = undefined;
  };

  for (const r of records) {
    if (r.kind === "message" && r.role === "system") {
      // 落盘写下的系统提示（`Session.clear` 的 `[Session cleared]`，#2351）。它的正文
      // 就是记录自己的文本：既不翻译、也不改写——实时那一路 `/clear` 之后屏幕上也
      // 只有这一句话，而回放如果换一个说法，两条路就又分叉了。
      // 不在这里 closeTurn：压缩可以在工具轮次之间发生（auto-compact），实时那一路
      // 那时**没有** done 帧，凭空补一个会把这一段助手文本提前封口。
      sink.addSystemMessage(r.content ?? "", sid);
      continue;
    }

    if (r.kind === "summary") {
      // 压缩边界（#2351）。实时那一行由 `DialogHost` 的 `compact_result` 分支写，
      // 文案是 `app.compacted`；这里调同一个入口、用同一个键，于是同一行。
      sink.addSystemMessage(t("app.compacted"), sid);
      continue;
    }

    if (r.kind === "message" && r.role === "user") {
      // 实时顺序：上一轮的 done 先到，用户才接着发下一条。
      closeTurn();
      rid = `hist-${r.record_index ?? "u"}`;
      // 用户行的时刻来自记录本身（此前 `timestamp` 类型上有、映射处被丢掉）。
      // meta 与实时那一路同形状：id 一定有，时刻有才写 —— 不塞 `timestamp: undefined`,
      // 否则同一个「还没有时刻」在两路上是两个不同的对象。
      const meta: { requestId?: string; timestamp?: string } = { requestId: rid };
      if (r.timestamp) meta.timestamp = r.timestamp;
      sink.addUserMessage(r.content ?? "", sid, meta);
      continue;
    }

    if (r.kind === "message" && r.role === "assistant") {
      if (!rid) rid = `hist-${r.record_index ?? "a"}`; // 页首截断在一轮中间
      const content = r.content ?? "";
      // 空正文的助手记录（只带 tool_calls 的那一轮）不发 delta —— 实时的 message_delta
      // 也只在有文本时到达，发一个空 chunk 会凭空造出一个空助手气泡。
      if (content) {
        sink.handleDelta([{ request_id: rid, content }], sid);
        turnOpen = true;
      }
      // 这一轮回复的时刻：收尾的 done 带上它，行上才显示得出来。空正文的助手记录
      // （只带 tool_calls 的那种）没有可见的行，记下来也无处显示，跳过。
      if (content && r.timestamp) assistantStamp = r.timestamp;
      for (const tc of r.tool_calls ?? []) {
        const callId = tc?.id ?? "";
        const name = tc?.function?.name ?? "";
        sink.handleToolStart(
          {
            request_id: rid,
            tool_call_id: callId,
            tool_name: name,
            intent: toolCallIntent(tc?.function?.arguments),
            // rant 2026-09-30T09:17:54 → #1787：这里此前只抽 intent，把 arguments 丢掉了，
            // 于是「打开旧会话」的折叠行与展开详情与实时不一致。归一成实时那一形状后透传
            // （截断/非法 JSON 由 toolCallArguments 与 formatToolArguments 兜底）。
            arguments: toolCallArguments(tc?.function?.arguments),
            // daemon 的历史投影在每一项 tool_calls 上算好了 `call_label`（读时算的，
            // 旧记录也有）——回放只是透传，走的是同一个 handleToolStart
            // （rant 2026-10-20T12:22:21）。
            call_label: tc?.call_label,
          },
          sid,
        );
        const res = results.get(callId);
        if (res) {
          sink.handleToolEnd(
            {
              tool_call_id: callId,
              tool_name: res.tool_name || name,
              content: res.content ?? "",
              error: res.error ?? false,
            },
            sid,
          );
          delivered.add(callId);
        }
      }
      continue;
    }

    if (r.kind === "tool_result" && r.tool_call_id && !delivered.has(r.tool_call_id)) {
      // 落单的结果（其助手记录不在本页）：实时那一路照样会播它的 tool_end。
      sink.handleToolEnd(
        {
          tool_call_id: r.tool_call_id,
          tool_name: r.tool_name ?? "",
          content: r.content ?? "",
          error: r.error ?? false,
        },
        sid,
      );
    }
  }

  closeTurn();
}

/**
 * 更早一页：回放出来的条目要整体插到**最前**，而实时 handler 只会往后 append。
 *
 * 所以先在一个独立的 store 实例里跑**同一套 handler**（缓冲，无订阅者 → 不触发任何渲染），
 * 再把得到的条目整块 `prependEntries`。多出来的只有落点，映射仍是实时那一份。
 * 独立实例用默认翻译器即可：唯一会用到翻译的分支是 `handleDone` 的「达轮次上限」提示，
 * 而回放的 done 不带 content，那个分支不可能触发。
 */
export function replayHistoryRecordsPrepend(
  records: HistoryRecord[],
  sid: string | null,
  transcript: TranscriptStore,
): number {
  const scratch = createTranscriptStore();
  replayHistoryRecords(records, sid, scratch);
  const entries = scratch.getEntries(sid);
  if (entries.length) transcript.prependEntries(entries, sid);
  return entries.length;
}
