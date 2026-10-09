import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import {
  formatToolArguments,
  type AssistantEntry,
  type AssistantSegment,
  type ToolGroup,
  type ToolRow,
  type TranscriptEntry,
  type TranscriptStore,
} from "../lib/transcript";
import { createMarkdownRenderer, type MarkdownRenderer, type StreamState } from "../lib/markdown";
import { scrollCompensation } from "../lib/history";
import { toolPhrases } from "../lib/copywriting";
import { useI18n, type TranslateFn } from "../lib/i18n";
import { MarkdownText } from "./MarkdownText";

/**
 * TranscriptView — 聊天区 React 渲染组件（Batch 2，设计 §5 Batch 2 项 1–3）。
 * 消费 lib/transcript.ts 纯状态机（每会话独立状态桶 P3），消息/工具行/合并组
 * 全部数据驱动渲染。与 chat.js 的 DOM 结构逐类对齐（.msg.user / .msg.assistant /
 * .tool-row / .tool-group / .history-load-bar 等类名不变，Batch 5 切换时复用
 * vanilla CSS，无需改样式表）。
 *
 * - 流式（typing）→ 块投影即时 markdown 渲染（rant 2026-09-02T21:07:35 方案 B：引擎
 *   markdown.ts streamProject 把已稳定块完整渲染并缓存 DOM，尾部 live 块只渲染已稳定
 *   部分；未闭合代码围栏保持纯文本，与 TUI fence_count%2 启发式一致——流式期间不再
 *   整段等 done 才格式化，与 TUI / react-markdown 竞品对齐）。done → streamFinalize
 *   全量校正；渲染器无 marked / 投影异常 → 回退纯文本 + renderMarkdown 整体渲染（旧
 *   路径；vendor marked/DOMPurify/hljs 构造注入，无 vendor 时降级转义）。✦ 标记独立于
 *   渲染文本，避免破坏块语法解析。
 * - 工具行：running 转圈 → done ✓ / failed；耗时 · Ns（仅成功）；输出默认隐藏，
 *   点击行展开（G91/G131 >2000 字符截断 + 展开全文按钮）。
 * - 合并组：bar 摘要（chat.toolGroupSummary）+ 收起/展开，user-expanded 后不再自动收起。
 * - 本组件不含滚动/欢迎屏副作用（Batch 5 接线时由宿主容器负责）。
 */

export interface TranscriptViewProps {
  store: TranscriptStore;
  sid?: string | null;
  /** 注入 markdown 渲染器（测试/浏览器接线用；缺省按 t 构造降级渲染器） */
  renderer?: MarkdownRenderer;
  /** 滚动到顶加载更早历史（rant 2026-09-01T20:19:40）：hasMore && !loading 时允许触发 */
  canLoadOlder?: boolean;
  /** 滚动到顶 / 点击顶部条的回调（触发方防抖；vanilla loadOlderHistory 语义） */
  onLoadOlder?: () => void;
}

export function TranscriptView({ store, sid = null, renderer, canLoadOlder = false, onLoadOlder }: TranscriptViewProps) {
  // 版本号快照：每次 store 变更 +1（getSnapshot 稳定引用，满足 useSyncExternalStore 要求）
  const version = useSyncExternalStore(store.subscribe, store.getVersion);
  const { t } = useI18n();
  const md = useMemo(() => renderer ?? createMarkdownRenderer({ t }), [renderer, t]);
  const entries = store.getEntries(sid);
  const loadBar = store.getLoadBar(sid);

  void version; // 版本号变化 → 重新读取 entries（同一数组引用被原地变更）

  // ── 滚动跟随（rant 2026-08-28T22:36:18：React 迁移丢失 vanilla 的 autoScroll/回到底部） ──
  // React 版之前无任何 scroll/autoScroll 追踪 → 新消息不断落到底部遮罩区，需手动滚动。
  // 以下镜像 vanilla app.js:773-776（autoScroll 标志 + 底部 40px 容差）与 app.js:1711-1719
  // （回到底部悬浮按钮）。scroll 事件不冒泡 → 用 capture:true 监听（vanilla 同）。
  const viewportRef = useRef<HTMLDivElement>(null);
  // autoScroll 用 ref（滚动高频，避免每帧 setState 重渲染）；atBottom 用 state（驱动按钮显隐）
  const autoScrollRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);

  const updateAutoScroll = useCallback((el: HTMLDivElement) => {
    const ab = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
    autoScrollRef.current = ab;
    setAtBottom(ab);
  }, []);

  // scroll 监听（capture，因 scroll 不冒泡；挂在 .transcript-view 自身上，捕获子元素滚动）
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    const onScroll = () => {
      updateAutoScroll(el);
      // 滚动到顶 → 加载更早一页（rant 2026-09-01T20:19:40：scrollTop<=2 && canLoadOlder；
      // 防抖由 onLoadOlder 触发方负责）
      if (onLoadOlder && canLoadOlder && el.scrollTop <= 2) {
        onLoadOlder();
      }
    };
    el.addEventListener("scroll", onScroll, { capture: true });
    return () => el.removeEventListener("scroll", onScroll, { capture: true });
  }, [updateAutoScroll, canLoadOlder, onLoadOlder]);

  // 新消息 append/流入 → 若 autoScroll 为 true 则滚到底。
  // 依赖 version（每次 store 变更 +1）而非 entries.length：流式文本是原地 append
  // 进已有 assistant 段（数组长度不变），依赖 length 会让流式期间不跟随 → 新消息
  // 落到底部遮罩区。version 变化覆盖 delta/tool/done 全部写入，autoScroll 为真即贴底。
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    if (autoScrollRef.current) {
      el.scrollTop = el.scrollHeight;
    }
    // 首次无条目时不误滚；version 变更即触发
  }, [version]);

  const scrollToBottom = () => {
    const el = viewportRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    autoScrollRef.current = true;
    setAtBottom(true);
  };

  // ── 前插的滚差补偿（宿主报障 2026-10-08 的第二半：上翻一页后视觉位置跳掉一屏） ──
  // vanilla `app.js` 的 historyPages 在**插入更早一页之前**读 `scrollTop`/`scrollHeight`，
  // 插入之后 `scrollTop = prevScrollTop + (scrollHeight - prevHeight)`——`lib/history.ts`
  // 的模块注释逐字记着这个式子。React 迁移只搬走了那个**纯函数**：`scrollCompensation`
  // 至今只有单测、零生产调用者，而 `Shell.tsx` 的 `loadOlderHistory` 注释一直声称
  // 「整块前插 + 滚差补偿由渲染层处理」。这里就是那个渲染层。
  //
  // 「插入前的度量」只能在 **render 期**读。React 在 render 返回之后才改 DOM，所以渲染期
  // 读到的还是上一屏；`useLayoutEffect` 里读到的 `scrollHeight` 已经是插入后的，差值恒为 0。
  // 也不能用「上一次提交时记下的 scrollTop」代替：用户上翻浏览**不产生 store 提交**
  // （滚动只翻 atBottom 这一个 state，且只在真假翻转时才重渲染），那个值可能停在几分钟前
  // 的底部位置上，据此补偿会把用户直接甩回底部 —— 比不补偿更糟。
  //
  // 为什么必须用 layout effect：补偿要在「DOM 已插入更早一页、浏览器还没绘制」之间完成，
  // 晚一帧就是用户看见的一次跳动。
  //
  // 补偿后重算 autoScroll：补偿把视口从底部移开时，紧随其后的追加若仍按 autoScroll=true
  // 落到 `scrollHeight`，刚加载出来的一页会被立刻甩到底部——补偿白做。「回到底部」按钮
  // 的显隐由同一个判断驱动，所以两者始终一致。
  const prepends = store.getPrepends(sid);
  const sidKey = sid ?? "";
  const lastSidRef = useRef(sidKey);
  const lastPrependRef = useRef(prepends);
  const preCommitRef = useRef<{ scrollTop: number; scrollHeight: number } | null>(null);
  const viewportEl = viewportRef.current;
  if (viewportEl) {
    // 渲染期只读 DOM（不写），写的是自己的 ref：这就是 React 对 getSnapshotBeforeUpdate 的替代
    preCommitRef.current = { scrollTop: viewportEl.scrollTop, scrollHeight: viewportEl.scrollHeight };
  }

  useLayoutEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    if (lastSidRef.current !== sidKey) {
      // 切会话：另一个桶的计数与本桶无关，重置而不是当成一次前插。
      lastSidRef.current = sidKey;
      lastPrependRef.current = prepends;
    } else if (prepends !== lastPrependRef.current) {
      lastPrependRef.current = prepends;
      const prev = preCommitRef.current;
      if (prev) {
        const next = scrollCompensation(prev.scrollTop, prev.scrollHeight, el.scrollHeight);
        if (next !== el.scrollTop) el.scrollTop = next;
        updateAutoScroll(el);
      }
    }
  }, [version, prepends, sidKey, updateAutoScroll]);

  return (
    <div className="transcript-view" data-testid="transcript-view" ref={viewportRef}>
      {loadBar ? (
        // 顶部历史条（宿主报障 2026-10-08：点击「↑ 加载更早消息」没有任何效果）。CSS 一直写着
        // `cursor: pointer`、文案也一直是祈使句（"↑ 加载更早消息"），但它此前只是纯文本 div——
        // 可点的样子、不可点的实体。`canLoadOlder`（hasMore && !loading）为真时它是**真按钮**
        // （键盘可达、点击即触发与滚动到顶同一个防抖回调）；为假时（加载中 / 没有更多）退化为
        // 状态文字，不再冒充控件。
        canLoadOlder && onLoadOlder ? (
          <button
            type="button"
            className="history-load-bar"
            data-testid="history-load-bar"
            onClick={onLoadOlder}
          >
            {loadBar}
          </button>
        ) : (
          <div className="history-load-bar" data-testid="history-load-bar">
            {loadBar}
          </div>
        )
      ) : null}
      {entries.map((entry, i) => (
        <EntryView key={i} entry={entry} index={i} t={t} md={md} store={store} sid={sid} />
      ))}
      {!atBottom ? (
        <button
          type="button"
          className="transcript-back-to-bottom"
          data-testid="back-to-bottom"
          onClick={scrollToBottom}
          title={t("chat.backToBottom")}
        >
          ↓ {t("chat.backToBottom")}
        </button>
      ) : null}
    </div>
  );
}

interface EntryViewProps {
  entry: TranscriptEntry;
  index: number;
  t: TranslateFn;
  md: MarkdownRenderer;
  store: TranscriptStore;
  sid: string | null;
}

function EntryView({ entry, index, t, md, store, sid }: EntryViewProps): ReactNode {
  switch (entry.kind) {
    case "user":
      // Stage 1（rant 2026-08-28T14:07:29）：用户消息为 markdown 字符串（tiptap 序列化），
      // 与 TUI 的 UserMarkdown 对齐渲染——否则 **bold** 会字面显示
      return (
        <div className="msg user">
          <MarkdownText text={entry.text} md={md} stripMark={false} />
        </div>
      );
    case "system":
      return <div className="msg system">{entry.text}</div>;
    case "assistant":
      return <AssistantView entry={entry} t={t} md={md} />;
    case "tool-row":
      return <ToolRowView row={entry.row} t={t} store={store} sid={sid} />;
    case "tool-group":
      return <ToolGroupView group={entry.group} entryIndex={index} t={t} store={store} sid={sid} />;
  }
}

/** 助手消息：每文本段一个 .msg.assistant 节点（chat.js createAssistantNode 语义） */
function AssistantView({ entry, t, md }: { entry: AssistantEntry; t: TranslateFn; md: MarkdownRenderer }) {
  return (
    <>
      {entry.segments.map((seg, i) => (
        <AssistantSegmentView key={i} entry={entry} segment={seg} t={t} md={md} />
      ))}
    </>
  );
}

/**
 * 助手文本段（每 rid 内按工具封存分段）。
 * typing（流式）期间：React 只挂一个空 host 节点，块投影引擎（markdown.ts
 * streamProject）把「✦ + div.md-stream（稳定块缓存 + 尾部 live 块）」增量写进 host——
 * 已闭合的段落/列表/代码围栏即时按 markdown 渲染，未闭合围栏（esp. 代码围栏）保持
 * 纯文本，不再等 done 才格式化。渲染器无 marked / 投影异常（返回 false）→ 回退纯文本
 * 追加（旧行为）。done/封存：project 模式 streamFinalize 一次性全量校正；plain 模式由
 * MarkdownText 整体渲染兜底。
 */
function AssistantSegmentView({
  entry,
  segment,
  t,
  md,
}: {
  entry: AssistantEntry;
  segment: AssistantSegment;
  t: TranslateFn;
  md: MarkdownRenderer;
}) {
  // 块投影 host：引擎把「✦ + .md-stream」写入此节点（streamProject 返回 true 后显示；
  // React 不管理其 children，避免与引擎增量 DOM 冲突）
  const hostRef = useRef<HTMLDivElement | null>(null);
  const stateRef = useRef<StreamState | null>(null);
  const projectedRef = useRef<boolean | null>(null); // null=未探测
  const [projected, setProjected] = useState<boolean | null>(null);

  // 流式每帧：把累计文本投影进 host。useLayoutEffect → 投影发生在浏览器绘制前，
  // typing 首帧即显示渲染结果（无「先纯文本后跳变」闪烁）。
  useLayoutEffect(() => {
    if (!segment.typing) return;
    const el = hostRef.current;
    if (!el) return;
    if (projectedRef.current === false) return; // 引擎不可用 → React 侧纯文本回退
    if (!stateRef.current) stateRef.current = { stableCount: 0, rawText: "" };
    let ok = false;
    try {
      ok = md.streamProject(el, segment.text, stateRef.current);
    } catch {
      ok = false;
    }
    if (!ok) {
      // 投影失败：清掉可能的部分写入，回退纯文本（assistant-plain 显示完整文本）
      stateRef.current = null;
      el.innerHTML = "";
    }
    if (projectedRef.current !== ok) {
      projectedRef.current = ok;
      setProjected(ok);
    }
  }, [segment.typing, segment.text, md]);

  // done/封存（typing 移除）：project 模式 → streamFinalize 把 live 块一次性校正为全量
  // markdown（与旧 renderMarkdown 整体渲染同源）；plain 模式由 MarkdownText 兜底。
  useEffect(() => {
    if (segment.typing || projectedRef.current !== true) return;
    const el = hostRef.current;
    if (!el) return;
    md.streamFinalize(el, segment.text).catch(() => {
      /* finalize 失败不阻断（引擎内部已有转义降级） */
    });
  }, [segment.typing, segment.text, md]);

  return (
    <div className="msg assistant">
      <div className={segment.typing ? "msg-body typing" : "msg-body"}>
        <div
          ref={hostRef}
          className="md-stream-host"
          style={{ display: projected === true ? undefined : "none" }}
        />
        {projected === true ? null : (
          <>
            <span className="msg-assistant-mark">✦ </span>
            {segment.typing ? (
              // 流式纯文本（chat.js 无投影时的 textContent 追加路径——引擎不可用时回退）
              <span className="assistant-plain">{segment.text}</span>
            ) : (
              <MarkdownText text={segment.text} md={md} />
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** done 后整体 markdown 渲染（与旧 done 渲染同源 renderMarkdown；✦ 标记剥离仅限助手消息）
 *  实现已提为共用组件 MarkdownText（rant 2026-09-30T09:06:16 / #1763：Rant 详情同路径）。 */

/** 工具友好状态行（进行中 → 完成/失败，默认折叠，点开展示原始输出） */
function ToolRowView({
  row,
  t,
  store,
  sid,
}: {
  row: ToolRow;
  t: TranslateFn;
  store: TranscriptStore;
  sid: string | null;
}) {
  const phrases = toolPhrases(row.toolName, t);
  const label =
    row.status === "running" ? phrases.doing : row.status === "done" ? phrases.done : t("tool.failText");
  const ok = row.status === "done";
  const timeText = ok && row.elapsed !== undefined ? `· ${row.elapsed.toFixed(1)}s` : null;
  const title =
    ok && row.elapsed !== undefined ? t("chat.elapsed", { s: row.elapsed.toFixed(1) + "s" }) : undefined;
  const content = row.content || "";
  const truncated = content.length > 2000 ? content.slice(0, 2000) + "…" : content;
  // rant 2026-09-30T09:17:54 → #1787：折叠行 = 工具名称 + intent（与 TUI 一致），
  // 展开 = 名称 / 输入 / 输出三段。名称此前从未渲染（row.toolName 存着却不用），
  // 输入则被 handleToolStart 丢掉。入参超 2000 字沿用输出的同一套截断 + 展开全文
  // （rant 明写「超过 2000 字沿用现有截断」），共用 fullExpanded 这个展开态。
  const argsFull = formatToolArguments(row.arguments);
  const args = argsFull.length > 2000 && !row.fullExpanded ? argsFull.slice(0, 2000) + "…" : argsFull;
  const showExpand =
    !row.fullExpanded && (content.length > 2000 || argsFull.length > 2000);

  return (
    <div
      className={`tool-row ${row.status}${row.outputExpanded ? " expanded" : ""}`}
      title={title}
      onClick={() => store.toggleRowOutput(sid, row.callId)}
    >
      {row.status === "running" ? <span className="tool-spinner" /> : null}
      {ok ? <span className="tool-check">✓ </span> : null}
      <span className="tool-name">{row.toolName}</span>
      <span className="tool-label">{label}</span>
      {row.intent ? <span className="tool-intent">{row.intent}</span> : null}
      {timeText ? <span className="tool-time">{timeText}</span> : null}
      <span className="tool-chevron">⌄</span>
      <div className={`tool-output ${row.outputExpanded ? "" : "hidden"}`}>
        <div className="tool-detail">
          <span className="tool-detail-title">{t("tool.detailName")}</span>
          <div className="tool-detail-body">{row.toolName}</div>
        </div>
        {args ? (
          <div className="tool-detail">
            <span className="tool-detail-title">{t("tool.detailInput")}</span>
            <pre className="tool-detail-body">{args}</pre>
          </div>
        ) : null}
        {content ? (
          <div className="tool-detail">
            <span className="tool-detail-title">{t("tool.detailOutput")}</span>
            <div className="tool-detail-body">{row.fullExpanded ? content : truncated}</div>
          </div>
        ) : null}
      </div>
      {showExpand ? (
        <button
          type="button"
          className="tool-expand-btn"
          onClick={(e) => {
            e.stopPropagation();
            store.expandRowContent(sid, row.callId);
          }}
        >
          {t("chat.expand")}
        </button>
      ) : null}
    </div>
  );
}

/** 连续工具合并组：bar 摘要 + 收起/展开（rant 21:28:49） */
function ToolGroupView({
  group,
  entryIndex,
  t,
  store,
  sid,
}: {
  group: ToolGroup;
  entryIndex: number;
  t: TranslateFn;
  store: TranscriptStore;
  sid: string | null;
}) {
  return (
    <div
      className={`tool-group ${group.collapsed ? "collapsed" : ""}`}
      data-user-expanded={group.userExpanded ? "1" : undefined}
    >
      <div
        className={`tool-group-bar ${group.barHidden ? "hidden" : ""}`}
        onClick={() => store.toggleGroup(sid, entryIndex)}
      >
        <span className="tool-group-chev">⌄</span>
        <span className="tool-group-summary">
          {group.summary
            ? t("chat.toolGroupSummary", {
                count: group.summary.count,
                time: group.summary.totalElapsed.toFixed(1) + "s",
              })
            : ""}
        </span>
      </div>
      <div className="tool-group-rows">
        {group.rows.map((row) => (
          <ToolRowView key={row.callId} row={row} t={t} store={store} sid={sid} />
        ))}
      </div>
    </div>
  );
}
