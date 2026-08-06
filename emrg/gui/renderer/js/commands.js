"use strict";
/**
 * commands.js — GUI / 指令注册表与解析器（rant 19:44 P1：解析器 + 补全 + 纯操作）
 * 对齐 TUI 15 个 / 指令。指令解析在 GUI 侧（renderer），daemon 零改动（协议已存在）。
 *
 * 阶段（分期）：
 *   phase 1 — 纯操作类（/clear /compact /version /help /image 提示）→ 已实现
 *   phase 2 — 会话类（/delete /rename /resume /rewind /sessions）→ 已实现
 *   phase 3 — 模型/记忆/技能类（/model /memory /skills）→ 已实现
 *   phase 4 — 演化类（/rant /trigger）→ 已实现（全部 15 指令完成）
 */

const Commands = (() => {
  // 指令注册表：cmd → { hint, phase }
  // hint 为 i18n 词典键（rant 21:19）：显示时经 t() 解析为当前语言文案
  const COMMANDS = {
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
    "/model": { hint: "cmd.model.hint", phase: 3 },
    "/memory": { hint: "cmd.memory.hint", phase: 3 },
    "/skills": { hint: "cmd.skills.hint", phase: 3 },
    "/rant": { hint: "cmd.rant.hint", phase: 4 },
    "/trigger": { hint: "cmd.trigger.hint", phase: 4 },
  };

  /**
   * 解析输入：/ 开头且匹配注册表 → { type:"command", cmd, args }；
   * / 开头未匹配 → { type:"unknown", cmd }；否则 { type:"message" }。
   */
  function parseInput(text) {
    const t = String(text || "").trim();
    if (!t.startsWith("/")) return { type: "message" };
    const [cmd, ...args] = t.split(/\s+/);
    const key = cmd.toLowerCase();
    if (COMMANDS[key]) return { type: "command", cmd: key, args };
    return { type: "unknown", cmd: key };
  }

  /** 解析 i18n hint：词典键 → 当前语言文案 */
  function hintText(cmd) {
    const meta = COMMANDS[cmd];
    if (!meta) return "";
    try {
      return window.EMRG_I18N ? window.EMRG_I18N.t(meta.hint) : meta.hint;
    } catch { return meta.hint; }
  }

  /** / 前缀补全：返回匹配的指令（含 hint），供补全菜单过滤 */
  function getCompletions(prefix) {
    const p = String(prefix || "").toLowerCase();
    return Object.entries(COMMANDS)
      .filter(([cmd]) => cmd.startsWith(p))
      .map(([cmd, meta]) => ({ cmd, hint: hintText(cmd), phase: meta.phase }));
  }

  return { COMMANDS, parseInput, getCompletions, hintText };
})();

window.EMRG_Commands = Commands;
