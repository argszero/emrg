"use strict";
/**
 * daemon_client.test.js — DaemonClient 单元测试（node:test，G99 定案）。
 * 零依赖：mock ws（Module._load 注入）+ 临时 HOME + 无真实 daemon。
 *
 * 覆盖（设计文档 §6.1）：
 * - ensureConnected：token 文件读取 + auth 首帧 + auth_ok 处理
 * - 坏 JSON 帧 → 忽略不崩
 * - ws close → 触发 disconnected 事件（重连回调由 main 层调度）
 * - sendTask：payload（type=task + session_id + prompt + images + id，无 stream 字段——非 stream 路径已删）
 * - sendCommand：payload（type + params）；cancel 无多余字段（G24）
 * - 帧分类（G21+G58）：tool_start/tool_end/delta/done/cancelled/error/pong/
 *   list_result/command_result 各帧正确分类
 * - 命令-响应配对（G93+G103）：配对 resolve / 超时 reject / error FIFO reject /
 *   无未决 error → 广播事件
 * - 分组生命周期（G83+G104）：tool_start/delta 建组 → done 清理；>20 丢最老
 * - 断连 pending 请求 reject（G89/G90）
 * - generateSessionId 格式 + 碰撞兜底（G28+G81）
 * - isRunning：TCP 探测（mock net.connect，G43/G90）
 * - 启动失败诊断（issue #1283）：标记 {size,ino} / 本次启动的 log 差值 / 沉默子进程
 *   如实说没写 / 退出码与信号 / 已死子进程立即失败（对齐 daemon_manager.py #1279）
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
const { DaemonClient, generateSessionId, TOKEN_FILE, EMRGD_PORT, EMRGD_START_ERR } = require("../daemon_client.js");
let tmpHome = null;
let origHome = null;
let origUserProfile = null;

// G129 (rant 2026-08-09T08:03:46): 测试隔离守卫——写 token 文件前断言目标路径
// 位于临时目录内。Windows 上 Node os.homedir() 优先读 USERPROFILE（HOME 无效），
// 若无此守卫，TOKEN_FILE() 会解析到真实 ~/.emrg/emrgd.token，测试假值
// ("seekrit-token") 会覆盖真实 daemon 认证文件 → 演化周期 10h 连不上。
function assertTokenFileInTmp(tokenFile) {
  const resolved = path.resolve(tokenFile);
  const tmpResolved = path.resolve(tmpHome);
  assert.ok(
    resolved.startsWith(tmpResolved + path.sep),
    `token file ${resolved} escapes tmpHome ${tmpResolved} — refusing to write`,
  );
}

function setupTempHome() {
  tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), "emrg-gui-test-"));
  fs.mkdirSync(path.join(tmpHome, ".emrg"), { recursive: true });
  origHome = process.env.HOME;
  origUserProfile = process.env.USERPROFILE;
  process.env.HOME = tmpHome;
  // G129: Windows os.homedir() 读 USERPROFILE —— 必须一并重定向，否则
  // os.homedir() 仍返回真实用户目录（这是 10h daemon gap 的直接根因）。
  process.env.USERPROFILE = tmpHome;
  // 预写 token 文件（模拟已运行 daemon）—— 路径必须落在 tmpHome 内
  const tokenFile = TOKEN_FILE();
  assertTokenFileInTmp(tokenFile);
  fs.writeFileSync(tokenFile, "seekrit-token");
}

function teardownTempHome() {
  if (origHome !== undefined) process.env.HOME = origHome; else delete process.env.HOME;
  if (origUserProfile !== undefined) process.env.USERPROFILE = origUserProfile; else delete process.env.USERPROFILE;
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

test("ensureConnected: token 文件读取 + auth 首帧 + auth_ok", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  assert.strictEqual(client.connected, true);
  assert.strictEqual(client._authFailed, false);
});

test("ensureConnected: token 文件缺失 → 拉起 daemon（spawn 参数正确 G28/G68/G125）", async () => {
  fs.rmSync(TOKEN_FILE(), { force: true });
  const client = new DaemonClient();
  // stub startDaemon：模拟拉起后写 token 文件
  let spawnCalls = null;
  client.startDaemon = async function () {
    spawnCalls = { python: this._findPython(), cwd: os.homedir() };
    fs.writeFileSync(TOKEN_FILE(), "seekrit-token");
  };
  await connectClient(client);
  assert.ok(spawnCalls, "startDaemon should be called");
  // 平台自适应：POSIX = .venv/bin/python，Windows = .venv\Scripts\python.exe
  const pyPath = process.platform === "win32"
    ? path.join(".venv", "Scripts", "python.exe")
    : path.join(".venv", "bin", "python");
  assert.ok(spawnCalls.python.endsWith(pyPath), `python=${spawnCalls.python} (expected ${pyPath})`);
  assert.strictEqual(spawnCalls.cwd, os.homedir());
});

test("P2 deltaBatchMs: 批量合并 message_delta，终态前冲刷保序（rant 14:11）", async () => {
  const client = new DaemonClient({ deltaBatchMs: 16 });
  await connectClient(client);
  const seen = [];
  client.onEvent((type, data) => seen.push([type, data]));
  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  const rid = "req-b";
  // 多条 delta → 不即时发（批量模式），16ms 后合并为一次 {chunks}
  send({ request_id: rid, content: "a", done: false, delta: true });
  send({ request_id: rid, content: "b", done: false, delta: true });
  assert.deepStrictEqual(seen.filter(([t]) => t === "message_delta"), [],
    "delta must not emit immediately in batch mode");
  await new Promise((r) => setTimeout(r, 40));
  const deltas = seen.filter(([t]) => t === "message_delta");
  assert.strictEqual(deltas.length, 1, "deltas batched into one message_delta");
  assert.ok(Array.isArray(deltas[0][1].chunks), "batched payload has chunks array");
  assert.strictEqual(deltas[0][1].chunks.length, 2);
});

test("P2 deltaBatchMs: done 终态到达 → 先冲刷残留 delta 再发 done（顺序保证）", async () => {
  const client = new DaemonClient({ deltaBatchMs: 1000 }); // 定时器远未到期
  await connectClient(client);
  const seen = [];
  client.onEvent((type, data) => seen.push([type, data]));
  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  const rid = "req-c";
  send({ request_id: rid, content: "a", done: false, delta: true });
  send({ request_id: rid, content: "a", done: true, delta: false });
  // delta 必须出现在 done 之前（顺序保证：delta 不晚于终态）
  const idxDelta = seen.findIndex(([t]) => t === "message_delta");
  const idxDone = seen.findIndex(([t]) => t === "done");
  assert.ok(idxDelta >= 0, "delta flushed before done");
  assert.ok(idxDone > idxDelta, "done must come after flushed delta");
  assert.strictEqual(seen[idxDelta][1].chunks.length, 1);
});

test("P2 deltaBatchMs: cancelled 终态 → 冲刷残留 delta 再发 cancelled", async () => {
  const client = new DaemonClient({ deltaBatchMs: 1000 });
  await connectClient(client);
  const seen = [];
  client.onEvent((type, data) => seen.push([type, data]));
  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  send({ request_id: "req-d", content: "x", done: false, delta: true });
  send({ type: "cancelled" });
  const types = seen.map(([t]) => t);
  assert.ok(types.indexOf("message_delta") < types.indexOf("cancelled"),
    "delta must be flushed before cancelled");
  assert.strictEqual(seen.find(([t]) => t === "message_delta")[1].chunks.length, 1);
});

test("P2 deltaBatchMs: 默认 0 = 每帧即时发（既有行为回归）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  const seen = [];
  client.onEvent((type, data) => seen.push([type, data]));
  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  send({ request_id: "req-e", content: "a", done: false, delta: true });
  send({ request_id: "req-e", content: "b", done: false, delta: true });
  const deltas = seen.filter(([t]) => t === "message_delta");
  assert.strictEqual(deltas.length, 2, "default mode emits per frame");
  assert.ok(!Array.isArray(deltas[0][1].chunks), "default payload is the frame, not chunks");
});

test("P2 deltaBatchMs: close 冲刷残留 delta", async () => {
  const client = new DaemonClient({ deltaBatchMs: 1000 });
  await connectClient(client);
  const seen = [];
  client.onEvent((type, data) => seen.push([type, data]));
  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  send({ request_id: "req-f", content: "y", done: false, delta: true });
  client.close();
  const deltas = seen.filter(([t]) => t === "message_delta");
  assert.strictEqual(deltas.length, 1, "close must flush pending delta");
  assert.strictEqual(deltas[0][1].chunks.length, 1);
});

test("P2 deltaBatchMs: error 终态 → 冲刷残留 delta 再发 error", async () => {
  const client = new DaemonClient({ deltaBatchMs: 1000 });
  await connectClient(client);
  const seen = [];
  client.onEvent((type, data) => seen.push([type, data]));
  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  send({ request_id: "req-g", content: "z", done: false, delta: true });
  send({ error: "boom" });
  const types = seen.map(([t]) => t);
  assert.ok(types.indexOf("message_delta") < types.indexOf("error"),
    "delta must be flushed before error");
  assert.strictEqual(seen.find(([t]) => t === "message_delta")[1].chunks.length, 1);
});

test("P2 skipStart: token 文件缺失 → 抛错不拉起 daemon（connManager 独占 daemon 生命周期）", async () => {
  fs.rmSync(TOKEN_FILE(), { force: true });
  const client = new DaemonClient();
  let spawnCalls = 0;
  client.startDaemon = async function () {
    spawnCalls += 1;
    throw new Error("startDaemon must not be called with skipStart");
  };
  await assert.rejects(
    () => client.ensureConnected({ skipStart: true }),
    /daemon not running \(skipStart\): no token file/
  );
  assert.strictEqual(spawnCalls, 0, "startDaemon must never be called");
  assert.strictEqual(client.connected, false);
});

test("P2 skipStart: stale port + daemon 已死 → 抛错不重拉（不删文件不 spawn）", async () => {
  // 预写 token 文件（连接用常量端口，必然失败场景由 ws error 模拟）
  fs.writeFileSync(TOKEN_FILE(), "seekrit-token"); // 127.0.0.1:1 拒绝连接
  const client = new DaemonClient();
  // 端口探测 mock：端口不通 = daemon 死（rant 15:26:42 固定端口为准）
  const origConnect = net.connect;
  net.connect = (opts) => {
    const sock = new (require("node:events").EventEmitter)();
    sock.destroy = () => {};
    setTimeout(() => sock.emit("error", new Error("ECONNREFUSED")), 1);
    return sock;
  };
  try {
    // daemon 已死（端口不通）→ 旧路径会删文件重拉；skipStart 必须拒绝
    let spawnCalls = 0;
    client.startDaemon = async function () {
      spawnCalls += 1;
      throw new Error("startDaemon must not be called with skipStart");
    };
    const p = client.ensureConnected({ skipStart: true });
    await waitForWs();
    const firstWs = currentMockWs;
    firstWs.emit("error", new Error("connect ECONNREFUSED"));
    await assert.rejects(p, /daemon unreachable \(skipStart\)/);
    assert.strictEqual(spawnCalls, 0, "startDaemon must never be called");
    // token 文件保留（connManager 重启恢复依赖它判断 daemon 状态）
    assert.ok(fs.existsSync(TOKEN_FILE()), "port file must be kept");
    assert.strictEqual(client.connected, false);
  } finally {
    net.connect = origConnect;
  }
});

test("Phase4: _findDaemonExecutable 打包模式定位捆绑 emrgd（POSIX）", async () => {
  const client = new DaemonClient({ isPackaged: true });
  const exe = client._findDaemonExecutable();
  const expected = path.join(os.homedir(), ".emrg", "install", "bin", process.platform === "win32" ? "emrgd.cmd" : "emrgd");
  assert.strictEqual(exe, expected);
  assert.ok(exe.includes(path.join(".emrg", "install", "bin")), `exe=${exe}`);
});

test("Phase4: isPackaged startDaemon 走捆绑 emrgd 分支（非 python -m）", async () => {
  const client = new DaemonClient({ isPackaged: true });
  // _findPython 只存在于源码分支；打包分支调用 _findDaemonExecutable。
  const exe = client._findDaemonExecutable();
  assert.ok(exe.endsWith("emrgd") || exe.endsWith("emrgd.cmd"), `exe=${exe}`);
  // 打包分支绝不能触达 _findPython：置为抛错，若被调用测试即失败。
  client._findPython = () => { throw new Error("_findPython must not be called in packaged mode"); };
  // _findDaemonExecutable 返回真实路径；真实 spawn 会执行该文件——用一个
  // 必然超时失败但能证明走了打包分支的方式：spawn 的 emrgd 不存在时 error 事件。
  // 更稳：stub isRunning 恒 false + 缩短等待，捕获 startDaemon 抛的超时错，
  // 说明它没有走 _findPython（否则直接抛上面的错）。
  client.isRunning = async () => false;
  const realSpawn = require("child_process").spawn;
  const childProcess = require("child_process");
  // daemon_client 顶层解构 { spawn }——只能替换 require.cache 中的模块导出。
  // 简化：monkey-patch childProcess.spawn 无法生效，改用 spy 模块缓存。
  // 这里直接验证：打包分支下 spawn 的 cmd 是 emrgd（通过临时替换模块再 require）。
  const cacheKey = require.resolve("../daemon_client.js");
  const originalModule = require.cache[cacheKey];
  delete require.cache[cacheKey];
  const origSpawn = require("child_process").spawn;
  let captured = null;
  require("child_process").spawn = (cmd, args, opts) => {
    captured = { cmd, args, opts };
    return { unref() {}, pid: 1234 };
  };
  let DaemonClientPackaged;
  try {
    DaemonClientPackaged = require("../daemon_client.js").DaemonClient;
    const c2 = new DaemonClientPackaged({ isPackaged: true });
    c2._findPython = () => { throw new Error("_findPython must not be called in packaged mode"); };
    c2.isRunning = async () => true;
    const child = await c2.startDaemon();
    assert.strictEqual(child.pid, 1234);
    assert.ok(captured, "spawn should be called");
    assert.ok(String(captured.cmd).endsWith("emrgd") || String(captured.cmd).endsWith("emrgd.cmd"), `cmd=${captured.cmd}`);
    assert.deepStrictEqual(captured.args, []);
    assert.strictEqual(captured.opts.detached, true);
  } finally {
    require("child_process").spawn = origSpawn;
    require.cache[cacheKey] = originalModule;
  }
});

test("G43 stale port: 连接失败（port 文件存在但拒绝）→ 删文件重拉", async () => {
  const client = new DaemonClient();
  // 端口探测 mock：端口不通 = daemon 死（rant 15:26:42 固定端口为准）
  const origConnect = net.connect;
  net.connect = (opts) => {
    const sock = new (require("node:events").EventEmitter)();
    sock.destroy = () => {};
    setTimeout(() => sock.emit("error", new Error("ECONNREFUSED")), 1);
    return sock;
  };
  try {
    let respawned = false;
    client.startDaemon = async function () {
      respawned = true;
      fs.writeFileSync(TOKEN_FILE(), "seekrit-token");
    };
    const p = client.ensureConnected();
    await waitForWs();
    const firstWs = currentMockWs;
    firstWs.emit("error", new Error("connect ECONNREFUSED"));
    // 重拉后创建第二个 ws → open → auth → auth_ok
    await waitForWs(() => currentMockWs !== firstWs);
    assert.ok(respawned, "startDaemon should respawn after stale port");
    assert.strictEqual(fs.existsSync(TOKEN_FILE()), true);
    assert.strictEqual(currentMockWs.url, "ws://127.0.0.1:" + EMRGD_PORT);
    currentMockWs.emit("open");
    await waitForAuthSent(currentMockWs);
    currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "auth_ok" })));
    await p;
    assert.strictEqual(client.connected, true);
  } finally {
    net.connect = origConnect;
  }
});

test("rant 15:26:42 加固：daemon 端口活着 → ws 失败不删 token 文件、不重拉", async () => {
  const client = new DaemonClient();
  // mock net.connect：端口通 = daemon 活着（固定端口 ground truth，不再读 emrgd.pid）
  const origConnect = net.connect;
  net.connect = (opts) => {
    const sock = new (require("node:events").EventEmitter)();
    sock.destroy = () => {};
    setTimeout(() => sock.emit("connect"), 1);
    return sock;
  };
  try {
    const tokenFile = TOKEN_FILE();
    fs.writeFileSync(tokenFile, "seekrit-token");
    assert.strictEqual(await client._daemonProcessAlive(), true, "端口通 → true");

    let respawned = false;
    client.startDaemon = async function () { respawned = true; };
    const p = client.ensureConnected();
    await waitForWs();
    const firstWs = currentMockWs;
    firstWs.emit("error", new Error("connect ECONNREFUSED"));
    // 守卫路径：不删文件、不重拉，直接抛"daemon unreachable (port alive)"
    await assert.rejects(p, /daemon unreachable \(port alive\)/);
    assert.strictEqual(fs.existsSync(tokenFile), true, "token 文件必须保留（daemon 还活着）");
    assert.strictEqual(respawned, false, "端口活着 → 不重拉 daemon（防风暴）");
  } finally {
    net.connect = origConnect;
  }
});

test("rant 15:26:42 加固：daemon 真死了（端口不通）→ 仍删文件重拉", async () => {
  const client = new DaemonClient();
  // mock net.connect：端口不通 = daemon 死了（pid 文件存在与否都不再影响判断）
  const origConnect = net.connect;
  net.connect = (opts) => {
    const sock = new (require("node:events").EventEmitter)();
    sock.destroy = () => {};
    setTimeout(() => sock.emit("error", new Error("ECONNREFUSED")), 1);
    return sock;
  };
  try {
    assert.strictEqual(await client._daemonProcessAlive(), false, "端口不通 → false");

    let respawned = false;
    client.startDaemon = async function () {
      respawned = true;
      fs.writeFileSync(TOKEN_FILE(), "seekrit-token");
    };
    const p = client.ensureConnected();
    await waitForWs();
    const firstWs = currentMockWs;
    firstWs.emit("error", new Error("connect ECONNREFUSED"));
    await waitForWs(() => currentMockWs !== firstWs);
    assert.ok(respawned, "真死 → 重拉 daemon");
    currentMockWs.emit("open");
    await waitForAuthSent(currentMockWs);
    currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "auth_ok" })));
    await p;
    assert.strictEqual(client.connected, true);
  } finally {
    net.connect = origConnect;
  }
});

test("rant 15:26:42：emrgd.pid 缺失/存在都不再是存活依据——固定端口为准", async () => {
  const client = new DaemonClient();
  const origConnect = net.connect;
  net.connect = (opts) => {
    const sock = new (require("node:events").EventEmitter)();
    sock.destroy = () => {};
    setTimeout(() => sock.emit("error", new Error("ECONNREFUSED")), 1);
    return sock;
  };
  try {
    // 即使 emrgd.pid 存在（指向当前活进程），端口不通 → 仍判死（pid 文件不参与判断）
    fs.writeFileSync(path.join(tmpHome, ".emrg", "emrgd.pid"), String(process.pid));
    assert.strictEqual(await client._daemonProcessAlive(), false, "端口不通 → false（pid 存在也无效）");
  } finally {
    net.connect = origConnect;
  }
});

test("rant 13:16:36 ⑤ spawn 节流：超 MAX_SPAWN_ATTEMPTS 后不再拉起 daemon", async () => {
  const client = new DaemonClient();
  let spawnCount = 0;
  // 镜像真实 startDaemon 的节流语义（检查上限 → 计数 +1 → spawn → 超时失败）
  client.startDaemon = async function () {
    if (this._spawnAttempts >= 3) {
      throw new Error("daemon failed to start after 3 attempts — please start it manually");
    }
    this._spawnAttempts += 1;
    spawnCount += 1;
    await new Promise((r) => setTimeout(r, 5));
    throw new Error("emrgd failed to start within timeout");
  };
  for (let i = 0; i < 3; i++) {
    await assert.rejects(client.startDaemon(), /emrgd failed to start within timeout/);
  }
  assert.strictEqual(spawnCount, 3, "3 次尝试内每次都会真正 spawn");
  // 第 4 次：不再 spawn，直接抛节流错误
  await assert.rejects(client.startDaemon(), /after 3 attempts/);
  assert.strictEqual(spawnCount, 3, "超过上限后不再 spawn（防窗口/重试风暴）");
  assert.strictEqual(client._spawnAttempts, 3);
});

test("rant 13:16:36 ⑤ spawn 节流计数在成功连接后归零", async () => {
  const client = new DaemonClient();
  // 先失败一次（计数 +1），再成功 auth → 计数归零
  client.startDaemon = async function () {
    this._spawnAttempts += 1; // 镜像真实 startDaemon 的计数
    await new Promise((r) => setTimeout(r, 10));
    throw new Error("emrgd failed to start within timeout");
  };
  client.isRunning = async () => false;
  await assert.rejects(client.startDaemon(), /emrgd failed to start within timeout/);
  assert.strictEqual(client._spawnAttempts, 1);
  // 恢复真实 startDaemon（token 文件已预写 → ensureConnected 直接 ws → auth_ok）
  delete client.startDaemon;
  const p = client.ensureConnected();
  await waitForWs();
  currentMockWs.emit("open");
  await waitForAuthSent(currentMockWs);
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "auth_ok" })));
  await p;
  assert.strictEqual(client.connected, true);
  assert.strictEqual(client._spawnAttempts, 0, "成功连接后 spawn 节流计数必须归零");
});

test("auth 失败（G88）：auth_ok 前 close → 停止自动重试", async () => {
  const client = new DaemonClient();
  const p = client.ensureConnected();
  await waitForWs();
  currentMockWs.emit("open");
  await waitForAuthSent(currentMockWs);
  currentMockWs.emit("close"); // auth 前断开
  await assert.rejects(p, /authentication failed/);
  assert.strictEqual(client._authFailed, true, "auth failed flag set → main 停止重连");
});

test("auth 超时（G142）：reject + _authFailed + ws.close + listener 清理", async () => {
  // 注入 30ms 短超时（生产默认 10s）——快速触发超时路径
  const client = new DaemonClient({ authTimeoutMs: 30 });
  const p = client.ensureConnected();
  await waitForWs();
  currentMockWs.emit("open");
  await waitForAuthSent(currentMockWs);
  // 不回 auth_ok 也不 close → 30ms 后 timer 触发
  await assert.rejects(p, /auth timeout/);
  assert.strictEqual(client._authFailed, true, "auth timeout → _authFailed（G88 停止重试）");
  // 超时后 ws 被 close（mock close 会 emit close → 但 listener 已清理，不会二次 reject）
  const ws = currentMockWs;
  const closeCbCount = (ws._listeners["close"] || []).length;
  assert.strictEqual(closeCbCount, 0, "close listener 已清理（防泄漏）");
  const msgCbCount = (ws._listeners["message"] || []).length;
  assert.strictEqual(msgCbCount, 0, "message listener 已清理（防泄漏）");
  // ws.close() 被调用（emit close 后无异常 = 清理生效）
  ws.emit("close");
});

test("坏 JSON 帧 → 忽略不崩（R53）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  const events = [];
  client.onEvent((t) => events.push(t));
  currentMockWs.emit("message", Buffer.from("{bad json"));
  currentMockWs.emit("message", Buffer.from("not json at all"));
  assert.deepStrictEqual(events, []);
  assert.strictEqual(client.connected, true);
});

test("ws close → disconnected 事件 + pending reject（G89/G90）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  const events = [];
  client.onEvent((t) => events.push(t));
  const pending = client.sendCommandAndWait("list_sessions", { cwd: tmpHome }, 5000);
  currentMockWs.emit("close");
  await assert.rejects(pending, /connection closed/);
  assert.ok(events.includes("disconnected"));
  assert.strictEqual(client.connected, false);
});

test("断连清 _currentStream（#338 回归：断连后无虚假 done）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  // 发起任务 → _currentStream 建立
  const rid = client.sendTask({ sessionId: "s_260803_1730_abcd1234", cwd: tmpHome, prompt: "hi" });
  assert.ok(client._currentStream, "_currentStream 已建立");
  assert.ok(client._currentStream.requestId === rid);
  const doneEvents = [];
  client.onEvent((t, d) => { if (t === "done") doneEvents.push(d); });
  // 断连 → clearActiveStream 清 _currentStream（#338）
  currentMockWs.emit("close");
  assert.strictEqual(client._currentStream, null, "断连后 _currentStream 被清（#338）");
  await new Promise((r) => setTimeout(r, 40));
  assert.deepStrictEqual(doneEvents, [], "断连后无虚假 done 事件");
});

test("sendTask payload（G32，无 stream 字段——rant 21:20:38）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  const rid = client.sendTask({ sessionId: "s_260803_1730_abcd1234", cwd: "/proj", prompt: "hello", images: null });
  const frame = JSON.parse(currentMockWs.sent.at(-1));
  assert.strictEqual(frame.type, "task");
  assert.strictEqual(frame.id, rid, "request_id 作为 id 字段");
  assert.strictEqual(frame.session_id, "s_260803_1730_abcd1234");
  assert.strictEqual(frame.cwd, "/proj");
  assert.strictEqual(frame.prompt, "hello");
    assert.strictEqual(frame.images, null);
  assert.strictEqual(frame.sandbox, "workspace-write", "rant 18:18：默认沙箱档位应随每条消息发送");
  assert.ok(frame.timestamp);
});

test("sendTask 外部预生成 requestId（G143）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  const outer = "s_260803_1730_outer1234";
  const rid = client.sendTask({ sessionId: "s_260803_1730_abcd1234", cwd: "/proj", prompt: "hi", requestId: outer });
  assert.strictEqual(rid, outer, "返回外部预生成 id");
  const frame = JSON.parse(currentMockWs.sent.at(-1));
  assert.strictEqual(frame.id, outer, "payload id 用外部预生成 id");
  assert.strictEqual(client._currentStream.requestId, outer, "_setCurrentStream 用外部 id（send 前自有流标记）");
});

test("sendCommand payload + cancel 无多余字段（G24）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  client.sendCommand("cancel");
  const frame = JSON.parse(currentMockWs.sent.at(-1));
  assert.deepStrictEqual(frame, { type: "cancel" });
  client.sendCommand("set_model", { model: "gpt-4o" });
  const f2 = JSON.parse(currentMockWs.sent.at(-1));
  assert.deepStrictEqual(f2, { type: "set_model", model: "gpt-4o" });
});

test("sendCommand 帧形状：payload 带 type 字段不覆盖消息类型（rant 2026-08-14T21:48）", async () => {
  // task CRUD payload 含任务类型字段 type（如 "evolution"）——消息类型必须保留，
  // 否则 daemon 路由失败返回 unknown message type，GUI 保存无响应。
  const client = new DaemonClient();
  await connectClient(client);
  client.sendCommand("task_create", { type: "evolution", name: "t1", project: "p1" });
  const frame = JSON.parse(currentMockWs.sent.at(-1));
  assert.strictEqual(frame.type, "task_create", "wire 消息类型必须保留，不被 payload 的 type 覆盖");
  assert.strictEqual(frame.name, "t1");
  assert.strictEqual(frame.type, "task_create");
});

test("帧分类（G21+G58）：各帧事件分发正确", async () => {
  const client = new DaemonClient();
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
  send({ type: "files_list", path: "/tmp", entries: [] }); // 右栏工作区 P1：list_result 白名单
  send({ type: "resume_result", session_id: "s1" });
  send({ type: "model_set", model: "m2" });
  send({ type: "session_deleted", session_id: "s1" });

  const types = seen.map(([t]) => t);
  assert.deepStrictEqual(types, [
    "tool_started", "tool_finished", "message_delta", "group_cleared", "done", "cancelled", "error", "pong",
    "list_result", "list_result", "command_result", "command_result", "command_result",
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
  const client = new DaemonClient();
  await connectClient(client);
  const p = client.sendCommandAndWait("list_sessions", { cwd: tmpHome }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "sessions_list", sessions: [{ session_id: "s1" }] })));
  const res = await p;
  assert.strictEqual(res.type, "sessions_list");
  assert.strictEqual(res.sessions.length, 1);
});

test("RESPONSE_TYPES 映射表与 daemon 命令名一致（修正 clear/rename/trigger + 补 rewind/read_memory）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  // clear_session → clear_result（原 clear 映射会超时）
  const p1 = client.sendCommandAndWait("clear_session", { session_id: "s1", cwd: tmpHome }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "clear_result", ok: true })));
  const r1 = await p1;
  assert.strictEqual(r1.type, "clear_result");
  // rename_session → rename_result
  const p2 = client.sendCommandAndWait("rename_session", { session_id: "s1", cwd: tmpHome, title: "t" }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "rename_result", ok: true })));
  const r2 = await p2;
  assert.strictEqual(r2.type, "rename_result");
  // trigger_task → trigger_result
  const p3 = client.sendCommandAndWait("trigger_task", { task: "x" }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "trigger_result", ok: true })));
  const r3 = await p3;
  assert.strictEqual(r3.type, "trigger_result");
  // rewind_session → rewind_result（补缺）
  const p4 = client.sendCommandAndWait("rewind_session", { session_id: "s1", cwd: tmpHome, record_index: 0 }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "rewind_result", ok: true })));
  const r4 = await p4;
  assert.strictEqual(r4.type, "rewind_result");
  // read_memory → memory_content（补缺）
  const p5 = client.sendCommandAndWait("read_memory", { name: "m" }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "memory_content", content: "x" })));
  const r5 = await p5;
  assert.strictEqual(r5.type, "memory_content");
  // github_connect → github_connect_result（Windows GCM rant Stage 2）
  const p6 = client.sendCommandAndWait("github_connect", { token: "ghp_x" }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "github_connect_result", ok: true, user: "octocat", error: null })));
  const r6 = await p6;
  assert.strictEqual(r6.type, "github_connect_result");
  assert.strictEqual(r6.ok, true);
  // github_disconnect → github_disconnect_result（Windows GCM rant Stage 2）
  const p7 = client.sendCommandAndWait("github_disconnect", {}, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "github_disconnect_result", ok: true, error: null })));
  const r7 = await p7;
  assert.strictEqual(r7.type, "github_disconnect_result");
  assert.strictEqual(r7.ok, true);
  // github_connect_web → github_connect_web_result（Windows GCM rant Stage 2b）
  const p8 = client.sendCommandAndWait("github_connect_web", {}, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "github_connect_web_result", ok: true, code: "ABCD-1234", url: "https://github.com/login/device", error: null })));
  const r8 = await p8;
  assert.strictEqual(r8.type, "github_connect_web_result");
  assert.strictEqual(r8.code, "ABCD-1234");
  // list_files → files_list（右栏工作区面板 P1，rant 2026-08-11T12:20:35）
  const p9 = client.sendCommandAndWait("list_files", { path: "/tmp" }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "files_list", path: "/tmp", entries: [], truncated: false })));
  const r9 = await p9;
  assert.strictEqual(r9.type, "files_list");
  assert.deepStrictEqual(r9.entries, []);
  // read_file → file_content（右栏工作区面板 P1）
  const p10 = client.sendCommandAndWait("read_file", { path: "/tmp/a.txt" }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "file_content", path: "/tmp/a.txt", content: "hi", binary: false })));
  const r10 = await p10;
  assert.strictEqual(r10.type, "file_content");
  assert.strictEqual(r10.content, "hi");
  // list_rants → rants_list（rant 14:10:14 P4：rant 面板）
  const p11 = client.sendCommandAndWait("list_rants", { status: "completed" }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  currentMockWs.emit("message", Buffer.from(JSON.stringify({ type: "rants_list", rants: [{ timestamp: "x", status: "completed", message: "m" }] })));
  const r11 = await p11;
  assert.strictEqual(r11.type, "rants_list");
  assert.strictEqual(r11.rants.length, 1);
});

test("rant 18:23:15 P2/P3：RESPONSE_TYPES 覆盖任务/模板 CRUD（task_result / templates_list / template_result）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  // task_create → task_result
  const p1 = client.sendCommandAndWait("task_create", { name: "t1", type: "evolution", project: "emrg", interval: 600 }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  send({ type: "task_result", ok: true, task: { name: "t1" }, summary: { added: ["t1"] } });
  const r1 = await p1;
  assert.strictEqual(r1.type, "task_result");
  assert.strictEqual(r1.ok, true);
  assert.deepStrictEqual(r1.summary.added, ["t1"]);
  // task_update → task_result
  const p2 = client.sendCommandAndWait("task_update", { name: "t1", interval: 1200 }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  send({ type: "task_result", ok: true, task: { name: "t1", interval: 1200 } });
  const r2 = await p2;
  assert.strictEqual(r2.type, "task_result");
  assert.strictEqual(r2.task.interval, 1200);
  // task_delete → task_result
  const p3 = client.sendCommandAndWait("task_delete", { name: "t1" }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  send({ type: "task_result", ok: true, summary: { removed: ["t1"] } });
  const r3 = await p3;
  assert.strictEqual(r3.type, "task_result");
  assert.deepStrictEqual(r3.summary.removed, ["t1"]);
  // task_template_list → templates_list
  const p4 = client.sendCommandAndWait("task_template_list", {}, 2000);
  await new Promise((r) => setTimeout(r, 10));
  send({ type: "templates_list", templates: [{ name: "evolution", builtin: true }, { name: "sync", builtin: false }] });
  const r4 = await p4;
  assert.strictEqual(r4.type, "templates_list");
  assert.strictEqual(r4.templates.length, 2);
  // task_template_create / update / delete → template_result
  const p5 = client.sendCommandAndWait("task_template_create", { name: "sync", prompt: "# s" }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  send({ type: "template_result", ok: true });
  const r5 = await p5;
  assert.strictEqual(r5.type, "template_result");
  assert.strictEqual(r5.ok, true);
  const p6 = client.sendCommandAndWait("task_template_update", { name: "sync", prompt: "# s2" }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  send({ type: "template_result", ok: true });
  const r6 = await p6;
  assert.strictEqual(r6.type, "template_result");
  const p7 = client.sendCommandAndWait("task_template_delete", { name: "sync" }, 2000);
  await new Promise((r) => setTimeout(r, 10));
  // G103：带 error 键的帧 reject 未决命令（daemon template_result 错误形态）
  send({ type: "template_result", ok: false, error: "builtin task type is read-only" });
  await assert.rejects(p7, /read-only/);
});

test("命令-响应配对超时 → reject（G93）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  await assert.rejects(
    client.sendCommandAndWait("list_sessions", { cwd: tmpHome }, 50),
    /command timeout: list_sessions/
  );
});

test("error 帧 FIFO reject 最早未决（G103）；无未决 error → 广播", async () => {
  const client = new DaemonClient();
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
  const client = new DaemonClient();
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
  const client = new DaemonClient();
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
  // token 文件缺失 → false
  fs.rmSync(TOKEN_FILE(), { force: true });
  assert.strictEqual(await client.isRunning(), false);
});

test("断连 pending 请求全部 reject + disconnected（G89）", async () => {
  const client = new DaemonClient();
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

// ── Rant 2026-08-09T18:47:37（GUI 连不上 daemon 回归）──────────────────

test("16:03:31: token 固定读规范 ~/.emrg → 存在则不 spawn 直接连接", async () => {
  // setupTempHome 已把 HOME/USERPROFILE 重定向到 tmpHome 并预写 ~/.emrg/emrgd.token；
  // projectDir 概念删除后（rant 2026-08-20T16:03:31）token 固定读该位置。
  const client = new DaemonClient();
  let spawned = false;
  client.startDaemon = async function () { spawned = true; };
  await connectClient(client);
  assert.strictEqual(spawned, false, "规范位置有 token 文件 → 必须复用，不 spawn");
  assert.strictEqual(client.connected, true);
  assert.strictEqual(currentMockWs.url, "ws://127.0.0.1:" + EMRGD_PORT, "连接 canonical home port");
});

test("16:03:31: stale token + spawn 节流失败 → probe 诚实失败，抛原始错误（不假装复用）", async () => {
  // 宿主场景：token 文件存在（stale，无 daemon 监听）。ws 失败 → 端口探测死 →
  // 删 stale token → spawn 节流抛错 → probe 无 token 可复用 → 诚实抛原始错误
  // （projectDir 概念删除后 token 只有一个规范位置，probe 复用需 token 存在）。
  const client = new DaemonClient();
  // 端口探测 mock：端口不通 = daemon 死（rant 15:26:42 固定端口为准，不再读 emrgd.pid）
  const origConnect = net.connect;
  net.connect = (opts) => {
    const sock = new (require("node:events").EventEmitter)();
    sock.destroy = () => {};
    setTimeout(() => sock.emit("error", new Error("ECONNREFUSED")), 1);
    return sock;
  };
  try {
  // spawn 命中节流（正是宿主看到的假错误 "after 3 attempts"）
  client.startDaemon = async function () {
    throw new Error("daemon failed to start after 3 attempts — please start it manually");
  };
  // 捕获日志 → 断言 4 状态诊断字段齐全（B1/B3）
  const logs = [];
  client.logger = { info: (...a) => logs.push(a.join(" ")), warn: (...a) => logs.push(a.join(" ")) };
  const p = client.ensureConnected();
  await waitForWs();
  const firstWs = currentMockWs;
  firstWs.emit("error", new Error("connect ECONNREFUSED")); // stale：拒绝
  await assert.rejects(() => p, /daemon failed to start after 3 attempts/, "诚实抛原始错误");
  const probeLine = logs.find((l) => l.includes("probe:"));
  assert.ok(probeLine, "必须输出 probe 诊断日志");
  assert.match(probeLine, /token_file_exists=false/, "token 已被删 → probe 如实上报");
  assert.match(probeLine, /token_file_content=/);
  assert.match(probeLine, /daemon_alive\(ping\)=/);
  assert.match(probeLine, /spawn_result=failed\(daemon failed to start after 3 attempts/);
  assert.ok(logs.some((l) => l.includes("no existing daemon reachable, giving up")), "诚实放弃日志");
  } finally {
    net.connect = origConnect;
  }
});

// ── P2 自有流锁（G65 每连接独立；rant 15:07:19）──────────────────────────

test("P2 ownStream: sendTask 恒标记 ownStream + requestId（非 stream 路径已删）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  const rid = client.sendTask({ sessionId: "s_260803_1730_abcd1234", cwd: "/proj", prompt: "hi", requestId: "req-own-1" });
  assert.strictEqual(client.ownStream, true, "ownStream set unconditionally");
  assert.strictEqual(client.ownStreamRequestId, "req-own-1");
  assert.strictEqual(rid, "req-own-1");
});

test("P2 ownStream: 自有 done（request 匹配）→ 释放锁；广播 done（不匹配）→ 保持", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  client.sendTask({ sessionId: "s_260803_1730_abcd1234", cwd: "/proj", prompt: "hi", requestId: "req-own-2" });
  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  // 广播 done（其他客户端/其他流）→ 锁保持
  send({ request_id: "req-other", done: true, delta: false });
  assert.strictEqual(client.ownStream, true, "broadcast done must not release own lock");
  // 自有 done → 释放
  send({ request_id: "req-own-2", done: true, delta: false });
  assert.strictEqual(client.ownStream, false, "own done must release lock");
  assert.strictEqual(client.ownStreamRequestId, null);
});

test("P2 ownStream: session busy 即发 error → 释放锁（防 G65 锁泄漏）", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  client.sendTask({ sessionId: "s_260803_1730_abcd1234", cwd: "/proj", prompt: "hi", requestId: "req-own-4" });
  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  send({ error: "session busy: another stream running" });
  assert.strictEqual(client.ownStream, false, "session busy error must release lock");
});

test("P2 ownStream: cancelled（request 匹配）→ 释放锁", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  client.sendTask({ sessionId: "s_260803_1730_abcd1234", cwd: "/proj", prompt: "hi", requestId: "req-own-5" });
  const send = (obj) => currentMockWs.emit("message", Buffer.from(JSON.stringify(obj)));
  send({ type: "cancelled", request_id: "req-own-5" });
  assert.strictEqual(client.ownStream, false, "own cancelled must release lock");
});

test("P2 ownStream: 断连 → 释放锁", async () => {
  const client = new DaemonClient();
  await connectClient(client);
  client.sendTask({ sessionId: "s_260803_1730_abcd1234", cwd: "/proj", prompt: "hi", requestId: "req-own-6" });
  assert.strictEqual(client.ownStream, true);
  client.close();
  assert.strictEqual(client.ownStream, false, "disconnect must release lock");
});

// ── 启动失败诊断（issue #1283 = daemon_manager.py #1279 的 GUI 半边）──────────
// ⚠️ 本组不 spawn 真实进程、不探测端口、不触碰真实 daemon（MANIFESTO 第四条附则二）：
// spawn 打桩，isRunning 打桩。测的是纯部件（标记/差值读取/失败描述）＋打桩驱动的等待循环。

/** emrgd.log 的规范位置（HOME/USERPROFILE 已被 beforeEach 重定向到临时目录）。 */
const logFile = () => path.join(os.homedir(), ".emrg", "emrgd.log");

test("#1283 本次没写 → 尾部为空；本次写了 → 只给本次的字节", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "old run: SystemExit: SIGTERM (15) received\n");
  const mark = client._logMark(logFile());
  assert.strictEqual(client._readLogTail(15, mark, logFile()), "");
  fs.appendFileSync(logFile(), "this attempt: config.toml is not valid TOML\n");
  const got = client._readLogTail(15, mark, logFile());
  assert.ok(got.includes("this attempt"));
  assert.ok(!got.includes("old run"), "上一轮的历史不得出现在本次的尾部里");
});

test("#1283 行数只在本次新增的范围内数", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "old\n");
  const mark = client._logMark(logFile());
  const block = Array.from({ length: 20 }, (_, i) => `new${i}`).join("\n");
  fs.appendFileSync(logFile(), `${block}\n`);
  assert.deepStrictEqual(
    client._readLogTail(3, mark, logFile()).split("\n"),
    ["new17", "new18", "new19"],
  );
});

test("#1283 日志不存在/不可读不是异常", () => {
  const client = new DaemonClient();
  const missing = path.join(os.homedir(), ".emrg", "nope.log");
  assert.strictEqual(client._readLogTail(15, client._logMark(missing), missing), "");
  assert.deepStrictEqual(client._logMark(missing), { size: 0, ino: null });
});

test("#1283 裸 offset 不是标记：响亮失败，而不是静默读错文件", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous\n");
  assert.throws(() => client._readLogTail(15, fs.statSync(logFile()).size, logFile()), TypeError);
});

test("#1283 标记取自文件本身，是文件中的一个点（不是常量/字符串长度）", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous run\n");
  const mark = client._logMark(logFile());
  assert.strictEqual(mark.size, fs.statSync(logFile()).size);
  assert.ok(mark.size > 0);
  assert.strictEqual(mark.ino, fs.statSync(logFile()).ino);
  fs.appendFileSync(logFile(), "this attempt\n");
  assert.ok(client._logMark(logFile()).size > mark.size, "标记必须随文件前进");
  assert.strictEqual(client._readLogTail(15, mark, logFile()).trim(), "this attempt");
});

test("#1283 原地截断（ino 不变、字节变少）→ 从 0 读起", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous run: SystemExit: SIGTERM (15) received\n");
  const mark = client._logMark(logFile());
  fs.writeFileSync(logFile(), "this attempt: config.toml is not valid TOML\n");
  assert.strictEqual(fs.statSync(logFile()).ino, mark.ino, "原地截断：还是标记所指的那个文件");
  assert.ok(fs.statSync(logFile()).size < mark.size, "regime：标记已越过文件末尾");
  const got = client._readLogTail(15, mark, logFile());
  assert.ok(got.includes("this attempt"));
  assert.ok(!got.includes("SIGTERM"));
});

test("#1283 标记所指的文件已不在（rotate 的形状）→ 整个文件都属于本次", () => {
  // 真实生产者是 RotatingFileHandler：旧文件改名、同名新文件重建，标记所指的
  // 那个文件从该路径上消失。这里用另一个文件确定性地重现该形状（ino 必不同），
  // 并选 size 判别不了的那一侧：新文件比标记大，标记落在新文件内部——
  // 只看 size 会从中间读起，把本次的第一行切掉。
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous ".repeat(200));
  const mark = client._logMark(logFile());
  const after = path.join(os.homedir(), ".emrg", "emrgd.log.rebuilt");
  fs.writeFileSync(after, `THIS-ATTEMPT\n${"y".repeat(4096)}`);
  assert.notStrictEqual(fs.statSync(after).ino, mark.ino);
  assert.ok(fs.statSync(after).size > mark.size, "regime：尺寸判别不了，只有身份能");
  const got = client._readLogTail(15, mark, after);
  assert.ok(got.startsWith("THIS-ATTEMPT"), "整文件都算本次的：第一行不得被切掉");
  assert.ok(!got.includes("previous"));
});

test("#1283 沉默死掉的子进程：如实说没写，并给出退出码", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous run: SystemExit: SIGTERM (15) received\n");
  const mark = client._logMark(logFile());
  const detail = client._startupFailureDetail(mark, { exitCode: 7 });
  assert.ok(detail.includes("wrote nothing"));
  assert.ok(detail.includes("exit=7"));
  assert.ok(!detail.includes("SIGTERM"), "上一轮的关闭不得当作本次的原因");
  assert.ok(detail.includes("previous run"), "仍明说更早的输出来自上一轮");
});

test("#1283 被信号杀掉的子进程报 signal，不报 still running", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous\n");
  const mark = client._logMark(logFile());
  const detail = client._startupFailureDetail(mark, { exitCode: null, signalCode: "SIGKILL" });
  assert.ok(detail.includes("signal=SIGKILL"), "Node 把信号放在 signalCode，不放进 exitCode");
  assert.ok(!detail.includes("still running"));
});

test("#1283 本次写了东西 → 报本次的行", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous\n");
  const mark = client._logMark(logFile());
  fs.appendFileSync(logFile(), "this attempt: Traceback ...\nRuntimeError: bad config\n");
  const detail = client._startupFailureDetail(mark, { exitCode: 1 });
  assert.ok(detail.includes("written by this start attempt"));
  assert.ok(detail.includes("RuntimeError: bad config"));
  assert.ok(!detail.includes("previous"));
});

test("#1283 已死的子进程立即失败（不烧完整个窗口）", async () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous\n");
  const mark = client._logMark(logFile());
  client.isRunning = async () => false;
  const t0 = Date.now();
  await assert.rejects(
    client._awaitDaemonReady({ exitCode: 143 }, mark, 5_000),
    (err) => err.message.includes("exited during startup") && err.message.includes("exit=143"),
  );
  assert.ok(Date.now() - t0 < 1000, "5s 的窗口不该被烧完");
});

test("#1283 非 number 的 exitCode 不得读成“已退出”（只认文档化的 number|null）", async () => {
  // 对照 daemon_manager.py 既有断言：非 int 的 returncode 不算退出——否则替身
  // 对象会把一次活的等待变成"启动即死"，诊断修复反过来变成启动回归。
  const client = new DaemonClient();
  client.isRunning = async () => false;
  await assert.rejects(
    client._awaitDaemonReady({ exitCode: "7" }, client._logMark(logFile()), 1),
    (err) => err.message.includes("failed to start within timeout"),
  );
});

test("#1283 活着的子进程没起来 → 报窗口 + still running", async () => {
  const client = new DaemonClient();
  client.isRunning = async () => false;
  await assert.rejects(
    client._awaitDaemonReady({ exitCode: null }, client._logMark(logFile()), 1),
    (err) => err.message.includes("failed to start within timeout") && err.message.includes("still running"),
  );
});

test("#1283 子进程起来了 → 安静返回", async () => {
  const client = new DaemonClient();
  client.isRunning = async () => true;
  const child = { pid: 4242 };
  assert.strictEqual(await client._awaitDaemonReady(child, client._logMark(logFile()), 1000), child);
});

// ── 子进程自己的 stderr：装日志 handler 之前就死掉的失败只有这一个出口 ────────
// 对照 emrg/client/daemon_manager.py 的 `_truncate_start_stderr` / `_read_start_stderr`
// （issue #1276 item 4）。这里不 spawn 任何 daemon：直接开文件、写文件、读回来。

test("#1276 本次启动的 stderr 文件被截断：报给宿主的只有本次的字节", () => {
  const client = new DaemonClient();
  const err = EMRGD_START_ERR();
  assert.ok(path.resolve(err).startsWith(path.resolve(tmpHome) + path.sep), "诊断文件必须在临时 HOME 内");
  fs.writeFileSync(err, "previous attempt: ImportError: no such patch\n");
  const fd = client._openStartStderr();
  assert.notStrictEqual(fd, null);
  try {
    assert.strictEqual(fs.readFileSync(err, "utf8"), "", "上一轮的字节必须消失");
    fs.writeSync(fd, "this attempt: ImportError: real cause\n");
  } finally {
    fs.closeSync(fd);
  }
  assert.strictEqual(client._readStartStderr().trim(), "this attempt: ImportError: real cause");
});

test("#1276 在装日志 handler 之前死掉的子进程：由它自己的 stderr 说出原因", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous run: SystemExit: SIGTERM (15) received\n");
  const mark = client._logMark(logFile());
  fs.writeFileSync(EMRGD_START_ERR(), "Traceback (most recent call last):\nImportError: boom\n");
  const detail = client._startupFailureDetail(mark, { exitCode: 1 }, undefined, EMRGD_START_ERR());
  assert.ok(detail.includes("ImportError: boom"), "子进程自己的原因就是这一节新增的事实");
  assert.ok(!detail.includes("SIGTERM"), "上一轮的关闭仍不得当作本次的原因");
  assert.ok(detail.includes("wrote nothing to emrgd.log"), "log 那一半照样如实说");
});

test("#1276 子进程的遗言排在日志尾巴之前（顺序即论证）", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous\n");
  const mark = client._logMark(logFile());
  fs.appendFileSync(logFile(), "this attempt: config.toml is not valid TOML\n");
  fs.writeFileSync(EMRGD_START_ERR(), "child: ImportError: no module named 'x'\n");
  const detail = client._startupFailureDetail(mark, { exitCode: 1 }, undefined, EMRGD_START_ERR());
  assert.ok(detail.includes("ImportError") && detail.includes("config.toml"));
  assert.ok(detail.indexOf("ImportError") < detail.indexOf("config.toml"));
});

test("#1276 未捕获的那一路说「未捕获」，不说「子进程没写」（沉默是测量，不是推断）", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous run: SystemExit: SIGTERM (15) received\n");
  const mark = client._logMark(logFile());
  // 不给 stderrFile：spawn 开不出诊断文件时交出来的就是这个形状（子进程 stderr
  // 走 "ignore"）。这一路压根没读，所以不得替它宣布沉默。
  const detail = client._startupFailureDetail(mark, { exitCode: 9 });
  assert.ok(detail.includes("wrote nothing to emrgd.log"));
  assert.ok(detail.includes("not captured"), "没读过的那一路要如实说没读");
  assert.ok(!detail.includes("wrote nothing to its own stderr"),
    "这一路没有可读的文件，它的沉默无从得知");
  assert.ok(detail.includes("exit=9"));
  assert.ok(detail.includes("previous run"), "仍明说更早的输出来自上一轮");
});

test("#1276 捕获了却真的没写：这一句沉默才成立", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous run: SystemExit: SIGTERM (15) received\n");
  const mark = client._logMark(logFile());
  const fd = client._openStartStderr(); // 本次截断打开，随后读回空
  assert.notStrictEqual(fd, null);
  fs.closeSync(fd);
  const detail = client._startupFailureDetail(mark, { exitCode: 9 }, undefined, EMRGD_START_ERR());
  assert.ok(detail.includes("wrote nothing to its own stderr"), "读过且为空，这句是测量出来的");
  assert.ok(!detail.includes("not captured"));
  assert.ok(detail.includes("exit=9"));
});

test("#1276 stderr 的行上限保住 traceback 的结尾，读不到不抛异常", () => {
  const client = new DaemonClient();
  const frames = Array.from({ length: 60 }, (_, i) => `  File "f${i}.py", line ${i}, in <module>`).join("\n");
  fs.writeFileSync(EMRGD_START_ERR(), `Traceback (most recent call last):\n${frames}\nImportError: the cause\n`);
  const got = client._readStartStderr();
  assert.ok(got.includes("ImportError: the cause"), "最后一行才是说出原因的那一行");
  assert.strictEqual(got.split(/\r?\n/).length, 40, "上限 40 行，且确实生效");
  assert.strictEqual(client._readStartStderr(40, path.join(tmpHome, ".emrg", "nope.err")), "");
});

test("#1276 spawn 把 stderr 接到诊断文件，而不是丢弃", () => {
  // 接线本身：诊断在上面，但 spawn 不把 fd 传下去就是死代码。断言源码而不是调用
  // ——调用会 spawn 真实 daemon（并先走 cleanup_server 的停止路径）。四个片段各是
  // 一条"这一路变死"的路：从哪个路径开 fd、子进程的 stderr 就是它、以及**报告拿到的
  // 是这一个 fd 的结果**（`errFd === null ? null : EMRGD_START_ERR()`——开不出来就
  // 交 null，报告才会说"未捕获"而不是"子进程没写"）。
  const src = fs.readFileSync(require.resolve("../daemon_client.js"), "utf8").replace(/\r\n/g, "\n");
  assert.ok(src.includes('stdio: ["ignore", "ignore", errFd === null ? "ignore" : errFd]'));
  assert.ok(src.includes('stdio: ["ignore", "ignore", errFdSource === null ? "ignore" : errFdSource]'));
  assert.ok(src.includes("const errFd = this._openStartStderr();"));
  assert.ok(src.includes("const errFdSource = this._openStartStderr();"));
  assert.ok(src.includes("errFd === null ? null : EMRGD_START_ERR()"));
  assert.ok(src.includes("errFdSource === null ? null : EMRGD_START_ERR()"));
});

// ── 第三种状态：spawn 本身失败（ENOENT）──────────────────────────────────────
// 打包路径 `~/.emrg/install/bin/emrgd` 是 `_findDaemonExecutable()` 无条件拼出来的
// （不检查存在），源码路径 `_findPython()` 在 .venv 缺失时退回裸 "python3"/"python"
// （也不检查）⇒ 装坏 / 第一次启动就能到。它既不是"退出"也不是"还在跑"。

test("#1283 从未启动：pid 同步可见，'error' 补上名字（ENOENT）", () => {
  const client = new DaemonClient();
  let onError = null;
  const child = {
    pid: undefined, // 本机 node 26.5.0 实测：spawn 不存在的路径时同步就是 undefined
    exitCode: null,
    signalCode: null,
    once(ev, cb) { if (ev === "error") onError = cb; },
  };
  const state = client._watchSpawn(child);
  assert.strictEqual(state.neverStarted, true, "不必等 'error'：pid 就是判据");
  assert.strictEqual(state.err, null);
  assert.strictEqual(client._neverStartedName(state), "");
  onError(Object.assign(new Error("spawn /nonexistent/emrgd-missing ENOENT"), { code: "ENOENT" }));
  assert.strictEqual(state.neverStarted, true);
  assert.strictEqual(state.err.code, "ENOENT");
  assert.strictEqual(client._neverStartedName(state), " (ENOENT)");
});

test("#1283 从未启动 → never started，绝不是 still running，也不烧窗口", async () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous\n");
  const mark = client._logMark(logFile());
  client.isRunning = async () => false;
  const child = { pid: undefined, exitCode: null, signalCode: null, once() {} };
  const state = client._watchSpawn(child);
  const t0 = Date.now();
  await assert.rejects(
    client._awaitDaemonReady(child, mark, 5_000, state),
    (err) => err.message.includes("never started")
      && !err.message.includes("still running")
      && !err.message.includes("failed to start within timeout")
      && !err.message.includes("exited during startup"),
  );
  assert.ok(Date.now() - t0 < 1000, "5s 的窗口不该被烧完");
});

test("#1283 从未启动的详细文案：仍说本次没写，并点名 ENOENT", () => {
  const client = new DaemonClient();
  fs.writeFileSync(logFile(), "previous run: SystemExit: SIGTERM (15) received\n");
  const mark = client._logMark(logFile());
  const state = { err: { code: "ENOENT" }, neverStarted: true };
  const detail = client._startupFailureDetail(mark, { pid: undefined }, state);
  assert.ok(detail.includes("never started (ENOENT)"));
  assert.ok(detail.includes("wrote nothing"));
  assert.ok(!detail.includes("still running"));
  assert.ok(!detail.includes("SIGTERM"));
});

test("#1283 startDaemon：spawn 打桩为 ENOENT → 立即失败并点名，不冒到 uncaughtException", async () => {
  // 真 spawn 会拉起 daemon——一律打桩（MANIFESTO 第四条附则二）。替身是真正的
  // EventEmitter：没有 'error' 监听者时 node --test 会因未处理的 'error' 抛错 ⇒
  // 这条测试同时证明「监听器确实挂上了」。
  const childProcess = require("child_process");
  const { EventEmitter } = require("events");
  const cacheKey = require.resolve("../daemon_client.js");
  const originalModule = require.cache[cacheKey];
  const origSpawn = childProcess.spawn;
  const file = logFile();
  fs.writeFileSync(file, "previous run: SystemExit: SIGTERM (15) received\n");
  try {
    delete require.cache[cacheKey];
    childProcess.spawn = () => {
      const child = new EventEmitter();
      child.unref = () => {};
      child.pid = undefined;
      child.exitCode = null;
      child.signalCode = null;
      setImmediate(() => child.emit(
        "error", Object.assign(new Error("spawn /nonexistent/emrgd-missing ENOENT"), { code: "ENOENT" }),
      ));
      return child;
    };
    const Reloaded = require("../daemon_client.js").DaemonClient;
    const c = new Reloaded();
    // 真实路径：探测是异步的 ⇒ 'error' 在第一次检查之前就已经送达，名字拿得到。
    c.isRunning = async () => { await new Promise((r) => setTimeout(r, 5)); return false; };
    const t0 = Date.now();
    await assert.rejects(
      c.startDaemon(),
      (err) => err.message.includes("never started (ENOENT)")
        && !err.message.includes("still running")
        && !err.message.includes("failed to start within timeout"),
    );
    assert.ok(Date.now() - t0 < 1000, "ENOENT 不该烧满 5s 窗口");
  } finally {
    childProcess.spawn = origSpawn;
    require.cache[cacheKey] = originalModule;
  }
});

test("#1283 startDaemon：标记在 spawn 之前取，失败信息只含本次写入", async () => {
  // 真实 spawn 会拉起 daemon——一律打桩（MANIFESTO 第四条附则二）。打桩的替身
  // 在 spawn 返回后立刻写日志并立刻死掉，正是"标记必须早于子进程"的场景。
  const childProcess = require("child_process");
  const cacheKey = require.resolve("../daemon_client.js");
  const originalModule = require.cache[cacheKey];
  const origSpawn = childProcess.spawn;
  const file = logFile();
  fs.writeFileSync(file, "previous run: SystemExit: SIGTERM (15) received\n");
  try {
    delete require.cache[cacheKey];
    childProcess.spawn = () => {
      fs.appendFileSync(file, "this attempt: ModuleNotFoundError: No module named 'emrg'\n");
      return { unref() {}, pid: 999, exitCode: 3 };
    };
    const Reloaded = require("../daemon_client.js").DaemonClient;
    const c = new Reloaded();
    c.isRunning = async () => false;
    await assert.rejects(
      c.startDaemon(),
      (err) => err.message.includes("exit=3")
        && err.message.includes("ModuleNotFoundError")
        && !err.message.includes("SIGTERM"),
    );
  } finally {
    childProcess.spawn = origSpawn;
    require.cache[cacheKey] = originalModule;
  }
});
