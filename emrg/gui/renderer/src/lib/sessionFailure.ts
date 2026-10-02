/**
 * sessionFailure.ts — 一个**失败**的会话操作该说哪一句。
 *
 * 源：vanilla `renderer/js/app.js` 三个 catch（#1024 删渲染层时，读者与代码一起走了，
 * 词典串 `app.switchFailed` / `app.newFailed` / `app.deleteFailed` /
 * `app.tooManyOpenSessions` 因此在 React 渲染层再没有读者）：
 *
 * - `switchSession` (:690) —— `/too many open sessions/i` → `app.tooManyOpenSessions`
 *   （**不带** `{msg}`），否则 `app.switchFailed`（带 `{msg}`）
 * - `newSession`    (:905) → `app.newFailed`
 * - `deleteSession` (:930) → `app.deleteFailed`
 *
 * 纯函数：只决定「哪一句 + 什么参数」，渲染交给调用点的 `t()`——这样三个调用点
 * 不会各自拼一次同一句话（同一句话有两个拼写就是两个未来）。
 *
 * ⚠️ 上游 main 进程抛的是**异常**（`emrg/gui/main.js`：`new Error("too many open
 * sessions (20) — close some first")`），IPC 因此 reject，所以判据读的是
 * `err.message`，与 vanilla 读 `e.message` 同一把尺。
 */

export type SessionOp = "switch" | "new" | "delete";

export interface SessionFailure {
  key: string;
  /** 有值 ⇒ 调用点用 `t(key, { msg })`；无值 ⇒ `t(key)` */
  msg?: string;
}

/** main 进程打开会话超限时抛的原话（`emrg/gui/main.js` 的 DEFAULT_CAP 分支） */
const CAP_RE = /too many open sessions/i;

/** 从任意抛出物里取一句话（vanilla 读 `e.message`；非 Error 的抛出物也要能读） */
function messageOf(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  try {
    return String(err);
  } catch {
    return "";
  }
}

export function sessionFailure(op: SessionOp, err: unknown): SessionFailure {
  const msg = messageOf(err);
  if (op === "switch") {
    // 超限不是「切换失败」——它有自己的一句话，因为宿主该做的动作不同（先关掉几个）
    return CAP_RE.test(msg) ? { key: "app.tooManyOpenSessions" } : { key: "app.switchFailed", msg };
  }
  return { key: op === "new" ? "app.newFailed" : "app.deleteFailed", msg };
}

/** 渲染成一句话：三个调用点共用，免得各拼一次 */
export function sessionFailureText(
  t: (key: string, params?: Record<string, unknown>) => string,
  op: SessionOp,
  err: unknown,
): string {
  const f = sessionFailure(op, err);
  return f.msg === undefined ? t(f.key) : t(f.key, { msg: f.msg });
}
