import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import {
  type AssistantEntry,
  type AssistantSegment,
  type ToolGroup,
  type ToolRow,
  type TranscriptEntry,
  type TranscriptStore,
} from "../lib/transcript";
import { createMarkdownRenderer, type MarkdownRenderer } from "../lib/markdown";
import { toolPhrases } from "../lib/copywriting";
import { useI18n, type TranslateFn } from "../lib/i18n";

/**
 * TranscriptView — 聊天区 React 渲染组件（Batch 2，设计 §5 Batch 2 项 1–3）。
 * 消费 lib/transcript.ts 纯状态机（每会话独立状态桶 P3），消息/工具行/合并组
 * 全部数据驱动渲染。与 chat.js 的 DOM 结构逐类对齐（.msg.user / .msg.assistant /
 * .tool-row / .tool-group / .history-load-bar 等类名不变，Batch 5 切换时复用
 * vanilla CSS，无需改样式表）。
 *
 * - 流式（typing）→ 纯文本；done → createMarkdownRenderer 整体渲染（与旧
 *   done 渲染同源 renderMarkdown；vendor marked/DOMPurify/hljs 构造注入，
 *   无 vendor 时降级转义）。✦ 标记独立于渲染文本，避免破坏块语法解析。
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
}

export function TranscriptView({ store, sid = null, renderer }: TranscriptViewProps) {
  // 版本号快照：每次 store 变更 +1（getSnapshot 稳定引用，满足 useSyncExternalStore 要求）
  const version = useSyncExternalStore(store.subscribe, store.getVersion);
  const { t } = useI18n();
  const md = useMemo(() => renderer ?? createMarkdownRenderer({ t }), [renderer, t]);
  const entries = store.getEntries(sid);
  const loadBar = store.getLoadBar(sid);

  void version; // 版本号变化 → 重新读取 entries（同一数组引用被原地变更）

  return (
    <div className="transcript-view" data-testid="transcript-view">
      {loadBar ? <div className="history-load-bar">{loadBar}</div> : null}
      {entries.map((entry, i) => (
        <EntryView key={i} entry={entry} index={i} t={t} md={md} store={store} sid={sid} />
      ))}
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
    case "history":
      // 历史消息同 markdown 渲染（旧纯文本是合法 markdown，回显不损坏）
      return (
        <div className="msg user history">
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
  return (
    <div className="msg assistant">
      {!entry.isOwn ? <div className="remote-label">{t("chat.fromOtherClient")}</div> : null}
      <div className={segment.typing ? "msg-body typing" : "msg-body"}>
        <span className="msg-assistant-mark">✦ </span>
        {segment.typing ? (
          // 流式纯文本（chat.js 无投影时的 textContent 追加路径；投影优化在浏览器接线批次）
          <span className="assistant-plain">{segment.text}</span>
        ) : (
          <MarkdownText text={segment.text} md={md} />
        )}
      </div>
    </div>
  );
}

/** done 后整体 markdown 渲染（与旧 done 渲染同源 renderMarkdown；✦ 标记剥离仅限助手消息） */
function MarkdownText({ text, md, stripMark = true }: { text: string; md: MarkdownRenderer; stripMark?: boolean }) {
  const [html, setHtml] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    const src = stripMark ? text.replace(/^✦\s*/, "") : text;
    md.renderMarkdown(src).then((h) => {
      if (!cancelled) setHtml(h);
    });
    return () => {
      cancelled = true;
    };
  }, [text, md, stripMark]);
  if (html === null) return <span className="assistant-plain">{text}</span>;
  return <span className="assistant-html" dangerouslySetInnerHTML={{ __html: html }} />;
}

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
  const showExpand = content.length > 2000 && !row.fullExpanded;

  return (
    <div
      className={`tool-row ${row.status}`}
      title={title}
      onClick={() => store.toggleRowOutput(sid, row.callId)}
    >
      {row.status === "running" ? <span className="tool-spinner" /> : null}
      {ok ? <span className="tool-check">✓ </span> : null}
      <span className="tool-label">{label}</span>
      {row.intent ? <span className="tool-intent">{row.intent}</span> : null}
      {timeText ? <span className="tool-time">{timeText}</span> : null}
      <span className="tool-chevron">⌄</span>
      {content ? (
        <div className={`tool-output ${row.outputExpanded ? "" : "hidden"}`}>
          {row.fullExpanded ? content : truncated}
        </div>
      ) : null}
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
