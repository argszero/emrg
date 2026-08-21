"use strict";
/**
 * commands.test.js — / 指令注册表与解析器单测（rant 19:44 P1）。
 * 覆盖：15 指令注册完整性、parseInput 三态（message/command/unknown）、
 *       getCompletions 前缀过滤 + hint 透传。
 */

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const RENDERER_JS = path.join(__dirname, "..", "renderer", "js");

function loadCommands() {
  const win = { window: null, console };
  win.window = win;
  const ctx = vm.createContext(win);
  const code = fs.readFileSync(path.join(RENDERER_JS, "commands.js"), "utf8");
  vm.runInContext(code, ctx, { filename: "renderer/js/commands.js" });
  return vm.runInContext("EMRG_Commands", ctx);
}

test("注册表含 TUI 全部 15 个 / 指令 + /open（rant 19:44 验收 + P5 扩展）", () => {
  const Commands = loadCommands();
  const expected = [
    "/clear", "/compact", "/delete", "/help", "/image", "/memory", "/model",
    "/open", "/rant", "/rename", "/resume", "/rewind", "/sessions", "/skills", "/trigger", "/version",
  ];
  for (const cmd of expected) {
    assert.ok(Commands.COMMANDS[cmd], `缺指令 ${cmd}`);
  }
  assert.strictEqual(Object.keys(Commands.COMMANDS).length, 16);
  // 每条指令都有 hint（补全菜单展示用）
  for (const [cmd, meta] of Object.entries(Commands.COMMANDS)) {
    assert.ok(meta.hint && meta.hint.length > 0, `${cmd} 缺 hint`);
    assert.ok(meta.phase >= 1 && meta.phase <= 4, `${cmd} phase 应在 1-4`);
  }
});

test("parseInput：普通消息 / 已知指令（含参数）/ 未知指令", () => {
  const Commands = loadCommands();
  // vm 上下文对象跨 Realm → 逐字段断言（同 renderer.smoke.test.js 教训）
  // 普通消息
  let r = Commands.parseInput("你好，帮我写周报");
  assert.strictEqual(r.type, "message");
  r = Commands.parseInput("  你好  ");
  assert.strictEqual(r.type, "message");
  // 已知指令（大小写不敏感）
  r = Commands.parseInput("/clear");
  assert.strictEqual(r.type, "command");
  assert.strictEqual(r.cmd, "/clear");
  assert.strictEqual(r.args.length, 0);
  r = Commands.parseInput("/Clear");
  assert.strictEqual(r.type, "command");
  assert.strictEqual(r.cmd, "/clear");
  r = Commands.parseInput("/rant 希望支持主题切换");
  assert.strictEqual(r.type, "command");
  assert.strictEqual(r.cmd, "/rant");
  assert.strictEqual(r.args.length, 1);
  assert.strictEqual(r.args[0], "希望支持主题切换");
  // 未知指令
  r = Commands.parseInput("/foobar");
  assert.strictEqual(r.type, "unknown");
  assert.strictEqual(r.cmd, "/foobar");
});

test("getCompletions：前缀过滤 + 排序 + hint 透传", () => {
  const Commands = loadCommands();
  // 空前缀 → 全部 16 条
  assert.strictEqual(Commands.getCompletions("").length, 16);
  // /r 前缀 → /rant /rename /resume /rewind（spread 转宿主 Realm 数组再比较）
  const r = [...Commands.getCompletions("/r")].map((i) => String(i.cmd)).sort();
  assert.deepStrictEqual(r, ["/rant", "/rename", "/resume", "/rewind"].sort());
  // 精确匹配 /clear → 单条 + hint
  const c = Commands.getCompletions("/clear");
  assert.strictEqual(c.length, 1);
  assert.strictEqual(String(c[0].cmd), "/clear");
  assert.ok(String(c[0].hint).length > 0);
  // 无匹配 → 空
  assert.strictEqual([...Commands.getCompletions("/zzz")].length, 0);
});
