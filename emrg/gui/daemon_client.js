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
const EMRGD_LOG = (projectDir = os.homedir()) => path.join(projectDir, ".emrg", "emrgd.log");
// Rant 2026-08-09T18:47:37（GUI 连不上 daemon 回归）：daemon 的规范运行时目录永远是
// ~/.emrg（daemon.py config_dir() = Path.home()/".emrg"；connect.py 无条件读
// ~/.emrg/emrgd.port）。GUI 的 projectDir 若被 config gui.project_dir 指向别处
// （非 home），按 projectDir 读 port/pid/log 全部落空 → 误判 daemon 不存在 →
// 反复 spawn 撞 PID 锁 → "failed to start after 3 attempts" 假错误，而真 daemon 一直活着。
// 规范位置常量：作为 projectDir 读取失败时的权威回退。
const HOME_PORT_FILE = () => path.join(os.homedir(), ".emrg", "emrgd.port");
const HOME_PID_FILE = () => path.join(os.homedir(), ".emrg", "emrgd.pid");
const HOME_EMRGD_LOG = () => path.join(os.homedir(), ".emrg", "emrgd.log");
const MAX_PAYLOAD = 16 * 1024 * 1024; // G62/G105：16MB 双向一致（工具输出上限 200KB）
const AUTH_TIMEOUT_MS = 10_000;
const SPAWN_WAIT_MS = 5_000;
const PENDING_TIMEOUT_MS = 5_000;
const STREAM_END_TIMEOUT_MS = 30_000; // G94：最后帧后 30s 无 done 强制结束
// Rant 2026-08-09T13:16:36 ⑤（防风暴总闸）：单个"连接生命周期"内最多 spawn
// MAX_SPAWN_ATTEMPTS 次 daemon——之后不再拉起，只把真实错误（含 emrgd.log 尾部）
// 抛给上层，杜绝 GUI 每 5s 反复 spawn（每次 spawn 都是一个新的 cmd 窗口来源）。
const MAX_SPAWN_ATTEMPTS = 3;

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
  list_files: "files_list", // 右栏工作区面板 P1：目录树（daemon.py list_files）
  read_file: "file_content", // 右栏工作区面板 P1：文件查看器（daemon.py read_file）
  // rant 2026-08-12T18:23:15 P2/P3：任务 + 自定义类型 CRUD（daemon.py task_create 等）
  task_create: "task_result",
  task_update: "task_result",
  task_delete: "task_result",
  task_template_list: "templates_list",
  task_template_create: "template_result",
  task_template_update: "template_result",
  task_template_delete: "template_result",
};

class DaemonClient {
  constructor({ projectDir = os.homedir(), logger = console, authTimeoutMs = AUTH_TIMEOUT_MS, isPackaged = false, deltaBatchMs = 0 } = {}) {
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
    this._spawnAttempts = 0; // 连接生命周期内 spawn 计数（成功 auth 后归零）
    // P2 connManager（rant 2026-08-10T15:07:19）：deltaBuf 批量（G122 16ms）每连接一份。
    // deltaBatchMs > 0 时本实例自行批量 message_delta，终态（done/error/cancelled）前
    // 强制冲刷保序（rant 14:11 孤儿节点教训）；默认 0 = 每帧即时发（既有行为不变）。
    this._deltaBatchMs = deltaBatchMs;
    this._deltaBuf = [];
    this._deltaTimer = null;
    // P2 connManager（rant 2026-08-10T15:07:19）：G65 自有流锁每连接一份。
    // 从 main.js 全局移入——多会话各自独立：本连接发出的 task 流运行中 →
    // ownStream=true，切会话/关连接前必须释放。
    this.ownStream = false;
    this.ownStreamRequestId = null;
  }

  // 释放自有流锁（G65）。done（request 匹配或 timeout 兜底）、session busy 即发
  // 错误、cancelled（request 匹配）与断连时调用；sendTask 抛异常时由调用方清理。
  _releaseOwnStream() {
    this.ownStream = false;
    this.ownStreamRequestId = null;
  }

  // ── 生命周期 ────────────────────────────────────────────

  // Rant 2026-08-09T18:47:37：读 port/token 的权威入口。先试 projectDir（G129 语义），
  // 缺失/畸形时回退 daemon 规范位置 ~/.emrg。返回 {port, token, source} 或 null。
  _readPortToken() {
    const tryRead = (file) => {
      try {
        const text = fs.readFileSync(file, "utf8");
        const [port, token] = text.split(/\s+/);
        if (port && token) return { port, token };
      } catch { /* missing/unreadable → try next */ }
      return null;
    };
    const project = tryRead(PORT_FILE(this.projectDir));
    if (project) return { ...project, source: "projectDir" };
    const home = tryRead(HOME_PORT_FILE());
    if (home) {
      this.logger.warn(
        `[gui] port file not found at projectDir (${PORT_FILE(this.projectDir)}) — ` +
        `reusing canonical ~/.emrg/emrgd.port (port=${home.port})`
      );
      return { ...home, source: "home" };
    }
    return null;
  }

  isRunning(timeoutMs = 1500) {
    // G43/G90：TCP 探测（不可简化为 port 文件存在）。18:47:37：port 源改为权威读取
    // （projectDir 回退 ~/.emrg），否则 projectDir≠home 时永远探测假路径 → 假 false。
    const pt = this._readPortToken();
    if (!pt) return Promise.resolve(false);
    const port = Number(pt.port);
    return new Promise((resolve) => {
      const sock = net.connect({ host: "127.0.0.1", port, timeout: timeoutMs });
      sock.once("connect", () => { sock.destroy(); resolve(true); });
      sock.once("error", () => { sock.destroy(); resolve(false); });
      sock.once("timeout", () => { sock.destroy(); resolve(false); });
    });
  }

  _readLogTail(lines = 15) {
    // R124 对应（daemon_manager.py）：spawn 超时后读 emrgd.log 尾部，
    // 让宿主看到真实失败原因（缺 DLL / PATH / 端口冲突），而不是干巴巴的
    // "failed to start within timeout"（rant 2026-08-09T13:16:36 验收项 ②）。
    // 18:47:37：log 也在规范 ~/.emrg 下——projectDir 读不到就回退 home。
    for (const file of [EMRGD_LOG(this.projectDir), HOME_EMRGD_LOG()]) {
      try {
        const data = fs.readFileSync(file, "utf8");
        const tail = data.trim().split("\n").slice(-lines).join("\n");
        return tail ? `\n  emrgd.log tail (${file}):\n${tail}` : "";
      } catch { /* try next */ }
    }
    return "";
  }

  async startDaemon() {
    // Rant 2026-08-09T13:16:36 ⑤：spawn 节流——超过上限不再拉起（防窗口/重试风暴）。
    if (this._spawnAttempts >= MAX_SPAWN_ATTEMPTS) {
      throw new Error(
        `daemon failed to start after ${MAX_SPAWN_ATTEMPTS} attempts — ` +
        `please start it manually ('emrg server') and check emrgd.log${this._readLogTail()}`
      );
    }
    this._spawnAttempts += 1;
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
      this.logger.info(`[gui] daemon spawned: pid=${child.pid} (packaged emrgd)`); // 18:47:37 B2
      const deadline = Date.now() + SPAWN_WAIT_MS;
      while (Date.now() < deadline) {
        if (await this.isRunning(500)) return child;
        await new Promise((r) => setTimeout(r, 300));
      }
      throw new Error(`emrgd failed to start within timeout${this._readLogTail()}`);
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
    this.logger.info(`[gui] daemon spawned: pid=${child.pid} (source mode)`); // 18:47:37 B2
    // 等最多 SPAWN_WAIT_MS 就绪
    const deadline = Date.now() + SPAWN_WAIT_MS;
    while (Date.now() < deadline) {
      if (await this.isRunning(500)) return child;
      await new Promise((r) => setTimeout(r, 300));
    }
    throw new Error(`emrgd failed to start within timeout${this._readLogTail()}`);
  }

  // Rant 2026-08-09T13:16:36 G43 加固：daemon 进程是否存活（emrgd.pid 探测）。
  // 存活 → ws 连接失败视为瞬时（daemon 重启/启动中），保留 port 文件交给退避重试；
  // 死亡 → 允许 G43 删文件重拉。
  // 18:47:37：pid 文件也在规范 ~/.emrg —— projectDir 读不到回退 home。
  _daemonProcessAlive() {
    const pidFiles = [
      path.join(this.projectDir, ".emrg", "emrgd.pid"),
      HOME_PID_FILE(),
    ];
    for (const pidFile of pidFiles) {
      try {
        const pid = Number(String(fs.readFileSync(pidFile, "utf8")).trim());
        if (!Number.isInteger(pid) || pid <= 0) return false;
        process.kill(pid, 0); // 信号 0 = 仅探测存在性
        return true;
      } catch (err) {
        if (err && err.code === "EPERM") return true; // 进程存在但权限不同（Windows）
        // ESRCH（不存在）/ ENOENT（无 pid 文件）→ 试下一个候选
      }
    }
    return false;
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

  // Rant 2026-08-09T18:47:37（A1 + B1）：探测"已存在的 daemon"——4 状态诊断日志
  // （port_file_exists / port_file_content / daemon_alive(ping) / spawn_result）。
  // spawn 失败 ≠ daemon 不存在：GUI 可能因 projectDir≠home 读错 port 文件，
  // 或 daemon 早已被 scheduler/TUI 拉起。返回 {port, token} 或 null。
  async _probeExistingDaemon(spawnResult = "n/a") {
    const pt = this._readPortToken();
    const portFileExists = !!(pt || this._readPortTokenRaw());
    const alive = pt ? await this.isRunning(1000) : false;
    this.logger.info(
      `[gui] probe: port_file_exists=${portFileExists}, port_file_content=${pt ? pt.port : "—"}, ` +
      `daemon_alive(ping)=${alive}, spawn_result=${spawnResult}`
    );
    if (pt && alive) return pt;
    return null;
  }

  // 读 port 文件原始存在性（不含解析），供 probe 日志用。
  _readPortTokenRaw() {
    for (const file of [PORT_FILE(this.projectDir), HOME_PORT_FILE()]) {
      try { if (fs.readFileSync(file, "utf8").trim()) return true; } catch { /* next */ }
    }
    return false;
  }

  // Rant 2026-08-09T18:47:37（A1）：spawn 失败（含 3 次节流）→ 探测已有 daemon →
  // 活着直接复用；确实无 daemon 才抛原始错误。spawn 成功则读回 port/token。
  async _spawnOrProbe() {
    try {
      await this.startDaemon();
    } catch (spawnErr) {
      const existing = await this._probeExistingDaemon(`failed(${String(spawnErr.message).slice(0, 60)})`);
      if (existing) {
        this.logger.warn(
          `[gui] spawn failed (${spawnErr.message}) — existing daemon detected at port=${existing.port}, reusing`
        );
        return existing;
      }
      this.logger.warn(`[gui] spawn failed (${spawnErr.message}) — no existing daemon reachable, giving up`);
      throw spawnErr;
    }
    // spawn 成功：daemon 永远写规范 ~/.emrg/emrgd.port（daemon.py config_dir()），
    // 用权威读取（projectDir 回退 home），不假设 projectDir==home。
    const pt = this._readPortToken();
    if (!pt) throw new Error("port file not written after spawn");
    this.logger.info(`[gui] daemon spawned ok: port=${pt.port}`);
    return pt;
  }

  async ensureConnected({ skipStart = false } = {}) {
    // Rant 2026-08-09T18:47:37：1. 读 port 文件（projectDir → 规范 ~/.emrg 回退）→
    // 无则拉 daemon；spawn 失败先探测已有 daemon，活着直接复用，不再盲报
    // "failed to start after 3 attempts"。每步打结构化诊断日志（B1-B5）。
    // P2 connManager（rant 2026-08-10T15:07:19）：skipStart=true 时 daemon 生命周期
    // 由 connManager 独占管理——本实例只连接**已运行**的 daemon，绝不自行拉起。
    let port, token;
    const pt = this._readPortToken();
    if (pt) {
      port = pt.port;
      token = pt.token;
      this.logger.info(
        `[gui] ensureConnected: port_file_exists=true, port_file_content=${port}, source=${pt.source}`
      );
    } else {
      if (skipStart) {
        throw new Error(
          `daemon not running (skipStart): no port file at ${PORT_FILE(this.projectDir)}`
        );
      }
      this.logger.info(`[gui] ensureConnected: port_file_exists=false — spawning daemon`);
      const r = await this._spawnOrProbe();
      port = r.port;
      token = r.token;
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
      if (skipStart) {
        this.logger.warn(
          `[gui] ws connect failed: ${e.message} — stale port, daemon dead (skipStart: not respawning)`
        );
        try { this.ws.close(); } catch { /* ignore */ }
        throw new Error(`daemon unreachable (skipStart): ${e.message}`);
      }
      this.logger.warn(`[gui] ws connect failed: ${e.message} — stale port, respawning daemon`);
      try { this.ws.close(); } catch { /* ignore */ }
      try { fs.unlinkSync(PORT_FILE(this.projectDir)); } catch { /* ignore */ }
      const r = await this._spawnOrProbe();
      port = r.port;
      token = r.token;
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
    this._spawnAttempts = 0; // 连接生命周期成功 → 重置 spawn 节流计数
    // Rant 2026-08-09T18:47:37 B5：最终状态一行自证——GUI 连的是谁、连没连上。
    this.logger.info(
      `[gui] ensureConnected result=connected, daemon_running=true, port=${port}, token_set=${!!token}`
    );

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
    this._flushDeltaBuf(); // 断连前冲刷残留 delta（防丢失）
    if (this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
    }
    this.connected = false;
  }

  // P2（rant 2026-08-10T15:07:19 + 14:11）：批量冲刷 delta 缓冲。
  // 有定时器则清；有残留则按 {chunks} 形状一次性发出（与 main.js G122 同形）。
  _flushDeltaBuf() {
    if (this._deltaTimer) {
      clearTimeout(this._deltaTimer);
      this._deltaTimer = null;
    }
    if (this._deltaBuf.length) {
      const chunks = this._deltaBuf;
      this._deltaBuf = [];
      this._emit("message_delta", { chunks });
    }
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

  sendTask({ sessionId, cwd, prompt, images = null, requestId = null, mode = "auto" }) {
    // G32：request_id 必须作为 id 字段发出（daemon 只回显不自生成）
    // G143：外部预生成 requestId 优先（renderer send 前标记自有流，消除 IPC 往返竞态窗口）
    // WorkBuddy P2：mode="ask" → daemon 不启用工具（纯对话）
    // rant 21:20:38：非 stream 路径已删除——所有 task 恒走 tool_loop（流式）
    const rid = requestId || crypto.randomUUID();
    const payload = {
      type: "task",
      id: rid,
      session_id: sessionId,
      cwd,
      prompt,
      timestamp: new Date().toISOString(),
      images,
    };
    if (mode && mode !== "auto") payload.mode = mode;
    this._setCurrentStream(rid);
    // G65：自有流锁——本连接发出流式 task 即标记，done/error/cancelled/断连释放
    // （多会话各自独立；main.js emrg:sendMessage 的 G65 切会话检查读本字段）
    this.ownStream = true;
    this.ownStreamRequestId = rid;
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
      this._flushDeltaBuf(); // 终态前冲刷（rant 14:11 同源：delta 不晚于终态）
      // 自有流取消 → 释放 G65 锁（带 request_id 的 cancelled 明确是本流的终态）
      if (frame.request_id === this.ownStreamRequestId) this._releaseOwnStream();
      this._emit("cancelled", frame);
      return;
    }
    if (["sessions_list", "models_list", "history_list", "tasks_list", "files_list"].includes(frame.type)) {
      this._emit("list_result", frame);
      return;
    }
    if (["resume_result", "model_set", "session_deleted", "clear_result"].includes(frame.type)) {
      this._emit("command_result", frame);
      return;
    }
    if (frame.done) {
      this._flushDeltaBuf(); // 终态前冲刷 delta：保证 delta 不晚于终态（rant 14:11）
      this._onDone(frame);
      this._emit("done", frame);
      return;
    }
    if (frame.delta) {
      this._onDelta(frame);
      if (this._deltaBatchMs > 0) {
        this._deltaBuf.push(frame);
        if (!this._deltaTimer) {
          this._deltaTimer = setTimeout(() => this._flushDeltaBuf(), this._deltaBatchMs);
        }
        return; // 批量模式：不即时发单帧
      }
      this._emit("message_delta", frame);
      return;
    }
    if (frame.error) {
      this._flushDeltaBuf(); // 终态前冲刷（rant 14:11 同源）
      // session busy 是即发错误（daemon 返回后无 done 跟随）——释放自有流锁，防 G65 锁泄漏
      // （流式错误如 LLM error 则有 done 跟随，由 done 分支释放，不在此处理）
      if (frame.error && String(frame.error).includes("session busy")) this._releaseOwnStream();
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
    // G65：仅自有流的 done 释放锁（广播 done 不影响）；timeout 兜底同样只清自有
    if (rid === this.ownStreamRequestId || (frame.timeout && this.ownStream)) {
      this._releaseOwnStream();
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
    this._releaseOwnStream(); // 断连即释放 G65 自有流锁（防锁泄漏）
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
