import type { TranslateFn } from "./utils";

/**
 * commands.ts — / 指令注册表与解析器（vanilla renderer/js/commands.js 迁移，Batch 1）。
 * 纯逻辑、不绑 DOM：React 组件与测试均可直接 import。
 * 契约与 vanilla 版基本一致（COMMANDS 条数在 commands.test.ts 里钉住 / parseInput 三态 /
 * getCompletions 前缀过滤）；`/sandbox` 是后来加的（rant 2026-10-09T09:43:39）。
 */
export interface CommandMeta {
  hint: string;
  phase: number;
  /**
   * 参数用法的 i18n 键。不吃参数的指令不写这一项：补全清单、`/help` 与指令卡读的
   * 是同一串（issue #3078）——三处各写一套，就是三个会各自漂移的答案。
   */
  usage?: string;
}

/** 指令注册表：cmd → { hint, phase }（hint 为 i18n 词典键，显示时经 t() 解析） */
export const COMMANDS: Record<string, CommandMeta> = {
  "/clear": { hint: "cmd.clear.hint", phase: 1 },
  "/compact": { hint: "cmd.compact.hint", phase: 1 },
  "/version": { hint: "cmd.version.hint", phase: 1 },
  "/help": { hint: "cmd.help.hint", phase: 1 },
  "/image": { hint: "cmd.image.hint", phase: 1 },
  "/delete": { hint: "cmd.delete.hint", phase: 2, usage: "cmd.delete.usage" },
  "/rename": { hint: "cmd.rename.hint", phase: 2, usage: "cmd.rename.usage" },
  "/resume": { hint: "cmd.resume.hint", phase: 2, usage: "cmd.resume.usage" },
  "/rewind": { hint: "cmd.rewind.hint", phase: 2 },
  "/sessions": { hint: "cmd.sessions.hint", phase: 2 },
  "/open": { hint: "cmd.open.hint", phase: 4 },
  "/model": { hint: "cmd.model.hint", phase: 3, usage: "cmd.model.usage" },
  "/memory": { hint: "cmd.memory.hint", phase: 3, usage: "cmd.memory.usage" },
  "/skills": { hint: "cmd.skills.hint", phase: 3 },
  "/rant": { hint: "cmd.rant.hint", phase: 4 },
  "/trigger": { hint: "cmd.trigger.hint", phase: 4 },
  // The GUI's `/sandbox` (rant 2026-10-09T09:43:39, GUI half). It is the same
  // command the TUI gained in Part 1, and the same three sub-ops — the daemon
  // judges the path either way, so the two front ends differ only in where the
  // answer is rendered (TUI: a chat line; GUI: the dialog and its list).
  "/sandbox": { hint: "cmd.sandbox.hint", phase: 4, usage: "cmd.sandbox.usage" },
};

export type ParseResult =
  | { type: "message" }
  | { type: "command"; cmd: string; args: string[] }
  | { type: "unknown"; cmd: string };

/** 一个指令词的形状：`/` + 单词（不含第二段斜杠、点号等路径特征） */
const COMMAND_WORD = /^\/[a-z0-9][a-z0-9_-]*$/;

/**
 * 解析输入：/ 开头且匹配注册表 → { type:"command", cmd, args }；
 * / 开头的**指令词**未匹配 → { type:"unknown", cmd }；否则 { type:"message" }。
 *
 * 「指令词」是 `/` 加一个单词。这条界线是附件逼出来的（issue #2920）：非图片附件落盘后
 * 正文里就是它的绝对路径，而附着在空输入框里的第一行恰好以 `/` 开头——`/tmp/notes.txt`
 * 曾被当成一条打错的指令吞掉，宿主看到「未知指令」而不是自己的消息。路径不是指令，所以
 * 带第二段斜杠或点号的东西一律是消息；`/foobar` 仍然按打错的指令报出来。
 */
export function parseInput(text: unknown): ParseResult {
  const t = String(text ?? "").trim();
  if (!t.startsWith("/")) return { type: "message" };
  const [cmd, ...args] = t.split(/\s+/);
  const key = cmd.toLowerCase();
  if (COMMANDS[key]) return { type: "command", cmd: key, args };
  if (!COMMAND_WORD.test(key)) return { type: "message" };
  return { type: "unknown", cmd: key };
}


/** 解析 i18n hint：词典键 → 当前语言文案（t 由调用方注入，i18n 未就绪时回退键本身） */
export function hintText(cmd: string, t: TranslateFn): string {
  const meta = COMMANDS[cmd];
  if (!meta) return "";
  try {
    return t(meta.hint);
  } catch {
    return meta.hint;
  }
}

/**
 * 参数用法：i18n 键 → 当前语言文案；没有参数可写的指令回空串。
 * 补全清单、`/help`、指令卡三处都调这一个函数，所以它们不可能各说一套。
 */
export function usageText(cmd: string, t: TranslateFn): string {
  const meta = COMMANDS[cmd];
  if (!meta?.usage) return "";
  try {
    return t(meta.usage);
  } catch {
    return meta.usage;
  }
}

/** / 前缀补全：返回匹配的指令（含 hint 与参数用法），供补全菜单过滤 */
export function getCompletions(
  prefix: string,
  t: TranslateFn,
): Array<{ cmd: string; hint: string; usage: string; phase: number }> {
  const p = String(prefix ?? "").toLowerCase();
  return Object.entries(COMMANDS)
    .filter(([cmd]) => cmd.startsWith(p))
    .map(([cmd, meta]) => ({
      cmd,
      hint: hintText(cmd, t),
      usage: usageText(cmd, t),
      phase: meta.phase,
    }));
}
