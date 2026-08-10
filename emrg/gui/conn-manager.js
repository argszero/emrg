// conn-manager.js — P2 of the GUI multi-session rant (2026-08-10T15:07:19)
//
// Connection manager = daemon lifecycle unique owner + one DaemonClient per
// open session (each session = one independent websocket connection, aligned
// with the TUI multi-open model).
//
// This slice completes the P2 wiring contract:
// - `ensureDaemon()` keeps a daemon-level connection (`_daemonConn`) used for
//   non-session commands (ping / list_sessions / set_model / github_* / ...).
//   Session connections only connect to the already-running daemon
//   (ensureConnected({ skipStart: true }) — never spawn).
// - `open(sid, projectPath)` creates the per-session connection with
//   per-connection delta batching (deltaBatchMs=16, #626) and resumes the
//   session (auto-subscribe). `{ resume: false }` skips resume for brand-new
//   sessions (daemon implicitly subscribes on first task message).
// - restart recovery: short-window all-drop → recoverAll (re-open + re-subscribe).
// - `onOpen` / `onRecovered` hooks let main.js attach the renderer event
//   bridge (sid-tagged) and refresh UI state after recovery.
//
// Design notes (from the rant):
// - Each open session = one independent ws connection → natural isolation, no
//   event routing.
// - resume_session(sid, cwd=projectPath) auto-subscribes the connection.
// - Already-open sid → reuse the existing connection (no duplicate).

const { DaemonClient } = require("./daemon_client.js");

class ConnManager {
  constructor({ projectDir, logger = console, isPackaged = false, restartWindowMs = 1000, singleRetryDelayMs = 1000 } = {}) {
    this.projectDir = projectDir;
    this.logger = logger;
    this.isPackaged = isPackaged;
    this._conns = new Map(); // sid -> { conn, projectPath }
    this._daemonConn = null; // daemon 级连接（ping/list_sessions 等非会话命令）
    this._openHooks = new Set(); // (sid, conn) => void（新会话连接建立时）
    this._recoverHooks = new Set(); // () => void（recoverAll 完成后）
    // daemon 重启恢复（rant 15:07:19 P2）：短窗口内所有连接同时断 → 判定 daemon 重启
    // → 全部重连重订阅；单条断 → 独立退避重试（多会话场景，单会话全断走恢复）。
    this._restartWindowMs = restartWindowMs;
    this._disconnects = new Map(); // sid -> timestamp（最近一次断连）
    this._recovering = false; // 恢复中守卫（防 close→disconnect→recoverAll 递归）
    this._singleRetryDelayMs = singleRetryDelayMs;
    this._singleRetries = new Map(); // sid -> timer（单连接独立退避在途）
  }

  // 确保 daemon 已运行（connManager = daemon 生命周期唯一 owner）。
  // 引导连接保留为 _daemonConn：供 ping/list_sessions 等非会话命令使用；
  // 会话连接统一 skipStart（只连不拉）。已连接 → 直接复用。
  async ensureDaemon() {
    if (this._daemonConn && this._daemonConn.connected) return this._daemonConn;
    const boot = new DaemonClient({
      projectDir: this.projectDir,
      logger: this.logger,
      isPackaged: this.isPackaged,
    });
    try {
      await boot.ensureConnected();
    } catch (e) {
      boot.close();
      throw e;
    }
    this._daemonConn = boot;
    return boot;
  }

  // daemon 级连接访问器（main.js 非会话命令用；未建立返回 null）
  daemonConn() {
    return this._daemonConn || null;
  }

  // open(sid, projectPath)：创建 DaemonClient → ensureConnected(skipStart) →
  // resume_session（自动订阅；resume:false 跳过，供新会话首条消息前用）。
  // 已打开的 sid 直接复用现有连接。失败时关闭连接不泄漏。
  async open(sid, projectPath, { resume = true } = {}) {
    const existing = this._conns.get(sid);
    if (existing) {
      if (existing.conn.connected) return existing.conn;
      this.close(sid); // 残留断连连接 → 关闭重开（_intentionalClose 标记抑制断线横幅）
    }
    await this.ensureDaemon();
    const conn = new DaemonClient({
      projectDir: this.projectDir,
      logger: this.logger,
      isPackaged: this.isPackaged,
      deltaBatchMs: 16, // P2：delta 批量（G122 16ms）每连接一份（#626）
    });
    try {
      await conn.ensureConnected({ skipStart: true }); // daemon 已就绪 → 只连不拉
      if (resume) {
        await conn.sendCommandAndWait("resume_session", { session_id: sid, cwd: projectPath }, 5000);
      }
    } catch (e) {
      conn.close(); // resume 失败（会话已删等）→ 不泄漏连接
      throw e;
    }
    // 断开监听 → 重启恢复判定（仅当所有打开会话在同一短窗口内断开）
    conn.onEvent((type) => {
      if (type === "disconnected") {
        if (conn._intentionalClose) return; // 主动关闭（切走/删除）不参与重启判定
        this._disconnects.set(sid, Date.now());
        this._onDisconnect(sid);
      }
    });
    this._conns.set(sid, { conn, projectPath });
    for (const cb of this._openHooks) {
      try { cb(sid, conn, projectPath); } catch (e) { this.logger.warn(`[gui] connManager onOpen hook error: ${e.message}`); }
    }
    return conn;
  }

  // close(sid)：conn.close（断开 ws）→ 移除。返回是否有关闭对象。
  // 标记 _intentionalClose：主动关闭（切走/删除）不触发 renderer 断线横幅
  // （桥检查该标记；真断连/daemon 重启的 disconnected 照常转发）。
  // P6（rant 15:07:19 边界）：关闭在忙连接先 cancel 再 close——流式进行中
  // （ownStream）先发 cancel 让 daemon 停流，再断 ws，防半途断线留脏状态
  // （fire-and-forget：断连/ws 已 null 时忽略，不阻塞同步 close 语义）。
  close(sid) {
    const entry = this._conns.get(sid);
    if (!entry) return false;
    this._cancelSingleRetry(sid); // 主动关闭 → 取消该会话的独立退避
    if (entry.conn.ownStream && entry.conn.ws) {
      try { entry.conn.sendCommand("cancel"); } catch { /* 断连时忽略 */ }
    }
    entry.conn._intentionalClose = true;
    entry.conn.close();
    this._conns.delete(sid);
    this._disconnects.delete(sid);
    return true;
  }

  // get(sid)：路由到对应实例；未打开返回 null。
  get(sid) {
    const entry = this._conns.get(sid);
    return entry ? entry.conn : null;
  }

  all() {
    return [...this._conns.keys()];
  }

  closeAll() {
    for (const sid of [...this._conns.keys()]) this.close(sid);
    if (this._daemonConn) {
      this._daemonConn.close();
      this._daemonConn = null;
    }
  }

  // 新会话连接建立钩子（main.js 挂 renderer 事件桥；recoverAll 重开路径同样触发）
  onOpen(callback) {
    this._openHooks.add(callback);
  }

  // recoverAll 完成钩子（main.js 刷新 UI 状态：status connected + sessions + pong）
  onRecovered(callback) {
    this._recoverHooks.add(callback);
  }

  // ── daemon 重启恢复（rant 15:07:19 P2）──────────────────────────────

  // 所有当前打开会话都在重启窗口内断开 → 判定 daemon 重启。
  _restartDetected() {
    const open = [...this._conns.keys()];
    if (open.length === 0) return false;
    const now = Date.now();
    return open.every((sid) => {
      const t = this._disconnects.get(sid);
      return t !== undefined && now - t <= this._restartWindowMs;
    });
  }

  _onDisconnect(sid) {
    if (this._recovering) return; // 恢复中自己触发的断开不递归
    if (this._restartDetected()) {
      this.logger.info(
        `[gui] connManager: all ${this._conns.size} connection(s) dropped within ${this._restartWindowMs}ms — daemon restart detected, recovering`
      );
      this.recoverAll().catch((e) =>
        this.logger.warn(`[gui] connManager recover failed: ${e.message}`)
      );
    } else {
      // 单条断（多会话场景，非重启）→ 独立退避重试
      this._scheduleSingleRetry(sid);
    }
  }

  // 单连接独立退避（rant 15:07:19 P2：单条断 → 独立退避重试）。
  // 退避到期后若该会话连接仍断 → open() 重开（stale → 关闭重开 + resume 重订阅）。
  _scheduleSingleRetry(sid) {
    if (this._singleRetries.has(sid)) return; // 已有退避在途
    this.logger.info(
      `[gui] connManager: session ${sid} dropped (not all) — independent backoff retry in ${this._singleRetryDelayMs}ms`
    );
    const timer = setTimeout(() => {
      this._singleRetries.delete(sid);
      this._retrySingle(sid).catch((e) =>
        this.logger.warn(`[gui] connManager: session ${sid} retry failed: ${e.message}`)
      );
    }, this._singleRetryDelayMs);
    timer.unref?.();
    this._singleRetries.set(sid, timer);
  }

  _cancelSingleRetry(sid) {
    const t = this._singleRetries.get(sid);
    if (t) {
      clearTimeout(t);
      this._singleRetries.delete(sid);
    }
  }

  async _retrySingle(sid) {
    const entry = this._conns.get(sid);
    if (!entry || entry.conn.connected) return; // 已重开/已主动关闭
    this.logger.info(`[gui] connManager retrying session ${sid}`);
    await this.open(sid, entry.projectPath); // stale → close + reopen（含 resume 重订阅）
  }

  // 全部重连重订阅（复用 open 序列：ensureDaemon → skipStart 会话连接 →
  // resume_session）。单会话恢复失败跳过不阻塞其余（写盘/重试由后续片处理）。
  async recoverAll() {
    if (this._recovering) return;
    this._recovering = true;
    const sessions = [...this._conns.entries()].map(([sid, entry]) => ({
      sid,
      projectPath: entry.projectPath,
    }));
    try {
      for (const { sid } of sessions) this.close(sid);
      for (const t of this._singleRetries.values()) clearTimeout(t);
      this._singleRetries.clear();
      this._disconnects.clear();
      for (const { sid, projectPath } of sessions) {
        try {
          await this.open(sid, projectPath);
          this.logger.info(`[gui] connManager recovered session ${sid}`);
        } catch (e) {
          this.logger.warn(`[gui] connManager recover: session ${sid} reopen failed: ${e.message}`);
        }
      }
    } finally {
      this._recovering = false;
    }
    for (const cb of this._recoverHooks) {
      try { cb(); } catch (e) { this.logger.warn(`[gui] connManager onRecovered hook error: ${e.message}`); }
    }
  }
}

module.exports = { ConnManager };
