"use strict";
/**
 * copywriting.js — 去黑话文案映射（非开发者友好）。
 * 工具名 → 友好的动词短语；系统级状态 → 口语化鼓励性文案。
 */

/** 工具名 → 进行中 / 完成 短语（渐进披露：默认折叠，点开展示原始输出） */
const TOOL_PHRASES = {
  bash: { doing: "正在运行命令…", done: "已运行命令" },
  read: { doing: "正在读取文件…", done: "已读取文件" },
  write: { doing: "正在写入文件…", done: "已写入文件" },
  edit: { doing: "正在修改文件…", done: "已修改文件" },
  glob: { doing: "正在查找文件…", done: "已找到文件" },
  grep: { doing: "正在搜索内容…", done: "已完成搜索" },
};

function toolPhrases(name) {
  return TOOL_PHRASES[name] || { doing: "正在处理…", done: "已完成" };
}

/** 工具失败（不 blame 用户，给出下一步） */
const TOOL_FAIL_TEXT = "这一步没成功，我换个方法试试";

/** 系统状态文案 */
const COPY = {
  disconnected: "连接中断了，正在重新连接…",
  reconnected: "回来了，我们继续 ✦",
  sessionBusy: "我还在处理上一条，稍等一下哦",
  sendFailed: "没发送成功，你的话我还留着，再试一次？",
  deleteConfirmTitle: "删除这段对话？",
  deleteConfirmBody: "删除后无法恢复。",
  noSessions: "还没有对话",
  aboutEvolution: (n) => (n ? `EMRG 已自我成长 ${n} 次，感谢你的每一次反馈` : "EMRG 正在成长中"),
};

window.EMRG_Copy = { toolPhrases, TOOL_FAIL_TEXT, COPY };
