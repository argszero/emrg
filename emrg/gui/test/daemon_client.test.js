"use strict";
/**
 * daemon_client.test.js — DaemonClient 单元测试（node:test，G99 定案）。
 * 零依赖：mock ws（Module._load 注入）+ 临时 HOME + 无真实 daemon。
 *
 * 覆盖（设计文档 §6.1）：
 * - ensureConnected：port 文件读取 + auth 首帧 + auth_ok 处理
 * - 坏 JSON 帧 → 忽略不崩
 * - ws close → 触发 disconnected 事件（重连回调由 main 层调度）
 * - sendTask：payload（type=task + session_id + prompt + images + id + stream:true）
 * - sendCommand：payload（type + params）；cancel 无多余字段（G24）
 * - 帧分类（G21+G58）：tool_start/tool_end/delta/done/cancelled/error/pong/
 *   list_result/command_result 各帧正确分类
 * - 命令-响应配对（G93+G103）：配对 resolve / 超时 reject / error FIFO reject /
 *   无未决 error → 广播事件
 * - 分组生命周期（G83+G104）：tool_start/delta 建组 → done 清理；>20 丢最老
 * - 断连 pending 请求 reject（G89/G90）
 * - generateSessionId 格式 + 碰撞兜底（G28+G81）
 * - isRunning：TCP 探测（mock net.connect，G43/G90）
 */

const { test, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert");
const Module = require("node:module");
const net = require("node:net");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// ── mock ws ──────────────────────────────────────────────
let currentMockWs = null;
class MockWs {
  constructor(url, opts) {
    this.url = url;
    this.opts = opts || {};
    this.sent = [];
    this._listeners = {};
    this.readyState = 0;
    currentMockWs = this; // 捕获最新实例（stale port 重拉后更新）
  }
  on(ev, cb) { (this._listeners[ev] = this._listeners[ev] || []).push(cb); return this; }
  once(ev, cb) {
    const wrap = (...a) => { this.off(ev, wrap); cb(...a); };
    wrap._orig = cb;
    return this.on(ev, wrap);
  }
  off(ev, cb) {
    if (!this._listeners[ev]) return this;
    this._listeners[ev] = this._listeners[ev].filter((l) => l !== cb && l._orig !== cb);
    return this;
  }
  emit(ev, ...a) { for (const cb of [...(this._listeners[ev] || [])]) cb(...a); return this; }
  send(data) { this.sent.push(typeof data === "string" ? data : JSON.stringify(data)); }
  close() { this.emit("close"); }
  destroy() { this.emit("close"); }
}

const origLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "ws") return MockWs;
  return origLoad.apply(this, arguments);
};
const { DaemonClient, generateSessionId, PORT_FILE } = require("../daemon_client.js");
let tmpHome = null;
let origHome = null;

function setupTempHome() {
  tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), "emrg-gui-test-"));
  fs.mkdirSync(path.join(tmpHome, ".emrg"), { recursive: true });
  origHome = process.env.HOME;
  process.env.HOME = tmpHome;
  // 预写 port 文件（模拟已运行 daemon）
  fs.writeFileSync(PORT_FILE(), "41234\nseekrit-token");
}

function teardownTempHome() {
  if (origHome !== undefined) process.env.HOME = origHome; else delete process.env.HOME;
  if (tmpHome) fs.rmSync(tmpHome, { recursive: true, force: true });
  tmpHome = null;
}

/** 等待 currentMockWs 创建（轮询，超时 2s） */
async function waitForWs(predicate = () => currentMockWs) {
  const deadline = Date.now() + 2000;
  while (Date.now() < deadline) {
    if (predicate()) return currentMockWs;
    await new Promise((r) => setTimeout(r, 5));
  }
  assert.ok(false, "ws not created within timeout");
}

/** 等待 auth 帧发送（send 在 open resolve 微任务后） */
async function waitForAuthSent(ws) {
  const deadline = Date.now() + 2000;
  while (ws.sent.length === 0 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 5));
  }
  assert.ok(ws.sent.length > 0, "auth frame should be sent");
}

/** 建立已连接 client（模拟 auth 完成） */
async function connectClient(client) {
  const p = client.ensureConnected();
  // 驱动 mock：等 ws 创建 → open → 收 auth → 回 auth_ok
  await waitForWs();
  currentMockWs.emit("open");
  await waitForAuthSent(currentMockWs);
  const authFrame = JSON.parse(currentMockWs.sent[0]);
  assert.strictEqual(authFrame.type, "auth");
  assert.strictEqual(authFrame.token, "seekrit-token");
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "auth_ok" })));
  await p;
  return currentMockWs;
}

beforeEach(() => {
  setupTempHome();
  currentMockWs = null;
});
afterEach(() => {
  teardownTempHome();
  currentMockWs = null;
});

test("ensureConnected: port 文件读取 + auth 首帧 + auth_ok", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  await connectClient(client);
  assert.strictEqual(client.connected, true);
  assert.strictEqual(client._authFailed, false);
});

test("ensureConnected: port 文件缺失 → 拉起 daemon（spawn 参数正确 G28/G68/G125）", async () => {
  fs.rmSync(PORT_FILE(), { force: true });
  const client = new DaemonClient({ projectDir: tmpHome });
  // stub startDaemon：模拟拉起后写 port 文件
  let spawnCalls = null;
  client.startDaemon = async function () {
    spawnCalls = { python: this._findPython(), projectDir: this.projectDir };
    fs.writeFileSync(PORT_FILE(), "41235\nseekrit-token");
  };
  await connectClient(client);
  assert.ok(spawnCalls, "startDaemon should be called");
  assert.ok(spawnCalls.python.endsWith(path.join(".venv", "bin", "python")), `python=${spawnCalls.python}`);
  assert.strictEqual(spawnCalls.projectDir, tmpHome);
});

test("G43 stale port: 连接失败（port 文件存在但拒绝）→ 删文件重拉", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  let respawned = false;
  client.startDaemon = async function () {
    respawned = true;
    fs.writeFileSync(PORT_FILE(), "41236\nseekrit-token");
  };
  const p = client.ensureConnected();
  await waitForWs();
  const firstWs = currentMockWs;
  firstWs.emit("error", new Error("connect ECONNREFUSED"));
  // 重拉后创建第二个 ws → open → auth → auth_ok
  await waitForWs(() => currentMockWs !== firstWs);
  assert.ok(respawned, "startDaemon should respawn after stale port");
  assert.strictEqual(fs.existsSync(PORT_FILE()), true);
  assert.strictEqual(currentMockWs.url, "ws://127.0.0.1:41236");
  currentMockWs.emit("open");
  await waitForAuthSent(currentMockWs);
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "auth_ok" })));
  await p;
  assert.strictEqual(client.connected, true);
});

test("auth 失败（G88）：auth_ok 前 close → 停止自动重试", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  const p = client.ensureConnected();
  await waitForWs();
  currentMockWs.emit("open");
  await waitForAuthSent(currentMockWs);
  currentMockWs.emit("close"); // auth 前断开
  await assert.rejects(p, /authentication failed/);
  assert.strictEqual(client._authFailed, true, "auth failed flag set → main 停止重连");
});

test("坏 JSON 帧 → 忽略不崩（R53）", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  await connectClient(client);
  const events = [];
  client.onEvent((t) => events.push(t));
  currentMockWs.emit("message", Buffer.from("{bad json"));
  currentMockWs.emit("message", Buffer.from("not json at all"));
  assert.deepStrictEqual(events, []);
  assert.strictEqual(client.connected, true);
});

test("ws close → disconnected 事件 + pending reject（G89/G90）", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  await connectClient(client);
  const events = [];
  client.onEvent((t) => events.push(t));
  const pending = client.sendCommandAndWait("list_sessions", { cwd: tmpHome }, 5000);
  currentMockWs.emit("close");
  await assert.rejects(pending, /connection closed/);
  assert.ok(events.includes("disconnected"));
  assert.strictEqual(client.connected, false);
});

test("sendTask payload（G32/G96）", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  await connectClient(client);
  const rid = client.sendTask({ sessionId: "s_260803_1730_abcd1234", cwd: "/proj", prompt: "hello", images: null });
  const frame = JSON.parse(currentMockWs.sent.at(-1));
  assert.strictEqual(frame.type, "task");
  assert.strictEqual(frame.id, rid, "request_id 作为 id 字段");
  assert.strictEqual(frame.session_id, "s_260803_1730_abcd1234");
  assert.strictEqual(frame.cwd, "/proj");
  assert.strictEqual(frame.prompt, "hello");
  assert.strictEqual(frame.stream, true, "stream 显式 true（G96）");
  assert.strictEqual(frame.images, null);
  assert.ok(frame.timestamp);
});

test("sendCommand payload + cancel 无多余字段（G24）", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  await connectClient(client);
  client.sendCommand("cancel");
  const frame = JSON.parse(currentMockWs.sent.at(-1));
  assert.deepStrictEqual(frame, { type: "cancel" });
  client.sendCommand("set_model", { model: "gpt-4o" });
  const f2 = JSON.parse(currentMockWs.sent.at(-1));
  assert.deepStrictEqual(f2, { type: "set_model", model: "gpt-4o" });
});

test("帧分类（G21+G58）：各帧事件分发正确", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  await connectClient(client);
  const seen = [];
  client.onEvent((type, data) => seen.push([type, data]));

  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  const rid = "req-1";
  send({ type: "tool_start", request_id: rid, tool_name: "bash", tool_call_id: "tc1", arguments: {} });
  send({ type: "tool_end", request_id: rid, tool_name: "bash", tool_call_id: "tc1", content: "out", error: false });
  send({ request_id: rid, content: "hi", done: false, delta: true });
  send({ request_id: rid, content: "hi", done: true, delta: false });
  send({ type: "cancelled" });
  send({ error: "boom" });
  send({ identity: { instance_id: "i1" }, uptime_seconds: 10, evolution_count: 3, model: "m1" });
  send({ type: "sessions_list", sessions: [] });
  send({ type: "resume_result", session_id: "s1" });
  send({ type: "model_set", model: "m2" });
  send({ type: "session_deleted", session_id: "s1" });

  const types = seen.map(([t]) => t);
  assert.deepStrictEqual(types, [
    "tool_started", "tool_finished", "message_delta", "group_cleared", "done", "cancelled", "error", "pong",
    "list_result", "command_result", "command_result", "command_result",
  ]);
  // pong 帧数据
  const pong = seen.find(([t]) => t === "pong")[1];
  assert.strictEqual(pong.identity.instance_id, "i1");
  assert.strictEqual(pong.uptime_seconds, 10);
  assert.strictEqual(pong.model, "m1");
  // resume_result/model_set/session_deleted 落 command_result
  const cmd = seen.filter(([t]) => t === "command_result");
  assert.strictEqual(cmd.length, 3);
});

test("命令-响应配对（G93）：list_sessions → sessions_list resolve", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  await connectClient(client);
  const p = client.sendCommandAndWait("list_sessions", { cwd: tmpHome }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "sessions_list", sessions: [{ session_id: "s1" }] })));
  const res = await p;
  assert.strictEqual(res.type, "sessions_list");
  assert.strictEqual(res.sessions.length, 1);
});

test("命令-响应配对超时 → reject（G93）", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  await connectClient(client);
  await assert.rejects(
    client.sendCommandAndWait("list_sessions", { cwd: tmpHome }, 50),
    /command timeout: list_sessions/
  );
});

test("error 帧 FIFO reject 最早未决（G103）；无未决 error → 广播", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  await connectClient(client);
  const seen = [];
  client.onEvent((t, d) => seen.push([t, d]));
  // 两个未决命令，error 帧 reject 最早
  const p1 = client.sendCommandAndWait("list_sessions", { cwd: tmpHome }, 2000);
  const p2 = client.sendCommandAndWait("list_projects", {}, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ error: "first failed" })));
  await assert.rejects(p1, /first failed/);
  assert.strictEqual(client._pending.size, 1, "p2 仍未决");
  // resolve p2 后无未决，error → 广播事件
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "projects_list", projects: [] })));
  await p2;
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ error: "broadcast error" })));
  assert.ok(seen.some(([t, d]) => t === "error" && d.error === "broadcast error"));
});

test("分组生命周期（G83+G104）：建组 → done 清理；>20 丢最老", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  await connectClient(client);
  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  // tool_start 建组（G104）
  send({ type: "tool_start", request_id: "r1", tool_name: "bash", tool_call_id: "t1" });
  assert.strictEqual(client._groups.size, 1);
  send({ request_id: "r2", content: "x", done: false, delta: true });
  assert.strictEqual(client._groups.size, 2);
  send({ request_id: "r1", content: "done", done: true, delta: false });
  assert.strictEqual(client._groups.has("r1"), false, "done 清理分组");
  assert.strictEqual(client._groups.has("r2"), true);
  // >20 丢最老
  for (let i = 0; i < 30; i++) send({ request_id: `bulk-${i}`, content: "", done: false, delta: true });
  assert.ok(client._groups.size <= 20, `group size ${client._groups.size} capped at 20`);
  // G110：clearGroups 清空全部（含 timer）
  const cleared = [];
  client.onEvent((t, d) => { if (t === "group_cleared") cleared.push(d.requestId); });
  client.clearGroups();
  assert.strictEqual(client._groups.size, 0, "clearGroups empties all groups");
  assert.ok(cleared.length >= 2, `group_cleared emitted for ${cleared.length} groups`);
});

test("generateSessionId 格式（G28+G81）", () => {
  const sid = generateSessionId();
  assert.match(sid, /^s_\d{6}_\d{4}_[0-9a-f]{8}$/);
  // 100 次调用无重复（随机碰撞兜底）
  const seen = new Set();
  for (let i = 0; i < 100; i++) seen.add(generateSessionId());
  assert.strictEqual(seen.size, 100);
});

test("isRunning：TCP 探测（G43/G90）", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  // mock net.connect：成功 → true
  const origConnect = net.connect;
  net.connect = (opts) => {
    const sock = new (require("node:events").EventEmitter)();
    sock.destroy = () => {};
    setTimeout(() => sock.emit("connect"), 1);
    return sock;
  };
  try {
    assert.strictEqual(await client.isRunning(), true);
  } finally {
    net.connect = origConnect;
  }
  // port 文件缺失 → false
  fs.rmSync(PORT_FILE(), { force: true });
  assert.strictEqual(await client.isRunning(), false);
});

test("断连 pending 请求全部 reject + disconnected（G89）", async () => {
  const client = new DaemonClient({ projectDir: tmpHome });
  await connectClient(client);
  const pending = [
    client.sendCommandAndWait("list_sessions", { cwd: tmpHome }, 5000),
    client.sendCommandAndWait("list_projects", {}, 5000),
  ];
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("close");
  for (const p of pending) await assert.rejects(p, /connection closed/);
  assert.strictEqual(client._pending.size, 0);
  assert.strictEqual(client._pendingFifo.length, 0);
});
