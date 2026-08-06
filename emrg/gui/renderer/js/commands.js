"use strict";
/**
 * commands.js — GUI / 指令注册表与解析器（rant 19:44 P1：解析器 + 补全 + 纯操作）
 * 对齐 TUI 15 个 / 指令。指令解析在 GUI 侧（renderer），daemon 零改动（协议已存在）。
 *
 * 阶段（分期）：
 *   phase 1 — 纯操作类（/clear /compact /version /help /image 提示）→ 已实现
 *   phase 2 — 会话类（/delete /rename /resume /rewind /sessions）→ 已实现
 *   phase 3 — 模型/记忆/技能类（/model /memory /skills）→ P3 已实现
 *   phase 4 — 演化类（/rant /trigger）→ P4
 */

const Commands = (() => {
  // 指令注册表：cmd → { hint, phase }
  const COMMANDS = {
    "/clear": { hint: "清空当前对话", phase: 1 },
    "/compact": { hint: "压缩当前对话历史", phase: 1 },
    "/version": { hint: "显示版本与实例信息", phase: 1 },
    "/help": { hint: "查看全部指令说明", phase: 1 },
    "/image": { hint: "发送图片（请直接粘贴）", phase: 1 },
    "/delete": { hint: "删除当前对话", phase: 2 },
    "/rename": { hint: "重命名当前对话", phase: 2 },
    "/resume": { hint: "切换/恢复对话", phase: 2 },
    "/rewind": { hint: "回退到历史消息点", phase: 2 },
    "/sessions": { hint: "查看全部对话", phase: 2 },
    "/model": { hint: "切换模型", phase: 3 },
    "/memory": { hint: "浏览记忆", phase: 3 },
    "/skills": { hint: "查看已加载技能", phase: 3 },
    "/rant": { hint: "驱动 EMRG 进化", phase: 4 },
    "/trigger": { hint: "触发后台任务", phase: 4 },
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

  /** / 前缀补全：返回匹配的指令（含 hint），供补全菜单过滤 */
  function getCompletions(prefix) {
    const p = String(prefix || "").toLowerCase();
    return Object.entries(COMMANDS)
      .filter(([cmd]) => cmd.startsWith(p))
      .map(([cmd, meta]) => ({ cmd, hint: meta.hint, phase: meta.phase }));
  }

  return { COMMANDS, parseInput, getCompletions };
})();

window.EMRG_Commands = Commands;
