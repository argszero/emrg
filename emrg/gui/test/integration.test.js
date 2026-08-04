"use strict";
/**
 * integration.test.js — 协议往返集成测试（设计文档 §6.2，G73 隔离）。
 * 直接 spawn 真实 Python daemon（不测 daemon 拉起的完整路径，G126），
 * 注入 HOME/USERPROFILE 指向临时目录 → 零污染真实 ~/.emrg、不碰真实 api_key。
 *
 * 范围（全部只读或隔离内操作）：
 * - ensureConnected → ping → pong（ServerPong 结构）
 * - list_sessions / list_models / list_projects
 * - resume_session（不存在）→ 错误帧（无破坏性）
 * - delete_session（不存在）→ 错误帧
 * - daemon 被杀 → ensureConnected 重连（G43 stale port 流程）
 *
 * 跳过条件：EMRG_SKIP_INTEGRATION=1（CI 单测步骤用，集成测试在独立 CI 步骤跑，G100/#357）。
 */

const { test, before, after, skip } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const SKIP = !!process.env.EMRG_SKIP_INTEGRATION;
if (SKIP) {
  skip("EMRG_SKIP_INTEGRATION=1 — 集成测试跳过（本地运行）");
}

const { DaemonClient, generateSessionId, PORT_FILE } = require("../daemon_client.js");

// ── 环境 ─────────────────────────────────────────────────
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "emrg-gui-integ-"));
const tmpEmrg = path.join(tmp, ".emrg");
fs.mkdirSync(tmpEmrg, { recursive: true });
fs.writeFileSync(path.join(tmpEmrg, "config.toml"),
  `[llm]\nbase_url = "http://localhost:9999/v1"\napi_key = "test-key"\nmodel = "test-model"\n`);

const origHome = process.env.HOME;
const origUserProfile = process.env.USERPROFILE;
process.env.HOME = tmp;
process.env.USERPROFILE = tmp;

function findPython() {
  const root = path.resolve(__dirname, "..", ".."); // emrg/gui → 项目根
  const candidates = [
    path.join(root, ".venv", "bin", "python"),
    path.join(root, ".venv", "Scripts", "python.exe"),
    "python3",
    "python",
  ];
  for (const c of candidates) {
    if (c.startsWith("/") || c.includes(path.sep)) {
      try { fs.accessSync(c, fs.constants.X_OK); return c; } catch { continue; }
    }
    return c;
  }
  return "python3";
}

let daemonProc = null;
let client = null;

function waitForPortFile(timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const check = () => {
      try {
        const text = fs.readFileSync(PORT_FILE(), "utf8");
        if (text && text.trim()) return resolve(text);
      } catch { /* not yet */ }
      if (Date.now() > deadline) return reject(new Error("daemon port file timeout"));
      setTimeout(check, 200);
    };
    check();
  });
}

function spawnDaemon() {
  const python = findPython();
  const child = spawn(python, ["-m", "emrg.server"], {
    env: { ...process.env, HOME: tmp, USERPROFILE: tmp },
    stdio: "ignore",
    detached: true,
  });
  child.unref();
  return child;
}

before(async () => {
  // ⚠️ EMRG_SKIP_INTEGRATION 时 skip() 只跳过测试体，before() 仍执行——
  //    必须显式短路，否则 Windows CI 单测步骤会 spawn daemon 超时（daemon port file timeout）
  if (process.env.EMRG_SKIP_INTEGRATION) return;
  daemonProc = spawnDaemon();
  await waitForPortFile();
  client = new DaemonClient({ projectDir: tmp });
  await client.ensureConnected();
});

async function killDaemon(child) {
  if (!child) return;
  // SIGTERM → 等待退出（最多 3s）→ SIGKILL 兜底（防 detached 孤儿残留）
  try { process.kill(-child.pid); } catch { /* ignore */ }
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline && child.exitCode === null) {
    await new Promise((r) => setTimeout(r, 100));
  }
  if (child.exitCode === null) {
    try { process.kill(-child.pid, "SIGKILL"); } catch { /* ignore */ }
  }
}

after(async () => {
  if (client) { try { client.close(); } catch { /* ignore */ } }
  // 清理 before spawn 的 daemon + 重连测试中 startDaemon 重拉的 daemon（G43 孤儿修复）
  await killDaemon(daemonProc);
  if (client && client._daemonChild && client._daemonChild !== daemonProc) {
    await killDaemon(client._daemonChild);
  }
  try { process.env.HOME = origHome; } catch { /* ignore */ }
  try { process.env.USERPROFILE = origUserProfile; } catch { /* ignore */ }
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* ignore */ }
});

test("ping → pong（ServerPong 结构）", { skip: SKIP }, async () => {
  const pong = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("pong timeout")), 5000);
    const off = client.onEvent((type, data) => {
      if (type === "pong") { off(); clearTimeout(timer); resolve(data); }
    });
    client.sendCommand("ping");
  });
  assert.ok(pong.identity, "identity present");
  assert.ok(pong.identity.instance_id, "instance_id present");
  assert.ok(pong.uptime_seconds >= 0);
  assert.ok(typeof pong.evolution_count === "number");
  assert.ok(pong.model, "model present");
});

test("list_sessions → sessions_list（隔离环境空列表）", { skip: SKIP }, async () => {
  const frame = await client.sendCommandAndWait("list_sessions", { cwd: tmp }, 5000);
  assert.strictEqual(frame.type, "sessions_list");
  assert.ok(Array.isArray(frame.sessions));
});

test("list_models → models_list", { skip: SKIP }, async () => {
  const frame = await client.sendCommandAndWait("list_models", {}, 5000);
  assert.strictEqual(frame.type, "models_list");
  assert.ok(Array.isArray(frame.models));
  assert.ok(frame.models.some((m) => m.model === "test-model" || (m.name || "").includes("test-model")),
    `models contain test-model: ${JSON.stringify(frame.models)}`);
});

test("list_projects → projects_list（空）", { skip: SKIP }, async () => {
  const frame = await client.sendCommandAndWait("list_projects", { cwd: tmp }, 5000);
  assert.strictEqual(frame.type, "projects_list");
  assert.ok(Array.isArray(frame.projects));
});

test("resume_session（不存在）→ 错误帧（无破坏性）", { skip: SKIP }, async () => {
  const sid = generateSessionId();
  await assert.rejects(
    client.sendCommandAndWait("resume_session", { session_id: sid, cwd: tmp }, 5000),
    /not found|error/i
  );
});

test("delete_session（不存在）→ 错误帧（无破坏性）", { skip: SKIP }, async () => {
  const sid = generateSessionId();
  await assert.rejects(
    client.sendCommandAndWait("delete_session", { session_id: sid, cwd: tmp }, 5000),
    /not found|error/i
  );
});

test("daemon 被杀 → ensureConnected 重连（G43 stale port 流程）", { skip: SKIP }, async () => {
  // 杀 daemon（不删 port 文件）→ 连接应失败 → stale 检测 → 删文件重拉
  try { process.kill(-daemonProc.pid); } catch { /* ignore */ }
  await new Promise((r) => setTimeout(r, 800));
  assert.strictEqual(client.connected, false, "daemon killed → disconnected");
  // ensureConnected 应自动重拉（G43 stale port）
  await client.ensureConnected();
  assert.strictEqual(client.connected, true);
  assert.ok(fs.existsSync(PORT_FILE()), "new port file written");
  // 重连后可继续通信
  const frame = await client.sendCommandAndWait("list_sessions", { cwd: tmp }, 5000);
  assert.strictEqual(frame.type, "sessions_list");
});
