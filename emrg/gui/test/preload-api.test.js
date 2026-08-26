"use strict";
/**
 * preload-api.test.js — window.emrg preload 桥 API 面守卫测试（React 迁移红线 #1）。
 *
 * 背景（cycle cyc20260826-131007 发现）：React 迁移设计文档 §2.2 声称 preload
 * API 46 个方法，实际 preload.js 已增长到 52 个 invoke 方法 + onEvent 订阅
 * （设计文档口径过时）。此前 build-config.test.js 只抽查 listFiles/readFile 两个
 * API——没有完整 API 面的回归守卫。React 化迁移（Batch 0-5）的红线就是
 * "window.emrg 方法集合原样保留"，本测试把整个契约钉死：
 *
 *   1. 全部 52 个 invoke 方法 + onEvent 必须存在（防误删/改名）
 *   2. 每个方法必须调用 ipcRenderer.invoke("emrg:<同名频道>")（防频道漂移）
 *   3. onEvent 必须订阅 emrg:event 广播（防事件通道漂移）
 *
 * 实现：静态解析 preload.js 的 api 对象字面量（vm 执行会因 require("electron")
 * 失败，故用正则提取 api 块的键与调用），与冻结的期望清单比对。
 */

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const PRELOAD = fs.readFileSync(path.join(__dirname, "..", "preload.js"), "utf8");

/** 冻结的期望 API 面：52 invoke 方法（按 preload.js 实际顺序）+ onEvent */
const EXPECTED_INVOKE_METHODS = [
  "init",
  "sendMessage",
  "listSessions",
  "restartDaemon",
  "switchSession",
  "newSession",
  "deleteSession",
  "closeSession",
  "getOpenSessions",
  "renameSession",
  "setModel",
  "clearSession",
  "compactSession",
  "listHistory",
  "rewindSession",
  "listMemories",
  "readMemory",
  "listFiles",
  "readFile",
  "previewHtml",
  "closePreview",
  "panelResized",
  "getPreviewState",
  "listSkills",
  "listProjects",
  "listProjectSessions",
  "registerProject",
  "removeProject",
  "listTasks",
  "triggerTask",
  "taskCreate",
  "taskUpdate",
  "taskDelete",
  "taskTemplateList",
  "taskTemplateCreate",
  "taskTemplateUpdate",
  "taskTemplateDelete",
  "sendRant",
  "listRants",
  "evolutionSummary",
  "githubStatus",
  "githubConnect",
  "githubDisconnect",
  "githubConnectWeb",
  "openExternal",
  "listModels",
  "openFile",
  "saveSettings",
  "getSettings",
  "cancel",
  "pickProjectDir",
  "log",
];

const EXPECTED_EVENT_SUBSCRIPTIONS = ["onEvent"];

/** 提取 api 对象字面量块（const api = { ... };） */
function extractApiBlock() {
  const m = PRELOAD.match(/const api = \{([\s\S]*?)\n\};/);
  assert.ok(m, "preload.js 中未找到 const api = { ... } 对象");
  return m[1];
}

/** 解析 api 块 → { invoke: Map<方法名, 频道>, subs: Map<方法名, 事件频道> } */
function parseApiBlock(block) {
  const invoke = new Map();
  const subs = new Map();
  for (const line of block.split("\n")) {
    // 普通 invoke 方法：  name: (...) => ipcRenderer.invoke("emrg:xxx", ...)
    const invokeMatch = line.match(
      /^\s{2}(\w+):\s*\([^)]*\)\s*=>\s*ipcRenderer\.invoke\("([^"]+)"\s*[,)]/,
    );
    if (invokeMatch) {
      invoke.set(invokeMatch[1], invokeMatch[2]);
      continue;
    }
    // onEvent 订阅：  onEvent: (cb) => { ... ipcRenderer.on("emrg:event", ...) }
    const subMatch = line.match(/^\s{2}(\w+):\s*\([^)]*\)\s*=>\s*\{/);
    if (subMatch) {
      subs.set(subMatch[1], "");
    }
  }
  // onEvent 的订阅频道从函数体内提取
  const onEventBody = block.match(/onEvent:[\s\S]*?ipcRenderer\.on\("([^"]+)"\s*,/);
  if (onEventBody) subs.set("onEvent", onEventBody[1]);
  return { invoke, subs };
}

const api = parseApiBlock(extractApiBlock());

test("window.emrg exposes the full 53-member contract (52 invoke + onEvent)", () => {
  // 52 invoke 方法全部存在（防误删/改名）
  for (const name of EXPECTED_INVOKE_METHODS) {
    assert.ok(api.invoke.has(name), `缺少 invoke 方法 ${name}`);
  }
  assert.strictEqual(api.invoke.size, EXPECTED_INVOKE_METHODS.length, "invoke 方法数不符（新方法需同步更新本测试的冻结清单）");
  // onEvent 订阅存在
  for (const name of EXPECTED_EVENT_SUBSCRIPTIONS) {
    assert.ok(api.subs.has(name), `缺少订阅方法 ${name}`);
  }
  // 没有多余/未知的方法（防意外新增破坏契约——新增 API 必须显式更新冻结清单）
  const allNames = new Set([...api.invoke.keys(), ...api.subs.keys()]);
  const expectedAll = new Set([...EXPECTED_INVOKE_METHODS, ...EXPECTED_EVENT_SUBSCRIPTIONS]);
  for (const name of allNames) {
    assert.ok(expectedAll.has(name), `api 中出现未在冻结清单中的方法 ${name}`);
  }
});

test("every invoke method maps to the emrg:<name> channel (no channel drift)", () => {
  for (const [name, channel] of api.invoke) {
    assert.strictEqual(channel, `emrg:${name}`, `方法 ${name} 的 IPC 频道 ${channel} 应为 emrg:${name}`);
  }
});

test("onEvent subscribes to the emrg:event broadcast channel", () => {
  assert.strictEqual(api.subs.get("onEvent"), "emrg:event", "onEvent 应订阅 emrg:event 广播频道");
});
