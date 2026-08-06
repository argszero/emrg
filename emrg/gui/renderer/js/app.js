"use strict";
/**
 * app.js — 应用主控：启动、事件处理（main 已分类）、模型切换器、右键菜单、主题。
 * 事件契约与 main 不变：message_delta/done/tool_started/tool_finished/cancelled/
 * error/pong/status/sessions/disconnected/group_cleared/list_result/command_result。
 */

const App = (() => {
  // 模型切换菜单键盘导航 handler（打开时注册，关闭时移除）
  let _modelMenuKeyHandler = null;
  // 右键菜单键盘导航 handler（同上生命周期管理）
  let _ctxMenuKeyHandler = null;

  const state = {
    sessionId: null,
    sessions: [],
    busy: false,
    ownStreamRequestId: null,
    apiKeyConfigured: false,
    configExists: false,
    projectDir: "",
    model: "",
    serverId: "",
    version: "", // WorkBuddy P3：init 返回的 package.json 版本号（/version 不再硬编码）
    evolutionCount: null,
    // WorkBuddy P3：上次已知进化计数（用于检测"进化刚发生"→toast）
    lastKnownEvolutionCount: null,
    autoScroll: true,
    // GUI / 指令补全菜单（rant 19:44 P1）：items=[{cmd,hint,phase}] index=当前高亮
    cmdMenu: { items: [], index: -1 },
    // WorkBuddy P2（rant 21:35）：工作模式 ask（纯对话）/ auto（默认，自动执行工具）
    mode: "auto",
  };

  // ── 启动 ─────────────────────────────────
  async function boot() {
    try {
      const init = await window.emrg.init();
      state.configExists = init.config_exists;
      state.apiKeyConfigured = init.api_key_configured;
      state.projectDir = init.project_dir || "";
      state.serverId = init.server_id || "";
      state.model = init.model || "";
      state.version = init.version || "";
      state.evolutionCount = init.evolution_count ?? null;
      state.lastKnownEvolutionCount = state.evolutionCount;
      updateConnectionDot(init.config_exists && init.api_key_configured ? "green" : "gray");
      updateModelSwitcher();
      updateGrowthCard();
      if (window.EMRG_I18N) window.EMRG_I18N.apply(); // rant 21:19：应用当前语言静态文案

      if (!init.config_exists) {
        Dialogs.showWelcome(); // 首启引导
        return;
      }
      if (!init.api_key_configured) {
        Dialogs.showSettings(); // key 空/占位符
        return;
      }
      if (!init.project_dir_valid) {
        Chat.addSystemMessage(_t("app.workdirInvalid"));
        Dialogs.showSettings();
        return;
      }
      if (init.sessions && init.sessions.length > 0) {
        state.sessions = init.sessions;
        Sidebar.render(init.sessions);
        const current = init.sessions.find((s) => s.session_id === state.sessionId);
        if (current) {
          Sidebar.highlight(state.sessionId);
        } else {
          await switchSession(init.sessions[0].session_id, { silent: true });
        }
      } else {
        await newSession();
      }
      // 修复：boot 成功路径必须启用输入框（此前仅 done/cancelled/disconnected/error
      // 回调会调用 setComposerDisabled(false)，形成"需先发消息才能启用输入框"死锁）
      setComposerDisabled(false);
    } catch (e) {
      Chat.addSystemMessage(_t("app.bootFailed", { msg: e.message }));
    }
  }

  // ── 发送 ─────────────────────────────────
  async function sendMessage() {
    const input = $("input");
    const text = input.value.trim();
    if (!text || state.busy) return;
    // GUI / 指令（rant 19:44 P1）：/ 开头 → 路由到指令 handler，不走 sendMessage
    const parsed = Commands.parseInput(text);
    if (parsed.type !== "message") {
      input.value = "";
      input.style.height = "auto";
      hideCmdMenu();
      await handleCommand(parsed);
      return;
    }
    if (!state.sessionId) {
      Chat.addSystemMessage(_t("app.needSession"));
      return;
    }
    state.busy = true;
    setComposerDisabled(true);
    Chat.addUserMessage(text);
    input.value = "";
    input.style.height = "auto";
    // G143：send 前预生成 requestId 并标记自有流——消除 IPC 往返竞态窗口
    const requestId = genRequestId();
    state.ownStreamRequestId = requestId;
    try {
      const res = await window.emrg.sendMessage({ sessionId: state.sessionId, text, requestId, mode: state.mode });
      state.ownStreamRequestId = res.requestId || requestId; // G124：以 daemon 回显为准
    } catch (e) {
      state.busy = false;
      state.ownStreamRequestId = null;
      setComposerDisabled(false);
      // G49：失败恢复输入框，文案不责怪用户
      Chat.addSystemMessage(EMRG_Copy.COPY.sendFailed);
      input.value = text;
    }
  }

  // ── / 指令（rant 19:44 P1-P4）──────────────────
  /** 执行 / 指令。全部 4 阶段已实现（phase 4 = 演化类 /rant /trigger）。 */
  async function handleCommand(parsed) {
    const cmd = parsed.cmd;
    const meta = Commands.COMMANDS[cmd];
    if (!meta) {
      Chat.addSystemMessage(_t("app.cmdUnknown", { cmd }));
      return;
    }
    try {
      switch (cmd) {
        case "/clear":
          if (!state.sessionId) {
            Chat.addSystemMessage(_t("app.needSession"));
            return;
          }
          await window.emrg.clearSession({ sessionId: state.sessionId });
          Chat.clear();
          Chat.addSystemMessage(_t("app.cleared"));
          break;
        case "/compact":
          if (!state.sessionId) {
            Chat.addSystemMessage(_t("app.needSession"));
            return;
          }
          await window.emrg.compactSession({ sessionId: state.sessionId });
          Chat.addSystemMessage(_t("app.compacted"));
          break;
        case "/version":
          showVersionInfo();
          break;
        case "/help":
          showHelpDialog();
          break;
        case "/image":
          Chat.addSystemMessage(_t("app.imagePaste"));
          break;
        case "/sessions":
        case "/resume":
          // P2：/resume <id> 直接切换；无参数 → 会话列表对话框
          if (parsed.args.length > 0) {
            await switchSession(parsed.args[0]);
          } else {
            showSessionsDialog();
          }
          break;
        case "/rename":
          // P2：复用现有重命名对话框（右键菜单同款）
          if (!state.sessionId) {
            Chat.addSystemMessage(_t("app.needSession"));
            return;
          }
          const cur = state.sessions.find((s) => s.session_id === state.sessionId);
          Dialogs.showRename(state.sessionId, cur ? cur.title : "");
          break;
        case "/delete":
          // P2：复用现有删除确认（右键菜单同款）
          if (!state.sessionId) {
            Chat.addSystemMessage(_t("app.needSession"));
            return;
          }
          Dialogs.showConfirm(EMRG_Copy.COPY.deleteConfirmTitle, EMRG_Copy.COPY.deleteConfirmBody, {
            okText: _t("dlg.delete"),
            danger: true,
            onOk: () => deleteSession(state.sessionId),
          });
          break;
        case "/rewind":
          // P2：历史消息点选择对话框
          showRewindDialog();
          break;
        case "/model":
          // P3：触发模型切换器（已有 UI）
          document.querySelector(".model-switcher")?.click();
          break;
        case "/memory":
          // P3：记忆浏览器对话框（/memory [session|project|<id>]）
          showMemoryDialog(parsed.args[0] || "");
          break;
        case "/skills":
          // P3：技能列表对话框
          showSkillsDialog();
          break;
        case "/rant":
          // P4：/rant 直接跟内容 → 快速提交；无参数 → 进化对话框
          if (parsed.args.length > 0) {
            await submitRant(parsed.args.join(" "), "");
          } else {
            showRantDialog();
          }
          break;
        case "/trigger":
          // P4：/trigger <name> 直接触发；无参数 → 任务列表对话框
          if (parsed.args.length > 0) {
            await doTrigger(parsed.args[0]);
          } else {
            showTasksDialog();
          }
          break;
        default:
          Chat.addSystemMessage(_t("app.cmdUnknown", { cmd }));
      }
    } catch (e) {
      Chat.addSystemMessage(_t("app.cmdFailed", { cmd, msg: e.message }));
    }
  }

  // /sessions /resume：会话列表对话框（复用 help-list 样式）
  async function showSessionsDialog() {
    const list = $("sessions-list");
    const dialog = $("sessions-dialog");
    if (!list || !dialog) return;
    await refreshSessions(); // 确保 state.sessions 最新
    list.innerHTML = "";
    if (state.sessions.length === 0) {
      list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("app.helpNoSessions")}</span></div>`;
    }
    state.sessions.forEach((s) => {
      const row = el("button", { class: "help-row", type: "button", style: "width:100%;text-align:left;cursor:pointer;background:none;border:none;" });
      const name = el("span", { class: "help-cmd" }, s.title || _t("app.unnamed"));
      const hint = el("span", { class: "help-hint" }, s.session_id === state.sessionId ? _t("app.current") : "");
      row.appendChild(name);
      row.appendChild(hint);
      row.addEventListener("click", async () => {
        dialog.close();
        await switchSession(s.session_id);
      });
      list.appendChild(row);
    });
    dialog.showModal();
  }

  // /rewind：历史消息点选择对话框（daemon list_history → 选择 → rewind_session）
  async function showRewindDialog() {
    const list = $("rewind-list");
    const dialog = $("rewind-dialog");
    if (!list || !dialog) return;
    if (!state.sessionId) {
      Chat.addSystemMessage(_t("app.needSession"));
      return;
    }
    list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("dlg.loading")}</span></div>`;
    dialog.showModal();
    try {
      const { messages } = await window.emrg.listHistory({ sessionId: state.sessionId });
      list.innerHTML = "";
      if (!messages || messages.length === 0) {
        list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("app.noHistory")}</span></div>`;
        return;
      }
      // 倒序：最新消息点在最上
      [...messages].reverse().forEach((m) => {
        const idx = m.record_index;
        const row = el("button", {
          class: "help-row",
          type: "button",
          style: "width:100%;text-align:left;cursor:pointer;background:none;border:none;",
        });
        const name = el("span", { class: "help-cmd" }, `#${idx}`);
        const hint = el("span", { class: "help-hint" }, (m.preview || m.content || "").slice(0, 60));
        row.appendChild(name);
        row.appendChild(hint);
        row.addEventListener("click", async () => {
          dialog.close();
          await doRewind(idx);
        });
        list.appendChild(row);
      });
    } catch (e) {
      list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("app.historyFailed", { msg: escapeHtml(e.message) })}</span></div>`;
    }
  }

  async function doRewind(recordIndex) {
    try {
      const res = await window.emrg.rewindSession({ sessionId: state.sessionId, recordIndex });
      Chat.clear();
      Chat.addSystemMessage(_t("app.rewound", { index: recordIndex, n: res.removedCount ?? 0 }));
    } catch (e) {
      Chat.addSystemMessage(_t("app.rewindFailed", { msg: e.message }));
    }
  }

  // /memory：记忆浏览器对话框（daemon list_memories → 列表；read_memory → 详情）
  async function showMemoryDialog(sub) {
    const list = $("memory-list");
    const detail = $("memory-detail");
    const dialog = $("memory-dialog");
    if (!list || !dialog) return;
    list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("dlg.loading")}</span></div>`;
    if (detail) detail.classList.add("hidden");
    dialog.showModal();
    const scope = String(sub || "").toLowerCase() === "session" ? "session" : "project";
    try {
      const memories = await window.emrg.listMemories({ scope, sessionId: state.sessionId });
      list.innerHTML = "";
      if (!memories || memories.length === 0) {
        list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("app.noMemories", { scope: scope === "session" ? _t("app.sessionMem") : _t("app.projectMem") })}</span></div>`;
        return;
      }
      for (const m of memories) {
        const row = el("button", {
          class: "help-row",
          type: "button",
          style: "width:100%;text-align:left;cursor:pointer;background:none;border:none;",
        });
        const title = m.title || m.id || _t("app.unnamed");
        const name = el("span", { class: "help-cmd" }, String(title).slice(0, 40));
        const hint = el("span", { class: "help-hint" }, (m.summary || m.content || "").slice(0, 50));
        row.appendChild(name);
        row.appendChild(hint);
        row.addEventListener("click", async () => {
          try {
            const mem = await window.emrg.readMemory({ memoryId: m.id, scope, sessionId: state.sessionId });
            const body = mem.content || mem.body || "";
            if (detail) {
              detail.innerHTML = `<div class="memory-detail-title">${escapeHtml(String(title).slice(0, 80))}</div><pre class="memory-detail-body">${escapeHtml(body.slice(0, 2000))}</pre>`;
              detail.classList.remove("hidden");
            } else {
              Chat.addSystemMessage(body.slice(0, 500));
            }
          } catch (err) {
            Chat.addSystemMessage(_t("app.readMemFailed", { msg: err.message }));
          }
        });
        list.appendChild(row);
      }
    } catch (e) {
      list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("app.memFailed", { msg: escapeHtml(e.message) })}</span></div>`;
    }
  }

  // /skills：技能列表对话框（main 进程读 ~/.emrg/skills + <project>/.emrg/skills）
  async function showSkillsDialog() {
    const list = $("skills-list");
    const dialog = $("skills-dialog");
    if (!list || !dialog) return;
    list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("dlg.loading")}</span></div>`;
    dialog.showModal();
    try {
      const skills = await window.emrg.listSkills();
      list.innerHTML = "";
      if (!skills || skills.length === 0) {
        list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("app.noSkills")}</span></div>`;
        return;
      }
      for (const s of skills) {
        const row = el("div", { class: "help-row" });
        const name = el("span", { class: "help-cmd" }, s.name || _t("app.unnamed"));
        const hint = el("span", { class: "help-hint" }, `${s.source || ""}${s.description ? " · " + s.description.slice(0, 50) : ""}`);
        row.appendChild(name);
        row.appendChild(hint);
        list.appendChild(row);
      }
    } catch (e) {
      list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("app.skillsFailed", { msg: escapeHtml(e.message) })}</span></div>`;
    }
  }

  // /rant：进化对话框（项目下拉 + 文本输入 → daemon rant 协议）
  async function showRantDialog() {
    const dialog = $("rant-dialog");
    const msgInput = $("rant-message");
    const projSel = $("rant-project");
    if (!dialog || !msgInput) return;
    // 加载项目列表填充下拉
    try {
      const projects = await window.emrg.listProjects();
      projSel.innerHTML = `<option value="">${_t("app.globalAll")}</option>`;
      for (const p of projects) {
        const name = typeof p === "string" ? p : (p.name || "");
        if (name) projSel.appendChild(el("option", { value: name }, name));
      }
    } catch { /* 项目加载失败则只留全局项 */ }
    msgInput.value = "";
    dialog.showModal();
    msgInput.focus();
  }

  async function submitRant(message, project) {
    const text = String(message || "").trim();
    if (!text) {
      Chat.addSystemMessage(_t("app.rantEmpty"));
      return;
    }
    try {
      const res = await window.emrg.sendRant({ message: text, project });
      Chat.addSystemMessage(`${_t("app.rantReceived")}${res.count ? _t("app.rantCount", { n: res.count }) : ""}`);
    } catch (e) {
      Chat.addSystemMessage(_t("app.rantFailed", { msg: e.message }));
    }
  }

  // /trigger：任务列表对话框（点击立即触发）
  async function showTasksDialog() {
    const list = $("tasks-list");
    const dialog = $("tasks-dialog");
    if (!list || !dialog) return;
    list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("dlg.loading")}</span></div>`;
    dialog.showModal();
    try {
      const tasks = await window.emrg.listTasks();
      list.innerHTML = "";
      if (!tasks || tasks.length === 0) {
        list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("app.noTasks")}</span></div>`;
        return;
      }
      for (const t of tasks) {
        const row = el("button", {
          class: "help-row",
          type: "button",
          style: "width:100%;text-align:left;cursor:pointer;background:none;border:none;",
        });
        const name = el("span", { class: "help-cmd" }, t.name || t.type || _t("app.unnamed"));
        const hint = el("span", { class: "help-hint" }, t.enabled === false ? _t("app.taskDisabled") : _t("app.taskInterval", { n: t.interval ?? "-" }));
        row.appendChild(name);
        row.appendChild(hint);
        row.addEventListener("click", async () => {
          dialog.close();
          await doTrigger(t.name);
        });
        list.appendChild(row);
      }
    } catch (e) {
      list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("app.tasksFailed", { msg: escapeHtml(e.message) })}</span></div>`;
    }
  }

  async function doTrigger(name) {
    const n = String(name || "").trim();
    if (!n) return;
    try {
      const res = await window.emrg.triggerTask({ name: n });
      if (res.error) {
        Chat.addSystemMessage(_t("app.triggerFailed", { msg: res.error }));
      } else {
        Chat.addSystemMessage(_t("app.triggered", { n }));
      }
    } catch (e) {
      Chat.addSystemMessage(_t("app.triggerFailed", { msg: e.message }));
    }
  }

  // / 补全菜单：输入以 / 开头 → 显示匹配指令；↑↓ 导航、Enter/点击选择填充
  function showCmdMenu(prefix) {
    const items = Commands.getCompletions(prefix);
    const menu = $("cmd-menu");
    if (!menu) return;
    if (items.length === 0) {
      hideCmdMenu();
      return;
    }
    state.cmdMenu = { items, index: 0 };
    menu.innerHTML = "";
    items.forEach((it, i) => {
      const row = el("button", { class: "cmd-menu-item", type: "button", dataset: { cmd: it.cmd } });
      row.innerHTML = `<span class="cmd-menu-name">${escapeHtml(it.cmd)}</span><span class="cmd-menu-hint">${escapeHtml(it.hint)}</span>`;
      row.addEventListener("mousedown", (e) => {
        e.preventDefault(); // 防输入框失焦
        selectCmd(it.cmd);
      });
      menu.appendChild(row);
    });
    menu.hidden = false;
    highlightCmdMenu();
  }

  function hideCmdMenu() {
    state.cmdMenu = { items: [], index: -1 };
    const menu = $("cmd-menu");
    if (menu) menu.hidden = true;
  }

  function highlightCmdMenu() {
    const menu = $("cmd-menu");
    if (!menu) return;
    [...menu.children].forEach((c, i) => c.classList.toggle("selected", i === state.cmdMenu.index));
  }

  /** 选择补全项：填充输入框 + 关闭菜单（用户可继续回车执行） */
  function selectCmd(cmd) {
    const input = $("input");
    input.value = cmd;
    input.focus();
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 150) + "px";
    hideCmdMenu();
  }

  /** /help 帮助对话框：列出全部指令 + 说明 */
  function showHelpDialog() {
    const list = $("help-list");
    if (!list) return;
    list.innerHTML = "";
    Object.entries(Commands.COMMANDS).forEach(([cmd, meta]) => {
      const row = el("div", { class: "help-row" });
      const name = el("span", { class: "help-cmd" }, cmd);
      const hint = el("span", { class: "help-hint" }, Commands.hintText(cmd)); // rant 21:19：hint 经 i18n 解析
      row.appendChild(name);
      row.appendChild(hint);
      list.appendChild(row);
    });
    $("help-dialog").showModal();
  }

  // ── 会话 ─────────────────────────────────
  async function switchSession(sid, opts = {}) {    // G65：busy 即自有流进行中/发送中（IPC 往返窗口内 ownStreamRequestId 尚未赋值）
    if (state.busy) {
      Chat.addSystemMessage(EMRG_Copy.COPY.sessionBusy);
      return;
    }
    try {
      const res = await window.emrg.switchSession({ sessionId: sid });
      state.sessionId = sid;
      state.ownStreamRequestId = null; // G110：切会话清自有流标记
      Chat.clear();
      updateEmptyState();
      if (res.error === "session_not_found") {
        Chat.addSystemMessage(_t("app.deletedSwitch"));
        if (res.next_session) {
          Sidebar.render(res.sessions || []);
          await switchSession(res.next_session, { silent: true });
        } else {
          await newSession();
        }
        return;
      }
      // G13：v1 不加载历史（G12）
      if (!opts.silent) {
        Chat.addSystemMessage(_t("app.switched"));
      }
      updateEmptyState();
      Sidebar.highlight(sid);
      setComposerDisabled(false); // 防御性：独立调用 switchSession 也确保输入框可用
    } catch (e) {
      Chat.addSystemMessage(_t("app.switchFailed", { msg: e.message }));
    }
  }

  async function newSession() {
    if (state.busy) {
      Chat.addSystemMessage(EMRG_Copy.COPY.sessionBusy);
      return;
    }
    try {
      const res = await window.emrg.newSession();
      state.sessionId = res.session_id;
      Chat.clear();
      updateEmptyState(); // 欢迎屏即反馈
      await refreshSessions();
      Sidebar.highlight(state.sessionId);
      setComposerDisabled(false); // 防御性：独立调用 newSession 也确保输入框可用
    } catch (e) {
      Chat.addSystemMessage(_t("app.newFailed", { msg: e.message }));
    }
  }

  async function deleteSession(sid) {
    if (state.busy && state.sessionId === sid) {
      Chat.addSystemMessage(EMRG_Copy.COPY.sessionBusy);
      return;
    }
    try {
      await window.emrg.deleteSession({ sessionId: sid });
      if (state.sessionId === sid) {
        const remaining = state.sessions.filter((s) => s.session_id !== sid);
        if (remaining.length > 0) {
          await switchSession(remaining[0].session_id, { silent: true });
        } else {
          await newSession();
        }
      }
      await refreshSessions();
    } catch (e) {
      Chat.addSystemMessage(_t("app.deleteFailed", { msg: e.message }));
    }
  }

  async function refreshSessions() {
    try {
      const sessions = await window.emrg.listSessions();
      state.sessions = sessions;
      Sidebar.render(sessions);
    } catch { /* 忽略 */ }
  }

  // ── 右键菜单（重命名 / 删除） ───────────
  function showConvMenu(item, sid, title) {
    // 设计 §3.2：右键菜单 = 重命名 / 删除（删除有友好确认）
    const menu = $("ctx-menu");
    menu.innerHTML = "";
    const mk = (label, danger, action) => {
      const b = el("button", { class: "ctx-item" + (danger ? " danger" : "") }, label);
      b.addEventListener("click", () => {
        hideCtxMenu();
        action();
      });
      menu.appendChild(b);
    };
    mk(_t("app.rename"), false, () => Dialogs.showRename(sid, title));
    mk(_t("app.deleteConv"), true, () => Dialogs.showConfirm(EMRG_Copy.COPY.deleteConfirmTitle, EMRG_Copy.COPY.deleteConfirmBody, {
      okText: _t("dlg.delete"),
      danger: true,
      onOk: () => deleteSession(sid),
    }));
    menu.hidden = false;
    // 定位在右键处，超出视口则上移/左移
    const rect = item.getBoundingClientRect();
    menu.style.left = Math.min(rect.right, window.innerWidth - 160) + "px";
    menu.style.top = Math.min(rect.bottom, window.innerHeight - 80) + "px";
    // 键盘导航：↑↓ 移动 / Enter 选择 / ESC 关闭（与模型切换器一致）
    const btns = menu.querySelectorAll(".ctx-item");
    let idx = btns.length ? 0 : -1;
    const setActive = (i) => {
      if (i < 0 || i >= btns.length) return;
      idx = i;
      btns.forEach((b, j) => b.classList.toggle("active", j === idx));
    };
    setActive(0);
    _ctxMenuKeyHandler = (ev) => {
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        setActive((idx + 1) % btns.length);
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        setActive((idx - 1 + btns.length) % btns.length);
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        if (idx >= 0) btns[idx].click();
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        hideCtxMenu();
      }
    };
    document.addEventListener("keydown", _ctxMenuKeyHandler);
  }

  function hideCtxMenu() {
    const menu = $("ctx-menu");
    menu.hidden = true;
    menu.innerHTML = "";
    if (_ctxMenuKeyHandler) {
      document.removeEventListener("keydown", _ctxMenuKeyHandler);
      _ctxMenuKeyHandler = null;
    }
  }

  // ── 模型切换器 ─────────────────────────
  async function updateModelSwitcher() {
    const label = $("model-switcher-label");
    if (label) label.textContent = state.model || (window.EMRG_I18N ? window.EMRG_I18N.t("composer.modelLoading") : "加载中…");
  }

  /** rant 21:19：locale 切换后重刷动态文案（i18n.apply 回调） */
  function refreshLocale() {
    updateModelSwitcher();
    // 空状态示例卡片：data-i18n 已由 apply 重刷；若补全菜单打开则重建（hint 本地化）
    const menu = $("cmd-menu");
    if (menu && !menu.hidden) {
      showCmdMenu(($("input").value || "").trim());
    }
  }

  // ── 自进化可见化（WorkBuddy P3）──────────────────
  /** 版本信息（/version 命令 + 进化 toast "去看看" 共用） */
  function showVersionInfo() {
    Chat.addSystemMessage(
      _t("app.versionInfo", { ver: state.version || "0.2.8", id: state.serverId || _t("app.unknown"), model: state.model || _t("app.unknown"), n: state.evolutionCount ?? 0 })
    );
  }

  /** 更新侧边栏成长状态卡（计数 + 提示语） */
  function updateGrowthCard() {
    const n = state.evolutionCount ?? 0;
    const countEl = $("growth-count");
    if (countEl) countEl.textContent = String(n);
    const noteEl = $("growth-note");
    if (noteEl) noteEl.textContent = EMRG_Copy.COPY.growthNote;
  }

  /** 进化完成 toast：检测 evolution_count 增长，一天最多提示一次 */
  function maybeShowEvolutionToast() {
    if (state.evolutionCount == null) return;
    const prev = state.lastKnownEvolutionCount;
    state.lastKnownEvolutionCount = state.evolutionCount;
    if (prev == null || state.evolutionCount <= prev) return; // 首次连接/无增长不提示
    // 频率控制：一天最多 1 次（localStorage 可能不可用 → 静默跳过）
    const today = new Date().toISOString().slice(0, 10);
    try {
      if (localStorage.getItem("emrg.evoToast.date") === today) return;
      localStorage.setItem("emrg.evoToast.date", today);
    } catch { /* ignore */ }
    showEvolutionToast();
  }

  function showEvolutionToast() {
    const toast = $("evolution-toast");
    if (!toast) return;
    const msg = $("evolution-toast-msg");
    if (msg) msg.textContent = EMRG_Copy.COPY.evolutionToastMsg(state.evolutionCount ?? 0);
    toast.classList.remove("hidden");
    const star = $("brand-star");
    if (star) star.classList.add("pulse");
  }

  function hideEvolutionToast() {
    const toast = $("evolution-toast");
    if (toast) toast.classList.add("hidden");
    const star = $("brand-star");
    if (star) star.classList.remove("pulse");
  }

  function initEvolutionToast() {
    const see = $("evolution-toast-see");
    if (see) {
      see.addEventListener("click", () => {
        hideEvolutionToast();
        showVersionInfo();
      });
    }
    const dismiss = $("evolution-toast-dismiss");
    if (dismiss) dismiss.addEventListener("click", hideEvolutionToast);
  }

  async function loadEvolutionSummary() {
    const recent = $("about-recent");
    if (!recent) return;
    try {
      const res = await window.emrg.evolutionSummary({ limit: 5 });
      if (res && res.count !== undefined) {
        state.evolutionCount = res.count;
        updateGrowthCard();
      }
      const items = (res && res.recent) || [];
      recent.innerHTML = "";
      if (items.length === 0) {
        recent.appendChild(el("div", { class: "about-recent-item" }, "还没有改进记录，输入 /rant 驱动第一次进化吧"));
        return;
      }
      const header = el("div", { class: "about-recent-title" }, "最近改进");
      recent.appendChild(header);
      for (const it of items) {
        const ts = String(it.timestamp || "").slice(0, 16).replace("T", " ");
        const ops = (it.operations || []).join(" · ");
        const row = el("div", { class: "about-recent-item" });
        row.appendChild(el("span", { class: "about-recent-time" }, ts));
        row.appendChild(el("span", {}, ops || "self-improvement"));
        recent.appendChild(row);
      }
    } catch { /* 摘要加载失败静默（进化卡仍显示 count） */ }
  }

  function initModelSwitcher() {
    const sw = $("model-switcher");
    sw.addEventListener("click", async (e) => {
      e.stopPropagation();
      closeModelMenu();
      try {
        const s = await window.emrg.getSettings();
        const models = s.models?.length ? s.models : s.model ? [s.model] : [];
        const menu = el("div", { class: "model-menu" });
        if (!models.length) {
          const empty = el("div", { class: "model-menu-empty" }, _t("app.noModels"));
          const go = el("a", {}, _t("app.goSettings"));
          go.addEventListener("click", () => {
            closeModelMenu();
            Dialogs.showSettings();
          });
          empty.appendChild(document.createTextNode("，"));
          empty.appendChild(go);
          menu.appendChild(empty);
        } else {
          for (const m of models) {
            const name = typeof m === "string" ? m : m.name;
            const vision = typeof m === "object" && m.vision;
            const item = el("div", { class: "model-menu-item" });
            item.appendChild(el("span", { class: "model-check" }, name === state.model ? "✓ " : ""));
            item.appendChild(el("span", {}, name));
            if (vision) item.appendChild(el("span", { class: "model-vision" }, " 🖼"));
            item.addEventListener("click", async () => {
              closeModelMenu();
              if (name === state.model) return;
              try {
                await window.emrg.setModel({ model: name });
                state.model = name;
                updateModelSwitcher();
                sw.classList.add("highlight");
                setTimeout(() => sw.classList.remove("highlight"), 1200);
              } catch (err) {
                Chat.addSystemMessage(_t("app.modelSwitchFailed", { msg: err.message }));
              }
            });
            menu.appendChild(item);
          }
        }
        $("main").appendChild(menu);
        document.addEventListener("click", closeModelMenu, { once: true });
        // 键盘导航：↑↓ 移动 / Enter 选择 / ESC 关闭（与 TUI 选择器一致的交互）
        const items = menu.querySelectorAll(".model-menu-item");
        let idx = items.length ? 0 : -1;
        const setActive = (i) => {
          if (i < 0 || i >= items.length) return;
          idx = i;
          items.forEach((it, j) => it.classList.toggle("active", j === idx));
        };
        setActive(0);
        _modelMenuKeyHandler = (ev) => {
          if (ev.key === "ArrowDown") {
            ev.preventDefault();
            setActive((idx + 1) % items.length);
          } else if (ev.key === "ArrowUp") {
            ev.preventDefault();
            setActive((idx - 1 + items.length) % items.length);
          } else if (ev.key === "Enter") {
            ev.preventDefault();
            if (idx >= 0) items[idx].click();
          } else if (ev.key === "Escape") {
            ev.preventDefault();
            closeModelMenu();
          }
        };
        document.addEventListener("keydown", _modelMenuKeyHandler);
      } catch (err) {
        Chat.addSystemMessage(_t("app.modelListFailed", { msg: err.message }));
      }
    });
  }

  function closeModelMenu() {
    const menu = document.querySelector(".model-menu");
    if (menu) menu.remove();
    if (_modelMenuKeyHandler) {
      document.removeEventListener("keydown", _modelMenuKeyHandler);
      _modelMenuKeyHandler = null;
    }
  }

  // ── 工作模式切换器（Ask/Auto，WorkBuddy P2） ───────
  function initModeSwitcher() {
    const sw = $("mode-switcher");
    if (!sw) return;
    sw.addEventListener("click", (e) => {
      const btn = e.target.closest ? e.target.closest(".mode-btn") : null;
      if (!btn || !btn.dataset.mode) return;
      setMode(btn.dataset.mode);
    });
  }

  function setMode(mode) {
    if (mode !== "ask" && mode !== "auto") return;
    state.mode = mode;
    const sw = $("mode-switcher");
    if (!sw) return;
    for (const btn of sw.querySelectorAll(".mode-btn")) {
      btn.classList.toggle("active", btn.dataset.mode === mode);
    }
    // Ask 模式提示（仅当切到 ask 时轻提示一次，不打断）
    if (mode === "ask") {
      Chat.addSystemMessage(_t("app.askModeNotice"));
    }
  }

  // ── 空状态欢迎屏 ───────────────────────
  function updateEmptyState() {
    const empty = $("empty-state");
    const chatView = $("chat-view");
    empty.classList.toggle("hidden", chatView.children.length > 0);
  }

  // ── 输入条状态 ─────────────────────────
  function setComposerDisabled(disabled) {
    $("input").disabled = disabled;
    $("send-btn").disabled = disabled;
    $("stop-btn").style.display = disabled ? "inline-flex" : "none";
  }

  // ── 连接状态 ───────────────────────────
  function updateConnectionDot(kind) {
    const dot = $("status-dot");
    if (dot) dot.className = "status-dot " + kind;
  }

  function showBanner(text) {
    const b = $("conn-banner");
    b.textContent = text;
    b.classList.remove("hidden");
  }
  function hideBanner() {
    $("conn-banner").classList.add("hidden");
  }

  // ── 事件处理（main 已分类） ─────────────
  async function handleEvent(evt) {
    const { type, data } = evt;
    switch (type) {
      case "message_delta":
        Chat.handleDelta(data.chunks || [data]);
        break;
      case "done":
        Chat.handleDone(data);
        if (data.request_id && (state.ownStreamRequestId === data.request_id || data.timeout)) {
          state.busy = false;
          state.ownStreamRequestId = null;
          setComposerDisabled(false);
        }
        break;
      case "tool_started":
        Chat.handleToolStart(data);
        break;
      case "tool_finished":
        Chat.handleToolEnd(data);
        ResultPanel.addToolResult(data);
        break;
      case "cancelled":
        state.busy = false;
        state.ownStreamRequestId = null;
        setComposerDisabled(false);
        break;
      case "error":
        handleError(data);
        break;
      case "pong":
        state.serverId = data.identity?.instance_id || state.serverId;
        state.model = data.model || state.model;
        state.evolutionCount = data.evolution_count ?? state.evolutionCount;
        updateModelSwitcher();
        updateGrowthCard();
        maybeShowEvolutionToast();
        break;
      case "status":
        handleStatus(data);
        break;
      case "sessions":
        state.sessions = data.sessions || [];
        Sidebar.render(data.sessions || []);
        break;
      case "disconnected":
        updateConnectionDot("red");
        showBanner(EMRG_Copy.COPY.disconnected);
        // G89：断连时恢复输入条（不能依赖 30s 超时兜底）
        state.busy = false;
        state.ownStreamRequestId = null;
        setComposerDisabled(false);
        // G97：广播分组缓存清理（DOM 保留；仅清 Map 引用）
        Chat.groupNodes.clear();
        // 进行中的工具行 → 结果未知（工具副作用不可重放）
        for (const row of Chat.toolRows.values()) {
          if (row.classList.contains("running")) {
            row.classList.remove("running");
            row.classList.add("failed");
            const label = row.querySelector(".tool-label");
            if (label) label.textContent = _t("app.unknownResult");
          }
        }
        break;
      case "group_cleared":
        Chat.groupNodes.delete(data.requestId);
        break;
      case "list_result":
        if (data.type === "sessions_list") {
          state.sessions = data.sessions || [];
          Sidebar.render(data.sessions || []);
        }
        break;
      case "command_result":
        if (data.type === "model_set") {
          state.model = data.model || state.model;
          updateModelSwitcher();
        }
        break;
      default:
        break;
    }
  }

  function handleError(data) {
    if (data.error && String(data.error).includes("session busy")) {
      Chat.addSystemMessage(EMRG_Copy.COPY.sessionBusy);
      state.busy = false;
      state.ownStreamRequestId = null;
      setComposerDisabled(false);
    } else {
      Chat.addSystemMessage(_t("app.error", { msg: data.error || _t("app.unknownError") }));
    }
  }

  function handleStatus(data) {
    if (data.connected) {
      updateConnectionDot("green");
      hideBanner();
      if (data.server_id) state.serverId = data.server_id;
      if (data.model) state.model = data.model;
      state.evolutionCount = data.evolution_count ?? state.evolutionCount;
      updateModelSwitcher();
      updateGrowthCard();
      maybeShowEvolutionToast();
      Chat.addSystemMessage(EMRG_Copy.COPY.reconnected);
    } else if (data.auth_failed) {
      updateConnectionDot("red");
      Chat.addSystemMessage(_t("app.authFailed"));
    } else {
      updateConnectionDot("red");
    }
  }

  // ── UI 绑定 ─────────────────────────────
  function bindUi() {
    $("send-btn").addEventListener("click", sendMessage);
    $("stop-btn").addEventListener("click", () => window.emrg.cancel().catch(() => {}));
    $("new-chat-btn").addEventListener("click", newSession);
    $("settings-btn").addEventListener("click", () => {
      loadEvolutionSummary(); // WorkBuddy P3（#502）：打开设置时加载最近改进
      Dialogs.showSettings();
    });
    Dialogs.initLangButtons(); // rant 21:19：设置语言选择器
    $("settings-cancel").addEventListener("click", () => $("settings-dialog").close());
    $("settings-save").addEventListener("click", Dialogs.saveSettings);
    $("pick-dir-btn").addEventListener("click", async () => {
      const dir = await window.emrg.pickProjectDir();
      if (dir) $("set-project-dir").value = dir;
    });
    $("welcome-pick-btn").addEventListener("click", async () => {
      const dir = await window.emrg.pickProjectDir();
      if (dir) $("welcome-project-dir").value = dir;
    });
    $("welcome-save").addEventListener("click", Dialogs.saveWelcome);
    $("confirm-cancel").addEventListener("click", Dialogs.closeConfirm);
    $("confirm-ok").addEventListener("click", Dialogs.confirmOk);
    $("help-close").addEventListener("click", () => $("help-dialog").close());
    $("sessions-close").addEventListener("click", () => $("sessions-dialog").close());
    $("rewind-close").addEventListener("click", () => $("rewind-dialog").close());
    $("memory-close").addEventListener("click", () => $("memory-dialog").close());
    $("skills-close").addEventListener("click", () => $("skills-dialog").close());
    $("rant-cancel").addEventListener("click", () => $("rant-dialog").close());
    $("rant-submit").addEventListener("click", async () => {
      const msg = $("rant-message")?.value || "";
      const proj = $("rant-project")?.value || "";
      $("rant-dialog").close();
      await submitRant(msg, proj);
    });
    $("tasks-close").addEventListener("click", () => $("tasks-dialog").close());

    // 设置/首启对话框：Enter 提交（与重命名/模型表单一致的交互）
    const enterToSave = (fn) => (e) => {
      if (e.key === "Enter") { e.preventDefault(); fn(); }
    };
    $("set-api-key").addEventListener("keydown", enterToSave(Dialogs.saveSettings));
    $("set-base-url").addEventListener("keydown", enterToSave(Dialogs.saveSettings));
    $("welcome-api-key").addEventListener("keydown", enterToSave(Dialogs.saveWelcome));
    $("welcome-base-url").addEventListener("keydown", enterToSave(Dialogs.saveWelcome));

    const input = $("input");
    input.addEventListener("keydown", (e) => {
      // / 补全菜单键盘导航（rant 19:44 P1）：↑↓ 移动、Enter 选择、Esc 关闭
      if (state.cmdMenu.items.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          state.cmdMenu.index = (state.cmdMenu.index + 1) % state.cmdMenu.items.length;
          highlightCmdMenu();
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          state.cmdMenu.index = (state.cmdMenu.index - 1 + state.cmdMenu.items.length) % state.cmdMenu.items.length;
          highlightCmdMenu();
          return;
        }
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          const item = state.cmdMenu.items[state.cmdMenu.index];
          if (item) selectCmd(item.cmd);
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          hideCmdMenu();
          return;
        }
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
      // Ctrl+Enter 同发送
      if (e.key === "Enter" && e.ctrlKey) {
        e.preventDefault();
        sendMessage();
      }
    });
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 150) + "px";
      // / 指令补全：以 / 开头且无空格（仍处于指令词）→ 弹出菜单
      const v = input.value.trim();
      if (v.startsWith("/") && !v.includes(" ")) {
        showCmdMenu(v);
      } else {
        hideCmdMenu();
      }
    });

    const chatView = $("chat-view");
    const updateBackToBottom = () => {
      const btn = $("back-to-bottom");
      const atBottom = chatView.scrollTop + chatView.clientHeight >= chatView.scrollHeight - 40;
      state.autoScroll = atBottom;
      // 上滑阅读（不在底部）→ 显示"回到底部"悬浮按钮（不打扰）
      if (btn) btn.classList.toggle("hidden", atBottom);
    };
    chatView.addEventListener("scroll", updateBackToBottom);
    $("back-to-bottom").addEventListener("click", () => {
      chatView.scrollTop = chatView.scrollHeight;
      state.autoScroll = true;
      $("back-to-bottom").classList.add("hidden");
    });

    // 空状态示例问题卡片 → 填入输入框
    $("empty-state").addEventListener("click", (e) => {
      const card = e.target.closest(".example-card");
      if (!card) return;
      // rant 21:19：示例提示随语言走（textContent 已本地化；data-example 为中文兜底）
      input.value = (card.textContent || card.dataset.example || "").trim();
      input.focus();
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 150) + "px";
    });

    // 右键菜单：点击别处隐藏（showConvMenu 内已绑定选项点击）
    document.addEventListener("click", (e) => {
      if (!$("ctx-menu").hidden && !e.target.closest("#ctx-menu")) hideCtxMenu();
    });

    // 快捷键：⌘N 新对话 / ⌘B 折叠侧边栏 / ⌘, 设置 / ESC 停止或关弹窗
    document.addEventListener("keydown", (e) => {
      if (e.metaKey || e.ctrlKey) {
        if (e.key === "n") {
          e.preventDefault();
          newSession();
        } else if (e.key === "b") {
          e.preventDefault();
          document.body.classList.toggle("sidebar-collapsed");
        } else if (e.key === ",") {
          e.preventDefault();
          Dialogs.showSettings();
        }
        return;
      }
      if (e.key === "Escape") {
        hideCtxMenu(); // 右键菜单优先关闭
        const dlgs = document.querySelectorAll("dialog[open]");
        if (dlgs.length) return; // dialog 原生 ESC 处理
        if (state.busy) {
          e.preventDefault();
          window.emrg.cancel().catch(() => {});
        }
      }
    });

    Dialogs.initThemeButtons();
    Dialogs.initModelForm();
    Dialogs.initRenameDialog();
    initModelSwitcher();
    initModeSwitcher(); // WorkBuddy P2：Ask/Auto 工作模式
    ResultPanel.init(); // WorkBuddy P1：结果面板（⌘\ 折叠 + 窄屏自动隐藏）
    initEvolutionToast(); // WorkBuddy P3：进化 toast 按钮绑定
  }

  // ── 暴露 ─────────────────────────────────
  return {
    state,
    boot,
    sendMessage,
    handleCommand,
    switchSession,
    newSession,
    deleteSession,
    refreshSessions,
    showConvMenu,
    handleEvent,
    bindUi,
    updateEmptyState,
    updateModelSwitcher,
    updateGrowthCard, // WorkBuddy P3：成长卡（导出供测试）
    maybeShowEvolutionToast, // WorkBuddy P3：进化 toast 检测
    showVersionInfo, // WorkBuddy P3：/version 内容（toast "去看看" 共用）
    refreshLocale, // rant 21:19：locale 切换后动态文案重刷
    setMode, // WorkBuddy P2：Ask/Auto 模式（导出供测试与外部调用）
    loadEvolutionSummary, // WorkBuddy P3：最近改进摘要
  };
})();

window.App = App;

// ── 启动（模块级只绑定一次；boot 可被 saveSettings 重复调用） ──
App.bindUi();
window.emrg.onEvent(App.handleEvent);
App.boot();
