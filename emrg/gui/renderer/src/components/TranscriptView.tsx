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
import {
  createMarkdownRenderer,
  installCodeCopy,
  type MarkdownRenderer,
  type StreamState,
} from "../lib/markdown";
import { seedSelection, selectionIn } from "../lib/selection";
import { canLoadOlder, scrollCompensation, shouldLoadOlder } from "../lib/history";
import { formatMessageTime, messageTimeTitle } from "../lib/utils";
import { toolPhrases } from "../lib/copywriting";
import { useI18n, type TranslateFn } from "../lib/i18n";
import { MARKDOWN_HTML_CLASS, MARKDOWN_PLAIN_CLASS, MarkdownText } from "./MarkdownText";

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
 * - 合并组：bar 摘要（chat.toolGroupSummary）+ 收起/展开，且**只**在宿主点 bar 时改变——
 *   工具边界既不自动展开也不自动收起（rant 2026-10-17T19:23:08）。
 * - 本组件不含滚动/欢迎屏副作用（Batch 5 接线时由宿主容器负责）。
 */

/**
 * 一条会话的滚动位置记录（issue #3024）。
 *
 * 只有两件事要记：视口停在哪个 `scrollTop`，以及离开时是否还在跟随（`following`
 * 为真时，回来之后新到的内容依旧补滚到底）。记录只活在客户端内存里：关掉客户端
 * 就没了，这是清单第 9 条要的「不跨客户端重启」。
 */
export interface ScrollRecord {
  scrollTop: number;
  following: boolean;
}

/** 会话 id → 它自己的滚动位置。 */
export type ScrollMemory = Map<string, ScrollRecord>;

/**
 * 「在底部」的唯一判定（issue #3024 清单第 2 条）：**没有容差带**。
 *
 * 之前是 `scrollTop + clientHeight >= scrollHeight - 40`，于是离底 39px 也算在底部
 * ——按钮凭空消失，而下一条流式文字把正在读的那一段甩走。现在只有贴底几种情况算：
 * 差值小于 1px（`scrollTop` 在某些缩放下是小数，等于 0 的那一格与 1 之间就落在这里）。
 */
export function isAtBottom(el: { scrollTop: number; clientHeight: number; scrollHeight: number }): boolean {
  return el.scrollHeight - el.clientHeight - el.scrollTop < 1;
}

/**
 * 读者输入的宽限窗口（issue #3046）。
 *
 * 一次滚轮或触控板手势会在输入事件之后陆续产生若干 scroll 事件，拖滚动条更久，所以「读者
 * 滚了」不能只看有没有输入事件，要看输入是不是**刚**发生。窗口取几帧的量级：比它更早的
 * 输入不再解释任何一个 scroll 事件，于是重排、图片解码与滚动锚定造成的位置变化不会被误判
 * 成读者自己滚走了。
 */
export const USER_INTENT_MS = 250;

export interface TranscriptViewProps {
  store: TranscriptStore;
  sid?: string | null;
  /** 注入 markdown 渲染器（测试/浏览器接线用；缺省按 t 构造降级渲染器） */
  renderer?: MarkdownRenderer;
  /**
   * 服务端还有更早的一页，与是否正在取这一页 —— 判定的两条事实，**原样**传进来
   * （rant 2026-09-01T20:19:40）。此前这里是合成后的 `canLoadOlder`，容器因此再也
   * 分不清「没有更早的页」与「正在取」；判定本身只有一处：`lib/history.ts` 的
   * `shouldLoadOlder` / `canLoadOlder`（issue #1979）。
   */
  hasMore?: boolean;
  loading?: boolean;
  /** 滚动到顶 / 点击顶部条的回调（触发方防抖；vanilla loadOlderHistory 语义） */
  onLoadOlder?: () => void;
  /**
   * 每条会话的滚动位置（issue #3024 清单第 6、7 条）。必须由**不随本组件卸载而消失**
   * 的那一层持有：切到别的页面时整个聊天区会被卸载，位置要活过那一次卸载，所以这份
   * Map 归 `Shell`，这里只读写。不传时本组件自持一份，仅供单测与独立渲染使用。
   */
  scrollMemory?: { readonly current: ScrollMemory };
}

/**
 * 正文容器的选择器：按在这些地方时**不**种折叠 caret。
 *
 * 按在正文里，浏览器自己那一下就已经给了起点；我们再 `removeAllRanges()` 放一个 caret，
 * 换掉的是宿主手上已有的选区（宿主报障 2026-11-23T14:33:25：已完成的回复选中后约 0.7 秒
 * 就被清掉，录屏里蓝色选区一次成形、一次消失）。种 caret 只服务**不带正文**的件——✦、
 * 消息时间、工具行、摘要条、两条消息之间的空白：那些地方浏览器给不出正文起点，拖动会从
 * 段落开头延伸（issue #3034）。
 *
 * 两个类名来自 `MarkdownText` 自己的导出，所以「渲染用的类名」与「按下判定的类名」不会
 * 各写一份再走偏——这次的缺陷正是它们走偏的那一次：名单里写的是 `.assistant-plain`
 * （markdown 解析完成前那一瞬的兜底类），而每一个已完成的回复带的是 `.assistant-html`。
 */
export const BODY_TEXT_SELECTOR = `.md-stream, .${MARKDOWN_HTML_CLASS}, .${MARKDOWN_PLAIN_CLASS}`;

export function TranscriptView({
  store,
  sid = null,
  renderer,
  hasMore = false,
  loading = false,
  onLoadOlder,
  scrollMemory,
}: TranscriptViewProps) {
  // 版本号快照：订阅的是**结构**版本 —— 条目集合变了才 +1。流式的文本增长不动它，所以
  // 每来一个 token 都不用把整条会话重新渲染一遍；条目自己的版本由 EntryView 单独订阅
  // （`useEntryVersion`）。订阅仍然走 `store.subscribe`：它只是触发一次取值，值没变
  // React 就跳过渲染，每帧成本因此与条目数无关。
  const structureVersion = useSyncExternalStore(store.subscribe, store.getStructureVersion);
  const { t } = useI18n();
  const md = useMemo(() => renderer ?? createMarkdownRenderer({ t }), [renderer, t]);
  const entries = store.getEntries(sid);
  const loadBar = store.getLoadBar(sid);

  void structureVersion; // 版本号变化 → 重新读取 entries（同一数组引用被原地变更）

  // ── 滚动跟随（rant 2026-08-28T22:36:18：React 迁移丢失 vanilla 的 autoScroll/回到底部） ──
  // React 版之前无任何 scroll/autoScroll 追踪 → 新消息不断落到底部遮罩区，需手动滚动。
  // 以下镜像 vanilla app.js:773-776（autoScroll 标志 + 底部 40px 容差）与 app.js:1711-1719
  // （回到底部悬浮按钮）。scroll 事件不冒泡 → 用 capture:true 监听（vanilla 同）。
  const viewportRef = useRef<HTMLDivElement>(null);
  /** 滚动的**内容**外层：只有它的高度变化能反映图片解码、展开、重排（issue #3024）。 */
  const contentRef = useRef<HTMLDivElement>(null);
  // autoScroll 用 ref（滚动高频，避免每帧 setState 重渲染）；atBottom 用 state（驱动按钮显隐）
  const autoScrollRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  // 读者输入的最近时刻、指针是否正按着：跟随开关的两个来源（issue #3046 清单第 1 条）。
  const lastUserInputAt = useRef(0);
  const pointerDownRef = useRef(false);
  // 最近一次补底写进去的位置与时刻：紧随其后的那个 scroll 事件是这次写入的回声，不是读者。
  const lastPadRef = useRef<{ top: number; at: number } | null>(null);
  const sidKey = sid ?? "";
  // 滚动事件与 ResizeObserver 的闭包不跟着每次重渲染重建，会话 id 走 ref
  const sidKeyRef = useRef(sidKey);
  sidKeyRef.current = sidKey;
  // 位置记录归调用方（切页时本组件会卸载，见 `scrollMemory` 的注释）；不传时自持一份
  const ownMemoryRef = useRef<ScrollMemory>(new Map());
  const memoryRef = scrollMemory ?? ownMemoryRef;

  /** 把当前视口写进这条会话那一格（清单第 6 条：按会话分开，不是全局一个值）。 */
  const rememberPosition = useCallback(
    (el: HTMLDivElement, following: boolean) => {
      memoryRef.current.set(sidKeyRef.current, { scrollTop: el.scrollTop, following });
    },
    [memoryRef],
  );

  const updateAutoScroll = useCallback(
    (el: HTMLDivElement) => {
      const ab = isAtBottom(el);
      autoScrollRef.current = ab;
      setAtBottom(ab);
      rememberPosition(el, ab);
    },
    [rememberPosition],
  );

  // ── 读者的输入才是跟随的开关（issue #3046） ──
  // 判定此前直接挂在 scroll 事件上：任何不是本组件选择的位置变化——它自己补底写入的回声、
  // ResizeObserver 的补偿、工具组表头每秒刷新引起的重排、图片解码、以及浏览器的滚动锚定
  // ——都被读成「读者走了」，于是跟随由组件自己关掉，而且关了不回来（关掉以后没有任何东西
  // 再把视口补到底）。现在 scroll 事件只负责它唯一能负责的事：**读者真的滚了**的时候，按
  // 此刻的离底距离更新跟随。读者滚动的起点是输入事件，不是位置变化，所以开关由输入驱动。
  const markUserInput = useCallback((): void => {
    lastUserInputAt.current = Date.now();
    // 读者一动手，上一次补底留下的回声凭证立即作废：否则读者恰好停在那个位置上时（刚挂载
    // 完补底写的是 0，而「滚到顶」也是 0），他这一次真正的滚动会被当成回声吞掉。
    lastPadRef.current = null;
  }, []);

  /** 这一下位置变化是不是读者弄的：输入在宽限窗口内，或指针正按着（拖滚动条）。 */
  const userDriven = useCallback(
    (): boolean => pointerDownRef.current || Date.now() - lastUserInputAt.current <= USER_INTENT_MS,
    [],
  );

  /**
   * 补到**真正的**底部（清单第 4 条）：写完再读一次，写入期间内容又长高就再写一次。
   * 每写一次都记下位置与时刻，于是紧随其后的那个 scroll 事件能被认出是自己这次写入的回声
   * （清单第 3 条），不会被当成读者滚动。
   */
  const padToBottom = useCallback(
    (el: HTMLDivElement) => {
      el.scrollTop = el.scrollHeight;
      if (!isAtBottom(el)) el.scrollTop = el.scrollHeight;
      lastPadRef.current = { top: el.scrollTop, at: Date.now() };
      autoScrollRef.current = true;
      setAtBottom(true);
      rememberPosition(el, true);
    },
    [rememberPosition],
  );

  // 输入监听：滚轮、触控板、键盘翻页、落在滚动条上的按下。capture 是为了连子元素上发生的
  // 输入一起看到；`pointerup` 挂在 document 上，因为松手可能落在视口之外。
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    const onInput = (): void => markUserInput();
    // 「按下」只有落在**滚动条**上才算读者在滚动（清单第 1 条）。按在正文里不算：读者点一下
    // 文字选中一段，随后内容长高产生的那个 scroll 事件不该被当成他自己滚走了 —— 那正是这次
    // 要修的那个缺陷换一个入口再来一次。滚动条的宽度是 offsetWidth - clientWidth，为 0 就是
    // 没有滚动条，此时任何按下都不算。
    const onPointerDown = (e: Event): void => {
      const rect = el.getBoundingClientRect();
      const gutter = el.offsetWidth - el.clientWidth;
      const onScrollbar = gutter > 0 && (e as MouseEvent).clientX >= rect.left + el.clientWidth;
      if (!onScrollbar) return;
      pointerDownRef.current = true;
      markUserInput();
    };
    const release = (): void => {
      pointerDownRef.current = false;
    };
    const types = ["wheel", "touchstart", "touchmove", "keydown"] as const;
    for (const ty of types) el.addEventListener(ty, onInput, { capture: true });
    el.addEventListener("pointerdown", onPointerDown, { capture: true });
    document.addEventListener("pointerup", release, { capture: true });
    document.addEventListener("pointercancel", release, { capture: true });
    return () => {
      for (const ty of types) el.removeEventListener(ty, onInput, { capture: true });
      el.removeEventListener("pointerdown", onPointerDown, { capture: true });
      document.removeEventListener("pointerup", release, { capture: true });
      document.removeEventListener("pointercancel", release, { capture: true });
    };
  }, [markUserInput]);

  // scroll 监听（capture，因 scroll 不冒泡；挂在 .transcript-view 自身上，捕获子元素滚动）
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    const onScroll = () => {
      // 刚补过底：位置正是那次写入的值，且在宽限窗口内 —— 这是回声，不是读者。
      const pad = lastPadRef.current;
      if (pad && el.scrollTop === pad.top && Date.now() - pad.at <= USER_INTENT_MS) {
        lastPadRef.current = null;
        return;
      }
      if (userDriven()) {
        // 读者真的滚了：跟随与否按此刻的离底距离定（读者可感知的行为一点没变）。
        updateAutoScroll(el);
      } else if (autoScrollRef.current) {
        // 位置变了但不是读者弄的（重排、图片解码、锚定）：跟随中就把视口补回底部。
        padToBottom(el);
      }
      // 滚动到顶 → 加载更早一页（rant 2026-09-01T20:19:40；防抖由 onLoadOlder 触发方负责）。
      // 判定不在这里重写：它就是 `lib/history.ts` 的 `shouldLoadOlder`（issue #1979 ——
      // 此处曾有一份副本，而生产路径读的正是副本，于是改库里的阈值不会有任何效果）。
      if (onLoadOlder && shouldLoadOlder(el.scrollTop, hasMore, loading)) {
        onLoadOlder();
      }
    };
    el.addEventListener("scroll", onScroll, { capture: true });
    return () => el.removeEventListener("scroll", onScroll, { capture: true });
  }, [updateAutoScroll, userDriven, padToBottom, hasMore, loading, onLoadOlder]);

  // ── 代码块复制按钮（issue #3037） ──
  // 挂在 `transcript-view` 这个长期存在的滚动容器上，而不是逐个按钮：块投影每帧重写尾部
  // live 块、done 时把 `.md-stream` 的 innerHTML 整个换掉，按钮节点被换掉，绑在它身上的
  // 监听随之消失。容器不重建，一次绑定覆盖流式中、封存后、以及任何一次重写。
  // `t` 换语言时换身份（I18nProvider 的 useMemo 按 locale 重建），所以依赖它重新绑定，
  // 复原文案才会是当下语言的。
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    return installCodeCopy(el, { t });
  }, [t]);

  // ── 高度变化也要重算（issue #3024 清单第 3、4 条） ──
  // 图片解码完、侧栏拖宽后文字重新折行、工具组展开、输入框长高 —— 这些都只改高度，一个
  // scroll 事件都不产生：判定与按钮于是停在变化前的取值上（明明在底部却不跟随），而跟随
  // 本身也只订阅了 store 的写入计数（纯高度增长一条都不跟）。所以两处都盯：容器自己的盒子
  // （窗口缩放、拖分隔条）与内容盒子（图片、展开、重排）。
  // jsdom 没有 ResizeObserver —— 要钉这条规则的单测自己装一个假的，不用这个接口的用例不受影响。
  useEffect(() => {
    const el = viewportRef.current;
    const content = contentRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      // 高度变了而一个 scroll 事件都没有（图片解码、文字重新折行、工具组表头刷新）：
      // 跟随中补到新的底部；不跟随就把视口留在读者正在读的地方（清单第 2、4 条）。
      if (autoScrollRef.current) padToBottom(el);
    });
    ro.observe(el);
    if (content) ro.observe(content);
    return () => ro.disconnect();
  }, [padToBottom]);

  // 新消息 append/流入 → 若 autoScroll 为 true 则滚到底。这一件事订阅的是每次变更都动的
  // `getVersion`（流式文本是原地 append 进已有段，结构不变），所以它住在独立的
  // `AutoScroll` 里：每个 token 只重渲染那一棵返回 null 的子树，条目列表不在它的渲染路径上。
  // 旧版把它放在本组件里、依赖 version —— 于是每帧重渲染整条会话，正是要修的那个成本。

  const scrollToBottom = () => {
    const el = viewportRef.current;
    if (!el) return;
    // padToBottom 顺带记下这次写入，于是一个点击不会在紧接着的 scroll 事件里被读成读者滚动。
    padToBottom(el);
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
  // 初值是 null 而不是 sidKey：**挂载本身也要走一次恢复**。切到别的页面再回来是一次
  // 重新挂载，初值若等于 sidKey，那一次就被当成「什么都没变」，位置永远落在底部
  // （issue #3024 清单第 7 条要的正是这一次）。
  const lastSidRef = useRef<string | null>(null);
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
      // 切会话（或切页回来重新挂载）：另一个桶的计数与本桶无关，重置而不是当成一次前插；
      // 同时把这一条会话自己的位置接回来（issue #3024 清单第 6、7、8 条）。
      lastSidRef.current = sidKey;
      lastPrependRef.current = prepends;
      const rec = memoryRef.current.get(sidKey);
      if (rec) {
        // 内容变短时记下的偏移可能超出新高度，夹回可滚动范围（浏览器自己也会夹，
        // 但上面那句「判定与恢复用同一个值」不该依赖浏览器的夹取）。
        el.scrollTop = Math.min(Math.max(rec.scrollTop, 0), Math.max(el.scrollHeight - el.clientHeight, 0));
        autoScrollRef.current = rec.following;
        setAtBottom(isAtBottom(el));
      } else {
        // 从没打开过的会话：贴底并跟随（清单第 8 条）。padToBottom 一并置跟随、清按钮、
        // 记位置，并留下这次写入的位置与时刻。
        padToBottom(el);
      }
    } else if (prepends !== lastPrependRef.current) {
      lastPrependRef.current = prepends;
      const prev = preCommitRef.current;
      if (prev) {
        const next = scrollCompensation(prev.scrollTop, prev.scrollHeight, el.scrollHeight);
        if (next !== el.scrollTop) el.scrollTop = next;
        updateAutoScroll(el);
      }
    }
  }, [structureVersion, prepends, sidKey, updateAutoScroll]);

  return (
    // 外层不动、内层滚（issue #3024 清单第 1 条）：按钮此前画在滚动容器内部，`position:
    // absolute` 于是把它放进**滚动内容**的坐标系 —— 上翻一屏就随内容一起滚出可视区，
    // 判定说要显示，屏幕上什么也没有。现在它是这层不滚动的壳的子元素。
    <div className="transcript-shell" data-testid="transcript-shell">
      <div className="transcript-view" data-testid="transcript-view" ref={viewportRef}>
        {loadBar ? (
          // 顶部历史条（宿主报障 2026-10-08：点击「↑ 加载更早消息」没有任何效果）。CSS 一直写着
          // `cursor: pointer`、文案也一直是祈使句（"↑ 加载更早消息"），但它此前只是纯文本 div——
          // 可点的样子、不可点的实体。`canLoadOlder(hasMore, loading)` 为真时它是**真按钮**
          // （键盘可达、点击即触发与滚动到顶同一个防抖回调）；为假时（加载中 / 没有更多）退化为
          // 状态文字，不再冒充控件。与上面的 scroll 触发读同一条规则（issue #1979）。
          canLoadOlder(hasMore, loading) && onLoadOlder ? (
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
      {/* 内容盒：只有它能观察到「高度变了但没有 scroll 事件」 */}
      {/* 按下的地方不是正文（✦、时间、工具行、摘要条、两条消息之间的空白）时，先放一个折叠
          选区在最近的 caret 上：随后的拖动于是从那个字延伸，而不是从段落开头（issue #3034）。 */}
      <div
        className="transcript-content"
        ref={contentRef}
        onPointerDown={(e) => {
          const target = e.target as HTMLElement | null;
          if (target && target.closest(BODY_TEXT_SELECTOR)) return;
          seedSelection(typeof document === "undefined" ? null : document, {
            x: e.clientX,
            y: e.clientY,
          });
        }}
      >
        {entries.map((entry, i) => (
          <EntryView key={entry.id} entry={entry} index={i} t={t} md={md} store={store} sid={sid} />
        ))}
      </div>
      {/* 跟随滚动：订阅每次变更（含每个 token），但它只重渲染自己 */}
      <AutoScroll
        store={store}
        viewportRef={viewportRef}
        autoScrollRef={autoScrollRef}
        padToBottom={padToBottom}
      />
      </div>
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

/**
 * 流式跟随滚动（rant 2026-08-28T22:36:18：React 迁移丢失 vanilla 的 autoScroll/回到底部）。
 *
 * 单独成一个组件是为了**成本**，不是为了美观：它必须对每个 token 都反应一次（流式文本是
 * 原地 append，条目集合不变），于是它订阅每次变更都动的 `getVersion`。它返回 null，所以
 * 每个 token 重渲染的只有这一棵子树；放在 `TranscriptView` 里订阅就是每帧重渲染整条会话。
 */
function AutoScroll({
  store,
  viewportRef,
  autoScrollRef,
  padToBottom,
}: {
  store: TranscriptStore;
  viewportRef: { current: HTMLDivElement | null };
  autoScrollRef: { current: boolean };
  padToBottom: (el: HTMLDivElement) => void;
}) {
  const version = useSyncExternalStore(store.subscribe, store.getVersion);
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    // 首次无条目时不误滚；version 变更即触发（覆盖 delta/tool/done 全部写入）。
    // 每一次补底都经过 padToBottom：它写下这次写入的位置与时刻，于是每个 token 引起的那个
    // scroll 事件不会被读成读者滚动（clip 掉跟随的正是这一串回声）。
    if (autoScrollRef.current) padToBottom(el);
  }, [version, viewportRef, autoScrollRef, padToBottom]);
  return null;
}

/**
 * 条目自己的版本号 —— 订阅**下沉到条目级**。
 *
 * `getEntryVersion` 只在这一条被写过时才变，所以一次 delta 只通知它自己的监听器：会话里
 * 有 5 条还是有 500 条，每帧重渲染的都只有正在变的那一条。父组件读的是
 * `getStructureVersion`，条目文本增长不动它，整棵树因此也不跟着重渲染。
 */
function useEntryVersion(store: TranscriptStore, id: string): number {
  const subscribe = useCallback((listener: () => void) => store.subscribeEntry(id, listener), [store, id]);
  const get = useCallback(() => store.getEntryVersion(id), [store, id]);
  return useSyncExternalStore(subscribe, get);
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
  // 这一条的语言：只有它自己被写过时才重渲染（流式隔离的落点）
  useEntryVersion(store, entry.id);
  switch (entry.kind) {
    case "user":
      // Stage 1（rant 2026-08-28T14:07:29）：用户消息为 markdown 字符串（tiptap 序列化），
      // 与 TUI 的 UserMarkdown 对齐渲染——否则 **bold** 会字面显示
      return (
        <div className="msg user">
          <MarkdownText text={entry.text} md={md} stripMark={false} />
          <MessageTime stamp={entry.timestamp} />
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

/**
 * 消息行上的时刻（rant 2026-10-17T10:03:08）：`HH:MM`，值只来自 daemon。
 *
 * 缺时刻/坏时刻渲染成**没有这个元素**，而不是空括号、`Invalid Date` 或 `NaN` ——
 * 老记录本就没有时间，而“缺陷只在渲染里看得见”正是空串要避免的。完整日期在 `title` 上，
 * 行上只留时钟（与 TUI 同口径）。
 */
function MessageTime({ stamp }: { stamp?: string }) {
  const clock = formatMessageTime(stamp);
  if (!clock) return null;
  return (
    <span className="msg-time" title={messageTimeTitle(stamp)}>
      {clock}
    </span>
  );
}

/** 助手消息：每文本段一个 .msg.assistant 节点（chat.js createAssistantNode 语义） */
function AssistantView({ entry, t, md }: { entry: AssistantEntry; t: TranslateFn; md: MarkdownRenderer }) {
  const last = entry.segments.length - 1;
  return (
    <>
      {entry.segments.map((seg, i) => (
        <AssistantSegmentView
          key={i}
          entry={entry}
          segment={seg}
          t={t}
          md={md}
          // 时刻属于这条消息，不属于每一段：一轮被工具切成多段时，只有最后一段显示它
          stamp={i === last ? entry.timestamp : undefined}
        />
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
 * 追加（旧行为）。done/封存：agent 模式 streamFinalize 一次性全量校正；plain 模式由
 * MarkdownText 整体渲染兜底。
 */
function AssistantSegmentView({
  entry,
  segment,
  t,
  md,
  stamp,
}: {
  entry: AssistantEntry;
  segment: AssistantSegment;
  t: TranslateFn;
  md: MarkdownRenderer;
  /** daemon 写下这条回复的时刻（只在最后一段传入）；缺席则这一行不显示时间 */
  stamp?: string;
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

  // 选区消失后补做被推迟的重写（issue #3034）。拖动结束不给任何回调，`selectionchange` 是
  // 唯一能报「选区没了」的信号；没有待补内容时这个监听什么都不做，所以它常驻也不会写 DOM。
  useEffect(() => {
    if (typeof document === "undefined") return;
    const onSelectionChange = () => {
      const host = hostRef.current;
      const state = stateRef.current;
      if (!host || !state) return;
      if (state.pendingText === undefined && state.pendingFinalize === undefined) return;
      if (selectionIn(host)) return;
      void md.resumeStream(host, state);
    };
    document.addEventListener("selectionchange", onSelectionChange);
    return () => document.removeEventListener("selectionchange", onSelectionChange);
  }, [md]);

  // done/封存（typing 移除）：agent 模式 → streamFinalize 把 live 块一次性校正为全量
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
      <MessageTime stamp={stamp} />
    </div>
  );
}

/** done 后整体 markdown 渲染（与旧 done 渲染同源 renderMarkdown；✦ 标记剥离仅限助手消息）
 *  实现已提为共用组件 MarkdownText（rant 2026-09-30T09:06:16 / #1763：Rant 详情同路径）。 */

/** 工具友好状态行（进行中 → 完成/失败，默认折叠，点开展示原始输出） */
/** 一行工具在**折叠形态**下的文字：批次标签 + 工具名 + 状态短语 + intent。
 *
 * 组摘要里的 `{latest}` 用的就是这个函数（#2980）：宿主在一条摘要里读到的最后一行，与
 * 他点开后看到的那一行必须是同一句话。另造一套措辞就会是同一个事实的两个说法。 */
export function toolRowText(row: ToolRow, t: TranslateFn): string {
  const phrases = toolPhrases(row.toolName, t);
  const label =
    row.status === "running"
      ? phrases.doing
      : row.status === "done"
        ? phrases.done
        : t("tool.failText");
  return `${row.callLabel ?? ""}${row.toolName} ${label}${row.intent ? ` ${row.intent}` : ""}`;
}

/** 组里还在跑的行贡献的时间（秒）；没有在跑的行、或它没有起点时返回 null。
 *
 * 这是客户端唯一自己算的一段时间，而且只算**运行期间**：一行结束时 daemon 报的 `elapsed`
 * 顶上来，本地推算值随即消失，屏幕上最后留下的是 daemon 量出来的那个数
 * （rant 2026-11-19T09:31:00）。`Math.max(0, …)` 兜住「起点在未来」——墙钟往回跳一步时，
 * 表头宁可停一下也不显示负数。
 */
export function runningShare(group: ToolGroup, nowMs?: number): number | null {
  if (nowMs === undefined) return null;
  let total = 0;
  let any = false;
  for (const row of group.rows) {
    if (row.status !== "running" || row.startedAtMs === undefined) continue;
    any = true;
    total += Math.max(0, (nowMs - row.startedAtMs) / 1000);
  }
  return any ? total : null;
}

/** 组摘要的一句话：四种缺省各有自己的键。
 *
 * `timed` 为假（重放的老记录一行都没量过耗时）时不出现时间、也不出现 `0.0s`；`latest`
 * 为空时整个括号不出现，而不是留一对空括号——两者都是宿主在屏幕上看出来的东西（#2980）。
 *
 * `nowMs` 传入时才把「还在跑」的那几行算进去，因此表头在运行期间每 0.1 秒往上走一格；
 * 不传（单元测试、非运行态）则只有已结束的行，与改动前逐字一致。
 */
export function summaryLine(
  group: ToolGroup,
  latest: string,
  t: TranslateFn,
  nowMs?: number,
): string {
  const summary = group.summary;
  if (!summary) return "";
  const count = summary.count;
  const live = runningShare(group, nowMs);
  const timed = summary.timed || live !== null;
  if (!timed) {
    return latest
      ? t("chat.toolGroupSummaryNoTime", { count, latest })
      : t("chat.toolGroupSummaryCountOnly", { count });
  }
  const time = (summary.totalElapsed + (live ?? 0)).toFixed(1) + "s";
  return latest
    ? t("chat.toolGroupSummary", { count, time, latest })
    : t("chat.toolGroupSummaryNoLatest", { count, time });
}

/** 工具行：折叠 = 名称 + intent，展开 = 名称 / 输入 / 输出三段（#1787）。 */
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
      {row.callLabel ? <span className="tool-call-label">{row.callLabel}</span> : null}
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

/** 组表头耗时的刷新节奏：只要组里还有工具在跑，每 0.1 秒往上走一格（rant 2026-11-19T09:31:00）。
 *
 * 定时器住在组自己身上，不在 Shell：0.1 秒的刷新只重渲染这一个组，整屏不动。组内全部
 * 结束后 effect 清掉定时器，数字停在最终值，不留常驻定时器。
 */
export const TOOL_GROUP_TICK_MS = 100;

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
  // 只有「在跑且知道起点」的行才让表头动起来：老记录没有起点，于是这里为假——
  // 不挂定时器、不算贡献、也不显示时间（rant 2026-11-19T09:31:00 第 7 条）。
  const running = group.rows.some(
    (r) => r.status === "running" && r.startedAtMs !== undefined,
  );
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setTick((n) => n + 1), TOOL_GROUP_TICK_MS);
    return () => clearInterval(id);
  }, [running]);

  const latestRow = group.rows.length ? group.rows[group.rows.length - 1] : undefined;
  const nowMs = running ? Date.now() : undefined;
  const summaryText = summaryLine(group, latestRow ? toolRowText(latestRow, t) : "", t, nowMs);

  return (
    <div className={`tool-group ${group.collapsed ? "collapsed" : ""}`}>
      <div
        className={`tool-group-bar ${group.barHidden ? "hidden" : ""}`}
        onClick={() => store.toggleGroup(sid, entryIndex)}
      >
        <span className="tool-group-chev">⌄</span>
        <span className="tool-group-summary">{summaryText}</span>
      </div>
      <div className="tool-group-rows">
        {group.rows.map((row) => (
          <ToolRowView key={row.callId} row={row} t={t} store={store} sid={sid} />
        ))}
      </div>
    </div>
  );
}
