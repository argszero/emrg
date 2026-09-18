"use strict";
/**
 * daemon_client.js — main 进程内唯一与 emrgd 通信的模块。
 *
 * 协议语义完全对照 emrg/client/daemon_manager.py（Phase 2 参考实现）：
 * - 读 ~/.emrg/emrgd.token（单行 token，0o600）→ ws://127.0.0.1:56031（EMRGD_PORT 常量）→ auth 首帧 → auth_ok
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

// Rant 2026-08-20T16:03:31：GUI"工作目录"概念已删除——daemon 运行时文件固定读取
// 规范位置 ~/.emrg（daemon.py config_dir() = Path.home()/".emrg"；connect.py 无条件
// 读 ~/.emrg/emrgd.token）。projectDir 参数与 G129 回退逻辑随概念一起清理（#884 后
// emrgd.token 已是唯一规范位置，回退冗余）。
const TOKEN_FILE = () => path.join(os.homedir(), ".emrg", "emrgd.token");
const EMRGD_LOG = () => path.join(os.homedir(), ".emrg", "emrgd.log");
// Issue #1276 item 4：子进程**自己的 stderr** 落到这里。emrgd 的日志 handler 是
// 子进程内部才装的（`emrg/server/__main__.py:_configure_logging`），所以"还没走到
// 那一步就死了"的失败（import 失败、补丁语法错、缺模块）在 emrgd.log 里一个字都
// 没有——此前 stderr 直接丢弃，宿主此时被告知"什么都没有"，正是该 issue 的第二个
// 症状。落**文件**而不是终端：stderr 被丢弃的理由是 daemon 不得写进客户端界面，
// 落文件保留这个性质，同时给失败一个可读的地方。不会重复写：emrgd 只在
// `sys.stderr.isatty()` 时加 StreamHandler，stderr 指向文件与 DEVNULL 一样不是 tty。
const EMRGD_START_ERR = () => path.join(os.homedir(), ".emrg", "emrgd-start.err");
// Fixed daemon port (rant 2026-08-19T08:05:21 + 2026-08-20T14:32:52): the
// daemon always listens on this constant — keep in sync with emrg/connect.py
// EMRGD_PORT and emrg/_stop_all.py _EMRGD_PORT. The token file no longer
// carries a port; all connections/probes use this constant.
const EMRGD_PORT = 56031;
const MAX_PAYLOAD = 16 * 1024 * 1024; // G62/G105：16MB 双向一致（工具输出上限 200KB）
const AUTH_TIMEOUT_MS = 10_000;
// Issue #1276 item 5：启动等待窗口。GUI 自己的默认值保持 5.0s 不变（改默认值是一次
// 行为变更，而"冷启动是否真需要更久"没有被测量过）；变的是它现在**可被宿主抬高**——
// 与 emrg/client/daemon_manager.py 的 `EMRG_START_TIMEOUT` 同一个变量、同一套回落规则
// （见 `_startWindowMs`）。此前 GUI 只有这个源码常量，而它被打包进 app.asar：最需要
// 抬高窗口的宿主（不用终端的那些人）恰恰是唯一够不着它的人。
const SPAWN_WAIT_MS = 5_000;
const SPAWN_WAIT_POLL_MS = 300;
const START_WINDOW_ENV = "EMRG_START_TIMEOUT";
const PENDING_TIMEOUT_MS = 5_000;
// Rant 2026-08-09T13:16:36 ⑤（防风暴总闸）：单个"连接生命周期"内最多 spawn
// MAX_SPAWN_ATTEMPTS 次 daemon——之后不再拉起，只把真实错误（含 emrgd.log 尾部）
// 抛给上层，杜绝 GUI 每 5s 反复 spawn（每次 spawn 都是一个新的 cmd 窗口来源）。
const MAX_SPAWN_ATTEMPTS = 3;
// Issue #1283：日志标记的"无标记"值——读整个文件（对齐 daemon_manager.py 的
// `(0, None)`）。标记本身是 {size, ino}，绝不是裸 offset：emrgd.log 由
// RotatingFileHandler 维护，会被 rotate（旧文件改名、同名新文件重建），
// 到那时裸 offset 索引的是另一个文件。
const NO_LOG_MARK = Object.freeze({ size: 0, ino: null });
// "没观察到任何 spawn 状态"——直接调用 `_awaitDaemonReady` / `_startupFailureDetail`
// （测试与将来别的调用者）时的默认值，与"子进程有 pid"同义。
const NO_SPAWN_STATE = Object.freeze({ err: null, neverStarted: false });

// 会话 ID 允许多种形态：
//  - 交互会话：s_<6位日期>_<4位时间>_<hex id>（generateSessionId 产物）
//  - 固定任务会话：emrg-evolution-<task-name>（首尾均连字符，无下划线分隔）
// 两者都可能经 GUI 的 switchSession/打开会话 传入，需都接受。
const SESSION_ID_RE = /^(s_\d{6}_\d{4}_[0-9a-f]{4,8}|emrg-evolution-[A-Za-z0-9_-]+)$/;

// G93：命令类型 → 响应帧类型映射（daemon 协议，daemon.py _process_message）
const RESPONSE_TYPES = {
  list_sessions: "sessions_list",
  list_models: "models_list",
  list_projects: "projects_list",
  list_history: "history_list",
  list_tasks: "tasks_list",
  list_rants: "rants_list",
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
  constructor({ logger = console, authTimeoutMs = AUTH_TIMEOUT_MS, isPackaged = false, deltaBatchMs = 0 } = {}) {
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

  // Rant 2026-08-20T16:03:31：读 token 的权威入口——固定读 daemon 规范位置
  // ~/.emrg/emrgd.token（#884 后唯一位置）。文件仅含单行 token；端口一律用
  // EMRGD_PORT 常量。返回 {token, source, port} 或 null。
  _readPortToken() {
    const tryRead = (file) => {
      try {
        const text = fs.readFileSync(file, "utf8");
        const token = text.trim();
        if (token) return { token };
      } catch { /* missing/unreadable → try next */ }
      return null;
    };
    const token = tryRead(TOKEN_FILE());
    if (token) return { ...token, source: "canonical", port: EMRGD_PORT };
    return null;
  }

  isRunning(timeoutMs = 1500) {
    // G43/G90：TCP 探测（不可简化为 token 文件存在）。18:47:37：token 源改为权威读取
    // （projectDir 回退 ~/.emrg），否则 projectDir≠home 时永远探测假路径 → 假 false。
    // 14:32:52：端口用常量 EMRGD_PORT，不再从文件读 port。
    const pt = this._readPortToken();
    if (!pt) return Promise.resolve(false);
    return new Promise((resolve) => {
      const sock = net.connect({ host: "127.0.0.1", port: EMRGD_PORT, timeout: timeoutMs });
      sock.once("connect", () => { sock.destroy(); resolve(true); });
      sock.once("error", () => { sock.destroy(); resolve(false); });
      sock.once("timeout", () => { sock.destroy(); resolve(false); });
    });
  }

  // ── 启动失败诊断（issue #1283，对齐 daemon_manager.py 的 #1279 修复）──────
  // R124 对应（daemon_manager.py）：spawn 超时后读 emrgd.log 尾部，让宿主看到真实
  // 失败原因（缺 DLL / PATH / 端口冲突），而不是干巴巴的 "failed to start within
  // timeout"（rant 2026-08-09T13:16:36 验收项 ②）。18:47:37：log 在规范 ~/.emrg 下。

  // 本次启动的标记：{size, ino}（issue #1283 缺陷 ①）。ino 是判别者——rotate 后
  // 旧文件改名、新文件重建，ino 必变，而 size 可能变大也可能变小（两种 regime 都
  // 会答错，见 _startupFailureDetail）。size 仍带着，只兜 ino 看不到的那一种：
  // 原地截断（同一个文件、字节变少）。取不到（不存在/不可读）→ NO_LOG_MARK。
  _logMark(file = EMRGD_LOG()) {
    try {
      const st = fs.statSync(file);
      return { size: st.size, ino: st.ino };
    } catch {
      return { size: NO_LOG_MARK.size, ino: NO_LOG_MARK.ino };
    }
  }

  // 本次启动**新增**的最后 lines 行，或 ""（本次没写）。整文件的尾部是历史——
  // 把历史当作失败原因，会把宿主引到上一轮的正常退出上（本轮要修的就是这个）。
  // 标记所指的文件若已不在（ino 变了），或标记已越过文件末尾（原地截断），
  // 都从 0 读起：被标记的那个文件已经不在了，当前文件的每一个字节都属于本次。
  _readLogTail(lines = 15, since = NO_LOG_MARK, file = EMRGD_LOG()) {
    if (!since || typeof since.size !== "number") {
      // 与 Python 侧同一断言（test_a_bare_offset_is_not_a_mark）：裸 offset 不是
      // 标记，此时响亮失败，而不是静默读错文件——这个缺陷当初正是这么活下来的。
      throw new TypeError("since must be a {size, ino} mark from _logMark(), not a bare offset");
    }
    try {
      const st = fs.statSync(file);
      let offset = since.size;
      if ((since.ino !== null && st.ino !== since.ino) || st.size < offset) offset = 0;
      const data = fs.readFileSync(file);
      const text = data.slice(offset).toString("utf8").replace(/\s+$/, "");
      return text ? text.split(/\r?\n/).slice(-lines).join("\n") : "";
    } catch {
      return "";
    }
  }

  // 本次启动的子进程 stderr 文件，**截断**打开（issue #1276 item 4）。拿不到 fd 就
  // 返回 null，调用方退回 "ignore"——诊断永远不得让启动本身失败。截断而不追加：
  // 报给宿主的内容必须全是本次写的，append 会把上一轮的 traceback 重新放到"本次
  // 失败的原因"的位置上，正是 #1283 修掉的那个形状。
  _openStartStderr(file = EMRGD_START_ERR()) {
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      return fs.openSync(file, "w");
    } catch {
      return null;
    }
  }

  // 本次启动子进程自己写的 stderr 末 `lines` 行。返回三种答案而不是两种：
  //   null = **读不到**（文件不存在/不可读/不是文件）；"" = 读到了且是空的；文本 = 读到了这些话。
  // 把第一种并进第二种，就是"对一条自己没能打开的通道宣布沉默"——实测过：命名了却读不到的
  // 文件，与读到且为空的文件，产出的文本逐字节相同，且都说 "the child wrote nothing to its own
  // stderr"。诊断不抛异常：读失败是一个**值**，由调用方如实报出。
  // 40 行而不是日志尾巴的 15：traceback 的**结尾**（异常那一行与它的 cause）才是
  // 原因，而一个 Python traceback 比 15 行长。
  _readStartStderr(lines = 40, file = EMRGD_START_ERR()) {
    let text;
    try {
      text = fs.readFileSync(file, "utf8").replace(/\s+$/, "");
    } catch {
      return null;
    }
    return text ? text.split(/\r?\n/).slice(-lines).join("\n") : "";
  }

  // 子进程是否已经退出、以何种方式（issue #1283 缺陷 ②）。Node 把"退出码"与
  // "被信号杀"分成两个字段（exitCode / signalCode）；Python 的 returncode 把信号
  // 表示为负数，所以那边只看 returncode 就够，这里两个都要看——否则被 SIGKILL 的
  // 子进程会被读成 "still running"。只有真正的 number / string 才算：mock 的
  // undefined 不得读成"已退出"（Python 侧既有断言：非 int 的 returncode 不算）。
  _childExit(child) {
    const code = child && child.exitCode;
    const signal = child && child.signalCode;
    return {
      code: typeof code === "number" ? code : null,
      signal: typeof signal === "string" ? signal : null,
    };
  }

  // spawn 本身失败（ENOENT：打包路径 `~/.emrg/install/bin/emrgd` 不在、PATH 里没有
  // python）是第三种状态——既不是"退出"也不是"还在跑"。它不在 exitCode/signalCode
  // 里：Node 把 'error' **异步**发给子进程，而 pid 从 spawn 返回那一刻起就是
  // undefined（本机 node 26.5.0 实测：spawn 一个不存在的路径 → 同步 pid=undefined /
  // exitCode=null；'error'(ENOENT) 之后 exitCode 变成 -2；CI 用的 node 22 是否同样
  // 变 -2 未测，所以判据不依赖它）。两件都记下来：
  //   · pid === undefined —— 同步可见，不必等 'error'；
  //   · 'error' 的 err —— 名字（ENOENT）只能从这里拿到。
  // 没有监听者时 'error' 会一路冒到 main.js 的 uncaughtException 日志里，而等待
  // 循环会把整个窗口烧完再谎称 "still running"——正是本诊断要说清的那个事实。
  _watchSpawn(child) {
    const state = { err: null, neverStarted: !child || child.pid === undefined };
    if (child && typeof child.once === "function") {
      child.once("error", (err) => {
        state.err = err;
        state.neverStarted = true;
      });
    }
    return state;
  }

  // 「从未启动」要用哪个名字说（`ENOENT` 只有 'error' 才给得出）。
  _neverStartedName(state) {
    if (!state || !state.err) return "";
    return ` (${state.err.code || state.err.message})`;
  }

  // 一次没能起来的启动，究竟知道些什么（issue #1283 + #1276 item 4）：三个事实，
  // 都不得用猜测代替——本次是否往 log 里写过东西、子进程是活着还是已经退出（退出
  // 码/信号）、以及子进程**自己**的 stderr 说了什么。第三条是另外两条看不见的：在
  // 装上日志 handler 之前就死掉的子进程只写 stderr，没有这一节时宿主被show一个空的
  // emrgd.log 并被告诉"什么都没有"。没写就**如实说没写**，绝不把更早的输出当成本次
  // 的原因。顺序即论证：子进程的遗言在前，它已经能记录的日志尾巴在后。
  _startupFailureDetail(since, child, spawnState = NO_SPAWN_STATE, stderrFile = null) {
    // `stderrFile === null` 表示本次**没有读到**这一路 stderr（开文件失败，spawn 退回
    // "ignore"）。"子进程什么都没写"、"这一路压根没读"、"这一路读不到" 是三件不同的事实，
    // 文本必须分开：给了路径且读到了才读得出沉默；没给路径，或给了却读不到，都不得替它
    // 宣布沉默（也不得引用更早一轮留在那个文件里的字节当成本次原因）。
    const captured = stderrFile !== null;
    const childErr = captured ? this._readStartStderr(40, stderrFile) : null; // null = 读不到
    const childSection = typeof childErr === "string" && childErr
      ? `\n  emrgd own stderr (written by this start attempt, ${stderrFile}):\n${childErr}`
      : captured
        ? (childErr === null
          ? `\n  emrgd own stderr: could not be read (nothing was read from it this attempt, ${stderrFile})`
          : "")
        : "\n  emrgd own stderr: not captured (nothing was read from it this attempt)";
    const tail = this._readLogTail(15, since);
    if (tail) {
      return childSection + `\n  emrgd.log tail (written by this start attempt, ${EMRGD_LOG()}):\n${tail}`;
    }
    let exists = true;
    try {
      fs.statSync(EMRGD_LOG());
    } catch {
      exists = false;
    }
    const { code, signal } = this._childExit(child);
    const how = spawnState.neverStarted
      ? `never started${this._neverStartedName(spawnState)}`
      : signal !== null
        ? `already exited (signal=${signal})`
        : code !== null ? `already exited (exit=${code})` : "still running";
    // 沉默是对一条**读到且为空**的通道的测量：读不到的那一路只支持"没读到它"这个事实。
    const silent = typeof childErr === "string" && childErr
      ? ""
      : captured
        ? (childErr === null
          ? ", and the child's own stderr could not be read"
          : ", and the child wrote nothing to its own stderr")
        : ", and the child's own stderr was not captured";
    return (
      childSection +
      `\n  this start attempt wrote nothing to emrgd.log` +
      `${exists ? "" : " (the file does not exist)"}${silent}; the child is ${how}.` +
      ` Any output earlier in the file is from a previous run.`
    );
  }

  // 等 daemon 起来；已经死掉的子进程立即失败（issue #1283 缺陷 ②），而不是把整个
  // 窗口烧完再报"没有退出码"。stdout 一律丢弃，stderr 落到本次启动的诊断文件
  // （issue #1276 item 4）——但那只在文件真开出来时才成立：`stderrFile` 为 null 的
  // 这一路没有读过任何东西，报告只说"未捕获"，不会替它宣布沉默。从未启动的子进程
  // （ENOENT）同样立即失败：它没有 pid，也永远不会写 log。
  async _awaitDaemonReady(child, mark, waitMs = SPAWN_WAIT_MS, spawnState = NO_SPAWN_STATE, stderrFile = null) {
    const deadline = Date.now() + waitMs;
    while (Date.now() < deadline) {
      if (await this.isRunning(500)) return child;
      if (spawnState.neverStarted) {
        throw new Error(
          `emrgd never started${this._neverStartedName(spawnState)}` +
          this._startupFailureDetail(mark, child, spawnState, stderrFile)
        );
      }
      const { code, signal } = this._childExit(child);
      if (code !== null || signal !== null) {
        const how = signal !== null ? `signal=${signal}` : `exit=${code}`;
        throw new Error(
          `emrgd exited during startup (${how})` +
          this._startupFailureDetail(mark, child, spawnState, stderrFile)
        );
      }
      await new Promise((r) => setTimeout(r, SPAWN_WAIT_POLL_MS));
    }
    throw new Error(
      // 报**真的等过**的那个界，不是宿主敲的那个数：窗口按 0.3s 的轮询量化，与
      // daemon_manager.py 的 `failed to start within {attempts * delay:.1f}s` 同一句法，
      // 于是两条入口的失败文案能被同一句话描述。
      `emrgd failed to start within ${(waitMs / 1000).toFixed(1)}s` +
      this._startupFailureDetail(mark, child, spawnState, stderrFile)
    );
  }

  /** 启动等待窗口（毫秒）：`EMRG_START_TIMEOUT`（秒）若已设置，否则 `SPAWN_WAIT_MS`。
   *
   * issue #1276 item 5 的 GUI 半边。这是 emrg/client/daemon_manager.py
   * `_start_window_seconds()` 的对照实现，读**同一个**环境变量、用同一套回落规则
   * ——宿主给 TUI 设过的值对 GUI 同样生效，而不是两条入口各有一个只有源码能改的窗口。
   *
   * 取值不合法时回落到默认并告警，**绝不抛**：与 `_truncate_start_stderr` 同一条规则
   * ——一个诊断/调参用的变量不得成为启动失败的原因。`Number()` 的可用性在此处够用：
   * 空串与非数字都是 NaN（空串在下面被单独当作"未设置"先返回），`Infinity`/`nan` 被
   * `Number.isFinite` 挡下，非正数被 `<= 0` 挡下 —— 与 Python 侧拒绝的是同一批形态。
   */
  _startWindowMs(env = process.env) {
    const raw = String(env[START_WINDOW_ENV] ?? "").trim();
    if (!raw) return SPAWN_WAIT_MS;
    const seconds = Number(raw);
    if (!Number.isFinite(seconds) || seconds <= 0) {
      this.logger.warn(
        `${START_WINDOW_ENV}='${raw}' is not a positive number of seconds — ` +
        `using ${SPAWN_WAIT_MS / 1000}s`
      );
      return SPAWN_WAIT_MS;
    }
    return seconds * 1000;
  }

  async startDaemon() {
    // Rant 2026-08-09T13:16:36 ⑤：spawn 节流——超过上限不再拉起（防窗口/重试风暴）。
    if (this._spawnAttempts >= MAX_SPAWN_ATTEMPTS) {
      // 这里是"整文件尾部"，不是"本次新增"：节流是连接生命周期级别的，没有单次
      // 尝试的标记可言，所以只说"去查 emrgd.log"，不声称这些行是本次写的。
      const tail = this._readLogTail(15, NO_LOG_MARK);
      throw new Error(
        `daemon failed to start after ${MAX_SPAWN_ATTEMPTS} attempts — ` +
        `please start it manually ('emrg server') and check emrgd.log` +
        (tail ? `\n  emrgd.log tail (${EMRGD_LOG()}):\n${tail}` : "")
      );
    }
    this._spawnAttempts += 1;
    // 标记必须在 spawn 之前取（issue #1283）：spawn 之后再取，本次的输出已经算在
    // 里面了，差值就不存在了。
    const mark = this._logMark();
    // Phase 4（rant #12 §4）：打包模式直接 spawn 捆绑 emrgd 可执行文件（脚本内部
    // exec python -m emrg.server）；源码模式保持 python -m emrg.server。
    if (this._isPackaged) {
      const emrgdPath = this._findDaemonExecutable();
      // 本次启动的 stderr（issue #1276 item 4）：装日志 handler 之前就死掉的子进程
      // 只有这一个出口。取不到 fd 时退回 "ignore"，与旧行为一致。
      const errFd = this._openStartStderr();
      const opts = {
        cwd: os.homedir(),
        stdio: ["ignore", "ignore", errFd === null ? "ignore" : errFd],
        detached: true,
      };
      // R36/R66：Windows .cmd 需 shell:true（非 PE），windowsHide 防黑窗闪烁；
      // Node shell 模式自动给含空格的用户名加引号（R92b）。
      if (process.platform === "win32") {
        opts.shell = true;
        opts.windowsHide = true;
      }
      this.logger.info(`[gui] spawning packaged daemon: ${emrgdPath} cwd=${os.homedir()}`);
      const child = spawn(emrgdPath, [], opts);
      if (errFd !== null) { try { fs.closeSync(errFd); } catch { /* 子进程已持有副本 */ } }
      child.unref();
      this._daemonChild = child;
      this.logger.info(`[gui] daemon spawned: pid=${child.pid} (packaged emrgd)`); // 18:47:37 B2
      const spawnState = this._watchSpawn(child);
      // 只有真开出了诊断文件才算"读过这一路"：拿不到 fd 时子进程的 stderr 退回
      // "ignore"，报告必须说"未捕获"，而不是替这一路宣布沉默。
      return await this._awaitDaemonReady(
        child, mark, this._startWindowMs(), spawnState,
        errFd === null ? null : EMRGD_START_ERR()
      );
    }
    // G125：spawn 设 cwd=project_dir（daemon load_skills 用 Path.cwd() 加载项目级 skills）
    const python = this._findPython();
    const args = ["-m", "emrg.server"];
    this.logger.info(`[gui] spawning daemon: ${python} ${args.join(" ")} cwd=${os.homedir()}`);
    const errFdSource = this._openStartStderr();
    const child = spawn(python, args, {
      cwd: os.homedir(),
      // G68：对照 DEVNULL——stdout 丢弃，stderr 落到本次启动的诊断文件（issue #1276
      // item 4）。见 EMRGD_START_ERR 的注释：装日志 handler 之前的失败只有这一个出口。
      stdio: ["ignore", "ignore", errFdSource === null ? "ignore" : errFdSource],
      detached: true, // 对照 start_new_session=True
      // windowsHide: python.exe 是 console 子系统——GUI spawn 时不隐藏会
      // 弹一个命令行黑窗（打包模式 emrgd.cmd 已改走 pythonw.exe，这里补源码模式）。
      ...(process.platform === "win32" ? { windowsHide: true } : {}),
    });
    if (errFdSource !== null) { try { fs.closeSync(errFdSource); } catch { /* 子进程已持有副本 */ } }
    child.unref(); // GUI 退出不带走 daemon
    this._daemonChild = child; // 暴露 child（集成测试 after 清理用）
    this.logger.info(`[gui] daemon spawned: pid=${child.pid} (source mode)`); // 18:47:37 B2
    // 等最多 _startWindowMs()（宿主可用 EMRG_START_TIMEOUT 抬高）就绪
    const spawnState = this._watchSpawn(child);
    return await this._awaitDaemonReady(
      child, mark, this._startWindowMs(), spawnState,
      errFdSource === null ? null : EMRGD_START_ERR()
    );
  }

  // Rant 2026-08-21T15:26:42：daemon 存活判断用固定端口 TCP 探测——
  // 固定端口才是 ground truth（rant 2026-08-19T08:05:21，connect.py
  // is_server_running_sync 同语义；rant 2026-08-21T16:45:06 后 emrgd.pid 已彻底
  // 移除）。端口通 = daemon 活着 = 绝不删 token；端口不通才允许 stale-token
  // 删除+重拉路径。
  _daemonProcessAlive(timeoutMs = 1000) {
    return new Promise((resolve) => {
      const sock = net.connect({ host: "127.0.0.1", port: EMRGD_PORT, timeout: timeoutMs });
      sock.once("connect", () => { sock.destroy(); resolve(true); });
      sock.once("error", () => { sock.destroy(); resolve(false); });
      sock.once("timeout", () => { sock.destroy(); resolve(false); });
    });
  }

  _findDaemonExecutable() {
    // Phase 4（rant #12 §4 R7）：打包模式定位捆绑 emrgd。
    // Windows: ~/.emrg/install/bin/emrgd.cmd；POSIX: ~/.emrg/install/bin/emrgd。
    const bin = path.join(os.homedir(), ".emrg", "install", "bin");
    const name = process.platform === "win32" ? "emrgd.cmd" : "emrgd";
    return path.join(bin, name);
  }

  // Rant 2026-08-21T12:44:34（restart-to-apply 跟进）：打包模式下 `emrg` 包位于
  // ~/.emrg/install/{source,lib}，宿主 PATH 的 python3 上没有 `emrg` 可导入——
  // 重启流程必须用安装版运行时 python（与 bin/emrgd 启动器同一解析逻辑）。
  _findInstalledPython() {
    const bin = path.join(os.homedir(), ".emrg", "install", "bin");
    if (process.platform === "win32") {
      // R100：bin/python（复制品）缺 DLL 不可用（DLL 在 python-dist/ 根）
      for (const n of ["python-dist\\python.exe", "python-dist\\python3.13.exe", "python.exe"]) {
        const p = path.join(bin, n);
        try { fs.accessSync(p); return p; } catch { /* next candidate */ }
      }
      return "python";
    }
    const p = path.join(bin, "python");
    try { fs.accessSync(p, fs.constants.X_OK); return p; } catch { /* fallthrough */ }
    return "python3";
  }

  // 安装版 PYTHONPATH 前缀（等价 bin/emrgd 的 PYTHONPATH="$PREFIX/source:$PREFIX/lib"）。
  _installedPythonPath() {
    const prefix = path.join(os.homedir(), ".emrg", "install");
    return [path.join(prefix, "source"), path.join(prefix, "lib")].join(path.delimiter);
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
  // （token_file_exists / token_file_content / daemon_alive(ping) / spawn_result）。
  // spawn 失败 ≠ daemon 不存在：daemon 可能早已被 scheduler/TUI 拉起。
  // 返回 {token, source, port} 或 null。
  async _probeExistingDaemon(spawnResult = "n/a") {
    const pt = this._readPortToken();
    const tokenFileExists = !!(pt || this._readPortTokenRaw());
    const alive = pt ? await this.isRunning(1000) : false;
    this.logger.info(
      `[gui] probe: token_file_exists=${tokenFileExists}, token_file_content=${pt ? "present" : "—"}, ` +
      `daemon_alive(ping)=${alive}, spawn_result=${spawnResult}`
    );
    if (pt && alive) return pt;
    return null;
  }

  // 读 token 文件原始存在性（不含解析），供 probe 日志用。
  _readPortTokenRaw() {
    for (const file of [TOKEN_FILE()]) {
      try { if (fs.readFileSync(file, "utf8").trim()) return true; } catch { /* next */ }
    }
    return false;
  }

  // Rant 2026-08-09T18:47:37（A1）：spawn 失败（含 3 次节流）→ 探测已有 daemon →
  // 活着直接复用；确实无 daemon 才抛原始错误。spawn 成功则读回 token。
  async _spawnOrProbe() {
    try {
      await this.startDaemon();
    } catch (spawnErr) {
      const existing = await this._probeExistingDaemon(`failed(${String(spawnErr.message).slice(0, 60)})`);
      if (existing) {
        this.logger.warn(
          `[gui] spawn failed (${spawnErr.message}) — existing daemon detected at port=${EMRGD_PORT}, reusing`
        );
        return existing;
      }
      this.logger.warn(`[gui] spawn failed (${spawnErr.message}) — no existing daemon reachable, giving up`);
      throw spawnErr;
    }
    // spawn 成功：daemon 永远写规范 ~/.emrg/emrgd.token（daemon.py config_dir()），
    // 权威读取固定该位置（16:03:31）。
    const pt = this._readPortToken();
    if (!pt) throw new Error("token file not written after spawn");
    this.logger.info(`[gui] daemon spawned ok: port=${EMRGD_PORT}`);
    return pt;
  }

  async ensureConnected({ skipStart = false } = {}) {
    // Rant 2026-08-09T18:47:37：1. 读 token 文件（固定规范 ~/.emrg）→
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
        `[gui] ensureConnected: token_file_exists=true, port=${port}, source=${pt.source}`
      );
    } else {
      if (skipStart) {
        throw new Error(
          `daemon not running (skipStart): no token file at ${TOKEN_FILE()}`
        );
      }
      this.logger.info(`[gui] ensureConnected: token_file_exists=false — spawning daemon`);
      const r = await this._spawnOrProbe();
      port = r.port;
      token = r.token;
    }

    // 2. ws 连接（G43 stale token：连接失败删文件重拉一次）
    try {
      this.ws = new WebSocket(`ws://127.0.0.1:${EMRGD_PORT}`, { maxPayload: MAX_PAYLOAD });
    } catch (e) {
      // ws 构造一般异步失败；在 open 事件处理
      throw e;
    }
    try {
      await this._awaitOpen();
    } catch (e) {
      // G43 加固（rant 2026-08-09T13:16:36 根因）：token 文件存在但连不上时，
      // 先探测固定端口（rant 2026-08-21T15:26:42：TCP 探活；rant 16:45:06 后
      // emrgd.pid 已彻底移除）——daemon 还活着就【绝不删 token 文件】。旧 G43
      // 直接 unlink 会把健康 daemon 的 token 文件删掉 → 僵尸态（daemon 活着、
      // scheduler 永远 cannot connect、PID 锁挡住新 spawn）。只有 daemon 真死
      // 了才删+重拉。
      if (await this._daemonProcessAlive()) {
        this.logger.warn(
          `[gui] ws connect failed: ${e.message} — daemon port alive, keeping token file (transient)`
        );
        try { this.ws.close(); } catch { /* ignore */ }
        throw new Error(`daemon unreachable (port alive): ${e.message}`);
      }
      if (skipStart) {
        this.logger.warn(
          `[gui] ws connect failed: ${e.message} — stale token, daemon dead (skipStart: not respawning)`
        );
        try { this.ws.close(); } catch { /* ignore */ }
        throw new Error(`daemon unreachable (skipStart): ${e.message}`);
      }
      this.logger.warn(`[gui] ws connect failed: ${e.message} — stale token, respawning daemon`);
      try { this.ws.close(); } catch { /* ignore */ }
      try { fs.unlinkSync(TOKEN_FILE()); } catch { /* ignore */ }
      const r = await this._spawnOrProbe();
      port = r.port;
      token = r.token;
      this.ws = new WebSocket(`ws://127.0.0.1:${EMRGD_PORT}`, { maxPayload: MAX_PAYLOAD });
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

  sendTask({ sessionId, cwd, prompt, images = null, requestId = null, sandbox = "workspace-write" }) {
    // G32：request_id 必须作为 id 字段发出（daemon 只回显不自生成）
    // G143：外部预生成 requestId 优先（renderer send 前标记自有流，消除 IPC 往返竞态窗口）
    // Rant 2026-08-20T18:18：sandbox 档位（read-only / workspace-write / danger-full-access），
    // 随每条 task 消息发送——daemon 用它控制该次工具执行的写权限（默认可写工作区）。
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
      sandbox,
    };
    this._setCurrentStream(rid);
    // G65：自有流锁——本连接发出流式 task 即标记，done/error/cancelled/断连释放
    // （多会话各自独立；main.js emrg:sendMessage 的 G65 切会话检查读本字段）
    this.ownStream = true;
    this.ownStreamRequestId = rid;
    this.ws.send(JSON.stringify(payload));
    return rid;
  }

  sendCommand(type, params = {}) {
    // Wire message type last: a payload field named "type" (e.g. the task type in
    // task CRUD) must never override the wire message type (rant 2026-08-14T21:48:00).
    this.ws.send(JSON.stringify({ ...params, type }));
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
  }

  _onDone(frame) {
    const rid = frame.request_id;
    if (rid && this._currentStream && this._currentStream.requestId === rid) {
      this.clearActiveStream();
    }
    // G65：仅自有流的 done 释放锁（广播 done 不影响）
    if (rid === this.ownStreamRequestId) {
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
    this.clearActiveStream(); // 清理当前流缓存
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

module.exports = { DaemonClient, generateSessionId, TOKEN_FILE, SESSION_ID_RE, MAX_PAYLOAD, EMRGD_PORT, EMRGD_START_ERR };
