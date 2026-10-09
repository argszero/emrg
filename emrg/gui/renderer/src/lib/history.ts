/**
 * history.ts — 历史按需加载分页状态机（Batch 2 remainder，蓝图 cycle-155056）。
 * 源：vanilla renderer/js/app.js historyPages（816-876，rant 14:15:12）：
 * - HISTORY_PAGE=50，切会话加载最近一页，滚动到顶加载更早一页
 * - hasMore && !loading 双守卫；空页关闭 hasMore
 * - 滚差补偿：新内容 prepend 后 scrollTop = prevScrollTop + (scrollHeight - prevHeight)
 *
 * 游标自 2026-09-20 起是 **record_index**（rant 2026-09-20T18:58:44），不再是「已加载
 * 条数 / 距最新的 offset」——后者在翻页间来了新消息时会平移，同时造成重复与不可达。
 *
 * 纯逻辑、零 DOM：React TranscriptView/容器在 Batch 5 接线时调用；scroll 补偿为
 * 纯函数（不依赖元素），可直接单测。
 */

/** 单会话分页状态（historyPages Map 的值） */
export interface HistoryPageState {
  /**
   * 最早已加载记录的 `record_index`：下一页的游标（daemon `before_index`）。
   *
   * rant 2026-09-20T18:58:44：以前这里放的是「已加载条数」，服务端把它当 offset
   * 从**最新往回数**——翻页之间来了新消息窗口就整体平移，于是既重复（实测 6/48
   * 条重复）也漏。`record_index` 是记录的绝对位置，只在前面追加新记录，游标不动。
   * null = 还没加载过任何一页（首页从最新往回取）。
   */
  oldestIndex: number | null;
  /** 服务端还有更早历史（hasMore && 本页非空才保持） */
  hasMore: boolean;
  /** in-flight 锁：防滚动连触发重入 */
  loading: boolean;
  /** 已加载的 record_index 集合（去重：压缩使历史变短时游标被夹回，窗口可能重叠） */
  loaded: Set<number>;
}

/** 一条带绝对位置的落盘记录（daemon list_history include_records 每条都带）。 */
export interface IndexedRecord {
  record_index?: number;
}

/** 与 vanilla 一致：每页条数 */
export const HISTORY_PAGE = 50;

/** 创建 sid → 分页状态 的 Map（Composer/容器持有，切会话不丢） */
export function createHistoryPages(): Map<string, HistoryPageState> {
  return new Map();
}

/** 取或建该会话的分页状态（vanilla historyPageState(sid)） */
export function historyPageState(
  pages: Map<string, HistoryPageState>,
  sid: string,
): HistoryPageState {
  let st = pages.get(sid);
  if (!st) {
    st = { oldestIndex: null, hasMore: false, loading: false, loaded: new Set() };
    pages.set(sid, st);
  }
  return st;
}

/**
 * 本页里尚未加载过的记录（回放前先过这一道，见 `applyHistoryPage` 的说明）。
 * 稳定游标下不该重叠；游标被夹回（历史被压缩变短）时会重叠，那时重叠部分必须丢掉，
 * 否则同一条记录会被回放两遍。
 */
export function unloadedRecords<T extends IndexedRecord>(st: HistoryPageState, page: T[]): T[] {
  return page.filter((r) => typeof r.record_index !== "number" || !st.loaded.has(r.record_index));
}

/**
 * 应用一页加载结果：推进游标（最早已加载的 record_index）、登记已加载、更新 hasMore
 * （空页 = 没有更多，vanilla `if (msgs.length === 0) st2.hasMore = false`）。
 *
 * ⚠️ 必须在 `unloadedRecords` **之后**调用——它把本页登记为已加载。返回本页记录数。
 */
export function applyHistoryPage(
  st: HistoryPageState,
  page: IndexedRecord[],
  hasMore: boolean,
): number {
  for (const r of page) {
    const i = r.record_index;
    if (typeof i !== "number") continue;
    st.loaded.add(i);
    if (st.oldestIndex === null || i < st.oldestIndex) st.oldestIndex = i;
  }
  st.hasMore = hasMore && page.length > 0;
  return page.length;
}

/**
 * 滚动位置补偿：更早历史 prepend 到顶部后，保持用户视觉位置不变。
 * `prevScrollTop + (newScrollHeight - prevScrollHeight)`；高度未变（空页/无新行）
 * 时差值 0，视觉位置天然不变。
 */
export function scrollCompensation(
  prevScrollTop: number,
  prevScrollHeight: number,
  newScrollHeight: number,
): number {
  return prevScrollTop + Math.max(0, newScrollHeight - prevScrollHeight);
}

/**
 * 「更早的一页还能不能加载」——判定的另一半（vanilla：`hasMore && !loading`）。
 *
 * `shouldLoadOlder` 与它分开导出，是因为两个**不同的问题**都要问这一半：
 * 「滚到顶要不要翻页」还要看滚动位置，而「顶部条此刻是不是可点控件」只看有没有
 * 可加载的页、以及是不是正在加载。两处各自写一遍 `hasMore && !loading` 就是同一
 * 条规则的两个副本（rant 2026-10-09T09:25:00 的第三点），所以这里只留一处，
 * `shouldLoadOlder` 自己也是调它。
 */
export function canLoadOlder(hasMore: boolean, loading: boolean): boolean {
  return hasMore && !loading;
}

/**
 * 顶部条这一次该写哪句话 —— 首屏（`loadHistory`）与翻页（`loadOlderHistory`）
 * 两个写入点共用这一条规则。
 *
 * 两处**看起来**是「同一状态、两套答案」：首屏没有更早的页就什么都不写，翻到头却写
 * 「没有更多历史」。它们问的其实是两个问题——首屏问「有没有更早的页可以取」（答案
 * 是没有就无可奉告，用户还没问），翻页后问「你刚问的那句，到头了吗」（到没到头都
 * 得回答）。所以判别式里必须带 `paged`：一条规则，两个问题各得其所。
 *
 * 这不是新行为：vanilla `app.js:838-841` 正是这个形状（首屏只有一个 `if (hasMore)`
 * 分支，else 在 `:867-870`）。React 侧把它抄成了两个散落的 `? :`（`Shell.tsx:431`/`:462`），
 * 看着像不一致，rant 2026-10-09T09:25:00 因此要求「一并理清」——理清的结果是把它
 * 写成一条带名字的规则，而不是改掉其中一处。
 */
export function loadBarKey(
  hasMore: boolean,
  paged: boolean,
): "app.historyLoadMore" | "app.historyNoMore" | null {
  if (hasMore) return "app.historyLoadMore";
  return paged ? "app.historyNoMore" : null;
}

/**
 * 滚动到顶触发条件（vanilla：`scrollTop <= 2 && hasMore && !loading`，
 * 150ms 防抖由接线层处理）。纯判定，供容器 scroll 监听复用。
 *
 * 这条规则曾有两个家：写在这，也重写在 `TranscriptView.tsx` 的 scroll 监听里，
 * 而**生产路径读的是重写的那份**——于是改这里不会有任何用户可见的效果，改了也
 * 不知道（rant 2026-10-09T09:25:00；issue #1979 把它收敛回这一处）。
 */
export function shouldLoadOlder(scrollTop: number, hasMore: boolean, loading: boolean): boolean {
  return scrollTop <= 2 && canLoadOlder(hasMore, loading);
}
