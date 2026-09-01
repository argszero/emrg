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
const { DaemonClient, generateSessionId, TOKEN_FILE, EMRGD_PORT } = require("../daemon_client.js");
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
