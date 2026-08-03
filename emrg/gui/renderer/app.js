"use strict";
/**
 * app.js — renderer UI 逻辑：聊天渲染、会话列表、工具状态、事件绑定。
 * 事件已由 main 分类（onEvent 收 {type, data}），renderer 不重复做帧分类（G67）。
 */

const $ = (id) => document.getElementById(id);

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
  autoScroll: true,
  groupNodes: new Map(), // requestId → DOM 节点
  toolCards: new Map(), // tool_call_id → DOM 节点
};

// ── 启动 ─────────────────────────────────────────────────

async function boot() {
  try {
    const init = await window.emrg.init();
    state.configExists = init.config_exists;
    state.apiKeyConfigured = init.api_key_configured;
    state.projectDir = init.project_dir || "";
    state.serverId = init.server_id || "";
    state.model = init.model || "";
    updateStatusBar(init);
    updateEvolutionCount(init.evolution_count);

    if (!init.config_exists) {
      showWelcome(); // G71：config 缺失 → 首启引导（不拉起 daemon）
      return;
    }
    if (!init.api_key_configured) {
      showSettings(); // G36：config 存在但 key 空/占位符
      return;
    }
    if (!init.project_dir_valid) {
      addSystemMessage("⚠️ 项目目录无效或不可写，请到设置中修正。");
      showSettings();
      return;
    }
    if (init.sessions && init.sessions.length > 0) {
      renderSessions(init.sessions);
      // 保持当前会话（boot 会因 saveSettings/saveWelcome 重跑——保存设置后不应弹回最新创建会话）
      const current = init.sessions.find((s) => s.session_id === state.sessionId);
      if (current) {
        highlightActiveSession(state.sessionId);
      } else {
        await switchSession(init.sessions[0].session_id, { silent: true });
      }
    } else {
      await newSession();
    }
  } catch (e) {
    addSystemMessage(`启动失败: ${e.message}`);
  }
}

// ── UI 绑定 ──────────────────────────────────────────────

function bindUi() {
  $("send-btn").addEventListener("click", sendMessage);
  $("stop-btn").addEventListener("click", () => window.emrg.cancel());
  $("new-session-btn").addEventListener("click", newSession);
  $("settings-btn").addEventListener("click", showSettings);
  $("settings-cancel").addEventListener("click", () => $("settings-dialog").close());
  $("settings-save").addEventListener("click", saveSettings);
  $("pick-dir-btn").addEventListener("click", async () => {
    const dir = await window.emrg.pickProjectDir();
    if (dir) $("set-project-dir").value = dir;
  });
  $("welcome-pick-btn").addEventListener("click", async () => {
    const dir = await window.emrg.pickProjectDir();
    if (dir) $("welcome-project-dir").value = dir;
  });
  $("welcome-save").addEventListener("click", saveWelcome);

  const input = $("input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
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
    state.autoScroll = chatView.scrollTop + chatView.clientHeight >= chatView.scrollHeight - 40;
  });
}

// ── 发送 ─────────────────────────────────────────────────

async function sendMessage() {
  const input = $("input");
  const text = input.value.trim();
  if (!text || state.busy) return;
  if (!state.sessionId) {
    addSystemMessage("请先创建/选择会话。");
    return;
  }
  state.busy = true;
  setComposerDisabled(true);
  addUserMessage(text);
  input.value = "";
  input.style.height = "auto";
  try {
    const res = await window.emrg.sendMessage({ sessionId: state.sessionId, text });
    state.ownStreamRequestId = res.requestId || null; // G124：标记自有流
  } catch (e) {
    state.busy = false;
    state.ownStreamRequestId = null;
    setComposerDisabled(false);
    addSystemMessage(`发送失败: ${e.message}（输入已恢复）`);
    input.value = text; // G49：失败恢复输入框
  }
}

// ── 会话 ─────────────────────────────────────────────────

async function switchSession(sid, opts = {}) {
  if (state.busy && state.ownStreamRequestId) {
    addSystemMessage("当前有进行中的响应，请等待完成或停止后再切换。"); // G65
    return;
  }
  try {
    const res = await window.emrg.switchSession({ sessionId: sid });
    state.sessionId = sid;
    state.ownStreamRequestId = null; // G110：切会话清自有流标记
    clearChat();
    if (res.error === "session_not_found") {
      addSystemMessage("会话已被删除，已切换到最近会话。");
      if (res.next_session) {
        renderSessions(res.sessions || []);
        await switchSession(res.next_session, { silent: true });
      } else {
        await newSession();
      }
      return;
    }
    // G13：历史不通过 resume 返回——v1 不加载历史（G12）
    addSystemMessage(`已切换到会话 ${sid}`);
    highlightActiveSession(sid);
  } catch (e) {
    addSystemMessage(`切换失败: ${e.message}`);
  }
}

async function newSession() {
  if (state.busy && state.ownStreamRequestId) {
    addSystemMessage("当前有进行中的响应，请等待完成或停止后再新建会话。"); // G65 同款锁
    return;
  }
  try {
    const res = await window.emrg.newSession();
    const sid = res.session_id;
    // 本地切订阅（resume 不存在的新会话会被 daemon 自动创建）
    state.sessionId = sid;
    clearChat();
    addSystemMessage(`新会话 ${sid}`);
    await refreshSessions();
    highlightActiveSession(sid);
  } catch (e) {
    addSystemMessage(`新建失败: ${e.message}`);
  }
}

async function deleteSession(sid) {
  // G65：自有流运行中禁止删除当前会话（删除后自动切换会被 busy 锁拒绝 → sessionId 指向已删会话）
  if (state.busy && state.ownStreamRequestId && state.sessionId === sid) {
    addSystemMessage("当前有进行中的响应，请等待完成或停止后再删除会话。");
    return;
  }
  if (!confirm(`确定删除会话 ${sid}？`)) return; // G76
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
    addSystemMessage(`删除失败: ${e.message}`);
  }
}

async function refreshSessions() {
  try {
    const sessions = await window.emrg.listSessions();
    renderSessions(sessions);
  } catch { /* 忽略 */ }
}

// ── 设置 ─────────────────────────────────────────────────

async function showSettings() {
  try {
    const s = await window.emrg.getSettings();
    $("set-api-key").value = s.apiKey || "";
    $("set-base-url").value = s.baseUrl || "";
    $("set-project-dir").value = s.projectDir || "";
    const sel = $("set-model");
    sel.innerHTML = "";
    for (const m of s.models || []) {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m;
      if (m === s.model) opt.selected = true;
      sel.appendChild(opt);
    }
    if (!s.models?.length && s.model) {
      const opt = document.createElement("option");
      opt.value = s.model;
      opt.textContent = s.model;
      opt.selected = true;
      sel.appendChild(opt);
    }
  } catch (e) {
    addSystemMessage(`读取设置失败: ${e.message}`);
  }
  $("settings-dialog").showModal();
}

async function saveSettings() {
  const config = {
    apiKey: $("set-api-key").value.trim(),
    baseUrl: $("set-base-url").value.trim(),
    model: $("set-model").value,
    projectDir: $("set-project-dir").value.trim(),
  };
  if (!config.apiKey) {
    alert("API Key 必填"); // G76
    return;
  }
  try {
    const res = await window.emrg.saveSettings(config);
    if ($("settings-dialog").open) $("settings-dialog").close(); // form method=dialog 可能已关
    addSystemMessage("设置已保存。");
    state.apiKeyConfigured = true;
    state.projectDir = config.projectDir || state.projectDir;
    await boot();
  } catch (e) {
    addSystemMessage(`保存失败: ${e.message}`);
  }
}

// ── 首启引导（G36/G71/G82/G116/G117）──────────────────

function showWelcome() {
  $("welcome-project-dir").value = ""; // 跳过 → 默认 ~/.emrg/evolution（G82）
  $("welcome-api-key").value = "";
  $("welcome-base-url").value = "";
  $("welcome-model").innerHTML = "<option value=''>加载中…</option>";
  $("welcome-dialog").showModal();
  // 尝试预加载模型列表（config 缺失时 getSettings 返回默认值 G52）
  window.emrg.getSettings().then((s) => {
    const sel = $("welcome-model");
    sel.innerHTML = "";
    const models = s.models?.length ? s.models : ["deepseek-chat", "deepseek-v3", "gpt-4o"];
    for (const m of models) {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m;
      sel.appendChild(opt);
    }
  });
}

async function saveWelcome() {
  const key = $("welcome-api-key").value.trim();
  if (!key) {
    alert("API Key 必填");
    return;
  }
  const config = {
    apiKey: key,
    baseUrl: $("welcome-base-url").value.trim(),
    model: $("welcome-model").value,
    projectDir: $("welcome-project-dir").value.trim() || "",
  };
  try {
    await window.emrg.saveSettings(config);
    if ($("welcome-dialog").open) $("welcome-dialog").close();
    addSystemMessage("初始设置完成，正在启动 daemon…");
    state.apiKeyConfigured = true;
    state.projectDir = config.projectDir || state.projectDir;
    await boot();
  } catch (e) {
    addSystemMessage(`初始化失败: ${e.message}`);
  }
}

// ── 渲染 ─────────────────────────────────────────────────

function addUserMessage(text) {
  const node = document.createElement("div");
  node.className = "msg user";
  node.textContent = text;
  appendMsg(node);
  return node;
}

function addAssistantMessage(text) {
  const node = document.createElement("div");
  node.className = "msg assistant";
  node.textContent = text || "";
  appendMsg(node);
  return node;
}

function addSystemMessage(text) {
  const node = document.createElement("div");
  node.className = "msg system";
  node.textContent = text;
  appendMsg(node);
}

function appendMsg(node) {
  $("chat-view").appendChild(node);
  scrollToBottom();
}

function clearChat() {
  $("chat-view").innerHTML = "";
  state.groupNodes.clear();
  state.toolCards.clear();
}

function scrollToBottom() {
  if (state.autoScroll) {
    const cv = $("chat-view");
    cv.scrollTop = cv.scrollHeight;
  }
}

function renderSessions(sessions) {
  state.sessions = sessions || [];
  const list = $("session-list");
  list.innerHTML = "";
  if (!state.sessions.length) {
    list.innerHTML = "<div class='session-item placeholder'>暂无会话</div>";
    return;
  }
  for (const s of state.sessions) {
    const item = document.createElement("div");
    item.className = "session-item";
    const title = s.title || s.session_id; // G27：title 优先 session_id 兜底
    const count = s.message_count || 0;
    item.innerHTML = `<span class="sess-title">${escapeHtml(title)}</span><span class="sess-count">${count} msgs</span>`;
    item.addEventListener("click", () => switchSession(s.session_id));
    item.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      deleteSession(s.session_id);
    });
    list.appendChild(item);
  }
  highlightActiveSession(state.sessionId);
}

function highlightActiveSession(sid) {
  for (const item of $("session-list").children) {
    item.classList.toggle("active", item.textContent.includes(sid) && item.classList.contains("session-item"));
  }
}

function updateStatusBar(init) {
  $("daemon-dot").className = "dot " + (init.config_exists && init.api_key_configured ? "green" : "gray");
  $("server-id").textContent = init.server_id || "—";
  $("model-name").textContent = init.model || "—";
  $("project-name").textContent = init.project_dir ? `📁 ${init.project_dir}` : "";
}

function setComposerDisabled(disabled) {
  $("input").disabled = disabled;
  $("send-btn").disabled = disabled;
  $("stop-btn").style.display = disabled ? "inline-block" : "none";
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ── 事件处理（main 已分类）───────────────────────────────

async function handleEvent(evt) {
  const { type, data } = evt;
  switch (type) {
    case "message_delta":
      // G122：main 已批量（chunks 数组）
      handleDelta(data.chunks || [data]);
      break;
    case "done":
      handleDone(data);
      break;
    case "tool_started":
      handleToolStart(data);
      break;
    case "tool_finished":
      handleToolEnd(data);
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
      $("server-id").textContent = state.serverId || "—";
      $("model-name").textContent = state.model || "—";
      updateEvolutionCount(data.evolution_count);
      break;
    case "status":
      handleStatus(data);
      break;
    case "sessions":
      renderSessions(data.sessions || []);
      break;
    case "disconnected":
      $("daemon-dot").className = "dot red";
      addSystemMessage("⏸ 与 daemon 的连接已断开，重连中…");
      // G89：断连时恢复输入条（流式中断连 → busy 永久卡死；不能依赖 30s 超时兜底）
      state.busy = false;
      state.ownStreamRequestId = null;
      setComposerDisabled(false);
      break;
    case "group_cleared":
      // DOM 保留，仅清缓存
      state.groupNodes.delete(data.requestId);
      break;
    case "list_result":
      if (data.type === "sessions_list") renderSessions(data.sessions || []);
      break;
    case "command_result":
      if (data.type === "model_set") {
        state.model = data.model || state.model;
        $("model-name").textContent = state.model;
      }
      break;
    default:
      // 忽略未知
      break;
  }
}

function handleDelta(chunks) {
  // G3：广播分组——新 request_id 建新节点并标注「来自其他客户端」
  for (const chunk of chunks) {
    const rid = chunk.request_id;
    if (!rid) continue;
    let node = state.groupNodes.get(rid);
    if (!node) {
      const isOwn = state.ownStreamRequestId === rid;
      node = document.createElement("div");
      node.className = "msg assistant";
      if (!isOwn) node.classList.add("remote");
      const label = document.createElement("div");
      label.className = "remote-label";
      label.textContent = isOwn ? "" : "（来自其他客户端）";
      if (!isOwn) node.appendChild(label);
      const body = document.createElement("div");
      body.className = "msg-body";
      node.appendChild(body);
      appendMsg(node);
      state.groupNodes.set(rid, node);
    }
    const body = node.querySelector(".msg-body") || node;
    body.textContent += chunk.content || "";
    scrollToBottom();
  }
}

function handleDone(data) {
  const rid = data.request_id;
  // 找到分组节点 → 整体 marked 渲染（G127：requestIdleCallback 调度）
  const node = state.groupNodes.get(rid);
  if (node) {
    const body = node.querySelector(".msg-body") || node;
    const text = body.textContent;
    const render = () => {
      window.emrgMarkdown.renderMarkdown(text).then((html) => {
        body.innerHTML = html;
        scrollToBottom();
      });
    };
    if (window.requestIdleCallback) {
      window.requestIdleCallback(render, { timeout: 2000 });
    } else {
      render(); // rIC 不可用直接同步（G91 200KB 上限兜底）
    }
    state.groupNodes.delete(rid);
  }
  if (data.timeout) {
    addSystemMessage("⚠️ 响应超时（daemon 未返回完成帧）。");
  }
  if (state.ownStreamRequestId === rid || data.timeout) {
    state.busy = false;
    state.ownStreamRequestId = null;
    setComposerDisabled(false);
  }
}

function handleToolStart(data) {
  const rid = data.request_id;
  if (rid && !state.groupNodes.has(rid)) {
    // G104：tool_start 也建组（LLM 先出 tool_calls 后出文本）——与 handleDelta 一致，广播流标 remote
    const isOwn = state.ownStreamRequestId === rid;
    const node = document.createElement("div");
    node.className = "msg assistant";
    if (!isOwn) node.classList.add("remote");
    const label = document.createElement("div");
    label.className = "remote-label";
    label.textContent = isOwn ? "" : "（来自其他客户端）";
    if (!isOwn) node.appendChild(label);
    const body = document.createElement("div");
    body.className = "msg-body";
    node.appendChild(body);
    appendMsg(node);
    state.groupNodes.set(rid, node);
  }
  const card = document.createElement("div");
  card.className = "tool-card running";
  card.innerHTML = `<span class="tool-icon">🔧</span> <span class="tool-name">${escapeHtml(data.tool_name)}</span> <span class="tool-status">运行中…</span>`;
  appendMsg(card);
  state.toolCards.set(data.tool_call_id, card);
}

function handleToolEnd(data) {
  const card = state.toolCards.get(data.tool_call_id);
  if (!card) return;
  const ok = !data.error;
  card.className = "tool-card " + (ok ? "done" : "failed");
  const elapsed = data.elapsed !== undefined ? `${data.elapsed.toFixed(1)}s` : "";
  card.querySelector(".tool-status").textContent = ok ? `完成 ${elapsed}` : `失败 ${elapsed}`;
  // G91/G131：content 默认截断 2000 字符 + 展开（textContent 纯文本，不做 marked）
  const content = data.content || "";
  if (content) {
    const truncated = content.length > 2000 ? content.slice(0, 2000) + "…" : content;
    const body = document.createElement("div");
    body.className = "tool-output";
    body.textContent = truncated;
    card.appendChild(body);
    if (content.length > 2000) {
      const btn = document.createElement("button");
      btn.className = "tool-expand";
      btn.textContent = "展开全文";
      btn.addEventListener("click", () => {
        body.textContent = content;
        btn.remove();
      });
      card.appendChild(btn);
    }
  }
  scrollToBottom();
}

function handleError(data) {
  // G42：session busy（带 session_id G128）/ 普通错误
  if (data.error && String(data.error).includes("session busy")) {
    addSystemMessage(`⚠️ 会话正忙（${data.session_id || state.sessionId}）——请等待当前响应完成。`);
    state.busy = false;
    state.ownStreamRequestId = null;
    setComposerDisabled(false);
  } else {
    addSystemMessage(`错误: ${data.error || "未知错误"}`);
  }
}

function handleStatus(data) {
  if (data.connected) {
    $("daemon-dot").className = "dot green";
    if (data.server_id) $("server-id").textContent = data.server_id;
    if (data.model) $("model-name").textContent = data.model;
    updateEvolutionCount(data.evolution_count);
    addSystemMessage("✓ 已重新连接");
  } else if (data.auth_failed) {
    $("daemon-dot").className = "dot red";
    addSystemMessage("⚠️ 认证失败——请检查 ~/.emrg 配置，手动重试。");
  } else {
    $("daemon-dot").className = "dot red";
  }
}

// G19：状态栏演化计数（pong evolution_count / init / status）
function updateEvolutionCount(count) {
  const el = $("evolution-count");
  if (!el) return;
  el.textContent = count !== undefined && count !== null ? `演化 ${count} 次` : "";
}

// ── 启动 ─────────────────────────────────────────────────

bindUi(); // 模块级只绑定一次（boot 可被 saveSettings/saveWelcome 重复调用，防重复 listener）
window.emrg.onEvent(handleEvent); // 模块级只注册一次（防 ipcRenderer listener 泄漏 → delta 重复渲染）
boot();
