"use strict";
/**
 * copywriting.js — 友好文案映射（docs/design/gui-redesign.md §4.2 工具过程 / §5.1 文案规范）
 * 按 tool_name 映射动词短语（面向非开发者，零技术黑话）。
 * 状态三态：doing（进行中）/ done（完成）/ fail（失败——统一鼓励性措辞，不责怪用户）。
 */
const TOOL_PHRASES = {
  bash:    { doing: "正在运行命令…", done: "命令已执行",     fail: "这一步没成功，我换个方法试试" },
  read:    { doing: "正在读取文件…", done: "已读取文件",     fail: "这一步没成功，我换个方法试试" },
  write:   { doing: "正在写入文件…", done: "文件已保存",     fail: "这一步没成功，我换个方法试试" },
  edit:    { doing: "正在修改文件…", done: "文件已修改",     fail: "这一步没成功，我换个方法试试" },
  glob:    { doing: "正在查找文件…", done: "文件查找完成",   fail: "这一步没成功，我换个方法试试" },
  grep:    { doing: "正在搜索内容…", done: "搜索完成",       fail: "这一步没成功，我换个方法试试" },
  browser: { doing: "正在浏览网页…", done: "网页操作完成",   fail: "这一步没成功，我换个方法试试" },
  default: { doing: "正在处理…",     done: "处理完成",       fail: "这一步没成功，我换个方法试试" },
};

/** 取工具友好短语：toolPhrase(toolName, "doing" | "done" | "fail") */
function toolPhrase(toolName, kind) {
  const p = TOOL_PHRASES[toolName] || TOOL_PHRASES.default;
  return p[kind] || TOOL_PHRASES.default[kind];
}
