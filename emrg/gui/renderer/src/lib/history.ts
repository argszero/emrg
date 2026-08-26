/**
 * history.ts — 历史按需加载分页状态机（Batch 2 remainder，蓝图 cycle-155056）。
 * 源：vanilla renderer/js/app.js historyPages（816-876，rant 14:15:12）：
 * - HISTORY_PAGE=50，切会话加载最近一页（offset 从最新往回数），滚动到顶加载更早一页
 * - hasMore && !loading 双守卫；空页关闭 hasMore
 * - 滚差补偿：新内容 prepend 后 scrollTop = prevScrollTop + (scrollHeight - prevHeight)
 *
 * 纯逻辑、零 DOM：React TranscriptView/容器在 Batch 5 接线时调用；scroll 补偿为
 * 纯函数（不依赖元素），可直接单测。
 */

/** 单会话分页状态（historyPages Map 的值） */
export interface HistoryPageState {
  /** 已加载条数（从最新往回数，作为下次 listHistory 的 offset） */
  offset: number;
  /** 服务端还有更早历史（hasMore && 本页非空才保持） */
  hasMore: boolean;
  /** in-flight 锁：防滚动连触发重入 */
  loading: boolean;
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
    st = { offset: 0, hasMore: false, loading: false };
    pages.set(sid, st);
  }
  return st;
}

/**
 * 应用一页加载结果：offset 前进、hasMore 更新（空页 = 没有更多，vanilla
 * `if (msgs.length === 0) st2.hasMore = false`）。返回本页消息数。
 */
export function applyHistoryPage(
  st: HistoryPageState,
  messageCount: number,
  hasMore: boolean,
): number {
  st.offset += messageCount;
  st.hasMore = hasMore && messageCount > 0;
  return messageCount;
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
 * 滚动到顶触发条件（vanilla：`scrollTop <= 2 && hasMore && !loading`，
 * 150ms 防抖由接线层处理）。纯判定，供容器 scroll 监听复用。
 */
export function shouldLoadOlder(scrollTop: number, hasMore: boolean, loading: boolean): boolean {
  return scrollTop <= 2 && hasMore && !loading;
}
