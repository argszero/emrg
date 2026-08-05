"use strict";
/**
 * app.js — 应用主控：启动、事件处理（main 已分类）、模型切换器、右键菜单、主题。
 * 事件契约与 main 不变：message_delta/done/tool_started/tool_finished/cancelled/
 * error/pong/status/sessions/disconnected/group_cleared/list_result/command_result。
 */

const App = (() => {
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
    evolutionCount: null,
    autoScroll: true,
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
      state.evolutionCount = init.evolution_count ?? null;
      updateConnectionDot(init.config_exists && init.api_key_configured ? "green" : "gray");
      updateModelSwitcher();

      if (!init.config_exists) {
        Dialogs.showWelcome(); // 首启引导
        return;
      }
      if (!init.api_key_configured) {
        Dialogs.showSettings(); // key 空/占位符
        return;
      }
      if (!init.project_dir_valid) {
        Chat.addSystemMessage("工作目录不可用，请到设置里改一下。");
        Dialogs.showSettings();
        return;
      }
      if (init.sessions && init.sessions.length > 0) {
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
    } catch (e) {
      Chat.addSystemMessage(`启动遇到了问题：${e.message}`);
    }
  }

  // ── 发送 ─────────────────────────────────
  async function sendMessage() {
    const input = $("input");
    const text = input.value.trim();
    if (!text || state.busy) return;
    if (!state.sessionId) {
      Chat.addSystemMessage("请先创建一个对话。");
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
      const res = await window.emrg.sendMessage({ sessionId: state.sessionId, text, requestId });
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

  // ── 会话 ─────────────────────────────────
  async function switchSession(sid, opts = {}) {
    // G65：busy 即自有流进行中/发送中（IPC 往返窗口内 ownStreamRequestId 尚未赋值）
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
        Chat.addSystemMessage("这个对话已被删除，已帮你切到最近的对话。");
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
        Chat.addSystemMessage("已切换对话。");
      }
      updateEmptyState();
      Sidebar.highlight(sid);
    } catch (e) {
      Chat.addSystemMessage(`切换对话失败了：${e.message}`);
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
    } catch (e) {
      Chat.addSystemMessage(`新建对话失败了：${e.message}`);
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
      Chat.addSystemMessage(`删除失败了：${e.message}`);
    }
  }

  async function refreshSessions() {
    try {
      const sessions = await window.emrg.listSessions();
      Sidebar.render(sessions);
    } catch { /* 忽略 */ }
  }

  // ── 右键菜单（重命名 / 删除） ───────────
  function showConvMenu(item, sid, title) {
    // 简单版：确认删除（P3 完善重命名）。右键菜单容器复用 conv-item 定位。
    Dialogs.showConfirm(EMRG_Copy.COPY.deleteConfirmTitle, EMRG_Copy.COPY.deleteConfirmBody, {
      okText: "删除",
      danger: true,
      onOk: () => deleteSession(sid),
    });
  }

  // ── 模型切换器 ─────────────────────────
  async function updateModelSwitcher() {
    const label = $("model-switcher-label");
    if (label) label.textContent = state.model || "选择模型";
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
          const empty = el("div", { class: "model-menu-empty" }, "还没有配置模型");
          const go = el("a", {}, "去设置添加");
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
                Chat.addSystemMessage(`切换模型失败了：${err.message}`);
              }
            });
            menu.appendChild(item);
          }
        }
        $("main").appendChild(menu);
        document.addEventListener("click", closeModelMenu, { once: true });
      } catch (err) {
        Chat.addSystemMessage(`读取模型列表失败了：${err.message}`);
      }
    });
  }

  function closeModelMenu() {
    const menu = document.querySelector(".model-menu");
    if (menu) menu.remove();
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
        break;
      case "status":
        handleStatus(data);
        break;
      case "sessions":
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
            if (label) label.textContent = "结果未知——连接中断";
          }
        }
        break;
      case "group_cleared":
        Chat.groupNodes.delete(data.requestId);
        break;
      case "list_result":
        if (data.type === "sessions_list") Sidebar.render(data.sessions || []);
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
      Chat.addSystemMessage(`出了点问题：${data.error || "未知错误"}`);
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
      Chat.addSystemMessage(EMRG_Copy.COPY.reconnected);
    } else if (data.auth_failed) {
      updateConnectionDot("red");
      Chat.addSystemMessage("认证失败了，请检查设置里的 API Key。");
    } else {
      updateConnectionDot("red");
    }
  }

  // ── UI 绑定 ─────────────────────────────
  function bindUi() {
    $("send-btn").addEventListener("click", sendMessage);
    $("stop-btn").addEventListener("click", () => window.emrg.cancel().catch(() => {}));
    $("new-chat-btn").addEventListener("click", newSession);
    $("settings-btn").addEventListener("click", Dialogs.showSettings);
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

    const input = $("input");
    input.addEventListener("keydown", (e) => {
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
    });

    const chatView = $("chat-view");
    chatView.addEventListener("scroll", () => {
      state.autoScroll =
        chatView.scrollTop + chatView.clientHeight >= chatView.scrollHeight - 40;
    });

    // 空状态示例问题卡片 → 填入输入框
    $("empty-state").addEventListener("click", (e) => {
      const card = e.target.closest(".example-card");
      if (!card) return;
      input.value = card.dataset.example || "";
      input.focus();
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 150) + "px";
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
    initModelSwitcher();
  }

  // ── 暴露 ─────────────────────────────────
  return {
    state,
    boot,
    sendMessage,
    switchSession,
    newSession,
    deleteSession,
    refreshSessions,
    showConvMenu,
    handleEvent,
    bindUi,
    updateEmptyState,
    updateModelSwitcher,
  };
})();

window.App = App;

// ── 启动（模块级只绑定一次；boot 可被 saveSettings 重复调用） ──
App.bindUi();
window.emrg.onEvent(App.handleEvent);
App.boot();
