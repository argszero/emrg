// conn-manager.js — P2 of the GUI multi-session rant (2026-08-10T15:07:19)
//
// Connection manager = daemon lifecycle unique owner + one DaemonClient per
// open session (each session = one independent websocket connection, aligned
// with the TUI multi-open model).
//
// This slice establishes the open/close/get contract and the daemon-ownership
// bootstrap. main.js rewiring to use this manager (single-session no-regression
// target) lands in a later slice — until then main.js keeps its single client.
//
// Design notes (from the rant):
// - Each open session = one independent ws connection → natural isolation, no
//   event routing.
// - connManager ensures the daemon is ready (spawn if missing) before opening;
//   session connections then use ensureConnected({ skipStart: true }) — they
//   only connect to the already-running daemon, never spawn.
// - resume_session(sid, cwd=projectPath) auto-subscribes the connection.
// - Already-open sid → reuse the existing connection (no duplicate).

const { DaemonClient } = require("./daemon_client.js");

class ConnManager {
  constructor({ projectDir, logger = console, isPackaged = false } = {}) {
    this.projectDir = projectDir;
    this.logger = logger;
    this.isPackaged = isPackaged;
    this._conns = new Map(); // sid -> { conn, projectPath }
  }

  // 确保 daemon 已运行（connManager = daemon 生命周期唯一 owner）。
  // 引导 client ensureConnected()：port 文件缺失 → spawn；已运行 → 直连。
  // 连接后立即关闭引导连接——会话连接统一走 skipStart（只连不拉）。
  async _ensureDaemon() {
    const boot = new DaemonClient({
      projectDir: this.projectDir,
      logger: this.logger,
      isPackaged: this.isPackaged,
    });
    try {
      await boot.ensureConnected();
    } finally {
      boot.close();
    }
  }

  // open(sid, projectPath)：创建 DaemonClient → ensureConnected(skipStart) →
  // resume_session（自动订阅）。已打开的 sid 直接复用现有连接。
  async open(sid, projectPath) {
    const existing = this._conns.get(sid);
    if (existing) return existing.conn;
    await this._ensureDaemon();
    const conn = new DaemonClient({
      projectDir: this.projectDir,
      logger: this.logger,
      isPackaged: this.isPackaged,
    });
    await conn.ensureConnected({ skipStart: true }); // daemon 已就绪 → 只连不拉
    await conn.sendCommandAndWait("resume_session", { session_id: sid, cwd: projectPath }, 5000);
    this._conns.set(sid, { conn, projectPath });
    return conn;
  }

  // close(sid)：conn.close（断开 ws）→ 移除。返回是否有关闭对象。
  close(sid) {
    const entry = this._conns.get(sid);
    if (!entry) return false;
    entry.conn.close();
    this._conns.delete(sid);
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
  }
}

module.exports = { ConnManager };
