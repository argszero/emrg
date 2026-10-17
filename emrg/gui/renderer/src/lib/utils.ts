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

/** `YYYY-MM-DD HH:MM` 的原始片段 —— 只认字符串自己的时钟，不换算时区。 */
const _MOMENT = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/;

/** 把 daemon 的 ISO 时刻拆成它自己写下的年月日时分；不可读 → null。 */
function _momentParts(iso: string | null | undefined): { date: string; time: string } | null {
  if (!iso) return null;
  const m = _MOMENT.exec(String(iso).trim());
  if (!m) return null;
  const [, y, mo, d, h, mi] = m;
  // 正则只保证是两位数字：`2026-13-45T99:99` 形状对、时刻不对，按不可读处理
  if (+mo < 1 || +mo > 12 || +d < 1 || +d > 31 || +h > 23 || +mi > 59) return null;
  return { date: `${y}-${mo}-${d}`, time: `${h}:${mi}` };
}

/**
 * 消息行上显示的 `HH:MM`，值取自 **daemon 写下那一刻**，或不可读时 `""`。
 *
 * 与 TUI 的 `format_message_time`（`emrg/client/python_tui/widgets/chat_row.py`）同口径：
 * 一个格式器给所有行用，用户消息与助手回复的格式不会分家；缺值/坏值渲染成**什么都没有**，
 * 而不是空括号、`Invalid Date` 或 `NaN` —— 老记录没有时刻，而只在渲染里才看得见的缺陷
 * 正是返回 `""` 要避免的。
 *
 * **不做时区换算**：读的是字符串自己写下的时钟。`fromisoformat` + `strftime` 也是这个
 * 语义（带偏移也不换到本地），两端因此对同一条记录给出同一个值；换算会让客户端的时区
 * 成为显示的输入，正中「时刻只来自 daemon」的反面。
 */
export function formatMessageTime(iso: string | null | undefined): string {
  const parts = _momentParts(iso);
  return parts ? parts.time : "";
}

/** 完整日期 `YYYY-MM-DD HH:MM`（行上悬停可见）；不可读 → `""`。 */
export function messageTimeTitle(iso?: string | null): string {
  const parts = _momentParts(iso);
  return parts ? `${parts.date} ${parts.time}` : "";
}

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
