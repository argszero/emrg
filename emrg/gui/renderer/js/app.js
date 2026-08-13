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
    // P3 slice 1（rant 15:07:19）：会话级状态表——每会话独立 busy/ownStreamRequestId/
    // mode/autoScroll。busy/ownStreamRequestId/mode 为**激活会话条目**的实时视图
    // （defineProperty getter/setter → sidState(sessionId)），既有调用点零改动；
    // 事件按 sid 路由时操作对应条目（后台会话的 done 不误清激活会话的 busy）。
    sessionsBySid: new Map(), // sid → { busy, ownStreamRequestId, mode, autoScroll }
    // P2 queue-injection（#655）：busy 时发送的消息入 daemon 队列（task_queued），
    // 此处按会话记录以便 queued_requeue 以原 requestId 重发（不重加用户行）。
    queuedSends: new Map(), // sid → [{ requestId, text, mode }]
    // P4 slice 2（rant 15:07:19）：跨项目打开的会话（侧边栏数据源，main 广播）
    openSessions: [], // [{ sid, projectName, projectPath, lastActive }]，lastActive 倒序
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
    // B3（rant 21:59:11）：每会话输入框草稿（浏览器 tab 式状态保留）——切换会话保存旧
    // sid 草稿、恢复新 sid 草稿；发送成功即清除该 sid 草稿
    drafts: new Map(), // sid → 草稿文本
    // rant 18:55:09 v0.2：工作区视图导航（null/undefined=会话视图；projects/tasks/rants/settings=面板视图）
    activeView: null,
  };

  // P3 slice 1：会话条目访问器（get-or-create）。无 sid/未激活 → 归入当前激活会话。
  function sidState(sid) {
    const key = sid || state.sessionId || "default";
    if (!state.sessionsBySid.has(key)) {
      state.sessionsBySid.set(key, { busy: false, ownStreamRequestId: null, mode: "auto", autoScroll: true, disconnected: false });
    }
    return state.sessionsBySid.get(key);
  }

  // 激活会话条目 = state.busy/ownStreamRequestId/mode 的事实源（P3 过渡期兼容层）
  Object.defineProperty(state, "busy", {
    get() { return sidState(state.sessionId).busy; },
    set(v) { sidState(state.sessionId).busy = v; },
  });
  Object.defineProperty(state, "ownStreamRequestId", {
    get() { return sidState(state.sessionId).ownStreamRequestId; },
    set(v) { sidState(state.sessionId).ownStreamRequestId = v; },
  });
  Object.defineProperty(state, "mode", {
    get() { return sidState(state.sessionId).mode; },
    set(v) { sidState(state.sessionId).mode = v; },
  });

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
        // P4 slice 2：main 恢复的打开会话 + 激活会话（跳过 switchSession 的 IPC 往返）
        state.openSessions = init.open_sessions || [];
        Sidebar.renderOpenSessions(state.openSessions);
        const restoredSid = init.active_sid;
        if (restoredSid && init.sessions.some((s) => s.session_id === restoredSid)) {
          state.sessionId = restoredSid;
          activateSessionView(restoredSid);
          updateEmptyState();
          Sidebar.highlight(restoredSid);
        } else {
          const current = init.sessions.find((s) => s.session_id === state.sessionId);
          if (current) {
            Sidebar.highlight(state.sessionId);
          } else {
            await switchSession(init.sessions[0].session_id, { silent: true });
          }
        }
      } else {
        await newSession();
      }
      // 修复：boot 成功路径必须启用输入框（此前仅 done/cancelled/disconnected/error
      // 回调会调用 setComposerDisabled(false)，形成"需先发消息才能启用输入框"死锁）
      setComposerDisabled(false);
      // rant 2026-08-11T09:18:16：启动主动更新提示——不开设置也能看到新版本
      // （非阻塞系统消息，一次幂等；daemon 未就绪时静默失败）
      if (window.EMRG_Dialogs?.promptUpdateAtStartup) {
        Dialogs.promptUpdateAtStartup();
      }
    } catch (e) {
      Chat.addSystemMessage(_t("app.bootFailed", { msg: e.message }));
    }
  }

  // ── 发送 ─────────────────────────────────
  async function sendMessage() {
    const input = $("input");
    const text = input.value.trim();
    if (!text) return;
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
    // P2 queue-injection（#655）：busy 不再拦截——daemon 排队注入（task_queued），
    // 回合结束未注入则 queued_requeue 以原 requestId 重发。busy 时记录待重发条目。
    const wasBusy = state.busy;
    state.busy = true;
    setComposerDisabled(true);
    Chat.addUserMessage(text);
    input.value = "";
    input.style.height = "auto";
    // B3：消息已发送 → 清除该会话草稿
    state.drafts.delete(state.sessionId);
    // G143：send 前预生成 requestId 并标记自有流——消除 IPC 往返竞态窗口
    const requestId = genRequestId();
    state.ownStreamRequestId = requestId;
    if (wasBusy) {
      const sid = state.sessionId;
      if (!state.queuedSends.has(sid)) state.queuedSends.set(sid, []);
      state.queuedSends.get(sid).push({ requestId, text, mode: state.mode });
    }
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
          Chat.clear(state.sessionId);
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
          // P2：/resume <id> 直接切换；无参数 → 现代两步弹窗（rant 14:10:14 P6：sessions-dialog 移除，侧边栏会话区 + /open 已覆盖）
          if (parsed.args.length > 0) {
            await switchSession(parsed.args[0]);
          } else {
            Dialogs.showOpenSessionDialog();
          }
          break;
        case "/open":
          // P5（rant 15:07:19）：打开会话对话框（两步：项目 → 会话，跨项目多开）
          Dialogs.showOpenSessionDialog();
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
          // P4：/rant 直接跟内容 → 快速提交；无参数 → 打开 Rant 面板 + 新建表单（rant 14:10:14 P6：rant-dialog 移除）
          if (parsed.args.length > 0) {
            await submitRant(parsed.args.join(" "), "");
          } else {
            await openRantsPanel();
            Dialogs.openRantForm();
          }
          break;
        case "/trigger":
          // P4：/trigger <name> 直接触发；无参数 → 打开任务面板（rant 14:10:14 P3：替代 tasks-dialog）
          if (parsed.args.length > 0) {
            await doTrigger(parsed.args[0]);
          } else {
            openTasksPanel();
          }
          break;
        default:
          Chat.addSystemMessage(_t("app.cmdUnknown", { cmd }));
      }
    } catch (e) {
      Chat.addSystemMessage(_t("app.cmdFailed", { cmd, msg: e.message }));
    }
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
      Chat.clear(state.sessionId);
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

  // /trigger：任务面板（rant 14:10:14 P3：替代 tasks-dialog；点击行立即触发）
  async function openTasksPanel() {
    if (state.activeView !== "tasks") switchView("tasks");
    try {
      await Dialogs.loadTaskMeta();
      await Dialogs.renderTaskList();
    } catch (e) {
      Chat.addSystemMessage(_t("app.tasksFailed", { msg: e.message }));
    }
  }

  // 项目面板（rant 14:10:14 P5：打开即加载项目列表）
  async function openProjectsPanel() {
    if (state.activeView !== "projects") switchView("projects");
    try {
      await Dialogs.renderProjectList();
    } catch (e) {
      Chat.addSystemMessage(_t("projects.addFailed", { msg: e.message }));
    }
  }

  // Rant 面板（rant 14:10:14 P4：打开即加载 rant 列表）
  async function openRantsPanel() {
    if (state.activeView !== "rants") switchView("rants");
    try {
      await Dialogs.renderRantList();
    } catch (e) {
      Chat.addSystemMessage(_t("rants.loadFailed", { msg: e.message }));
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
  // P3 slice 2：每会话一个 .session-view 容器（浏览器 tab 效果——切换 display 保留状态）
  function ensureSessionView(sid) {
    if (!sid) return $("workspace");
    // 在 wrapper 内按 dataset.sid 查找既有容器（不依赖 getElementById——测试沙箱对
    // 未知 id 返回新 mock，且 mock 的 .id 不入 attributes；dataset 双端一致）
    let view = [...$("workspace").children].find((c) => c.dataset?.sid === sid);
    if (!view) {
      view = el("div", { class: "session-view", id: "session-view-" + sid, dataset: { sid } });
      $("workspace").appendChild(view);
    }
    Chat.registerContainer(sid, view); // 幂等：重复注册覆盖同一元素
    return view;
  }

  // rant 18:55:09 v0.2：工作区视图互斥（激活会话视图 → 同步隐藏所有面板视图 + 清导航高亮）
  function activateSessionView(sid) {
    for (const p of VIEWS) {
      const btn = $(`nav-${p}`);
      if (btn) btn.classList.toggle("active", false);
      const panel = $(`panel-${p}`);
      if (panel) panel.classList.remove("active");
    }
    // 浏览器 tab 语义：仅**新建**容器滚到底；既有容器保留滚动位置（rant 18:55:09 验收
    // "回会话视图滚动位置保留"——面板往返/会话切换不跳底）
    const existed = [...$("workspace").children].some((c) => c.dataset?.sid === sid);
    const view = ensureSessionView(sid);
    for (const child of $("workspace").children) {
      if (child.classList) child.classList.remove("active");
    }
    view.classList.add("active");
    if (!existed) Chat.scrollToBottom(sid);
    setWorkspaceChrome("sessions"); // 恢复输入区 + 成果面板（含 back-to-bottom 按位置恢复）
    state.activeView = "sessions";
    return view;
  }

  // B3（rant 21:59:11）：会话切换时保留/恢复输入框草稿（浏览器 tab 式状态）
  function saveDraft(sid) {
    if (!sid) return;
    state.drafts.set(sid, $("input").value);
  }
  function restoreDraft(sid) {
    const input = $("input");
    input.value = sid ? (state.drafts.get(sid) || "") : "";
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 150) + "px";
  }

  /** 会话的项目根目录（openSessions 优先，回退全局 projectDir；P3.1 文件树根） */
  function projectPathFor(sid) {
    const os = state.openSessions.find((s) => s.sid === sid);
    return (os && os.projectPath) || state.projectDir || "";
  }

  async function switchSession(sid, opts = {}) {    // G65：busy 即自有流进行中/发送中（IPC 往返窗口内 ownStreamRequestId 尚未赋值）
    if (state.busy) {
      Chat.addSystemMessage(EMRG_Copy.COPY.sessionBusy);
      return;
    }
    try {
      // B3：离开前保存当前会话草稿
      saveDraft(state.sessionId);
      const res = await window.emrg.switchSession({ sessionId: sid, projectPath: opts.projectPath });
      state.sessionId = sid;
      // B3：恢复目标会话草稿
      restoreDraft(sid);
      // P3 slice 1：切换只移动激活指针——每会话条目保留自己的 busy/ownStreamRequestId
      // P3 slice 2：激活该会话容器（状态保留，不 Chat.clear——切回继续看到原消息/流式现场）
      activateSessionView(sid);
      ResultPanel.switchSession(sid); // P2 框架：右栏 Tab/产物状态按 sid 隔离
      FileTree.setSession(sid, projectPathFor(sid)); // P3.1：文件树根跟随会话项目
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
      // G13：v1 不加载历史（G12）→ rant 14:15:12：非 silent 切换加载最近 50 条 + 滚动分页
      if (!opts.silent) {
        Chat.addSystemMessage(_t("app.switched"), sid);
        await loadHistory(sid);
      }
      // P3 finalize：切入断线会话 → 提示自动重连中（状态保留，不打断输入——G89）
      if (sidState(sid).disconnected) {
        Chat.addSystemMessage(_t("app.sessionDisconnected"), sid);
      }
      updateEmptyState();
      Sidebar.highlight(sid);
      setComposerDisabled(false); // 防御性：独立调用 switchSession 也确保输入框可用
    } catch (e) {
      // P6（rant 15:07:19 上限 20）：超限提示本地化（main 抛 too many open sessions）
      Chat.addSystemMessage(/too many open sessions/i.test(e.message || "") ? _t("app.tooManyOpenSessions") : _t("app.switchFailed", { msg: e.message }));
    }
  }

  // ── 工作区视图导航（rant 18:55:09 v0.2：面板 → 工作区视图；sessions=会话视图）──
  const VIEWS = ["sessions", "projects", "tasks", "rants", "settings"];

  /** 切换工作区视图：点当前激活项关闭回会话视图，点其他项切换；高亮当前导航（DOM 显隐 .active 互斥，状态保留）。 */
  function switchView(name) {
    if (!VIEWS.includes(name)) return;
    const isOpen = state.activeView === name;
    for (const p of VIEWS) {
      const btn = $(`nav-${p}`);
      if (btn) btn.classList.toggle("active", false);
    }
    if (!isOpen && name !== "sessions") {
      // 打开面板视图：隐藏全部会话视图 + 全部面板视图（互斥——显式清面板，不依赖 DOM 树），激活目标面板
      state.activeView = name;
      for (const child of $("workspace").children) {
        if (child.classList) child.classList.remove("active");
      }
      for (const p of VIEWS) {
        const v = $(`panel-${p}`);
        if (v) v.classList.remove("active");
      }
      const view = $(`panel-${name}`);
      if (view) view.classList.add("active");
      const btn = $(`nav-${name}`);
      if (btn) btn.classList.add("active");
      setWorkspaceChrome("panel"); // 隐藏输入区 + 成果面板 + 空状态
    } else {
      // 点当前激活项（toggle 关闭）/ 点 💬 会话 → 回会话视图
      showSessionsView();
    }
  }

  /** 回会话视图：激活当前 sid 的会话视图（无会话 → 仅恢复工作区 chrome） */
  function showSessionsView() {
    state.activeView = "sessions";
    if (state.sessionId) {
      activateSessionView(state.sessionId);
    } else {
      for (const child of $("workspace").children) {
        if (child.classList) child.classList.remove("active");
      }
      setWorkspaceChrome("sessions");
    }
    updateEmptyState();
  }

  /** 工作区外围 chrome（输入区 + 空状态 + 成果面板 + 拖拽手柄）按视图模式显隐：panel 隐藏，sessions 恢复。 */
  function setWorkspaceChrome(mode) {
    const composer = $("composer-wrap");
    const empty = $("empty-state");
    const panel = $("result-panel");
    const resizer = $("result-resizer");
    const btb = $("back-to-bottom");
    if (mode === "panel") {
      composer.classList.add("hidden");
      empty.classList.add("hidden");
      if (panel) panel.classList.add("hidden");
      if (resizer) resizer.classList.add("hidden");
      if (btb) btb.classList.add("hidden"); // 回到底部按钮属会话视图，面板视图下隐藏
      // HTML 预览（WebContentsView）随面板隐藏（main 侧比对路径；无预览时 no-op）
      try { if (window.emrg && typeof window.emrg.closePreview === "function") window.emrg.closePreview({}); } catch { /* ignore */ }
    } else {
      composer.classList.remove("hidden");
      if (panel) panel.classList.remove("hidden");
      if (resizer) resizer.classList.remove("hidden");
      updateBackToBottomState(); // 回会话视图：按当前滚动位置恢复按钮
      updateEmptyState();
    }
  }

  /** 当前滚动容器：激活会话的 .session-view（#workspace overflow:hidden 自身不滚动） */
  function activeScrollEl() {
    const ws = $("workspace");
    if (state.sessionId) {
      const v = [...(ws.children || [])].find((c) => c.dataset?.sid === state.sessionId);
      if (v) return v;
    }
    const active = [...(ws.children || [])].find((c) => c.classList && c.classList.contains("active"));
    return active || ws;
  }

  /** 更新"回到底部"按钮 + autoScroll（scroll 事件不冒泡 → 需 capture 捕获子 .session-view 滚动） */
  function updateBackToBottomState() {
    const btn = $("back-to-bottom");
    const el = activeScrollEl();
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
    state.autoScroll = atBottom;
    // 上滑阅读（不在底部）→ 显示"回到底部"悬浮按钮（不打扰）
    if (btn) btn.classList.toggle("hidden", atBottom);
  }

  /** rant 14:10:14 P2：设置面板 tab 切换（模型服务/工作目录/GitHub/外观/语言/关于）。 */
  const SETTINGS_TABS = ["model", "workdir", "github", "appearance", "language", "about"];
  function switchSettingsTab(name) {
    if (!SETTINGS_TABS.includes(name)) return;
    for (const t of SETTINGS_TABS) {
      const tabBtn = $(`settings-tab-${t}`);
      const body = $(`settings-body-${t}`);
      if (tabBtn) tabBtn.classList.toggle("active", t === name);
      if (body) body.classList.toggle("hidden", t !== name);
    }
  }

  // ── 历史按需加载（rant 14:15:12：切会话恢复最近 N 条 + 滚动到顶加载更早）──
  const historyPages = new Map(); // sid → { offset, hasMore, loading }
  const HISTORY_PAGE = 50;

  function historyPageState(sid) {
    if (!historyPages.has(sid)) {
      historyPages.set(sid, { offset: 0, hasMore: false, loading: false });
    }
    return historyPages.get(sid);
  }

  /** 渲染历史消息到该会话容器（只读 user 气泡，不触发工具/交互）。 */
  function renderHistoryMessages(sid, messages) {
    for (const m of messages || []) {
      Chat.addHistoryMessage(m.preview || m.content || "", sid);
    }
  }

  /** 加载最近一页历史（offset 从最新往回数）——切会话时调用。 */
  async function loadHistory(sid) {
    const st2 = historyPageState(sid);
    st2.loading = true;
    try {
      const res = await window.emrg.listHistory({ sessionId: sid, limit: HISTORY_PAGE, offset: st2.offset });
      renderHistoryMessages(sid, res.messages || []);
      st2.offset += (res.messages || []).length;
      st2.hasMore = !!res.hasMore;
      if (st2.hasMore) {
        Chat.setLoadBar(sid, _t("app.historyLoadMore"));
      }
    } catch (e) {
      Chat.addSystemMessage(_t("app.historyFailed", { msg: e.message }), sid);
    } finally {
      st2.loading = false;
    }
  }

  /** 滚动到顶 → 加载更早一页（prepend，保持滚动位置）。 */
  async function loadOlderHistory(sid) {
    const st2 = historyPageState(sid);
    if (!st2.hasMore || st2.loading) return;
    st2.loading = true;
    const view = Chat.chatContainer(sid);
    const prevScrollTop = view.scrollTop;
    const prevHeight = view.scrollHeight;
    try {
      const res = await window.emrg.listHistory({ sessionId: sid, limit: HISTORY_PAGE, offset: st2.offset });
      const msgs = res.messages || [];
      // prepend：先清加载条，再逐条插到顶部（addHistoryMessage prepend 会插在加载条之后）
      for (const m of msgs) {
        Chat.addHistoryMessage(m.preview || m.content || "", sid, { prepend: true });
      }
      st2.offset += msgs.length;
      st2.hasMore = !!res.hasMore;
      if (msgs.length === 0) st2.hasMore = false;
      if (st2.hasMore) {
        Chat.setLoadBar(sid, _t("app.historyLoadMore"));
      } else {
        Chat.setLoadBar(sid, _t("app.historyNoMore"));
      }
      // 保持视觉位置：新内容插到顶部后滚差补偿
      view.scrollTop = prevScrollTop + (view.scrollHeight - prevHeight);
    } catch (e) {
      Chat.addSystemMessage(_t("app.historyFailed", { msg: e.message }), sid);
    } finally {
      st2.loading = false;
    }
  }

  /** 会话视图滚动：到顶且有更早 → 加载（防抖 150ms，绑定在 bindUi 内）。 */
  let historyScrollTimer = null;

  async function newSession(opts = {}) {
    if (state.busy) {
      Chat.addSystemMessage(EMRG_Copy.COPY.sessionBusy);
      return;
    }
    try {
      // B3：离开前保存当前会话草稿；新会话从空草稿开始
      saveDraft(state.sessionId);
      const res = await window.emrg.newSession({ projectPath: opts.projectPath });
      state.sessionId = res.session_id;
      state.drafts.set(res.session_id, ""); // 新会话无草稿
      activateSessionView(state.sessionId); // P3 slice 2：新会话独立容器（空）
      ResultPanel.switchSession(state.sessionId); // P2 框架：新会话右栏状态复位
      FileTree.setSession(state.sessionId, projectPathFor(state.sessionId)); // P3.1：文件树根跟随
      Chat.clear(state.sessionId); // 新会话从空开始（容器可能被复用）
      updateEmptyState(); // 欢迎屏即反馈
      await refreshSessions();
      Sidebar.highlight(state.sessionId);
      setComposerDisabled(false); // 防御性：独立调用 newSession 也确保输入框可用
      restoreDraft(state.sessionId); // 新会话草稿为空 → 清空输入框
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
      // P3 slice 2：删除会话 → 释放其容器（若已打开）
      Chat.unregisterContainer(sid);
      const view = [...$("workspace").children].find((c) => c.dataset?.sid === sid);
      if (view) view.remove();
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

  // ── P4 slice 2：打开会话（跨项目侧边栏） ───────────
  // 关闭 = 断开连接 + 移出列表，**保留磁盘数据**（与删除区分：关闭留数据/删除删数据）
  async function closeOpenSession(sid) {
    if (state.busy && state.sessionId === sid) {
      Chat.addSystemMessage(EMRG_Copy.COPY.sessionBusy);
      return;
    }
    try {
      await window.emrg.closeSession({ sessionId: sid });
      Chat.unregisterContainer(sid); // 释放容器（若已打开）
      const view = [...$("workspace").children].find((c) => c.dataset?.sid === sid);
      if (view) view.remove();
      if (state.sessionId === sid) {
        // 关闭激活会话 → 切到剩余打开会话中最近激活的，否则新建
        const remaining = state.openSessions.filter((s) => s.sid !== sid);
        if (remaining.length > 0) {
          await switchSession(remaining[0].sid, { silent: true });
        } else {
          await newSession();
        }
      }
    } catch (e) {
      Chat.addSystemMessage(_t("app.closeFailed", { msg: e.message }));
    }
  }

  // 打开会话右键菜单：关闭（留数据）/ 重命名 / 删除（删数据，确认）
  function showOpenSessionsMenu(item, entry) {
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
    const cur = state.sessions.find((s) => s.session_id === entry.sid);
    const title = (cur && cur.title) || entry.sid;
    mk(_t("app.closeSession"), false, () => closeOpenSession(entry.sid));
    mk(_t("app.rename"), false, () => Dialogs.showRename(entry.sid, title));
    mk(_t("app.deleteConv"), true, () => Dialogs.showConfirm(EMRG_Copy.COPY.deleteConfirmTitle, EMRG_Copy.COPY.deleteConfirmBody, {
      okText: _t("dlg.delete"),
      danger: true,
      onOk: () => deleteSession(entry.sid),
    }));
    menu.hidden = false;
    const rect = item.getBoundingClientRect();
    menu.style.left = Math.min(rect.right, window.innerWidth - 160) + "px";
    menu.style.top = Math.min(rect.bottom, window.innerHeight - 80) + "px";
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
    // 演化刚完成 = 演化真正需要 GitHub 的时刻（Windows GCM rant Stage 2）
    maybeShowGithubBanner();
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

  // ── GitHub 连接横幅（Windows GCM rant Stage 2） ────
  // 触发时机：演化计数增长（演化刚完成、需要 GitHub 推 PR）且未认证时。
  // 本地聊天不依赖 GitHub → 启动时不打扰。横幅可关闭；关闭后本会话不再弹。
  let _githubBannerDismissed = false;

  function showGithubBanner() {
    const b = $("github-banner");
    if (!b) return;
    const msg = $("github-banner-msg");
    if (msg) msg.textContent = _t("settings.githubBannerMsg");
    b.classList.remove("hidden");
  }

  function hideGithubBanner() {
    const b = $("github-banner");
    if (b) b.classList.add("hidden");
  }

  async function maybeShowGithubBanner() {
    if (_githubBannerDismissed) return;
    try {
      const s = await window.emrg.githubStatus();
      if (s && s.authenticated) return; // 已连接 → 无需提示
      showGithubBanner();
    } catch { /* githubStatus 不可用（daemon 忙/未合入）→ 静默跳过 */ }
  }

  function initGithubBanner() {
    const connect = $("github-banner-connect");
    if (connect) {
      connect.addEventListener("click", () => {
        hideGithubBanner();
        Dialogs.showSettings(); // 设置页 GitHub 连接区（Stage 2a）
      });
    }
    const dismiss = $("github-banner-dismiss");
    if (dismiss) {
      dismiss.addEventListener("click", () => {
        _githubBannerDismissed = true;
        hideGithubBanner();
      });
    }
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
        recent.appendChild(el("div", { class: "about-recent-item" }, _t("app.noImprovements")));
        return;
      }
      const header = el("div", { class: "about-recent-title" }, _t("app.recentImprovements"));
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
    // P3 slice 2：检查**激活会话**容器（wrapper 恒含 session-view 子节点，不能数 wrapper）
    const activeView = Chat.chatContainer(state.sessionId);
    empty.classList.toggle("hidden", activeView.children.length > 0);
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
    // P3（rant 15:07:19）：main 事件桥附带 sid（#629 起）——Chat 按会话隔离状态；
    // 无 sid（单会话过渡期）→ 默认桶，行为与改造前一致。
    const sid = evt.sid;
    switch (type) {
      case "message_delta":
        Chat.handleDelta(data.chunks || [data], sid);
        break;
      case "done":
        Chat.handleDone(data, sid);
        // P3 slice 1：done 释放**该事件所属会话**的 busy 锁（后台会话的广播 done
        // 不误清激活会话）；仅当该会话是激活会话时同步输入条 UI。
        {
          const sst = sidState(sid);
          if (data.request_id && (sst.ownStreamRequestId === data.request_id || data.timeout)) {
            sst.busy = false;
            sst.ownStreamRequestId = null;
            if (!sid || sid === state.sessionId) setComposerDisabled(false);
          }
        }
        break;
      case "tool_started":
        Chat.handleToolStart(data, sid);
        break;
      case "tool_finished":
        Chat.handleToolEnd(data, sid);
        ResultPanel.addToolResult(data, sid); // P2 框架：产物登记按事件 sid 归类（P3.2 消费）
        break;
      case "cancelled":
        Chat.clearTyping(sid); // rant 14:11：取消时移除在途节点 typing 光标（无 request_id，全清）
        // P3 slice 1：cancelled 释放该事件所属会话的锁（后台会话取消不误清激活会话）
        {
          const sst = sidState(sid);
          sst.busy = false;
          sst.ownStreamRequestId = null;
          if (!sid || sid === state.sessionId) setComposerDisabled(false);
        }
        break;
      // P2 queue-injection（#655）：4 个 daemon→client 广播帧（busy 排队注入协议）
      case "task_queued":
        Chat.addSystemMessage(_t("app.queued", { pos: data.position || 0 }), sid);
        break;
      case "steer_committed":
        // 已注入当前回合——从待重发记录移除（回合内 deltas 会带上原 turn 的回复）
        {
          const q = state.queuedSends.get(sid);
          if (q && data.request_id) {
            const idx = q.findIndex((e) => e.requestId === data.request_id);
            if (idx >= 0) q.splice(idx, 1);
            if (q.length === 0) state.queuedSends.delete(sid);
          }
        }
        break;
      case "queued_requeue":
        // 回合正常结束且消息从未注入——daemon 锁已释放，以原 requestId 静默重发
        // （不重加用户行；后台会话按 sid 处理，只操作该会话条目）
        {
          const q = state.queuedSends.get(sid);
          if (q && q.length) {
            const ids = new Set(data.request_ids || []);
            const toResend = q.filter((e) => ids.has(e.requestId));
            const remaining = q.filter((e) => !ids.has(e.requestId));
            if (toResend.length) {
              // P2 审查 ❌ 同 #695：was_busy 在循环前捕获，单客户端时首条重发
              // 开启新回合，M2+ 到达时 daemon busy 被再排队但客户端未跟踪 → 下个
              // queued_requeue 找不到 → 静默丢失。修复=每条重发若 (wasBusy || i>0)
              // 重新跟踪——steer_committed 移除已注入的，下个 queued_requeue 重发
              // 其余，收敛。
              const wasBusy = sidState(sid).busy;
              for (let i = 0; i < toResend.length; i++) {
                const item = toResend[i];
                const sst = sidState(sid);
                sst.busy = true;
                sst.ownStreamRequestId = item.requestId;
                if (!sid || sid === state.sessionId) setComposerDisabled(true);
                try {
                  const res = await window.emrg.sendMessage({ sessionId: sid, text: item.text, requestId: item.requestId, mode: item.mode });
                  sst.ownStreamRequestId = res.requestId || item.requestId;
                } catch (e) {
                  sst.busy = false;
                  sst.ownStreamRequestId = null;
                  if (!sid || sid === state.sessionId) setComposerDisabled(false);
                }
                if (wasBusy || i > 0) {
                  remaining.push({ requestId: item.requestId, text: item.text, mode: item.mode });
                }
              }
              if (remaining.length) state.queuedSends.set(sid, remaining);
              else state.queuedSends.delete(sid);
              Chat.addSystemMessage(_t("app.queuedResent", { n: toResend.length }), sid);
            }
          }
        }
        break;
      case "queued_cancelled":
        // 回合被取消/异常/断连——daemon 丢弃队列
        if (state.queuedSends.delete(sid)) {
          Chat.addSystemMessage(_t("app.queuedCancelled"), sid);
        }
        break;
      case "error":
        handleError(data, sid);
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
      case "open_sessions":
        // P4 slice 2：main 广播打开会话列表变化 → 刷新侧边栏打开会话区
        state.openSessions = data.openSessions || [];
        Sidebar.renderOpenSessions(state.openSessions);
        break;
      case "disconnected":
        // P3 finalize：断连按 sid 隔离——后台会话断连不影响全局 UI（无横幅/红点），
        // 仅激活会话（或无 sid 的单会话过渡期）断连显示全局横幅 + 红点。
        {
          const sst = sidState(sid);
          sst.busy = false;
          sst.ownStreamRequestId = null;
          sst.disconnected = true; // P3 finalize：该会话条目标断线（P4 恢复 UI 用）
          state.queuedSends.delete(sid); // P2 queue-injection：断连 daemon 丢队列
          const isActive = !sid || sid === state.sessionId;
          if (isActive) {
            updateConnectionDot("red");
            showBanner(EMRG_Copy.COPY.disconnected);
            // G89：断连时恢复输入条（不能依赖 30s 超时兜底）
            setComposerDisabled(false);
          }
          // 容器标断线（仅当该 sid 有**独立注册**容器——chatContainer 对未注册
          // sid 回退激活容器，打错标；P4 openSessions 后每会话都有注册容器）
          if (Chat.hasContainer(sid)) {
            const cv = Chat.chatContainer(sid);
            if (cv && cv.classList) cv.classList.add("disconnected");
          }
        }
        // P3：广播分组缓存清理按会话隔离（DOM 保留；仅清该会话 Map 引用；无 sid → 默认桶）
        Chat.groupNodesFor(sid).clear();
        // 进行中的工具行 → 结果未知（工具副作用不可重放）
        for (const row of Chat.toolRowsFor(sid).values()) {
          if (row.classList.contains("running")) {
            row.classList.remove("running");
            row.classList.add("failed");
            const label = row.querySelector(".tool-label");
            if (label) label.textContent = _t("app.unknownResult");
          }
        }
        break;
      case "update_downloaded":
        // rant 2026-08-12T12:10:12：daemon 后台自动下载 + 校验完新安装包 →
        // 非阻塞提示"已就绪，点击安装"（设置 → 关于显示安装按钮）
        Chat.addSystemMessage(
          _t("app.updateReady", { latest: data.downloaded_version || "" }),
        );
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

  function handleError(data, sid) {
    const sst = sidState(sid);
    if (data.error && String(data.error).includes("session busy")) {
      Chat.addSystemMessage(EMRG_Copy.COPY.sessionBusy, sid);
      sst.busy = false;
      sst.ownStreamRequestId = null;
      if (!sid || sid === state.sessionId) setComposerDisabled(false);
    } else {
      Chat.clearTyping(sid); // rant 14:11：流式错误时移除在途节点 typing 光标
      Chat.addSystemMessage(_t("app.error", { msg: data.error || _t("app.unknownError") }), sid);
    }
  }

  function handleStatus(data) {
    if (data.connected) {
      updateConnectionDot("green");
      hideBanner();
      // P3 finalize：重连成功 → 清全部会话断线标记 + 容器 .disconnected 类
      for (const entry of state.sessionsBySid.values()) entry.disconnected = false;
      for (const child of $("workspace").children) {
        if (child.classList && child.classList.contains("disconnected")) child.classList.remove("disconnected");
      }
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
    } else if (data.daemon_stopped) {
      // Rant 2026-08-09T13:16:36 ⑤：spawn 节流命中——显示真实失败原因（含
      // emrgd.log 尾部），提示宿主手动启动，不再无限重试弹窗。
      updateConnectionDot("red");
      Chat.addSystemMessage(_t("app.daemonStopped", { msg: data.error || "" }));
    } else {
      updateConnectionDot("red");
    }
  }

  // ── UI 绑定 ─────────────────────────────
  function bindUi() {
    $("send-btn").addEventListener("click", sendMessage);
    $("stop-btn").addEventListener("click", () => window.emrg.cancel().catch(() => {}));
    // rant 18:55:09 v0.2：侧边栏导航点击 → 工作区视图切换（data-view）
    for (const p of VIEWS) {
      $(`nav-${p}`)?.addEventListener("click", () => {
        switchView(p);
        // 面板视图打开时加载对应数据（settings 走 showSettings 刷新全部；tasks/projects/rants 走各自加载）
        if (p === "settings" && state.activeView === "settings") {
          loadEvolutionSummary();
          Dialogs.showSettings();
        } else if (p === "tasks" && state.activeView === "tasks") {
          openTasksPanel();
        } else if (p === "projects" && state.activeView === "projects") {
          openProjectsPanel();
        } else if (p === "rants" && state.activeView === "rants") {
          openRantsPanel();
        }
      });
    }
    $("new-chat-btn").addEventListener("click", () => Dialogs.showNewSessionDialog());
    // B2（rant 21:59:11）：侧边栏"打开会话"入口 → 两步弹窗（选项目 → 选会话）
    $("open-chat-btn")?.addEventListener("click", () => Dialogs.showOpenSessionDialog());
    Dialogs.initOpenSessionDialog(); // P5：打开会话对话框绑定
    Dialogs.initNewSessionDialog(); // P5 slice 2：新建会话对话框绑定
    $("open-session-new-session")?.addEventListener("click", () => Dialogs.showNewSessionDialog()); // P5 slice 2：打开弹窗 → 新建会话
    $("settings-btn").addEventListener("click", () => {
      loadEvolutionSummary(); // WorkBuddy P3（#502）：打开设置时加载最近改进
      Dialogs.showSettings();
    });
    Dialogs.initLangButtons(); // rant 21:19：设置语言选择器
    $("settings-cancel").addEventListener("click", () => switchView("settings")); // P2：取消=关闭设置视图
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
    $("rewind-close").addEventListener("click", () => $("rewind-dialog").close());
    $("memory-close").addEventListener("click", () => $("memory-dialog").close());
    $("skills-close").addEventListener("click", () => $("skills-dialog").close());
    // rant 14:10:14 P6：sessions-dialog / rant-dialog 已移除（侧边栏会话区 + /open + Rant 面板覆盖）

    // rant 14:10:14 P2：设置面板 tab 切换（模型服务/工作目录/GitHub/外观/语言/关于）
    document.querySelectorAll("#settings-tabs .panel-tab").forEach((tab) => {
      tab.addEventListener("click", () => switchSettingsTab(tab.dataset.settingsTab));
    });
    // rant 14:10:14 P5：项目面板「添加项目」→ 选目录 → 注册
    $("project-add-btn")?.addEventListener("click", async () => {
      try {
        const res = await window.emrg.pickProjectDir();
        if (res && res.path) {
          await window.emrg.registerProject({ path: res.path });
          Chat.addSystemMessage(_t("projects.added", { path: res.path }));
          await Dialogs.renderProjectList();
        }
      } catch (e) {
        Chat.addSystemMessage(_t("projects.addFailed", { msg: e.message }));
      }
    });

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

    const chatView = $("workspace");
    // scroll 事件不冒泡 → 必须用 capture 捕获子 .session-view 的滚动（#634 起滚动容器下移；
    // 此前监听挂在 #workspace 上从不触发，back-to-bottom 与滚动加载历史在真实 GUI 静默失效）
    chatView.addEventListener("scroll", updateBackToBottomState, true);
    $("back-to-bottom").addEventListener("click", () => {
      const el = activeScrollEl();
      el.scrollTop = el.scrollHeight;
      state.autoScroll = true;
      $("back-to-bottom").classList.add("hidden");
    });
    // rant 14:15:12：会话视图滚动到顶 → 加载更早历史（防抖 150ms）
    chatView.addEventListener("scroll", (e) => {
      const t = e.target;
      const isView = t && t.classList && t.classList.contains("session-view");
      if (!isView || !state.sessionId) return;
      const st2 = historyPageState(state.sessionId);
      if (t.scrollTop <= 2 && st2.hasMore && !st2.loading) {
        clearTimeout(historyScrollTimer);
        historyScrollTimer = setTimeout(() => loadOlderHistory(state.sessionId), 150);
      }
    }, { capture: true, passive: true });

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
          // B1（rant 21:59:11）：⌘N 与侧边栏按钮一致 → 项目选择弹窗
          Dialogs.showNewSessionDialog();
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
    Dialogs.initGithubSection(); // Windows GCM rant Stage 2：设置页 GitHub 连接
    Dialogs.initDeviceDialog(); // Windows GCM rant Stage 2b：device flow 对话框
    Dialogs.initTaskManagement(); // rant 18:23:15 P3：定时任务/自定义类型管理
    Dialogs.initRantPanel(); // rant 14:10:14 P4：rant 面板（筛选/新建）
    initGithubBanner(); // Windows GCM rant Stage 2：演化需 GitHub 但未认证时的连接横幅
    initModelSwitcher();
    initModeSwitcher(); // WorkBuddy P2：Ask/Auto 工作模式
    ResultPanel.init(); // WorkBuddy P1：结果面板（⌘\ 折叠 + 窄屏自动隐藏）
    FileTree.setSession(state.sessionId, projectPathFor(state.sessionId)); // P3.1：文件树初始根
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
    closeOpenSession, // P4 slice 2：关闭打开会话（保留数据）
    showOpenSessionsMenu, // P4 slice 2：打开会话右键菜单
    activateSessionView, // P3 slice 2：激活会话容器（display 切换；导出供测试）
    switchView, // rant 18:55:09 v0.2：工作区视图切换（导出供测试）
    switchSettingsTab, // rant 14:10:14 P2：设置面板 tab 切换（导出供测试）
    openTasksPanel, // rant 14:10:14 P3：任务面板打开 + 加载（导出供测试）
    openProjectsPanel, // rant 14:10:14 P5：项目面板打开 + 加载（导出供测试）
    openRantsPanel, // rant 14:10:14 P4：rant 面板打开 + 加载（导出供测试）
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
