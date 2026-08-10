// conn-manager.test.js — P2 (rant 2026-08-10T15:07:19) connection manager contract
// open/close/get: daemon bootstrap + skipStart session connections + resume_session.

"use strict";

const assert = require("node:assert");
const { test, beforeEach, afterEach } = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");

// ── MockWs（与 daemon_client.test.js 同款）──────────────────────────────
let currentMockWs = null;

class MockWs {
  constructor(url) {
    this.url = url;
    this.sent = [];
    this._listeners = {};
    currentMockWs = this;
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

const { ConnManager } = require("../conn-manager.js");
const { DaemonClient } = require("../daemon_client.js");

let tmpHome = null;
let origHome = null;
let origUserProfile = null;

function setupTempHome() {
  tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), "emrg-conn-test-"));
  fs.mkdirSync(path.join(tmpHome, ".emrg"), { recursive: true });
  origHome = process.env.HOME;
  origUserProfile = process.env.USERPROFILE;
  process.env.HOME = tmpHome;
  process.env.USERPROFILE = tmpHome;
  // 预写 port 文件（模拟已运行 daemon）——路径必须落在 tmpHome 内
  const portFile = path.join(tmpHome, ".emrg", "emrgd.port");
  assert.ok(
    path.resolve(portFile).startsWith(path.resolve(tmpHome) + path.sep),
    "port file escapes tmpHome",
  );
  fs.writeFileSync(portFile, "41234\nseekrit-token");
}

function teardownTempHome() {
  if (origHome !== undefined) process.env.HOME = origHome; else delete process.env.HOME;
  if (origUserProfile !== undefined) process.env.USERPROFILE = origUserProfile; else delete process.env.USERPROFILE;
  if (tmpHome) fs.rmSync(tmpHome, { recursive: true, force: true });
  tmpHome = null;
}

async function waitForWs(predicate = () => currentMockWs) {
  const deadline = Date.now() + 2000;
  while (Date.now() < deadline) {
    if (predicate()) return currentMockWs;
    await new Promise((r) => setTimeout(r, 5));
  }
  assert.ok(false, "ws not created within timeout");
}

async function waitForAuthSent(ws) {
  const deadline = Date.now() + 2000;
  while (ws.sent.length === 0 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 5));
  }
  assert.ok(ws.sent.length > 0, "auth frame should be sent");
}

/** 驱动一条连接完成 auth（open → auth 帧 → auth_ok） */
async function driveAuth(ws) {
  ws.emit("open");
  await waitForAuthSent(ws);
  const authFrame = JSON.parse(ws.sent[0]);
  assert.strictEqual(authFrame.type, "auth");
  ws.emit("message", Buffer.from(JSON.stringify({ type: "auth_ok" })));
}

/** 驱动 manager.open 的完整两段流程：引导连接 + 会话连接（含 resume_session 响应） */
async function driveOpen(manager, sid, projectPath) {
  const p = manager.open(sid, projectPath);
  // ① 引导 client：连接 → auth → 关闭
  let bootWs = await waitForWs();
  await driveAuth(bootWs);
  // 等引导 close（ensureConnected resolve → close 触发 ws.close → 新连接可能已建）
  await new Promise((r) => setTimeout(r, 10));
  // ② 会话 client：连接 → auth → resume_session → resume_result
  let sessionWs = await waitForWs(() => currentMockWs !== bootWs);
  await driveAuth(sessionWs);
  // 等 resume_session 帧发出
  const deadline = Date.now() + 2000;
  while (sessionWs.sent.length < 2 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 5));
  }
  const resumeFrame = JSON.parse(sessionWs.sent.at(-1));
  assert.strictEqual(resumeFrame.type, "resume_session", "session open must resume_session");
  assert.strictEqual(resumeFrame.session_id, sid);
  assert.strictEqual(resumeFrame.cwd, projectPath);
  sessionWs.emit("message", Buffer.from(JSON.stringify({ type: "resume_result", session_id: sid })));
  const conn = await p;
  return { conn, sessionWs };
}

beforeEach(() => {
  setupTempHome();
  currentMockWs = null;
});
afterEach(() => {
  teardownTempHome();
  currentMockWs = null;
});

test("P2 open: 引导 daemon + skipStart 会话连接 + resume_session 自动订阅", async () => {
  const manager = new ConnManager({ projectDir: tmpHome });
  const { conn, sessionWs } = await driveOpen(manager, "sess-1", "/proj/a");

  assert.ok(conn instanceof DaemonClient);
  assert.strictEqual(conn.connected, true);
  assert.strictEqual(manager.get("sess-1"), conn);
  assert.deepStrictEqual(manager.all(), ["sess-1"]);
  // 会话连接 url = 预写 port 文件端口（daemon 未重启）
  assert.strictEqual(sessionWs.url, "ws://127.0.0.1:41234");
});

test("P2 open: 已打开 sid → 复用连接，不重复 resume_session", async () => {
  const manager = new ConnManager({ projectDir: tmpHome });
  const first = await driveOpen(manager, "sess-1", "/proj/a");
  const sentBefore = first.sessionWs.sent.length;
  const second = await manager.open("sess-1", "/proj/a");
  assert.strictEqual(second, first.conn, "same sid must reuse the same conn");
  assert.strictEqual(first.sessionWs.sent.length, sentBefore, "no second resume_session");
});

test("P2 get: 未打开 sid → null", async () => {
  const manager = new ConnManager({ projectDir: tmpHome });
  assert.strictEqual(manager.get("nope"), null);
});

test("P2 close: 断开并移除；再 get → null；重复 close → false", async () => {
  const manager = new ConnManager({ projectDir: tmpHome });
  const { conn } = await driveOpen(manager, "sess-1", "/proj/a");
  assert.strictEqual(manager.close("sess-1"), true);
  assert.strictEqual(manager.get("sess-1"), null);
  assert.strictEqual(conn.connected, false, "close must disconnect the ws");
  assert.strictEqual(manager.close("sess-1"), false, "second close returns false");
});

test("P2 closeAll: 全部关闭", async () => {
  const manager = new ConnManager({ projectDir: tmpHome });
  await driveOpen(manager, "sess-1", "/proj/a");
  await driveOpen(manager, "sess-2", "/proj/b");
  manager.closeAll();
  assert.strictEqual(manager.all().length, 0);
});
