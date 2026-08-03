"use strict";
/**
 * main.js — Electron main 进程。
 * 职责：窗口创建、daemon 生命周期（拉起/重连）、daemon_client 管理、IPC handler。
 * 安全：contextIsolation + nodeIntegration:false + sandbox:true（renderer 零网络权限）。
 */

const { app, BrowserWindow, dialog, ipcMain } = require("electron");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const { parse: parseToml, stringify: stringifyToml } = require("smol-toml");
const { DaemonClient, generateSessionId, SESSION_ID_RE, PORT_FILE } = require("./daemon_client");

// ── 单实例锁（G85/G120：第二个实例退出并 focus 已有窗口）──
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  main();
}

function main() {
  const logger = createLogger();
  let win = null;
  let client = null;
  let projectDir = os.homedir();
  let configExists = false;
  let currentSessionId = null;
  let ownStream = false; // 自有流运行中（G65：禁止切会话）
  let reconnectTimer = null;
  let stopping = false;

  // ── 窗口 ────────────────────────────────────────────────

  function createWindow() {
    win = new BrowserWindow({
      width: 1000,
      height: 700,
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        preload: path.join(__dirname, "preload.js"),
        backgroundThrottling: false, // G92：后台窗口流式不延迟
      },
    });
    win.loadFile(path.join(__dirname, "renderer", "index.html"));
    // G87：窗口状态持久化
    restoreWindowBounds(win);
    win.on("close", () => saveWindowBounds(win));
    // G101：renderer 崩溃恢复
    win.webContents.on("render-process-gone", () => {
      logger.warn("[gui] renderer gone — reloading");
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

  function ensureConfigTemplate() {
    // G116：优先复用 ensure_config() 生成官方模板（含 [[llm.models]] 预置）
    return new Promise((resolve) => {
      const python = client?._findPython() || "python3";
      const child = spawn(python, ["-c", "from emrg.config import ensure_config; ensure_config()"], {
        cwd: projectDir,
        stdio: "ignore",
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
    const allowed = ["apiKey", "baseUrl", "model", "projectDir"];
    const out = {};
    for (const k of allowed) {
      if (k in c) out[k] = typeof c[k] === "string" ? c[k] : String(c[k]);
    }
    return out;
  }

  function registerIpc() {
    ipcMain.handle("emrg:init", async () => {
      // G34/G71/G112：config 存在性 → ensureConnected → ping → list_sessions
      configExists = fs.existsSync(configPath());
      const cfg = readConfig();
      projectDir = cfg.gui?.project_dir || os.homedir();
      // G121：校验 project_dir 存在可写
      let projectDirValid = true;
      try {
        fs.accessSync(projectDir, fs.constants.W_OK);
      } catch { projectDirValid = false; }

      if (!configExists) {
        // config 缺失 → 不拉起 daemon（daemon 启动即崩），直接返回缺配置
        return { config_exists: false, api_key_configured: false, project_dir: projectDir, project_dir_valid: projectDirValid, server_id: "", model: "" };
      }
      const keyConfigured = isKeyConfigured(cfg.llm?.api_key);
      if (!keyConfigured) {
        return { config_exists: true, api_key_configured: false, project_dir: projectDir, project_dir_valid: projectDirValid, server_id: "", model: "" };
      }

      await ensureConnected();
      const pong = await waitForPong();
      const sessions = await listSessions();
      return {
        config_exists: true,
        api_key_configured: true,
        project_dir: projectDir,
        project_dir_valid: projectDirValid,
        server_id: pong?.identity?.instance_id || "",
        model: pong?.model || "",
        sessions,
      };
    });

    ipcMain.handle("emrg:sendMessage", async (_e, { sessionId, text }) => {
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      if (!validateText(text)) throw new Error("invalid text");
      if (!client || !client.connected) throw new Error("daemon not connected");
      ownStream = true;
      const requestId = client.sendTask({ sessionId, cwd: projectDir, prompt: text, stream: true });
      return { ok: true, requestId }; // G124：回传 requestId → renderer 识别自有流
    });

    ipcMain.handle("emrg:listSessions", async () => listSessions());

    ipcMain.handle("emrg:switchSession", async (_e, { sessionId }) => {
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      if (ownStream) throw new Error("stream in progress — cannot switch"); // G65
      if (!client?.connected) throw new Error("daemon not connected");
      client.clearGroups(); // G110：切会话清空旧分组缓存（含 timer），防广播"幽灵"残留
      const meta = await client.sendCommandAndWait("resume_session", { session_id: sessionId, cwd: projectDir }, 5000)
        .catch((e) => {
          // G106：被动删除恢复——resume error → 刷新列表 + 切最近
          if (/not found|error/i.test(e.message)) {
            return listSessions().then((sessions) => {
              const next = sessions[0]?.session_id || null;
              return { error: "session_not_found", sessions, next_session: next };
            });
          }
          throw e;
        });
      currentSessionId = sessionId;
      win.setTitle(`EMRG — ${sessionId}`); // G109
      return meta;
    });

    ipcMain.handle("emrg:deleteSession", async (_e, { sessionId }) => {
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      await client.sendCommandAndWait("delete_session", { session_id: sessionId, cwd: projectDir }, 5000);
      return { ok: true };
    });

    ipcMain.handle("emrg:newSession", async () => {
      // G14/G81：本地生成 session_id（无 new_session 消息）
      const sid = generateSessionId();
      return { session_id: sid };
    });

    ipcMain.handle("emrg:setModel", async (_e, { model }) => {
      await client.sendCommandAndWait("set_model", { model }, 5000);
      return { ok: true };
    });

    ipcMain.handle("emrg:listModels", async () => {
      const frame = await client.sendCommandAndWait("list_models", {}, 5000);
      return frame.models || [];
    });

    ipcMain.handle("emrg:getSettings", async () => {
      const cfg = readConfig();
      const key = cfg.llm?.api_key || "";
      // G118：占位符返回空串；camelCase 形状
      const models = (cfg.llm?.models || []).map((m) => m.name || m.model);
      return {
        apiKey: isKeyConfigured(key) ? key : "",
        baseUrl: cfg.llm?.base_url || "",
        model: cfg.llm?.model || "",
        projectDir: cfg.gui?.project_dir || os.homedir(),
        models,
      };
    });

    ipcMain.handle("emrg:saveSettings", async (_e, rawConfig) => {
      const cfg = validateConfig(rawConfig || {});
      const wasRunning = await client?.isRunning() || false; // G123
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
      if (cfg.projectDir !== undefined) {
        toml.gui = toml.gui || {};
        toml.gui.project_dir = cfg.projectDir; // G115：snake_case 落盘
        projectDir = cfg.projectDir;
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
      await client?.sendCommand("cancel");
      ownStream = false;
      return { ok: true };
    });

    ipcMain.handle("emrg:pickProjectDir", async () => {
      // G70/G115：只选不写盘（保存才落盘）
      const res = await dialog.showOpenDialog(win, { properties: ["openDirectory"] });
      return res.canceled ? null : res.filePaths[0];
    });
  }

  // ── daemon 生命周期 ─────────────────────────────────────

  async function ensureConnected() {
    if (!client) {
      client = new DaemonClient({ projectDir, logger });
      // G122：message_delta 16ms 批量推送
      let deltaBuf = [];
      let deltaTimer = null;
      client.onEvent((type, data) => {
        if (type === "message_delta") {
          deltaBuf.push(data);
          if (!deltaTimer) {
            deltaTimer = setTimeout(() => {
              const chunks = deltaBuf;
              deltaBuf = [];
              deltaTimer = null;
              if (win && !win.isDestroyed()) {
                win.webContents.send("emrg:event", { type: "message_delta", data: { chunks } });
              }
            }, 16);
          }
          return;
        }
        if (type === "done" && data.timeout) ownStream = false;
        if (type === "done") ownStream = false;
        if (type === "error") { /* error 帧：有流式错误等 done */ }
        if (win && !win.isDestroyed()) {
          win.webContents.send("emrg:event", { type, data });
        }
      });
      client.onEvent((type) => {
        if (type === "disconnected") {
          ownStream = false;
          scheduleReconnect();
        }
      });
    }
    try {
      await client.ensureConnected();
      logger.info("[gui] connected to emrgd");
      cancelReconnect();
      sendToRenderer("status", { connected: true });
    } catch (e) {
      if (client._authFailed) {
        // G88：认证失败 → 停止自动重试
        sendToRenderer("status", { connected: false, auth_failed: true, error: e.message });
        return;
      }
      logger.warn(`[gui] ensureConnected failed: ${e.message}`);
      scheduleReconnect();
    }
  }

  function scheduleReconnect() {
    if (stopping || reconnectTimer) return;
    reconnectTimer = setTimeout(async () => {
      reconnectTimer = null;
      sendToRenderer("status", { connected: false, reconnecting: true });
      await ensureConnected();
      if (client?.connected) {
        // G41：重连成功 → list_sessions + 重新 resume 当前会话
        const sessions = await listSessions();
        sendToRenderer("sessions", { sessions });
        if (currentSessionId) {
          try {
            await client.sendCommandAndWait("resume_session", { session_id: currentSessionId, cwd: projectDir }, 5000);
          } catch { /* 会话可能已删 */ }
        }
        const pong = await waitForPong();
        sendToRenderer("status", { connected: true, server_id: pong?.identity?.instance_id, model: pong?.model });
      }
    }, 1000);
  }

  function cancelReconnect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  }

  async function waitForPong(timeoutMs = 3000) {
    return new Promise((resolve) => {
      const off = client.onEvent((type, data) => {
        if (type === "pong") {
          off();
          clearTimeout(timer);
          resolve(data);
        }
      });
      const timer = setTimeout(() => { off(); resolve(null); }, timeoutMs);
      client.sendCommand("ping");
    });
  }

  async function listSessions() {
    try {
      const frame = await client.sendCommandAndWait("list_sessions", { cwd: projectDir }, 5000);
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

  // ── 应用生命周期 ────────────────────────────────────────

  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(() => {
    registerIpc();
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on("window-all-closed", () => {
    stopping = true;
    cancelReconnect();
    if (client) client.close();
    if (process.platform !== "darwin") app.quit();
  });

  process.on("uncaughtException", (e) => logger.error("[gui] uncaughtException", e));
}
