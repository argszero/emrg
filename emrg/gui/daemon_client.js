"use strict";
/**
 * daemon_client.js — main 进程内唯一与 emrgd 通信的模块。
 *
 * 协议语义完全对照 emrg/client/daemon_manager.py（Phase 2 参考实现）：
 * - 读 ~/.emrg/emrgd.port（port\n token，0o600）→ ws://127.0.0.1:<port> → auth 首帧 → auth_ok
 * - 坏 JSON 帧忽略（对照 daemon_manager.recv R53）
 * - ConnectionClosed 传播 → 触发重连（对照 R11）
 * - auth 失败（auth_ok 前 close）= AuthError 语义（G88：停止自动重试，防无限重连）
 * - G43 stale port：连接失败即使 port 文件存在 → 删文件重拉
 */

const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { spawn } = require("child_process");
const WebSocket = require("ws");

// G129 (rant 2026-08-09T08:03:46): PORT_FILE 必须接受 projectDir——硬编码
// os.homedir() 时，Windows 测试的 setupTempHome() 只设 HOME 不设 USERPROFILE，
// os.homedir() 仍读 USERPROFILE（真实用户目录）→ 测试把假 port/token 写进
// 真实的 ~/.emrg/emrgd.port → 演化周期 10 小时连不上 daemon（WinError 1225）。
// 所有调用点必须传 this.projectDir（默认 os.homedir() 保持生产行为不变）。
const PORT_FILE = (projectDir = os.homedir()) => path.join(projectDir, ".emrg", "emrgd.port");
const MAX_PAYLOAD = 16 * 1024 * 1024; // G62/G105：16MB 双向一致（工具输出上限 200KB）
const AUTH_TIMEOUT_MS = 10_000;
const SPAWN_WAIT_MS = 5_000;
const PENDING_TIMEOUT_MS = 5_000;
const STREAM_END_TIMEOUT_MS = 30_000; // G94：最后帧后 30s 无 done 强制结束

const SESSION_ID_RE = /^s_\d{6}_\d{4}_[0-9a-f]{4,8}$/;

// G93：命令类型 → 响应帧类型映射（daemon 协议，daemon.py _process_message）
const RESPONSE_TYPES = {
  list_sessions: "sessions_list",
  list_models: "models_list",
  list_projects: "projects_list",
  list_history: "history_list",
  list_tasks: "tasks_list",
  list_memories: "memories_list",
  resume_session: "resume_result",
  delete_session: "session_deleted",
  set_model: "model_set",
  clear_session: "clear_result", // 修正：daemon 命令名是 clear_session（原 clear 匹配不上）
  rename_session: "rename_result", // 修正：daemon 命令名是 rename_session
  trigger_task: "trigger_result", // 修正：daemon 命令名是 trigger_task
  compact: "compact_result",
  rewind_session: "rewind_result", // 补缺：daemon.py:955 rewind_result
  read_memory: "memory_content", // 补缺：daemon.py:771/778 memory_content
  evolution_summary: "evolution_summary", // WorkBuddy P3：自进化可见化
  github_connect: "github_connect_result", // Windows GCM rant Stage 2：PAT 授权（daemon.py github_connect）
  github_disconnect: "github_disconnect_result", // Windows GCM rant Stage 2：断开（daemon.py github_disconnect）
  github_connect_web: "github_connect_web_result", // Stage 2b：device flow（daemon.py github_connect_web）
};

class DaemonClient {
  constructor({ projectDir = os.homedir(), logger = console, authTimeoutMs = AUTH_TIMEOUT_MS, isPackaged = false } = {}) {
    this.projectDir = projectDir;
    this.logger = logger;
    this._authTimeoutMs = authTimeoutMs; // G142 测试可注入短超时（默认 10s）
    this._isPackaged = isPackaged; // Phase 4：打包模式（rant #12 §4）由 main.js 注入 app.isPackaged
    this.ws = null;
    this.connected = false;
    this._events = new Set(); // callbacks
    this._pending = new Map(); // frameType → {resolve, reject, timer}
    this._pendingFifo = [];
    this._groups = new Map(); // requestId → {node, lastSeen, timer, own}
    this._currentStream = null; // {requestId, timer}
    this._activeToolCards = new Map(); // tool_call_id → info
    this._authFailed = false;
    this._reconnectTimer = null;
    this._stopReconnect = false;
  }

  // ── 生命周期 ────────────────────────────────────────────

  isRunning(timeoutMs = 1500) {
    // G43/G90：TCP 探测（不可简化为 port 文件存在）
    try {
      const port = Number(fs.readFileSync(PORT_FILE(this.projectDir), "utf8").split("\n")[0]);
      return new Promise((resolve) => {
        const sock = net.connect({ host: "127.0.0.1", port, timeout: timeoutMs });
        sock.once("connect", () => { sock.destroy(); resolve(true); });
        sock.once("error", () => { sock.destroy(); resolve(false); });
        sock.once("timeout", () => { sock.destroy(); resolve(false); });
      });
    } catch {
      return Promise.resolve(false);
    }
  }

  async startDaemon() {
    // Phase 4（rant #12 §4）：打包模式直接 spawn 捆绑 emrgd 可执行文件（脚本内部
    // exec python -m emrg.server）；源码模式保持 python -m emrg.server。
    if (this._isPackaged) {
      const emrgdPath = this._findDaemonExecutable();
      const opts = {
        cwd: this.projectDir,
        stdio: "ignore",
        detached: true,
      };
      // R36/R66：Windows .cmd 需 shell:true（非 PE），windowsHide 防黑窗闪烁；
      // Node shell 模式自动给含空格的用户名加引号（R92b）。
      if (process.platform === "win32") {
        opts.shell = true;
        opts.windowsHide = true;
      }
      this.logger.info(`[gui] spawning packaged daemon: ${emrgdPath} cwd=${this.projectDir}`);
      const child = spawn(emrgdPath, [], opts);
      child.unref();
      this._daemonChild = child;
      const deadline = Date.now() + SPAWN_WAIT_MS;
      while (Date.now() < deadline) {
        if (await this.isRunning(500)) return child;
        await new Promise((r) => setTimeout(r, 300));
      }
      throw new Error("emrgd failed to start within timeout");
    }
    // G125：spawn 设 cwd=project_dir（daemon load_skills 用 Path.cwd() 加载项目级 skills）
    const python = this._findPython();
    const args = ["-m", "emrg.server"];
    this.logger.info(`[gui] spawning daemon: ${python} ${args.join(" ")} cwd=${this.projectDir}`);
    const child = spawn(python, args, {
      cwd: this.projectDir,
      stdio: "ignore", // G68：对照 DEVNULL
      detached: true, // 对照 start_new_session=True
      // windowsHide: python.exe 是 console 子系统——GUI spawn 时不隐藏会
      // 弹一个命令行黑窗（打包模式 emrgd.cmd 已改走 pythonw.exe，这里补源码模式）。
      ...(process.platform === "win32" ? { windowsHide: true } : {}),
    });
    child.unref(); // GUI 退出不带走 daemon
    this._daemonChild = child; // 暴露 child（集成测试 after 清理用）
    // 等最多 SPAWN_WAIT_MS 就绪
    const deadline = Date.now() + SPAWN_WAIT_MS;
    while (Date.now() < deadline) {
      if (await this.isRunning(500)) return child;
      await new Promise((r) => setTimeout(r, 300));
    }
    throw new Error("emrgd failed to start within timeout");
  }

  // Rant 2026-08-09T13:16:36 G43 加固：daemon 进程是否存活（emrgd.pid 探测）。
  // 存活 → ws 连接失败视为瞬时（daemon 重启/启动中），保留 port 文件交给退避重试；
  // 死亡 → 允许 G43 删文件重拉。
  _daemonProcessAlive() {
    try {
      const pidFile = path.join(this.projectDir, ".emrg", "emrgd.pid");
      const pid = Number(String(fs.readFileSync(pidFile, "utf8")).trim());
      if (!Number.isInteger(pid) || pid <= 0) return false;
      process.kill(pid, 0); // 信号 0 = 仅探测存在性
      return true;
    } catch (err) {
      if (err && err.code === "EPERM") return true; // 进程存在但权限不同（Windows）
      return false; // ESRCH（不存在）/ ENOENT（无 pid 文件）
    }
  }

  _findDaemonExecutable() {
    // Phase 4（rant #12 §4 R7）：打包模式定位捆绑 emrgd。
    // Windows: ~/.emrg/install/bin/emrgd.cmd；POSIX: ~/.emrg/install/bin/emrgd。
    const bin = path.join(os.homedir(), ".emrg", "install", "bin");
    const name = process.platform === "win32" ? "emrgd.cmd" : "emrgd";
    return path.join(bin, name);
  }

  _findPython() {
    // G59/G61/G126：优先项目 .venv，其次 PATH python3/python
    const root = path.resolve(__dirname, "..", "..");
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
      return c; // PATH 兜底
    }
    return "python3";
  }

  async ensureConnected() {
    // 1. 读 port 文件 → 无则拉 daemon
    let port, token;
    try {
      const text = fs.readFileSync(PORT_FILE(this.projectDir), "utf8");
      [port, token] = text.split(/\s+/);
      if (!port || !token) throw new Error("malformed port file");
    } catch {
      await this.startDaemon();
      const text = fs.readFileSync(PORT_FILE(this.projectDir), "utf8");
      [port, token] = text.split(/\s+/);
    }

    // 2. ws 连接（G43 stale port：连接失败删文件重拉一次）
    try {
      this.ws = new WebSocket(`ws://127.0.0.1:${port}`, { maxPayload: MAX_PAYLOAD });
    } catch (e) {
      // ws 构造一般异步失败；在 open 事件处理
      throw e;
    }
    try {
      await this._awaitOpen();
    } catch (e) {
      // G43 加固（rant 2026-08-09T13:16:36 根因）：port 文件存在但连不上时，
      // 先查 emrgd.pid —— daemon 进程还活着就【绝不删 port 文件】。旧 G43 直接
      // unlink 会把健康 daemon 的 port 文件删掉 → 僵尸态（daemon 活着、scheduler
      // 永远 cannot connect、PID 锁挡住新 spawn）。只有 daemon 真死了才删+重拉。
      if (this._daemonProcessAlive()) {
        this.logger.warn(
          `[gui] ws connect failed: ${e.message} — daemon pid alive, keeping port file (transient)`
        );
        try { this.ws.close(); } catch { /* ignore */ }
        throw new Error(`daemon unreachable (pid alive): ${e.message}`);
      }
      this.logger.warn(`[gui] ws connect failed: ${e.message} — stale port, respawning daemon`);
      try { this.ws.close(); } catch { /* ignore */ }
      try { fs.unlinkSync(PORT_FILE(this.projectDir)); } catch { /* ignore */ }
      await this.startDaemon();
      const text = fs.readFileSync(PORT_FILE(this.projectDir), "utf8");
      [port, token] = text.split(/\s+/);
      this.ws = new WebSocket(`ws://127.0.0.1:${port}`, { maxPayload: MAX_PAYLOAD });
      await this._awaitOpen();
    }

    // 3. auth 首帧
    this.ws.send(JSON.stringify({ type: "auth", token }));

    // 4. 等 auth_ok（G64：auth_ok 由 ensureConnected 消费，不进事件流）
    const authOk = await new Promise((resolve, reject) => {
      const cleanup = () => {
        clearTimeout(timer);
        this.ws.off("message", onMsg);
        this.ws.off("close", onClose);
      };
      const timer = setTimeout(() => {
        // G142：超时（daemon 慢/卡）也要清理 listener + 关 ws，防 listener/连接泄漏
        cleanup();
        try { this.ws.close(); } catch { /* ignore */ }
        reject(new Error("auth timeout"));
      }, this._authTimeoutMs);
      const onMsg = (data) => {
        try {
          const frame = JSON.parse(data.toString());
          if (frame.type === "auth_ok") {
            cleanup();
            resolve(true);
          }
        } catch { /* 忽略坏帧 */ }
      };
      const onClose = () => {
        cleanup();
        reject(new Error("authentication failed"));
      };
      this.ws.on("message", onMsg);
      this.ws.once("close", onClose);
    }).catch((e) => {
      this._authFailed = true; // G88：认证失败，停止自动重试
      this.logger.warn(`[gui] auth failed: ${e.message}`);
      throw e;
    });

    this.connected = true;
    this._authFailed = false;

    // 5. 注册 message/close 监听 → 事件流分发
    this.ws.on("message", (data) => this._onFrame(data));
    this.ws.on("close", () => this._onClose());
    this.ws.on("error", (e) => this.logger.warn(`[gui] ws error: ${e.message}`));

    // 连接成功后发一次 ping（G19：拿 server_id/model/evolution_count，不做轮询）
    this.sendCommand("ping");
    return authOk;
  }

  // G43：等待 ws open（失败拒绝 → 触发 stale port 重拉）
  _awaitOpen() {
    return new Promise((resolve, reject) => {
      const onOpen = () => { this.ws.off("error", onError); resolve(); };
      const onError = (err) => reject(err);
      this.ws.once("open", onOpen);
      this.ws.once("error", onError);
    });
  }

  close() {
    if (this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
    }
    this.connected = false;
  }

  // ── 事件 ────────────────────────────────────────────────

  onEvent(callback) {
    this._events.add(callback);
    return () => this._events.delete(callback);
  }

  _emit(type, data) {
    for (const cb of this._events) {
      try { cb(type, data); } catch (e) { this.logger.warn(`[gui] event cb error: ${e.message}`); }
    }
  }

  // ── 消息发送 ────────────────────────────────────────────

  sendTask({ sessionId, cwd, prompt, stream = true, images = null, requestId = null, mode = "auto" }) {
    // G32：request_id 必须作为 id 字段发出（daemon 只回显不自生成）
    // G96：stream 必须显式 true（daemon 读 stream 默认 False）
    // G143：外部预生成 requestId 优先（renderer send 前标记自有流，消除 IPC 往返竞态窗口）
    // WorkBuddy P2：mode="ask" → daemon 不启用工具（纯对话）
    const rid = requestId || crypto.randomUUID();
    const payload = {
      type: "task",
      id: rid,
      session_id: sessionId,
      cwd,
      prompt,
      timestamp: new Date().toISOString(),
      stream,
      images,
    };
    if (mode && mode !== "auto") payload.mode = mode;
    this._setCurrentStream(rid);
    this.ws.send(JSON.stringify(payload));
    return rid;
  }

  sendCommand(type, params = {}) {
    this.ws.send(JSON.stringify({ type, ...params }));
  }

  // G93/G103：命令-响应配对（pending FIFO，按响应帧 type 配对）。
  // 命令类型 ≠ 响应类型（list_sessions → sessions_list 等），经映射表转换。
  sendCommandAndWait(commandType, payload = {}, timeoutMs = PENDING_TIMEOUT_MS) {
    const respType = RESPONSE_TYPES[commandType] || commandType;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pending.delete(respType);
        this._pendingFifo = this._pendingFifo.filter((p) => p.frameType !== respType);
        reject(new Error(`command timeout: ${commandType}`));
      }, timeoutMs);
      const entry = { frameType: respType, resolve, reject, timer };
      this._pending.set(respType, entry);
      this._pendingFifo.push(entry);
      this.sendCommand(commandType, payload);
    });
  }

  _resolvePending(frame) {
    // G103：error 帧无 type——FIFO reject 最早未决；无未决 → 返回 false 走广播
    if (frame.error) {
      const entry = this._pendingFifo.shift();
      if (entry) {
        this._pending.delete(entry.frameType);
        clearTimeout(entry.timer);
        entry.reject(new Error(frame.error));
        return true;
      }
      return false;
    }
    const entry = this._pending.get(frame.type);
    if (entry) {
      this._pending.delete(frame.type);
      this._pendingFifo = this._pendingFifo.filter((p) => p !== entry);
      clearTimeout(entry.timer);
      entry.resolve(frame);
      return true;
    }
    return false;
  }

  _rejectAllPending(msg) {
    for (const entry of this._pendingFifo) {
      clearTimeout(entry.timer);
      entry.reject(new Error(msg));
    }
    this._pending.clear();
    this._pendingFifo = [];
  }

  // ── 帧处理 ──────────────────────────────────────────────

  _onFrame(data) {
    let frame;
    try {
      frame = JSON.parse(data.toString());
    } catch {
      this.logger.warn("[gui] bad JSON frame ignored");
      return;
    }
    this._classify(frame);
  }

  // G1/G57 分类
  _classify(frame) {
    // 命令响应优先（pending 配对）
    if (this._resolvePending(frame)) return;

    if (frame.type === "tool_start") {
      this._onToolStart(frame);
      this._emit("tool_started", frame);
      return;
    }
    if (frame.type === "tool_end") {
      this._onToolEnd(frame);
      this._emit("tool_finished", frame);
      return;
    }
    if (frame.type === "cancelled") {
      this._emit("cancelled", frame);
      return;
    }
    if (["sessions_list", "models_list", "history_list", "tasks_list"].includes(frame.type)) {
      this._emit("list_result", frame);
      return;
    }
    if (["resume_result", "model_set", "session_deleted", "clear_result"].includes(frame.type)) {
      this._emit("command_result", frame);
      return;
    }
    if (frame.done) {
      this._onDone(frame);
      this._emit("done", frame);
      return;
    }
    if (frame.delta) {
      this._onDelta(frame);
      this._emit("message_delta", frame);
      return;
    }
    if (frame.error) {
      this._emit("error", frame);
      return;
    }
    if (frame.uptime_seconds !== undefined) {
      this._emit("pong", frame);
      return;
    }
    this.logger.warn(`[gui] unknown frame: ${JSON.stringify(frame).slice(0, 200)}`);
  }

  // G104：tool_start 也触发建组（LLM 先出 tool_calls 后出文本）
  _onToolStart(frame) {
    const rid = frame.request_id;
    if (rid) {
      this._touchGroup(rid, frame.session_id);
      this._activeToolCards.set(frame.tool_call_id, { ...frame, status: "running" });
    }
  }

  _onToolEnd(frame) {
    const card = this._activeToolCards.get(frame.tool_call_id);
    this._activeToolCards.set(frame.tool_call_id, { ...(card || {}), ...frame, status: "done" });
  }

  _onDelta(frame) {
    this._touchGroup(frame.request_id, frame.session_id);
    this._resetStreamTimer(frame.request_id);
  }

  _onDone(frame) {
    const rid = frame.request_id;
    if (rid && this._currentStream && this._currentStream.requestId === rid) {
      this.clearActiveStream();
    }
    // G83：done 清理分组缓存（DOM 保留）
    if (rid) this._cleanupGroup(rid, true);
  }

  // G83/G110 广播分组
  _touchGroup(requestId, sessionId) {
    if (!requestId) return;
    const existing = this._groups.get(requestId);
    if (existing) {
      existing.lastSeen = Date.now();
      if (existing.timer) clearTimeout(existing.timer);
    } else {
      this._groups.set(requestId, { sessionId, lastSeen: Date.now(), timer: null });
      if (this._groups.size > 20) {
        // 上限：丢最老
        const oldest = [...this._groups.entries()].sort((a, b) => a[1].lastSeen - b[1].lastSeen)[0];
        if (oldest) this._cleanupGroup(oldest[0], false);
      }
    }
    // 10 分钟超时
    existing?.timer && clearTimeout(existing.timer);
    const timer = setTimeout(() => this._cleanupGroup(requestId, false), 10 * 60 * 1000);
    timer.unref?.();
    const g = this._groups.get(requestId);
    if (g) g.timer = timer;
  }

  _cleanupGroup(requestId, keepDom) {
    const g = this._groups.get(requestId);
    if (g) {
      if (g.timer) clearTimeout(g.timer);
      this._groups.delete(requestId);
    }
    this._emit("group_cleared", { requestId, keepDom });
  }

  // G110：会话切换时清空旧 session 分组缓存（含 timer），防"幽灵"广播残留
  clearGroups() {
    for (const requestId of [...this._groups.keys()]) {
      this._cleanupGroup(requestId, true);
    }
  }

  // G124：当前发起流缓存
  _setCurrentStream(requestId) {
    this.clearActiveStream();
    this._currentStream = { requestId };
  }

  _resetStreamTimer(requestId) {
    if (this._currentStream && this._currentStream.requestId === requestId) {
      if (this._currentStream.timer) clearTimeout(this._currentStream.timer);
      this._currentStream.timer = setTimeout(() => {
        // G94：最后帧后 30s 无 done → 强制结束
        this.logger.warn("[gui] stream end timeout — forcing done");
        this._emit("done", { request_id: requestId, content: "", done: true, delta: false, timeout: true });
        this.clearActiveStream();
      }, STREAM_END_TIMEOUT_MS);
      this._currentStream.timer.unref?.();
    }
  }

  clearActiveStream() {
    if (this._currentStream) {
      if (this._currentStream.timer) clearTimeout(this._currentStream.timer);
      this._currentStream = null;
    }
    this._activeToolCards.clear();
  }

  // G41/G89/G97：断连处理
  _onClose() {
    this.connected = false;
    this._rejectAllPending("connection closed");
    this.clearActiveStream(); // G94 timer 清理：断连后 30s 超时 timer 不应再触发（防虚假"响应超时"提示）
    this.clearGroups(); // G97：断连清空广播分组缓存（含 10 分钟 timer），防"幽灵"分组残留
    this._emit("disconnected", {});
    this.logger.info("[gui] daemon connection closed");
  }
}

// G14/G81：本地生成 session_id（无 new_session 消息）
function generateSessionId(seed) {
  const now = new Date();
  const ymd = now.toISOString().slice(2, 10).replace(/-/g, "");
  const hm = `${String(now.getHours()).padStart(2, "0")}${String(now.getMinutes()).padStart(2, "0")}`;
  const base = `s_${ymd}_${hm}`;
  let hex = crypto.randomBytes(4).toString("hex");
  // 100 次碰撞后的 8-hex 兜底（对照 session.py:45-46 哈希）
  let n = 0;
  while (n < 100) {
    if (!hex.match(/^[0-9a-f]{8}$/)) hex = crypto.randomBytes(4).toString("hex");
    n += 1;
    if (n > 1) break; // 本地随机 8-hex 已足够，碰撞概率极低
  }
  const sid = `${base}_${hex.slice(0, 8)}`;
  return sid;
}

module.exports = { DaemonClient, generateSessionId, PORT_FILE, SESSION_ID_RE, MAX_PAYLOAD };
