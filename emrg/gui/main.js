"use strict";
/**
 * main.js — Electron main 进程。
 * 职责：窗口创建、daemon 生命周期（拉起/重连）、daemon_client 管理、IPC handler。
 * 安全：contextIsolation + nodeIntegration:false + sandbox:true（renderer 零网络权限）。
 */

const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
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
  let ownStreamRequestId = null; // 自有流 request_id（广播 done 不清锁）
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
    // 设计 §7.1：直接接收所需字段 + 基本类型检查（防写坏 config.toml 的健壮性，非安全设计）
    const out = {};
    for (const k of ["apiKey", "baseUrl", "model", "projectDir", "theme"]) {
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
        evolution_count: pong?.evolution_count ?? 0, // G19：init 透传演化计数（waitForPong 已消耗 pong）
        sessions,
      };
    });

    ipcMain.handle("emrg:sendMessage", async (_e, { sessionId, text, requestId }) => {
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      if (!validateText(text)) throw new Error("invalid text");
      if (requestId !== undefined && (typeof requestId !== "string" || requestId.length < 8 || requestId.length > 64)) {
        throw new Error("invalid request_id"); // G143：renderer 预生成 id 的格式护栏
      }
      if (!client || !client.connected) throw new Error("daemon not connected");
      ownStream = true;
      let rid;
      try {
        // G143：renderer 预生成 requestId（send 前标记自有流，消除 IPC 往返竞态窗口）
        rid = client.sendTask({ sessionId, cwd: projectDir, prompt: text, stream: true, requestId });
      } catch (e) {
        ownStream = false; // sendTask 抛异常（ws.send 失败）→ 释放锁，防 G65 锁泄漏
        ownStreamRequestId = null;
        throw e;
      }
      ownStreamRequestId = rid; // 追踪自有流（G65 锁仅由自有 done 释放）
      return { ok: true, requestId: rid }; // G124：回传 requestId → renderer 识别自有流
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
      if (meta.error === "session_not_found") {
        // G106：resume 失败（会话已删）→ 不更新 currentSessionId（renderer 会切到 next_session，
        // 但 main 侧保持旧值，重连 resume 也不指向已删会话；renderer 随后 switchSession(next) 会纠正）
        return meta;
      }
      currentSessionId = sessionId;
      win.setTitle(`EMRG — ${sessionId}`); // G109
      return meta;
    });

    ipcMain.handle("emrg:deleteSession", async (_e, { sessionId }) => {
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      await client.sendCommandAndWait("delete_session", { session_id: sessionId, cwd: projectDir }, 5000);
      return { ok: true };
    });

    ipcMain.handle("emrg:renameSession", async (_e, { sessionId, title }) => {
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      const clean = String(title || "").trim().slice(0, 80); // 截断超长标题
      if (!clean) throw new Error("empty title");
      const frame = await client.sendCommandAndWait("rename_session", { session_id: sessionId, cwd: projectDir, title: clean }, 5000);
      return { ok: true, title: frame.title || clean };
    });

    ipcMain.handle("emrg:newSession", async () => {
      // G14/G81：本地生成 session_id（无 new_session 消息）
      const sid = generateSessionId();
      // 同步 main 侧会话状态：重连后 resume 正确会话（G41）+ 窗口标题（G109）
      currentSessionId = sid;
      win.setTitle(`EMRG — ${sid}`);
      return { session_id: sid };
    });

    ipcMain.handle("emrg:clearSession", async (_e, { sessionId }) => {
      // GUI / 指令 P1：/clear — 清空当前会话（daemon 协议 clear_session 已存在）
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      await client.sendCommandAndWait("clear_session", { session_id: sessionId, cwd: projectDir }, 5000);
      return { ok: true };
    });

    ipcMain.handle("emrg:compactSession", async (_e, { sessionId }) => {
      // GUI / 指令 P1：/compact — 压缩当前会话历史（daemon 协议 compact 已存在）
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      await client.sendCommandAndWait("compact", { session_id: sessionId, cwd: projectDir }, 5000);
      return { ok: true };
    });

    ipcMain.handle("emrg:listHistory", async (_e, { sessionId }) => {
      // GUI / 指令 P2：/rewind — 获取会话历史消息点（daemon 协议 list_history 已存在）
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      const frame = await client.sendCommandAndWait("list_history", { session_id: sessionId, cwd: projectDir }, 5000);
      return { messages: frame.messages || [] };
    });

    ipcMain.handle("emrg:rewindSession", async (_e, { sessionId, recordIndex }) => {
      // GUI / 指令 P2：/rewind — 回退到指定历史消息点（daemon 协议 rewind_session 已存在）
      if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
      if (typeof recordIndex !== "number" || !Number.isInteger(recordIndex) || recordIndex < 0) {
        throw new Error("invalid record_index");
      }
      const frame = await client.sendCommandAndWait(
        "rewind_session",
        { session_id: sessionId, cwd: projectDir, record_index: recordIndex },
        5000
      );
      return { ok: true, removedCount: frame.removed_count ?? 0 };
    });

    ipcMain.handle("emrg:listMemories", async (_e, { scope = "project", sessionId } = {}) => {
      // GUI / 指令 P3：/memory — 列出记忆（daemon list_memories → memories_list）
      const params = { scope, cwd: projectDir };
      if (scope === "session") {
        if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
        params.session_id = sessionId;
      }
      const frame = await client.sendCommandAndWait("list_memories", params, 5000);
      return frame.memories || [];
    });

    ipcMain.handle("emrg:readMemory", async (_e, { memoryId, scope = "project", sessionId } = {}) => {
      // GUI / 指令 P3：/memory <id> — 读取单条记忆（daemon read_memory → memory_content）
      if (typeof memoryId !== "string" || !memoryId.trim()) throw new Error("invalid memory_id");
      const params = { scope, memory_id: memoryId.trim(), cwd: projectDir };
      if (scope === "session") {
        if (!validateSessionId(sessionId)) throw new Error("invalid session_id");
        params.session_id = sessionId;
      }
      const frame = await client.sendCommandAndWait("read_memory", params, 5000);
      return frame.memory || { id: memoryId, content: "" };
    });

    ipcMain.handle("emrg:listSkills", async () => {
      // GUI / 指令 P3：/skills — 读取技能列表（TUI 本地 load_skills 等价物，daemon 无协议）
      // 技能在 ~/.emrg/skills/*.md（user）与 <projectDir>/.emrg/skills/*.md（project）
      const skills = [];
      const dirs = [
        { dir: path.join(os.homedir(), ".emrg", "skills"), source: "user" },
        { dir: path.join(projectDir, ".emrg", "skills"), source: "project" },
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
      const frame = await client.sendCommandAndWait("list_projects", {}, 5000);
      return frame.projects || [];
    });

    ipcMain.handle("emrg:listTasks", async () => {
      // GUI / 指令 P4：/trigger — daemon list_tasks → tasks_list
      const frame = await client.sendCommandAndWait("list_tasks", {}, 5000);
      return frame.tasks || [];
    });

    ipcMain.handle("emrg:triggerTask", async (_e, { name }) => {
      // GUI / 指令 P4：/trigger <name> — daemon trigger_task → trigger_result
      if (typeof name !== "string" || !name.trim()) throw new Error("invalid task name");
      const frame = await client.sendCommandAndWait("trigger_task", { name: name.trim() }, 5000);
      return frame;
    });

    ipcMain.handle("emrg:sendRant", async (_e, { message, project = "" } = {}) => {
      // GUI / 指令 P4：/rant — 提交反馈到演化系统（daemon rant 协议，字段序与 rants.jsonl 一致）
      if (typeof message !== "string" || !message.trim()) throw new Error("invalid rant message");
      const frame = await client.sendCommandAndWait("rant", {
        message: message.trim().slice(0, 10000),
        project: String(project || "").trim(),
        timestamp: new Date().toISOString(),
      }, 5000);
      return { ok: true, count: frame.count ?? 0 };
    });

    ipcMain.handle("emrg:setModel", async (_e, { model }) => {
      await client.sendCommandAndWait("set_model", { model }, 5000);
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
      const frame = await client.sendCommandAndWait("list_models", {}, 5000);
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
        projectDir: cfg.gui?.project_dir || os.homedir(),
        models,
        modelDetails,
        theme: cfg.gui?.theme || "system", // §7.1：外观主题持久化（浅色/深色/跟随系统）
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
      if (client?.ws) {
        try { await client.sendCommand("cancel"); } catch { /* 断连时忽略 */ }
      }
      ownStream = false;
      ownStreamRequestId = null;
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
      // Phase 4（rant #12 §4 R7）：打包模式传 app.isPackaged → daemon_client 走
      // 捆绑 emrgd 分支（_findDaemonExecutable）。
      client = new DaemonClient({ projectDir, logger, isPackaged: app.isPackaged });
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
        if (type === "done") {
          // 仅自有流的 done 释放 G65 锁（广播 done 不影响）；timeout 兜底同样只清自有
          if (data.request_id === ownStreamRequestId || (data.timeout && ownStream)) {
            ownStream = false;
            ownStreamRequestId = null;
          }
        }
        if (type === "error") {
          // session busy 是即发错误（daemon 返回后无 done 跟随）——释放 ownStream，防 G65 锁泄漏
          // （流式错误如 LLM error 则有 done 跟随，由 done 分支释放，不在此处理）
          if (data.error && String(data.error).includes("session busy")) {
            ownStream = false;
            ownStreamRequestId = null;
          }
        }
        if (win && !win.isDestroyed()) {
          win.webContents.send("emrg:event", { type, data });
        }
      });
      client.onEvent((type) => {
        if (type === "disconnected") {
          ownStream = false;
          ownStreamRequestId = null;
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
    if (client) client.close();
    if (process.platform !== "darwin") app.quit();
  });

  process.on("uncaughtException", (e) => logger.error("[gui] uncaughtException", e));
}
