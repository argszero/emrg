import type { TranslateFn } from "./utils";

/**
 * commands.ts — / 指令注册表与解析器（vanilla renderer/js/commands.js 迁移，Batch 1）。
 * 纯逻辑、不绑 DOM：React 组件与测试均可直接 import。
 * 契约与 vanilla 版完全一致（COMMANDS 16 条 / parseInput 三态 / getCompletions 前缀过滤）。
 */
export interface CommandMeta {
  hint: string;
  phase: number;
}

/** 指令注册表：cmd → { hint, phase }（hint 为 i18n 词典键，显示时经 t() 解析） */
export const COMMANDS: Record<string, CommandMeta> = {
  "/clear": { hint: "cmd.clear.hint", phase: 1 },
  "/compact": { hint: "cmd.compact.hint", phase: 1 },
  "/version": { hint: "cmd.version.hint", phase: 1 },
  "/help": { hint: "cmd.help.hint", phase: 1 },
  "/image": { hint: "cmd.image.hint", phase: 1 },
  "/delete": { hint: "cmd.delete.hint", phase: 2 },
  "/rename": { hint: "cmd.rename.hint", phase: 2 },
  "/resume": { hint: "cmd.resume.hint", phase: 2 },
  "/rewind": { hint: "cmd.rewind.hint", phase: 2 },
  "/sessions": { hint: "cmd.sessions.hint", phase: 2 },
  "/open": { hint: "cmd.open.hint", phase: 4 },
  "/model": { hint: "cmd.model.hint", phase: 3 },
  "/memory": { hint: "cmd.memory.hint", phase: 3 },
  "/skills": { hint: "cmd.skills.hint", phase: 3 },
  "/rant": { hint: "cmd.rant.hint", phase: 4 },
  "/trigger": { hint: "cmd.trigger.hint", phase: 4 },
};

export type ParseResult =
  | { type: "message" }
  | { type: "command"; cmd: string; args: string[] }
  | { type: "unknown"; cmd: string };

/**
 * 解析输入：/ 开头且匹配注册表 → { type:"command", cmd, args }；
 * / 开头未匹配 → { type:"unknown", cmd }；否则 { type:"message" }。
 */
export function parseInput(text: unknown): ParseResult {
  const t = String(text ?? "").trim();
  if (!t.startsWith("/")) return { type: "message" };
  const [cmd, ...args] = t.split(/\s+/);
  const key = cmd.toLowerCase();
  if (COMMANDS[key]) return { type: "command", cmd: key, args };
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

/** / 前缀补全：返回匹配的指令（含 hint），供补全菜单过滤 */
export function getCompletions(prefix: string, t: TranslateFn): Array<{ cmd: string; hint: string; phase: number }> {
  const p = String(prefix ?? "").toLowerCase();
  return Object.entries(COMMANDS)
    .filter(([cmd]) => cmd.startsWith(p))
    .map(([cmd, meta]) => ({ cmd, hint: hintText(cmd, t), phase: meta.phase }));
}
