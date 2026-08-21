"use strict";
/**
 * main.js — Electron main 进程。
 * 职责：窗口创建、daemon 生命周期（拉起/重连）、daemon_client 管理、IPC handler。
 * 安全：contextIsolation + nodeIntegration:false + sandbox:true（renderer 零网络权限）。
 */

const { app, BrowserWindow, dialog, ipcMain, shell, WebContentsView } = require("electron");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { pathToFileURL } = require("url");
const { spawn } = require("child_process");
const { parse: parseToml, stringify: stringifyToml } = require("smol-toml");
const { generateSessionId, SESSION_ID_RE } = require("./daemon_client");
const { ConnManager } = require("./conn-manager");
const { guiStatePath, sanitizeOpenSessions, saveGuiState, DEFAULT_CAP } = require("./gui-state");
const APP_VERSION = require("./package.json").version;

// ── 单实例锁（G85/G120：第二个实例退出并 focus 已有窗口）──
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  main();
}

function main() {
  const logger = createLogger();
  let win = null;
  let connManager = null; // P2（rant 15:07:19）：连接管理器 = daemon 生命周期唯一 owner
  // Rant 2026-08-20T16:03:31：GUI"工作目录"概念已删除——无项目上下文时的兜底 cwd 固定为 home。
  const DEFAULT_CWD = os.homedir();
  let configExists = false;
  let currentSessionId = null;
  let reconnectTimer = null;
  // Rant 2026-08-09T13:16:36 ③/⑤：重连指数退避（1s→2s→4s→…封顶 60s）。
  // 之前固定 1s——daemon 缺失时每 5s 一轮 spawn，弹窗/日志风暴。成功连接后复位。
  let reconnectDelayMs = 1000;
  const MAX_RECONNECT_DELAY_MS = 60_000;
  // Rant 2026-08-09T13:16:36 ⑤：daemon_stopped 提示每个连接生命周期只发一次——
  // 否则退避封顶 60s 后每轮重试都命中节流、渲染层每分钟追加一条重复系统消息
  // （对称 TUI app.py _throttle_warned，PR #594）。成功连接后复位。
  let daemonStoppedNotified = false;
  let stopping = false;
  // Rant 2026-08-21T12:44:34：定时探活心跳——daemon 级 disconnected 事件此前无人
  // 监听（grep 证实），断连只能靠 ws close 被动通知；心跳每 15s ping 一次，
  // 无 pong 或连接已断 → 主动 scheduleReconnect（指数退避已有）。
  let heartbeatTimer = null;
  const HEARTBEAT_MS = 15_000;
  // P4（rant 15:07:19）：跨项目打开的会话状态——sid → {projectName, projectPath,
  // lastActive}。写盘防抖 1s（打开/关闭/切换时更新，镜像 rant 设计）。
  let openSessions = new Map();
  let guiStateTimer = null;
  const GUI_STATE_DEBOUNCE_MS = 1000;
  // P2.3（rant 12:20:35）：HTML 预览 WebContentsView——懒创建、单实例复用、右对齐 bounds 同步。
  // renderer 崩溃 reload 后由 renderer 侧拉取（emrg:getPreviewState）恢复（main 是真相源）。
  let previewView = null;       // WebContentsView 实例（首次打开 HTML tab 才创建）
  let previewAdded = false;     // 已 addChildView（防重复添加）
  let previewPath = null;       // 当前加载的预览文件绝对路径（null = 无）
  let previewVisible = false;   // 当前是否显示（激活 HTML tab）
  let previewLayout = { width: 280, collapsed: false, contentTop: 0 }; // renderer 上报的面板布局

  // ── 窗口 ────────────────────────────────────────────────

  // rant 2026-08-11T17:37:03：打包版曾用 Electron 默认图标（蓝色原子球）。
  // package.json build.mac/win/linux.icon 显式指向 packaging/assets 单文件修复主图标；
  // 这里为 Windows/Linux 窗口标题栏提供运行时图标（打包版经 extraResources 落到 resources/icon.png）。
  function windowIconPath() {
    const candidates = [
      path.join(__dirname, "..", "icon.png"), // packaged: resources/icon.png（extraResources）
      path.join(__dirname, "..", "packaging", "assets", "icon.png"), // source: 仓库 packaging/assets/icon.png
    ];
    return candidates.find((p) => fs.existsSync(p)) || undefined;
  }

  function createWindow() {
    win = new BrowserWindow({
      width: 1000,
      height: 700,
      icon: windowIconPath(),
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        preload: path.join(__dirname, "preload.js"),
        backgroundThrottling: false, // G92：后台窗口流式不延迟
      },
    });
    win.loadFile(path.join(__dirname, "renderer", "index.html"));
    // P2.3：窗口尺寸变化 → 预览 bounds 跟随（右对齐矩形）
    win.on("resize", () => updatePreviewBounds());
    // G87：窗口状态持久化
    restoreWindowBounds(win);
    win.on("close", () => saveWindowBounds(win));
    // G101：renderer 崩溃恢复
    win.webContents.on("render-process-gone", () => {
      logger.warn("[gui] renderer gone — reloading");
      // P2.3：预览 view 是独立 WebContents，崩溃不影响它；reload 完成后 renderer
      // 经 emrg:getPreviewState 拉取当前预览路径重新开 Tab（恢复 bounds/loadURL 一致，
      // 防预览与 Tab 栏不匹配，R5-④）
      win.loadFile(path.join(__dirname, "renderer", "index.html"));
    });
    win.webContents.on("unresponsive", () => {
      logger.warn("[gui] renderer unresponsive");
    });
  }

  function restoreWindowBounds(w) {
    try {
      const p = path.join(os.homedir(), ".emrg", "gui-window.json");
      if (fs.existsSync(p)) {
        const b = JSON.parse(fs.readFileSync(p, "utf8"));
        w.setBounds(b);
      }
    } catch (e) { logger.debug(`[gui] window restore failed: ${e.message}`); }
  }
  function saveWindowBounds(w) {
    try {
      const p = path.join(os.homedir(), ".emrg", "gui-window.json");
      fs.mkdirSync(path.dirname(p), { recursive: true });
      fs.writeFileSync(p, JSON.stringify(w.getBounds()), { mode: 0o600 });
    } catch (e) { logger.debug(`[gui] window save failed: ${e.message}`); }
  }

  // ── 日志（G84）──────────────────────────────────────────

  function createLogger() {
    const logDir = path.join(os.homedir(), ".emrg");
    const logPath = path.join(logDir, "emrg-gui.log");
    let fd = null;
    try {
      fs.mkdirSync(logDir, { recursive: true });
      fd = fs.openSync(logPath, "a");
    } catch { /* 日志不可用时降级 console */ }
    const write = (level, msg) => {
      const line = `[${new Date().toISOString()}] [${level}] ${msg}\n`;
      if (fd) { try { fs.writeSync(fd, line); } catch { /* ignore */ } }
      // eslint-disable-next-line no-console
      if (process.env.EMRG_GUI_DEBUG) console.log(line.trim());
    };
    return {
      info: (m) => write("INFO", m),
      warn: (m) => write("WARN", m),
      debug: (m) => write("DEBUG", m),
      error: (m, e) => write("ERROR", `${m} ${e?.stack || e || ""}`),
    };
  }

  // ── config 工具（G60/G69/G116/G117）────────────────────

  function configPath() { return path.join(os.homedir(), ".emrg", "config.toml"); }

  // Phase 4（rant #12 §4 R108）：打包模式无 .venv/PATH python → 内联模板直接生成
  // （与 emrg/config.py ensure_config() 105-130 行同源）；源码模式复用 ensure_config()。
  const CONFIG_TEMPLATE = `[llm]
# OpenAI-compatible API endpoint
base_url = "https://api.deepseek.com"
api_key = "sk-..."
model = "deepseek-chat"
max_tokens = 8192
temperature = 0.7
context_window = 131072
auto_compact_threshold = 0.0
# vision: set to true if model supports OpenAI vision API (image_url content type)
vision = false

# Additional models for /model switching (optional — add or remove as needed)
# model: API model name (optional — defaults to name if not set)
[[llm.models]]
name = "deepseek-v3"
model = "deepseek-chat"
context_window = 131072
vision = false

[[llm.models]]
name = "deepseek-r1"
model = "deepseek-reasoner"
context_window = 65536
vision = false
`;

  function ensureConfigTemplate() {
    // G116：优先复用 ensure_config() 生成官方模板（含 [[llm.models]] 预置）
    return new Promise((resolve) => {
      if (app.isPackaged) {
        if (!fs.existsSync(configPath())) {
          try {
            fs.mkdirSync(path.dirname(configPath()), { recursive: true });
            fs.writeFileSync(configPath(), CONFIG_TEMPLATE, { mode: 0o600 });
          } catch (e) {
            resolve("");
            return;
          }
        }
        resolve(fs.readFileSync(configPath(), "utf8"));
        return;
      }
      const python = connManager?.daemonConn()?._findPython() || "python3";
      const child = spawn(python, ["-c", "from emrg.config import ensure_config; ensure_config()"], {
        cwd: DEFAULT_CWD,
        stdio: "ignore",
        ...(process.platform === "win32" ? { windowsHide: true } : {}),
      });
      child.on("exit", () => {
        resolve(fs.existsSync(configPath()) ? fs.readFileSync(configPath(), "utf8") : "");
      });
      child.on("error", () => resolve(""));
    });
  }

  function readConfig() {
    try {
      return parseToml(fs.readFileSync(configPath(), "utf8"));
    } catch { return {}; }
  }

  function writeConfig(toml) {
    // G69：mode 0o600（含明文 api_key）
    fs.mkdirSync(path.dirname(configPath()), { recursive: true });
    fs.writeFileSync(configPath(), stringifyToml(toml), { mode: 0o600 });
  }

  function isKeyConfigured(key) {
    // G117：占位符 sk-... 视为未配置
    return !!key && key !== "sk-...";
  }

  // ── IPC（G102/G114 输入校验）────────────────────────────

  function validateSessionId(id) {
    return typeof id === "string" && SESSION_ID_RE.test(id);
  }

  function validateText(t) {
    return typeof t === "string" && t.length > 0 && t.length <= 20000;
  }

  function validateConfig(c) {
    // 设计 §7.1：直接接收所需字段 + 基本类型检查（防写坏 config.toml 的健壮性，非安全设计）
    const out = {};
    for (const k of ["apiKey", "baseUrl", "model", "theme"]) {
      if (c[k] !== undefined) out[k] = typeof c[k] === "string" ? c[k] : String(c[k]);
    }
    if (Array.isArray(c.models)) {
      // models: [{name, model?, vision?}] → 写 [[llm.models]]（name 必填）
      out.models = c.models
        .filter((m) => m && typeof m === "object")
        .map((m) => {
          const name = String(m.name || "").trim();
          if (!name) return null;
          const item = { name };
          if (m.model && String(m.model).trim() && String(m.model).trim() !== name) item.model = String(m.model).trim();
          if (m.vision !== undefined) item.vision = Boolean(m.vision);
          return item;
        })
        .filter(Boolean);
    }
    return out;
  }

  function registerIpc() {
    ipcMain.handle("emrg:init", async () => {
      // G34/G71/G112：config 存在性 → ensureConnected → ping → list_sessions
      configExists = fs.existsSync(configPath());
      const cfg = readConfig();

      if (!configExists) {
        // config 缺失 → 不拉起 daemon（daemon 启动即崩），直接返回缺配置
        return { config_exists: false, api_key_configured: false, server_id: "", model: "", version: APP_VERSION };
      }
      const keyConfigured = isKeyConfigured(cfg.llm?.api_key);
      if (!keyConfigured) {
        return { config_exists: true, api_key_configured: false, server_id: "", model: "", version: APP_VERSION };
      }

      await ensureConnected();
      const pong = await waitForPong();
      const sessions = await listSessions();
      // P4 slice 2：启动恢复打开会话（gui_state.json → 重开连接 + resume 重订阅）
      await restoreOpenSessions(sessions);
      return {
        config_exists: true,
        api_key_configured: true,
        server_id: pong?.identity?.instance_id || "",
        model: pong?.model || "",
        evolution_count: pong?.evolution_count ?? 0, // G19：init 透传演化计数（waitForPong 已消耗 pong）
        current_version: pong?.current_version || "", // rant 18:30:57：安装版本（GUI 对比显示升级横幅）
        version: APP_VERSION, // WorkBuddy P3：版本号随 package.json 走（此前 renderer 硬编码 v0.2.7）
        sessions,
        open_sessions: openSessionsList(),
        active_sid: currentSessionId, // P4 slice 2：恢复后的激活会话（renderer 直接采用）
      };
    });

    ipcMain.handle("emrg:sendMessage", async (_e, { sessionId, text, requestId, sandbox }) => {
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      if (!validateText(text)) throw new Error("invalid text");
      if (requestId !== undefined && (typeof requestId !== "string" || requestId.length < 8 || requestId.length > 64)) {
        throw new Error("invalid request_id"); // G143：renderer 预生成 id 的格式护栏
      }
      // P2：每会话独立连接——首条消息前自动打开（新会话不 resume，daemon 隐式订阅）
      // P5 slice 2：cwd 取该会话所属项目（跨项目会话用其项目路径，非全局 projectDir）
      const sessionCwd = openSessions.get(sessionId)?.projectPath || DEFAULT_CWD;
      let conn = connManager?.get(sessionId);
      if (!conn || !conn.connected) {
        conn = await openSession(sessionId, sessionCwd, { resume: false });
      }
      markSessionActive(sessionId); // P4：发送 = 会话活动 → lastActive + 持久化
      let rid;
      try {
        // G143：renderer 预生成 requestId（send 前标记自有流，消除 IPC 往返竞态窗口）
        // Rant 2026-08-20T18:18：sandbox 档位（read-only / workspace-write / danger-full-access）
        // G65：conn.sendTask 内部标记 ownStream（每连接独立锁）
        rid = conn.sendTask({ sessionId, cwd: sessionCwd, prompt: text, requestId, sandbox });
      } catch (e) {
        conn._releaseOwnStream(); // sendTask 抛异常（ws.send 失败）→ 释放锁，防 G65 锁泄漏
        throw e;
      }
      return { ok: true, requestId: rid }; // G124：回传 requestId → renderer 识别自有流
    });

    ipcMain.handle("emrg:listSessions", async () => listSessions());

    // Rant 2026-08-21T12:44:34：一键"重启生效"。旧实现只发 shutdown —— daemon 会被
    // TUI 客户端拉回（TUI 断线自动重连+自动 spawn，emrg/client/app.py:378-394），
    // 而 GUI 自己永不重连（实证 emrg-gui.log 11:41：杀 daemon 后 36 分钟无重连，
    // 状态栏绿点假象 + "daemon not connected"）。
    // 新实现：spawn `python -m emrg._stop_all --skip-gui` —— 复用全链路 stop
    // （顺序 GUI→TUI→daemon，客户端先死不会重拉 daemon；--skip-gui 跳过 stop_gui，
    // 否则 taskkill /IM EMRG.exe / ps-scan EMRG.app 会杀掉 GUI 主进程本身，
    // relaunch 永不执行）→ 等 exit 0 → GUI 自己 app.relaunch() + app.exit(0) →
    // 新 GUI 进程启动 → ensureDaemon 用新安装代码 spawn 新 daemon。TUI 不需要感知
    // 重启（直接被杀，不会进重连循环）。
    ipcMain.handle("emrg:restartDaemon", async () => {
      const python = connManager?.daemonConn()?._findPython() || "python3";
      const result = await new Promise((resolve) => {
        const child = spawn(python, ["-m", "emrg._stop_all", "--skip-gui"], {
          cwd: os.homedir(),
          stdio: ["ignore", "ignore", "pipe"],
        });
        let err = "";
        child.stderr?.on("data", (d) => { err += String(d); });
        child.on("error", (e) => resolve({ ok: false, error: `spawn failed: ${e.message}` }));
        child.on("close", (code) => resolve(
          code === 0
            ? { ok: true }
            : { ok: false, error: `stop_all exit ${code}: ${err.slice(-500)}` }
        ));
      });
      if (!result.ok) throw new Error(result.error);
      app.relaunch(); // 新 GUI 进程启动 → ensureDaemon 用新安装代码 spawn 新 daemon
      app.exit(0);
      return { ok: true };
    });

    ipcMain.handle("emrg:switchSession", async (_e, { sessionId, projectPath } = {}) => {
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      // P6（rant 15:07:19 边界）：projectPath 校验（跨项目打开时传项目路径）
      if (projectPath !== undefined && (typeof projectPath !== "string" || !projectPath.trim())) {
        throw new Error("invalid project path");
      }
      // P6（rant 15:07:19 上限 20）：显式打开新会话超限 → 提示不自动关（已打开 sid 复用不拦）
      if (openSessions.size >= DEFAULT_CAP && !openSessions.has(sessionId)) {
        throw new Error(`too many open sessions (${DEFAULT_CAP}) — close some first`);
      }
      // G65：自有流运行中禁止切会话（每连接独立锁，查当前激活连接）
      if (connManager?.get(currentSessionId)?.ownStream) throw new Error("stream in progress — cannot switch");
      const prevSid = currentSessionId;
      // G110：切会话清空旧连接分组缓存（含 timer），防广播"幽灵"残留
      connManager?.get(prevSid)?.clearGroups();
      // P5 slice 2：跨项目打开——用该项目路径 resume（非全局 projectDir）
      const targetPath = projectPath || openSessions.get(sessionId)?.projectPath || DEFAULT_CWD;
      try {
        await openSession(sessionId, targetPath); // 打开（新）会话连接 + resume_session 自动订阅
      } catch (e) {
        // G106：被动删除恢复——resume error → 刷新列表 + 切最近
        if (/not found|error/i.test(e.message)) {
          connManager?.close(sessionId); // 防御：失败连接已由 open 内部关闭
          return listSessions().then((sessions) => {
            const next = sessions[0]?.session_id || null;
            return { error: "session_not_found", sessions, next_session: next };
          });
        }
        throw e;
      }
      currentSessionId = sessionId;
      win.setTitle(`EMRG — ${sessionId}`); // G109
      // P4（rant 15:07:19）：多会话保持——切走**不再关闭**旧会话连接（浏览器 tab
      // 效果：切回继续生成/看现场）。关闭走 emrg:closeSession（P4 slice 2 侧边栏）。
      markSessionActive(sessionId); // 更新 lastActive + activeSid 防抖写盘
      return {};
    });

    ipcMain.handle("emrg:deleteSession", async (_e, { sessionId }) => {
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      await requireConn().sendCommandAndWait("delete_session", { session_id: sessionId, cwd: DEFAULT_CWD }, 5000);
      connManager?.close(sessionId); // P2：删除会话 → 关闭该会话连接（若打开）
      openSessions.delete(sessionId); // P4：删除（删数据）→ 一并移出打开会话簿记
      schedulePersistGuiState();
      broadcastOpenSessions();
      return { ok: true };
    });

    ipcMain.handle("emrg:renameSession", async (_e, { sessionId, title }) => {
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      const clean = String(title || "").trim().slice(0, 80); // 截断超长标题
      if (!clean) throw new Error("empty title");
      const frame = await requireConn().sendCommandAndWait("rename_session", { session_id: sessionId, cwd: DEFAULT_CWD, title: clean }, 5000);
      // 跨项目会话重命名成功后立即同步侧边栏标题（rant 12:01:44）
      const v = openSessions.get(sessionId);
      if (v) {
        v.title = frame.title || clean;
        broadcastOpenSessions();
      }
      return { ok: true, title: frame.title || clean };
    });

    // P4（rant 15:07:19）：关闭会话 = 断开连接 + 移除 + 持久化，**保留磁盘数据**
    // （与 delete_session 区分：关闭留数据 / 删除删数据）。
    ipcMain.handle("emrg:closeSession", async (_e, { sessionId }) => {
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      return closeSession(sessionId);
    });

    // P4：跨项目打开会话列表（侧边栏数据源，slice 2 消费）
    ipcMain.handle("emrg:getOpenSessions", async () => ({
      openSessions: openSessionsList(),
      activeSid: currentSessionId,
    }));

    ipcMain.handle("emrg:newSession", async (_e, { projectPath } = {}) => {
      // P6（rant 15:07:19 边界）：projectPath 校验（新建会话指定项目时）
      if (projectPath !== undefined && (typeof projectPath !== "string" || !projectPath.trim())) {
        throw new Error("invalid project path");
      }
      // G14/G81：本地生成 session_id（无 new_session 消息）
      const sid = generateSessionId();
      // 同步 main 侧会话状态：重连后 resume 正确会话（G41）+ 窗口标题（G109）
      currentSessionId = sid;
      win.setTitle(`EMRG — ${sid}`);
      // P5 slice 2：新会话指定项目 → 先记簿记（发送时用其 cwd 建连接）
      if (projectPath) touchOpenSession(sid, projectPath);
      // P4：新会话在首条消息前不建连接（sendMessage 自动 open）——此处仅标记激活
      // 并刷新持久化（activeSid 前进；openSessions 条目随 openSession 落簿记）
      schedulePersistGuiState();
      broadcastOpenSessions();
      return { session_id: sid };
    });

    ipcMain.handle("emrg:clearSession", async (_e, { sessionId }) => {
      // GUI / 指令 P1：/clear — 清空当前会话（daemon 协议 clear_session 已存在）
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      await requireConn().sendCommandAndWait("clear_session", { session_id: sessionId, cwd: DEFAULT_CWD }, 5000);
      return { ok: true };
    });

    ipcMain.handle("emrg:compactSession", async (_e, { sessionId }) => {
      // GUI / 指令 P1：/compact — 压缩当前会话历史（daemon 协议 compact 已存在）
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      await requireConn().sendCommandAndWait("compact", { session_id: sessionId, cwd: DEFAULT_CWD }, 5000);
      return { ok: true };
    });

    ipcMain.handle("emrg:listHistory", async (_e, { sessionId, limit, offset } = {}) => {
      // GUI / 指令 P2：/rewind + rant 14:15:12 历史按需加载（limit/offset 可选）
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      const payload = { session_id: sessionId, cwd: DEFAULT_CWD };
      if (limit != null) payload.limit = limit;
      if (offset != null) payload.offset = offset;
      const frame = await requireConn().sendCommandAndWait("list_history", payload, 5000);
      return { messages: frame.messages || [], hasMore: !!frame.has_more };
    });

    ipcMain.handle("emrg:rewindSession", async (_e, { sessionId, recordIndex }) => {
      // GUI / 指令 P2：/rewind — 回退到指定历史消息点（daemon 协议 rewind_session 已存在）
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      if (typeof recordIndex !== "number" || !Number.isInteger(recordIndex) || recordIndex < 0) {
        throw new Error("invalid record_index");
      }
      const frame = await requireConn().sendCommandAndWait(
        "rewind_session",
        { session_id: sessionId, cwd: DEFAULT_CWD, record_index: recordIndex },
        5000
      );
      return { ok: true, removedCount: frame.removed_count ?? 0 };
    });

    ipcMain.handle("emrg:listMemories", async (_e, { scope = "project", sessionId } = {}) => {
      // GUI / 指令 P3：/memory — 列出记忆（daemon list_memories → memories_list）
      const params = { scope, cwd: DEFAULT_CWD };
      if (scope === "session") {
        if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
        params.session_id = sessionId;
      }
      const frame = await requireConn().sendCommandAndWait("list_memories", params, 5000);
      return frame.memories || [];
    });

    ipcMain.handle("emrg:readMemory", async (_e, { memoryId, scope = "project", sessionId } = {}) => {
      // GUI / 指令 P3：/memory <id> — 读取单条记忆（daemon read_memory → memory_content）
      if (typeof memoryId !== "string" || !memoryId.trim()) throw new Error("invalid memory_id");
      const params = { scope, memory_id: memoryId.trim(), cwd: DEFAULT_CWD };
      if (scope === "session") {
        if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
        params.session_id = sessionId;
      }
      const frame = await requireConn().sendCommandAndWait("read_memory", params, 5000);
      return frame.memory || { id: memoryId, content: "" };
    });

    // 右栏工作区面板 P1（rant 2026-08-11T12:20:35）：list_files / read_file 透传
    // 走 requireConn() = 当前会话连接天然认证；daemon list_files → files_list
    ipcMain.handle("emrg:listFiles", async (_e, { path: p } = {}) => {
      if (typeof p !== "string" || !p.trim()) throw new Error("invalid path");
      const frame = await requireConn().sendCommandAndWait("list_files", { path: p.trim() }, 10000);
      if (frame.error) throw new Error(frame.error);
      return { entries: frame.entries || [], truncated: !!frame.truncated };
    });

    ipcMain.handle("emrg:readFile", async (_e, { path: p, startLine, lineLimit } = {}) => {
      if (typeof p !== "string" || !p.trim()) throw new Error("invalid path");
      const params = { path: p.trim() };
      if (startLine !== undefined) params.start_line = startLine;
      if (lineLimit !== undefined) params.line_limit = lineLimit;
      const frame = await requireConn().sendCommandAndWait("read_file", params, 10000);
      if (frame.error) throw new Error(frame.error);
      return { content: frame.content || "", binary: !!frame.binary, truncated: !!frame.truncated, totalLines: frame.total_lines };
    });

    // ── P2.3 + P3.4（rant 12:20:35）：HTML 预览 WebContentsView ──────────
    // 混合模型：非 HTML 走 renderer DOM 查看器；.html/.htm 走内嵌浏览器预览。
    // 懒创建（R7-⑤）+ 单实例复用（一次只显示一个预览，切换 = 重新加载 R7-⑥）。
    ipcMain.handle("emrg:previewHtml", async (_e, { path: p } = {}) => showPreview(p));

    ipcMain.handle("emrg:closePreview", async (_e, { path: p } = {}) => {
      // 关闭/切走 HTML tab → 隐藏预览（懒销毁：窗口关闭自动回收，R5-⑤）
      const target = typeof p === "string" && p.trim() ? path.resolve(p.trim()) : null;
      if (previewVisible && (!target || previewPath === target)) {
        previewVisible = false;
        previewPath = null;
        updatePreviewBounds();
      }
      return { ok: true };
    });

    // renderer 上报面板布局（宽度/折叠/内容区顶部 = Tab 栏高）→ main 调 setBounds（R2-⑧）
    ipcMain.handle("emrg:panelResized", async (_e, { width, collapsed, contentTop } = {}) => {
      previewLayout = {
        width: typeof width === "number" && width > 0 ? width : previewLayout.width,
        collapsed: collapsed === true,
        contentTop: typeof contentTop === "number" && contentTop >= 0 ? contentTop : previewLayout.contentTop,
      };
      updatePreviewBounds();
      return { ok: true };
    });

    // renderer 崩溃 reload 后拉取当前预览状态（main 是真相源，R5-④ 恢复通道）
    ipcMain.handle("emrg:getPreviewState", async () => ({
      path: previewVisible && previewPath ? previewPath : null,
    }));

    ipcMain.handle("emrg:listSkills", async () => {
      // GUI / 指令 P3：/skills — 读取技能列表（TUI 本地 load_skills 等价物，daemon 无协议）
      // 技能在 ~/.emrg/skills/*.md（user）与 <projectDir>/.emrg/skills/*.md（project）
      const skills = [];
      const dirs = [
        { dir: path.join(os.homedir(), ".emrg", "skills"), source: "user" },
        { dir: path.join(DEFAULT_CWD, ".emrg", "skills"), source: "project" },
      ];
      for (const { dir, source } of dirs) {
        let files = [];
        try { files = fs.readdirSync(dir).filter((f) => f.endsWith(".md")); } catch { continue; }
        for (const f of files) {
          try {
            const text = fs.readFileSync(path.join(dir, f), "utf8");
            const m = text.match(/^---\n([\s\S]*?)\n---/);
            const meta = m ? m[1] : "";
            const name = (meta.match(/^name:\s*(.+)$/m) || [])[1]?.trim() || f.replace(/\.md$/, "");
            const desc = (meta.match(/^description:\s*(.+)$/m) || [])[1]?.trim() || "";
            skills.push({ name, description: desc, source });
          } catch { /* 单个技能读取失败跳过 */ }
        }
      }
      return skills;
    });

    ipcMain.handle("emrg:listProjects", async () => {
      // GUI / 指令 P4：/rant 项目下拉 — daemon list_projects → projects_list
      const frame = await requireConn().sendCommandAndWait("list_projects", {}, 5000);
      return frame.projects || [];
    });

    // P5（rant 15:07:19）：某项目的会话列表（list_sessions(cwd=projectPath)）
    ipcMain.handle("emrg:listProjectSessions", async (_e, { projectPath }) => {
      if (typeof projectPath !== "string" || !projectPath) throw new Error("invalid project path");
      const frame = await requireConn().sendCommandAndWait("list_sessions", { cwd: projectPath }, 5000);
      return { sessions: frame.sessions || [] };
    });

    // P5：新建项目 = 轻量命令带 cwd → daemon 隐式 _touch_project 注册（零改动）
    ipcMain.handle("emrg:registerProject", async (_e, { path: p }) => {
      if (typeof p !== "string" || !p) throw new Error("invalid project path");
      try {
        fs.accessSync(p, fs.constants.W_OK); // 目录可写校验（G121）
      } catch {
        throw new Error("project directory not writable");
      }
      await requireConn().sendCommandAndWait("list_sessions", { cwd: p }, 5000); // 隐式注册
      return { ok: true, path: p };
    });

    // P5 slice 2：删除项目 = 只删 projects.yml 条目（保留磁盘数据）+ 关闭该项目已打开会话
    // 受保护项目不可删除（内置 project emrg / 内置 task emrg-task → 演化依赖，删了悬空）；
    // `.emrg` 是 _touch_project 历史记录**非内置 → 可删**（再次访问重新注册）。
    // 返回 { ok, removed, closed: [sids] }；renderer 收到后负责切换激活会话。
    ipcMain.handle("emrg:removeProject", async (_e, { name, path: p } = {}) => {
      if (typeof name !== "string" || !name.trim()) throw new Error("invalid project name");
      // 受保护项目：内置 project `emrg` + 内置 task `emrg-task`
      if (name === "emrg" || name === "emrg-task") {
        return { ok: false, protected: true, error: "protected system project" };
      }
      // 该项目已打开的会话 → 关闭连接 + 移除 + 写盘（激活中先切相邻由 renderer 处理）
      const closed = [];
      for (const [sid, v] of [...openSessions.entries()]) {
        if (v.projectPath === p || v.projectName === name) {
          connManager?.close(sid);
          openSessions.delete(sid);
          closed.push(sid);
        }
      }
      if (currentSessionId && closed.includes(currentSessionId)) {
        currentSessionId = null; // 激活会话被关 → 无激活（renderer 切相邻）
      }
      const frame = await requireConn().sendCommandAndWait("remove_project", { name: name.trim() }, 5000);
      schedulePersistGuiState();
      broadcastOpenSessions();
      return { ok: true, removed: Boolean(frame.removed), closed, name };
    });

    ipcMain.handle("emrg:listTasks", async () => {
      // GUI / 指令 P4：/trigger — daemon list_tasks → tasks_list
      const frame = await requireConn().sendCommandAndWait("list_tasks", {}, 5000);
      return frame.tasks || [];
    });

    ipcMain.handle("emrg:triggerTask", async (_e, { name }) => {
      // GUI / 指令 P4：/trigger <name> — daemon trigger_task → trigger_result
      if (typeof name !== "string" || !name.trim()) throw new Error("invalid task name");
      const frame = await requireConn().sendCommandAndWait("trigger_task", { name: name.trim() }, 5000);
      return frame;
    });

    // ── Task/template CRUD IPC (rant 2026-08-12T18:23:15 P3) ──────────
    ipcMain.handle("emrg:taskCreate", async (_e, payload) => {
      const { name, type, project, interval, enabled, repo, description } = payload || {};
      if (typeof name !== "string" || !name.trim()) throw new Error("invalid task name");
      if (typeof type !== "string" || !type.trim()) throw new Error("invalid task type");
      if (typeof project !== "string" || !project.trim()) throw new Error("invalid project");
      const frame = await requireConn().sendCommandAndWait("task_create", {
        name: name.trim(), task_type: type.trim(), project: project.trim(),
        interval, enabled, repo, description,
      }, 8000);
      if (!frame.ok && frame.error) throw new Error(frame.error);
      return frame;
    });

    ipcMain.handle("emrg:taskUpdate", async (_e, payload) => {
      const { name, type, ...fields } = payload || {};
      if (typeof name !== "string" || !name.trim()) throw new Error("invalid task name");
      if (type !== undefined && typeof type !== "string") throw new Error("invalid task type");
      if (type !== undefined && type.trim()) fields.task_type = type.trim();
      const frame = await requireConn().sendCommandAndWait("task_update", { name: name.trim(), ...fields }, 8000);
      if (!frame.ok && frame.error) throw new Error(frame.error);
      return frame;
    });

    ipcMain.handle("emrg:taskDelete", async (_e, { name }) => {
      if (typeof name !== "string" || !name.trim()) throw new Error("invalid task name");
      const frame = await requireConn().sendCommandAndWait("task_delete", { name: name.trim() }, 8000);
      if (!frame.ok && frame.error) throw new Error(frame.error);
      return frame;
    });

    ipcMain.handle("emrg:taskTemplateList", async () => {
      const frame = await requireConn().sendCommandAndWait("task_template_list", {}, 5000);
      return frame.templates || [];
    });

    ipcMain.handle("emrg:taskTemplateCreate", async (_e, { name, prompt }) => {
      if (typeof name !== "string" || !name.trim()) throw new Error("invalid template name");
      if (typeof prompt !== "string" || !prompt.trim()) throw new Error("invalid template prompt");
      const frame = await requireConn().sendCommandAndWait("task_template_create", { name: name.trim(), prompt }, 8000);
      if (!frame.ok && frame.error) throw new Error(frame.error);
      return frame;
    });

    ipcMain.handle("emrg:taskTemplateUpdate", async (_e, { name, prompt }) => {
      if (typeof name !== "string" || !name.trim()) throw new Error("invalid template name");
      if (typeof prompt !== "string" || !prompt.trim()) throw new Error("invalid template prompt");
      const frame = await requireConn().sendCommandAndWait("task_template_update", { name: name.trim(), prompt }, 8000);
      if (!frame.ok && frame.error) throw new Error(frame.error);
      return frame;
    });

    ipcMain.handle("emrg:taskTemplateDelete", async (_e, { name }) => {
      if (typeof name !== "string" || !name.trim()) throw new Error("invalid template name");
      const frame = await requireConn().sendCommandAndWait("task_template_delete", { name: name.trim() }, 8000);
      if (!frame.ok && frame.error) throw new Error(frame.error);
      return frame;
    });

    // rant 14:10:14 P4：rant 面板 — 读取 rants.jsonl（可选 status 筛选）
    ipcMain.handle("emrg:listRants", async (_e, { status = "" } = {}) => {
      if (typeof status !== "string") throw new Error("invalid status");
      const frame = await requireConn().sendCommandAndWait("list_rants", { status: status.trim() }, 5000);
      return frame.rants || [];
    });

    ipcMain.handle("emrg:sendRant", async (_e, { message, project = "" } = {}) => {
      // GUI / 指令 P4：/rant — 提交反馈到演化系统（daemon rant 协议，字段序与 rants.jsonl 一致）
      if (typeof message !== "string" || !message.trim()) throw new Error("invalid rant message");
      const frame = await requireConn().sendCommandAndWait("rant", {
        message: message.trim().slice(0, 10000),
        project: String(project || "").trim(),
        // timestamp deliberately NOT sent: daemon stamps rants with local time
        // (rant 2026-08-07T13:34Z — GUI previously sent new Date().toISOString(),
        // which is UTC and 8h behind on UTC+8 hosts)
      }, 5000);
      return { ok: true, count: frame.count ?? 0 };
    });

    ipcMain.handle("emrg:evolutionSummary", async (_e, { limit = 5 } = {}) => {
      // GUI / 指令 P3：自进化可见化 — daemon evolution_summary（count + 最近改进）
      const frame = await requireConn().sendCommandAndWait("evolution_summary", { limit }, 5000);
      return { count: frame.count ?? 0, recent: frame.recent || [] };
    });

    ipcMain.handle("emrg:githubStatus", async () => {
      // Windows GCM rant Stage 2：设置页 GitHub 连接状态（daemon github_status）
      const frame = await requireConn().sendCommandAndWait("github_status", {}, 10000);
      return { authenticated: Boolean(frame.authenticated), user: frame.user || null };
    });

    ipcMain.handle("emrg:githubConnect", async (_e, { token }) => {
      // Windows GCM rant Stage 2：PAT 授权 + setup-git（daemon github_connect）
      const frame = await requireConn().sendCommandAndWait("github_connect", { token: String(token || "").trim() }, 40000);
      return { ok: Boolean(frame.ok), user: frame.user || null, error: frame.error || null };
    });

    ipcMain.handle("emrg:githubDisconnect", async () => {
      // Windows GCM rant Stage 2：断开 GitHub（daemon github_disconnect）
      const frame = await requireConn().sendCommandAndWait("github_disconnect", {}, 40000);
      return { ok: Boolean(frame.ok), error: frame.error || null };
    });

    ipcMain.handle("emrg:githubConnectWeb", async () => {
      // Windows GCM rant Stage 2b：device flow 启动（daemon github_connect_web）
      const frame = await requireConn().sendCommandAndWait("github_connect_web", {}, 15000);
      return { ok: Boolean(frame.ok), code: frame.code || null, url: frame.url || null, error: frame.error || null };
    });

    ipcMain.handle("emrg:openExternal", async (_e, { url }) => {
      // Windows GCM rant Stage 2b：打开 device flow 授权页（默认浏览器）
      if (typeof url !== "string" || !/^https:\/\//.test(url)) return { ok: false };
      await shell.openExternal(url);
      return { ok: true };
    });

    ipcMain.handle("emrg:setModel", async (_e, { model }) => {
      await requireConn().sendCommandAndWait("set_model", { model }, 5000);
      return { ok: true };
    });

    ipcMain.handle("emrg:openFile", async (_e, { filePath }) => {
      // GUI / 指令 WorkBuddy P1：产物面板打开文件（系统默认程序）
      if (typeof filePath !== "string" || !filePath.trim()) throw new Error("invalid file path");
      const p = path.resolve(filePath.trim());
      if (!fs.existsSync(p)) return { ok: false, error: "file_not_found" };
      const err = await shell.openPath(p);
      return { ok: err === "", error: err || "" };
    });

    ipcMain.handle("emrg:listModels", async () => {
      const frame = await requireConn().sendCommandAndWait("list_models", {}, 5000);
      return frame.models || [];
    });

    ipcMain.handle("emrg:getSettings", async () => {
      const cfg = readConfig();
      const key = cfg.llm?.api_key || "";
      // G118：占位符返回空串；camelCase 形状
      // models：name 字符串数组（向后兼容旧 renderer 下拉）；modelDetails：完整对象数组（§7.1 多模型编辑用）
      const models = (cfg.llm?.models || []).map((m) => m.name || m.model);
      const modelDetails = (cfg.llm?.models || []).map((m) => ({
        name: m.name || m.model || "",
        ...(m.model && m.model !== (m.name || "") ? { model: m.model } : {}),
        ...(m.vision !== undefined ? { vision: m.vision } : {}),
      }));
      return {
        apiKey: isKeyConfigured(key) ? key : "",
        baseUrl: cfg.llm?.base_url || "",
        model: cfg.llm?.model || "",
        models,
        modelDetails,
        theme: cfg.gui?.theme || "system", // §7.1：外观主题持久化（浅色/深色/跟随系统）
      };
    });

    ipcMain.handle("emrg:saveSettings", async (_e, rawConfig) => {
      const cfg = validateConfig(rawConfig || {});
      const wasRunning = Boolean(connManager?.daemonConn()?.connected); // G123：daemon 已连 = 运行中
      let text;
      if (!fs.existsSync(configPath())) {
        text = await ensureConfigTemplate(); // G116
        if (!text) text = "";
      } else {
        text = fs.readFileSync(configPath(), "utf8");
      }
      const toml = text ? parseToml(text) : {};
      // G60：全量读-改-写（保留 [llm] 全部键 + 其它段）
      toml.llm = toml.llm || {};
      if (cfg.apiKey !== undefined) toml.llm.api_key = cfg.apiKey;
      if (cfg.baseUrl !== undefined) toml.llm.base_url = cfg.baseUrl;
      if (cfg.model !== undefined) toml.llm.model = cfg.model;
      if (cfg.theme !== undefined) {
        toml.gui = toml.gui || {};
        toml.gui.theme = cfg.theme; // §7.1：主题持久化（浅色/深色/跟随系统）
      }
      if (cfg.models !== undefined) {
        // §7.1：多模型保存——合并写 [[llm.models]]（保留已有项的 context_window 等高级字段）
        const existing = toml.llm.models || [];
        toml.llm.models = cfg.models.map((item) => {
          const prev = existing.find((p) => (p.name || p.model) === item.name);
          return { ...(prev || {}), ...item };
        });
      }
      writeConfig(toml);
      // 保存后 daemon mtime 检测自动重启（G11）
      if (wasRunning) {
        // G119：不主动重连（等 ws close 自然触发）
      } else {
        // G123：首启保存（config 从无到有）→ 主动拉起
        await ensureConnected();
      }
      return { ok: true, daemon_was_running: wasRunning };
    });

    ipcMain.handle("emrg:cancel", async () => {
      // G24：无参数
      // G141：断连边界——ws 可能已 null/closed（_onClose 后 connected=false），sendCommand 抛异常
      // 不能让它泄漏为 IPC reject → renderer unhandled rejection（对比 sendMessage 的 try-catch 防护）
      // P2：cancel 发到当前激活连接（自有流所在连接），并释放其 G65 锁
      const c = activeConn();
      if (c?.ws) {
        try { await c.sendCommand("cancel"); } catch { /* 断连时忽略 */ }
      }
      c?._releaseOwnStream();
      return { ok: true };
    });

    ipcMain.handle("emrg:pickProjectDir", async () => {
      // G70/G115：只选不写盘（保存才落盘）
      const res = await dialog.showOpenDialog(win, { properties: ["openDirectory"] });
      return res.canceled ? null : res.filePaths[0];
    });
  }

  // ── daemon 生命周期（P2：connManager 为 daemon 唯一 owner）──────────

  // 惰性初始化 connManager（挂事件桥 + 恢复钩子）。
  function ensureConnManager() {
    if (connManager) return connManager;
    connManager = new ConnManager({ logger, isPackaged: app.isPackaged });
    // 每个新会话连接建立时挂 renderer 事件桥（附带 sid；含 recoverAll 重开路径）
    connManager.onOpen((sid, conn, projectPath) => {
      conn.onEvent((type, data) => {
        // P4：消息活动刷新该会话 lastActive（激活/发送/done 更新，写盘防抖 1s）
        if (type === "done" || type === "message_delta") touchOpenSession(sid, projectPath);
        if (win && !win.isDestroyed()) {
          // 主动关闭（切走/删除）不触发断线横幅——真断连/daemon 重启照常转发
          if (type === "disconnected" && conn._intentionalClose) return;
          win.webContents.send("emrg:event", { type, data, sid });
        }
      });
    });
    // daemon 重启恢复完成后刷新 UI 状态（对齐旧 G41 重连成功块）
    connManager.onRecovered(async () => {
      try {
        const sessions = await listSessions();
        sendToRenderer("sessions", { sessions });
        const pong = await waitForPong();
        sendToRenderer("status", { connected: true, server_id: pong?.identity?.instance_id, model: pong?.model, current_version: pong?.current_version || "" });
        logger.info("[gui] connManager recovery complete");
      } catch (e) {
        logger.warn(`[gui] post-recovery refresh failed: ${e.message}`);
      }
    });
    return connManager;
  }

  // 当前激活连接：有会话连接用会话连接（同一 daemon，命令通用）；否则 daemon 级连接。
  function activeConn() {
    if (!connManager) return null;
    if (currentSessionId) {
      const c = connManager.get(currentSessionId);
      if (c) return c;
    }
    return connManager.daemonConn();
  }

  // 同步取可用连接（未连 → 抛错，与旧 `if (!client?.connected) throw` 语义一致）。
  function requireConn() {
    const c = activeConn();
    if (!c || !c.connected) throw new Error("daemon not connected");
    return c;
  }

  // 打开（或复用）会话连接；事件桥由 onOpen 钩子统一挂（含 recoverAll 重开）。
  async function openSession(sid, projectPath, { resume = true } = {}) {
    const existing = connManager.get(sid);
    if (existing && existing.connected) {
      touchOpenSession(sid, projectPath); // P4：复用连接也刷新打开会话状态
      return existing;
    }
    const conn = await connManager.open(sid, projectPath, { resume });
    touchOpenSession(sid, projectPath);
    return conn;
  }

  // ── P4 openSessions 簿记 + gui_state.json 持久化 ──────────────────

  function projectNameOf(projectPath) {
    return path.basename(projectPath) || projectPath;
  }

  // 记录/刷新一个打开会话（复用连接、恢复重开、recoverAll 均刷新）
  function touchOpenSession(sid, projectPath) {
    openSessions.set(sid, {
      projectName: projectNameOf(projectPath),
      projectPath,
      lastActive: new Date().toISOString(),
    });
    broadcastOpenSessions();
    // 跨项目标题（rant 12:01:44）：异步拉该会话所在项目列表取 title——
    // 找到则 v.title = s.title 并广播（失败/无 title → 保持 undefined → 侧边栏 sid 兜底）
    listSessions(projectPath)
      .then((sessions) => {
        const v = openSessions.get(sid);
        if (!v) return; // 会话已关闭/移除
        const s = sessions.find((x) => x.session_id === sid);
        if (s && s.title) {
          v.title = s.title;
          broadcastOpenSessions();
        }
      })
      .catch(() => { /* 拉取失败保持 undefined → sid 兜底 */ });
  }

  // 激活会话变化（切换/新会话/发送）→ 更新 lastActive + activeSid → 防抖写盘
  function markSessionActive(sid) {
    if (sid && openSessions.has(sid)) {
      openSessions.get(sid).lastActive = new Date().toISOString();
    }
    schedulePersistGuiState();
    broadcastOpenSessions();
  }

  function schedulePersistGuiState() {
    if (guiStateTimer) clearTimeout(guiStateTimer);
    guiStateTimer = setTimeout(() => {
      guiStateTimer = null;
      persistGuiStateNow();
    }, GUI_STATE_DEBOUNCE_MS);
    guiStateTimer.unref?.();
  }

  function persistGuiStateNow() {
    try {
      const entries = [...openSessions.entries()].map(([sid, v]) => ({
        sid,
        projectName: v.projectName,
        projectPath: v.projectPath,
        lastActive: v.lastActive,
      }));
      const state = {
        openSessions: sanitizeOpenSessions(entries), // 写盘侧也守上限 20
        activeSid: currentSessionId,
      };
      saveGuiState(os.homedir(), state);
    } catch (e) {
      logger.warn(`[gui] gui_state.json persist failed: ${e.message}`);
    }
  }

  // 读盘 + 清洗（启动恢复用；损坏/缺失 → 空）
  function readGuiState() {
    try {
      const p = guiStatePath(os.homedir());
      if (!fs.existsSync(p)) return { openSessions: [], activeSid: null };
      const raw = JSON.parse(fs.readFileSync(p, "utf8"));
      return {
        openSessions: sanitizeOpenSessions(raw.openSessions || []),
        activeSid: raw.activeSid || null,
      };
    } catch (e) {
      logger.warn(`[gui] gui_state.json read failed: ${e.message}`);
      return { openSessions: [], activeSid: null };
    }
  }

  // P4 slice 2：启动恢复——gui_state.json 中的打开会话（上限 20）逐个重开连接 +
  // resume 重订阅；activeSid 失效 → 第一个有效；失效条目跳过 + 重写盘。
  async function restoreOpenSessions(sessions) {
    const { openSessions: saved, activeSid } = readGuiState();
    const validSids = new Set((sessions || []).map((s) => s.session_id));
    let restored = 0;
    for (const entry of saved) {
      if (restored >= 20) break; // 恢复上限（sanitize 已守，双保险）
      if (!validSids.has(entry.sid)) continue; // 失效条目跳过
      try {
        await openSession(entry.sid, entry.projectPath); // resume 重订阅
        restored += 1;
      } catch (e) {
        logger.warn(`[gui] restore session ${entry.sid} failed: ${e.message}`);
        openSessions.delete(entry.sid); // 失效 → 移除
      }
    }
    // activeSid：优先恢复原激活；失效 → 第一个有效
    if (openSessions.has(activeSid)) {
      currentSessionId = activeSid;
    } else if (openSessions.size > 0) {
      currentSessionId = openSessionsList()[0].sid;
    }
    if (currentSessionId) {
      win?.setTitle(`EMRG — ${currentSessionId}`);
    }
    schedulePersistGuiState(); // 失效条目剔除后重写盘
    broadcastOpenSessions();
    return restored;
  }

  // 通知 renderer 打开会话列表变化（侧边栏数据源，slice 2）
  function broadcastOpenSessions() {
    sendToRenderer("open_sessions", {
      openSessions: openSessionsList(),
      activeSid: currentSessionId,
    });
  }

  // 关闭会话（保留磁盘数据）：断连 + 移除 + 持久化。P4 slice 2 侧边栏"关闭"入口用。
  async function closeSession(sid) {
    const entry = openSessions.get(sid);
    connManager?.close(sid); // 主动关闭（_intentionalClose 抑制断线横幅）
    openSessions.delete(sid);
    schedulePersistGuiState();
    if (currentSessionId === sid) currentSessionId = null; // 关闭激活会话 → 无激活
    broadcastOpenSessions();
    return { ok: true, closed: !!entry };
  }

  function openSessionsList() {
    return [...openSessions.entries()]
      .map(([sid, v]) => ({ sid, projectName: v.projectName, projectPath: v.projectPath, lastActive: v.lastActive, title: v.title }))
      .sort((a, b) => String(b.lastActive || "").localeCompare(String(a.lastActive || "")));
  }

  function guiStateFilePath() {
    return guiStatePath(os.homedir());
  }

  async function ensureConnected() {
    ensureConnManager();
    try {
      await connManager.ensureDaemon();
      logger.info("[gui] connected to emrgd");
      cancelReconnect();
      reconnectDelayMs = 1000; // 退避复位
      daemonStoppedNotified = false; // 节流提示复位（下个生命周期可再提示）
      startHeartbeat(); // rant 2026-08-21T12:44:34：定时探活（断连主动重连）
      sendToRenderer("status", { connected: true });
    } catch (e) {
      const dm = connManager.daemonConn();
      if (dm?._authFailed) {
        // G88：认证失败 → 停止自动重试
        sendToRenderer("status", { connected: false, auth_failed: true, error: e.message });
        return;
      }
      // Rant 2026-08-09T13:16:36 ⑤：spawn 节流命中 → 告知宿主真实原因
      // （含 emrgd.log 尾部），不再无限拉起 daemon。只提示一次，防退避重试
      // 每分钟重复追加系统消息。
      if (String(e.message).includes("after 3 attempts") && !daemonStoppedNotified) {
        daemonStoppedNotified = true;
        sendToRenderer("status", { connected: false, daemon_stopped: true, error: e.message });
      }
      logger.warn(`[gui] ensureConnected failed: ${e.message}`);
      scheduleReconnect();
    }
  }

  // daemon 级重连退避（connManager 重启恢复覆盖会话连接；此处覆盖"无会话连接
  // 时 daemon 连接不可用"的初始/空闲场景）。
  function scheduleReconnect() {
    if (stopping || reconnectTimer) return;
    stopHeartbeat(); // 重连期间不再心跳（心跳失败路径会再调本函数，防叠）
    const delay = reconnectDelayMs;
    reconnectDelayMs = Math.min(reconnectDelayMs * 2, MAX_RECONNECT_DELAY_MS); // 指数退避
    reconnectTimer = setTimeout(async () => {
      reconnectTimer = null;
      sendToRenderer("status", { connected: false, reconnecting: true });
      await ensureConnected();
      if (connManager.daemonConn()?.connected) {
        // G41（P2 改写）：恢复当前会话连接（若 daemon 重启后未由 recoverAll 重开）
        if (currentSessionId && !connManager.get(currentSessionId)) {
          try { await openSession(currentSessionId, DEFAULT_CWD); } catch { /* 会话可能已删 */ }
        }
        const sessions = await listSessions();
        sendToRenderer("sessions", { sessions });
        const pong = await waitForPong();
        sendToRenderer("status", { connected: true, server_id: pong?.identity?.instance_id, model: pong?.model, current_version: pong?.current_version || "" });
      }
    }, delay);
  }

  function cancelReconnect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  }

  async function waitForPong(timeoutMs = 3000, connOverride = null) {
    const conn = connOverride || activeConn();
    if (!conn || !conn.connected) return null; // 未连接 → 直接超时语义（不抛）
    return new Promise((resolve) => {
      const off = conn.onEvent((type, data) => {
        if (type === "pong") {
          off();
          clearTimeout(timer);
          resolve(data);
        }
      });
      const timer = setTimeout(() => { off(); resolve(null); }, timeoutMs);
      conn.sendCommand("ping");
    });
  }

  // ── 心跳探活（rant 2026-08-21T12:44:34）─────────────────────
  function startHeartbeat() {
    if (heartbeatTimer || stopping) return;
    heartbeatTimer = setInterval(() => { _heartbeatTick(); }, HEARTBEAT_MS);
    logger.info(`[gui] heartbeat started (every ${HEARTBEAT_MS / 1000}s)`);
  }

  function stopHeartbeat() {
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
  }

  async function _heartbeatTick() {
    if (stopping) { stopHeartbeat(); return; }
    const conn = connManager?.daemonConn();
    if (!conn) return;
    if (!conn.connected) {
      // daemon 级断连无人监听（旧缺陷）→ 心跳主动补触发重连
      logger.warn("[gui] heartbeat: daemon connection dropped — scheduling reconnect");
      stopHeartbeat();
      scheduleReconnect();
      return;
    }
    const pong = await waitForPong(3000, conn);
    if (!pong) {
      logger.warn("[gui] heartbeat: no pong from daemon — scheduling reconnect");
      stopHeartbeat();
      try { conn.close(); } catch { /* ignore */ }
      scheduleReconnect();
    }
  }

  async function listSessions(cwd = DEFAULT_CWD) {
    try {
      const frame = await requireConn().sendCommandAndWait("list_sessions", { cwd }, 5000);
      return frame.sessions || [];
    } catch (e) {
      logger.warn(`[gui] list_sessions failed: ${e.message}`);
      return [];
    }
  }

  function sendToRenderer(type, data) {
    if (win && !win.isDestroyed()) {
      win.webContents.send("emrg:event", { type, data });
    }
  }

  // ── P2.3 + P3.4：HTML 预览 WebContentsView ────────────────────────

  /** 懒创建预览 view（R7-⑤）：sandbox + contextIsolation 对齐主窗口；安全清单（缺口 8）：
   *  setWindowOpenHandler 禁新窗 + will-frame-navigate 仅允许 file:// 主框架导航（防 HTML
   *  内跳转远程 URL）。程序化 loadURL 不走 will-frame-navigate——入口只有 showPreview，
   *  已校验扩展名 + 绝对路径 + 文件存在，无绕过路径。 */
  function ensurePreviewView() {
    if (previewView) return previewView;
    previewView = new WebContentsView({
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false },
    });
    previewView.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    previewView.webContents.on("will-frame-navigate", (event, url, isInPlace, isMainFrame) => {
      if (isMainFrame && !url.startsWith("file:")) event.preventDefault();
    });
    return previewView;
  }

  /** 右对齐矩形（R4-②）：x = winW - panelW、y = contentTop（Tab 栏高）、w = panelW、
   *  h = winH - contentTop。折叠 → 置不可见区域（0 尺寸 + 不可见）。纯函数，便于单测。 */
  function previewRect(winW, winH, layout) {
    if (layout.collapsed) return { x: winW, y: 0, width: 0, height: 0 };
    const width = Math.max(40, Math.min(layout.width || 280, winW));
    const contentTop = layout.contentTop || 0;
    return {
      x: Math.max(0, winW - width),
      y: contentTop,
      width,
      height: Math.max(0, winH - contentTop),
    };
  }

  function updatePreviewBounds() {
    if (!previewView || !win || win.isDestroyed()) return;
    if (!previewVisible) { previewView.setVisible(false); return; }
    const cb = win.getContentBounds();
    const r = previewRect(cb.width, cb.height, previewLayout);
    previewView.setBounds(r);
    previewView.setVisible(true);
  }

  /** 显示 HTML 预览：校验 → 懒创建 → loadURL（切换 = 重新加载 R7-⑥）→ bounds 同步 */
  async function showPreview(p) {
    if (typeof p !== "string" || !p.trim()) return { ok: false, error: "invalid path" };
    const abs = path.resolve(p.trim());
    if (!/\.html?$/i.test(abs)) return { ok: false, error: "not_html" };
    if (!fs.existsSync(abs)) return { ok: false, error: "file_not_found" };
    try {
      const view = ensurePreviewView();
      if (!previewAdded && win && !win.isDestroyed()) {
        win.contentView.addChildView(view);
        previewAdded = true;
      }
      if (previewPath !== abs) {
        await view.webContents.loadURL(pathToFileURL(abs).href);
        previewPath = abs;
      }
      previewVisible = true;
      updatePreviewBounds();
      return { ok: true };
    } catch (e) {
      logger.warn(`[gui] preview load failed: ${e.message}`);
      return { ok: false, error: String(e.message || "load_failed") };
    }
  }

  // ── 应用生命周期 ────────────────────────────────────────

  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  // ── Phase 4 AppImage 自解压（rant #12 §5 R65/R88/R93）──────────
  // AppImage 是自解压安装器：首次运行把 resourcesPath/runtime 复制到
  // ~/.emrg/install/（稳定路径，非 AppImage 挂载），并建 ~/.local/bin/emrg 软链。
  // 仅 Linux（process.env.APPIMAGE 存在）；macOS/Windows 由安装器直接放置。
  function ensureAppImageExtracted() {
    if (process.platform !== "linux" || !process.env.APPIMAGE) return;
    const home = os.homedir();
    const installBin = path.join(home, ".emrg", "install", "bin");
    if (fs.existsSync(installBin)) return; // 已解压
    const runtimeSrc = path.join(process.resourcesPath, "runtime");
    if (!fs.existsSync(runtimeSrc)) return;
    try {
      // R88：先 mkdir ~/.local/bin 否则 symlinkSync ENOENT
      fs.mkdirSync(path.join(home, ".local", "bin"), { recursive: true });
      // R93：250MB 复制期间显示"正在安装 EMRG..."提示防误判卡死
      if (win && !win.isDestroyed()) {
        win.webContents.send("emrg:event", { type: "status", data: { connected: false, installing: true } });
      }
      fs.cpSync(runtimeSrc, path.join(home, ".emrg", "install"), { recursive: true });
      const link = path.join(home, ".local", "bin", "emrg");
      if (!fs.existsSync(link)) {
        fs.symlinkSync(path.join(home, ".emrg", "install", "bin", "emrg"), link, "file");
      }
      logger.info("[gui] AppImage runtime extracted to ~/.emrg/install/");
    } catch (e) {
      logger.error("[gui] AppImage extraction failed (retry on next launch)", e);
    } finally {
      if (win && !win.isDestroyed()) {
        win.webContents.send("emrg:event", { type: "status", data: { connected: false, installing: false } });
      }
    }
  }

  app.whenReady().then(() => {
    registerIpc();
    createWindow();
    ensureAppImageExtracted(); // R65：自解压归 main.js（electron-builder AppRun 无官方自定义）
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on("window-all-closed", () => {
    stopping = true;
    cancelReconnect();
    stopHeartbeat(); // rant 2026-08-21T12:44:34：退出清理心跳定时器
    persistGuiStateNow(); // P4：退出前冲刷未落盘的打开会话状态（防抖 timer 取消）
    connManager?.closeAll(); // P2：关闭全部会话连接 + daemon 级连接
    // P2.3：窗口关闭自动销毁子 view（R5-⑤ 无需手动清理）；复位状态防二次使用
    previewView = null;
    previewAdded = false;
    previewPath = null;
    previewVisible = false;
    if (process.platform !== "darwin") app.quit();
  });

  process.on("uncaughtException", (e) => logger.error("[gui] uncaughtException", e));
}
