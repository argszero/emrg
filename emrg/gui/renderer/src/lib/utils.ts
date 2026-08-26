/**
 * utils.ts — 纯逻辑助手迁移（vanilla renderer/js/utils.js 的 React 版）。
 *
 * Batch 0 迁移无 DOM 依赖的纯函数（escapeHtml / genRequestId / relTime）；
 * DOM 助手（$ / el / applyTheme / showToast）随组件化在后续批次收敛。
 * 行为与 vanilla 版保持一致（测试断言同源）。
 */

/** HTML 转义（与 vanilla utils.js escapeHtml 一致） */
export function escapeHtml(s: unknown): string {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * 生成 requestId（G143，与 vanilla utils.js genRequestId 一致）。
 * secure context 下 crypto.randomUUID；低版本兜底。
 */
export function genRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export type TranslateFn = (key: string, params?: Record<string, unknown>) => string;

/**
 * 相对时间（与 vanilla utils.js relTime 一致）：ISO 时间串 → 刚刚 / {n} 分钟前 /
 * {n} 小时前 / {n} 天前。走注入的翻译函数（组件侧从 i18n context 取）；缺失/非法
 * 输入返回空串（调用点自行隐藏）。
 */
export function relTime(iso: string | null | undefined, t: TranslateFn): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffMin = Math.floor((Date.now() - then) / 60000);
  if (diffMin < 1) return t("relTime.justNow");
  if (diffMin < 60) return t("relTime.minutesAgo", { n: diffMin });
  const hrs = Math.floor(diffMin / 60);
  if (hrs < 24) return t("relTime.hoursAgo", { n: hrs });
  const days = Math.floor(hrs / 24);
  return t("relTime.daysAgo", { n: days });
}
