/**
 * evolutionToast.ts — 「自进化完成」提示的节流规则（vanilla `maybeShowEvolutionToast`
 * 的 `localStorage("emrg.evoToast.date")`，`js/app.js:1122-1127`）。
 *
 * 为什么是**独立的一个家**：这条规则有两个容易写错的地方，而它们与渲染无关 ——
 * ① 判据是**本地日历日**（`toISOString().slice(0,10)`），不是「距上次 N 小时」；
 * ② 存储可能**不可用**（禁用 cookie / 隐私模式 / 非浏览器环境），而 vanilla 的
 * `try/catch`（catch 里只有一句 ignore）让不可用时**照常显示** —— 不显示等于把
 * 提示悄悄关掉，而这正是它存在的原因。
 *
 * ⚠️ 与 `#github-banner` 的**区别**（同一处 vanilla 代码里紧挨着的两个东西）：
 * 那条**不受**节流（`maybeShowGithubBanner()` 在节流块**之前**被调用，`js/app.js:1121`），
 * 因为「没连 GitHub」是一个**状态**，每次演化都值得再说一遍；本模块管的是那个**事件**
 * 提示，一天一次。别把两者混起来。
 */

/** 节流用的存储键（vanilla 同名，跨版本沿用）、 */
export const EVO_TOAST_DATE_KEY = "emrg.evoToast.date";

/** 一个时刻属于哪一天 —— 节流用的拼写只有这一处。 */
export function evolutionToastDay(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10);
}

/**
 * 读取 `localStorage`，**不可用就回 null**（而不是抛）。
 *
 * `window.localStorage` 的**取值本身**在某些策略下会抛（不是方法调用才抛），所以
 * 连访问都要在守卫里；这也是本模块唯一碰 `window` 的地方，便于在测试里改道。
 */
export function safeLocalStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/**
 * 现在是否该弹一次「进化完成」提示？**答 yes 就顺手把今天记下**（vanilla 也是先记
 * 后弹 —— 于是「弹过」这件事与提示是否真的渲染出来无关，一天之内不会连弹两次）。
 *
 * 三种答案都量得出来：
 * - 今天已经记过 → `false`（节流）；
 * - 没记过 / 记的是别的日子 → `true`，并写入今天；
 * - 存储拿不到，或读写抛 → `true`（**照常显示** —— vanilla 的 catch 里只有一句 ignore）。
 */
export function claimEvolutionToastDay(
  storage: Pick<Storage, "getItem" | "setItem"> | null,
  day: string = evolutionToastDay(),
): boolean {
  if (!storage) return true;
  try {
    if (storage.getItem(EVO_TOAST_DATE_KEY) === day) return false;
    storage.setItem(EVO_TOAST_DATE_KEY, day);
    return true;
  } catch {
    return true;
  }
}
