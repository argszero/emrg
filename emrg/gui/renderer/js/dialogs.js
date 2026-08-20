"use strict";
/**
 * dialogs.js — 设置 / 首启引导 / 确认对话框。
 * - 设置：模型服务（API Key/接口地址/可用模型增删改设默认）、工作目录、外观（主题三选）
 * - 多模型管理：可用模型列表（默认项 + [[llm.models]] 全部条目），＋添加/行内编辑/删除/单选设默认
 * - 首启：品牌欢迎语 + 口语化文案
 * - 确认：替代 confirm()（友好文案，不显示 session ID）
 */

const Dialogs = (() => {
  // ── 主题 ─────────────────────────────────
  let theme = "system";

  function renderThemeOptions() {
    const wrap = $("theme-options");
    if (!wrap) return;
    for (const btn of wrap.querySelectorAll(".theme-option")) {
      btn.classList.toggle("active", btn.dataset.theme === theme);
    }
  }

  function initThemeButtons() {
    const wrap = $("theme-options");
    if (!wrap) return;
    wrap.addEventListener("click", (e) => {
      const btn = e.target.closest(".theme-option");
      if (!btn) return;
      theme = btn.dataset.theme || "system";
      applyTheme(theme);
      syncTemplateEditorTheme(); // Monaco vs/vs-dark 跟随（rant 09:17:45 验收 6）
      renderThemeOptions();
    });
  }

  // ── 语言（rant 21:19：i18n 手动覆盖，即时生效）──
  function renderLangOptions() {
    const wrap = $("lang-options");
    if (!wrap) return;
    // 原始 localStorage 值："" = 跟随系统（高亮第一项）
    let saved = "";
    try {
      if (typeof localStorage !== "undefined") saved = localStorage.getItem("emrg.locale") || "";
    } catch { /* ignore */ }
    for (const btn of wrap.querySelectorAll(".theme-option")) {
      btn.classList.toggle("active", (btn.dataset.lang || "") === saved);
    }
  }

  function initLangButtons() {
    const wrap = $("lang-options");
    if (!wrap) return;
    wrap.addEventListener("click", (e) => {
      const btn = e.target.closest(".theme-option");
      if (!btn) return;
      const loc = btn.dataset.lang || "";
      if (window.EMRG_I18N) window.EMRG_I18N.setLocale(loc); // 立即重刷全界面
      renderLangOptions();
    });
  }

  // ── 多模型管理 ───────────────────────────
  // defaultName：当前默认模型（llm.model）；extraModels：[[llm.models]] 条目（含与默认同名的配置项）
  let defaultName = "";
  let extraModels = [];

  function renderModelList() {
    const list = $("model-list");
    list.innerHTML = "";
    // 默认行（vision/modelId 从同名 extraModels 条目取，编辑默认模型时保留）
    const def = extraModels.find((m) => m.name === defaultName);
    list.appendChild(
      renderModelItem(
        defaultName || _t("dlg.notConfigured"),
        true,
        Boolean(def?.vision),
        def?.model && def.model !== def.name ? def.model : ""
      )
    );
    // 额外模型行（去重：跳过与默认同名的——默认行已展示）
    const seen = new Set([defaultName]);
    for (const m of extraModels) {
      if (seen.has(m.name)) continue;
      seen.add(m.name);
      list.appendChild(
        renderModelItem(m.name, false, Boolean(m.vision), m.model && m.model !== m.name ? m.model : "")
      );
    }
    if (extraModels.filter((m) => m.name !== defaultName).length === 0) {
      const hint = el("div", { class: "model-list-empty" }, _t("dlg.noOtherModels"));
      list.appendChild(hint);
    }
  }

  function renderModelItem(name, isDefault, vision, modelId) {
    const row = el("div", { class: "model-item" + (isDefault ? " default" : "") });
    // 单选圆点 → 设为默认
    const radio = el("button", { class: "model-radio" + (isDefault ? " checked" : ""), title: isDefault ? _t("dlg.currentDefault") : _t("dlg.setDefault") });
    if (isDefault) {
      radio.textContent = "●";
    } else {
      radio.textContent = "○";
      radio.addEventListener("click", () => {
        defaultName = name;
        renderModelList();
      });
    }
    row.appendChild(radio);
    const title = el("span", { class: "model-name" }, name);
    if (isDefault) title.appendChild(el("span", { class: "model-badge" }, _t("dlg.defaultBadge")));
    row.appendChild(title);
    if (modelId) row.appendChild(el("span", { class: "model-id" }, modelId));
    if (vision) row.appendChild(el("span", { class: "model-vision" }, _t("dlg.supportsImages")));
    // 操作：编辑（非默认行 → 打开表单填本条）；删除（默认不可删）
    const actions = el("span", { class: "model-actions" });
    const editBtn = el("button", { class: "model-action-btn", title: _t("dlg.edit") }, _t("dlg.edit"));
    editBtn.addEventListener("click", () => openModelForm(name, isDefault));
    actions.appendChild(editBtn);
    if (!isDefault) {
      const delBtn = el("button", { class: "model-action-btn danger", title: _t("dlg.delete") }, _t("dlg.delete"));
      delBtn.addEventListener("click", () => {
        showConfirm(_t("dlg.deleteModelTitle"), _t("dlg.deleteModelBody", { name }), {
          okText: _t("dlg.delete"),
          danger: true,
          onOk: () => {
            extraModels = extraModels.filter((m) => m.name !== name);
            renderModelList();
          },
        });
      });
      actions.appendChild(delBtn);
    }
    row.appendChild(actions);
    return row;
  }

  // 行内表单：添加（无参）或编辑（name 非空）
  let editingName = null;
  function openModelForm(name, isDefault = false) {
    editingName = isDefault ? (name || null) : name;
    $("model-form-name").value = name || "";
    // 编辑时预填 model/vision
    const found = extraModels.find((m) => m.name === name);
    $("model-form-id").value = found?.model && found.model !== found.name ? found.model : "";
    $("model-form-vision").checked = Boolean(found?.vision);
    $("model-form").classList.remove("hidden");
    $("model-form-name").focus();
  }

  function closeModelForm() {
    $("model-form").classList.add("hidden");
    editingName = null;
    $("model-form-name").value = "";
    $("model-form-id").value = "";
    $("model-form-vision").checked = false;
  }

  function saveModelForm() {
    const name = $("model-form-name").value.trim();
    if (!name) {
      showConfirm(_t("dlg.stepTitle"), _t("dlg.nameRequiredBody"), { okText: _t("dlg.gotIt"), danger: false });
      return;
    }
    const modelId = $("model-form-id").value.trim() || name;
    const vision = $("model-form-vision").checked;

    if (editingName === null) {
      // 添加新模型
      if (name === defaultName || extraModels.some((m) => m.name === name)) {
        showConfirm(_t("dlg.duplicateTitle"), _t("dlg.duplicateBody"), { okText: _t("dlg.gotIt"), danger: false });
        return;
      }
      extraModels.push({ name, model: modelId, vision });
    } else if (editingName === defaultName) {
      // 编辑默认模型：更新默认名 + 其配置项（保留在 extraModels 中）
      const prev = extraModels.find((m) => m.name === editingName);
      extraModels = extraModels.filter((m) => m.name !== editingName);
      extraModels.push({ name, model: modelId, vision, ...(prev ? {} : {}) });
      defaultName = name;
    } else {
      // 编辑非默认模型
      if (name === defaultName) {
        showConfirm(_t("dlg.duplicateTitle"), _t("dlg.duplicateDefaultBody"), { okText: _t("dlg.gotIt"), danger: false });
        return;
      }
      if (name !== editingName && extraModels.some((m) => m.name === name)) {
        showConfirm(_t("dlg.duplicateTitle"), _t("dlg.duplicateBody"), { okText: _t("dlg.gotIt"), danger: false });
        return;
      }
      extraModels = extraModels.map((m) => (m.name === editingName ? { name, model: modelId, vision } : m));
    }
    closeModelForm();
    renderModelList();
  }

  function initModelForm() {
    $("add-model-btn").addEventListener("click", () => openModelForm(""));
    $("model-form-save").addEventListener("click", saveModelForm);
    $("model-form-cancel").addEventListener("click", closeModelForm);
    // 键盘：Enter 保存 / ESC 取消（与重命名对话框一致的交互）
    $("model-form-name").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); saveModelForm(); }
      else if (e.key === "Escape") { e.preventDefault(); closeModelForm(); }
    });
    $("model-form-id").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); saveModelForm(); }
      else if (e.key === "Escape") { e.preventDefault(); closeModelForm(); }
    });
  }

  // ── 设置 ─────────────────────────────────
  async function showSettings() {
    try {
      const s = await window.emrg.getSettings();
      $("set-api-key").value = s.apiKey || "";
      $("set-base-url").value = s.baseUrl || "";
      if (s.theme) theme = s.theme;
      renderThemeOptions();
      renderLangOptions(); // rant 21:19：语言选择器高亮当前值
      defaultName = s.model || "";
      extraModels = (s.modelDetails || []).map((m) => ({
        name: m.name || "",
        model: m.model || m.name || "",
        vision: Boolean(m.vision),
      }));
      renderModelList();
      // WorkBuddy P3：关于区（版本 + 进化计数）随设置面板打开时刷新
      try {
        const ver = $("about-version");
        if (ver) ver.textContent = `v${App.state.version || "0.2.8"}`;
        const evo = $("about-evolutions");
        if (evo) evo.textContent = `🌱 ${_t("copy.growthCountPrefix")} ${App.state.evolutionCount ?? 0} ${_t("copy.times")}`;
      } catch { /* 元素缺失（测试桩）时忽略 */ }
      // Windows GCM rant Stage 2：GitHub 连接状态随设置面板打开时刷新
      await refreshGithubStatus();
    } catch (e) {
      Chat.addSystemMessage(_t("settings.readFailed", { msg: e.message }));
    }
    // rant 18:55:09 v0.2：设置改为工作区视图（不再弹窗）；已在视图内（nav 打开后刷新）则不再 toggle
    if (App.state.activeView !== "settings") App.switchView("settings");
  }

  async function saveSettings() {
    const config = {
      apiKey: $("set-api-key").value.trim(),
      baseUrl: $("set-base-url").value.trim(),
      model: defaultName,
      theme,
      models: extraModels,
    };
    if (!config.apiKey) {
      showConfirm(_t("dlg.stepTitle"), _t("app.authKeyRequired"), {
        okText: _t("dlg.gotIt"),
        danger: false,
      });
      return;
    }
    try {
      await window.emrg.saveSettings(config);
      Chat.addSystemMessage(_t("dlg.saved"));
      App.state.apiKeyConfigured = true;
      App.state.model = defaultName;
      App.updateModelSwitcher();
      await App.boot();
    } catch (e) {
      Chat.addSystemMessage(_t("settings.saveFailed", { msg: e.message }));
    }
  }

  // ── 首启引导 ─────────────────────────────
  function showWelcome() {
    $("welcome-api-key").value = "";
    $("welcome-base-url").value = "";
    $("welcome-model").innerHTML = `<option value="">${_t("dlg.loading")}</option>`;
    $("welcome-dialog").showModal();
    window.emrg.getSettings().then((s) => {
      const sel = $("welcome-model");
      sel.innerHTML = "";
      const models = s.modelDetails?.length
        ? s.modelDetails.map((m) => m.name)
        : s.models?.length
          ? s.models
          : ["deepseek-chat", "deepseek-v3", "gpt-4o"];
      for (const m of models) {
        const opt = el("option", { value: m }, m);
        if (m === s.model) opt.selected = true; // G144
        sel.appendChild(opt);
      }
      if (sel.selectedIndex < 0) sel.selectedIndex = 0;
    });
  }

  async function saveWelcome() {
    const key = $("welcome-api-key").value.trim();
    if (!key) {
      showConfirm(_t("dlg.stepTitle"), _t("app.authKeyRequired"), {
        okText: _t("dlg.gotIt"),
        danger: false,
      });
      return;
    }
    const config = {
      apiKey: key,
      baseUrl: $("welcome-base-url").value.trim(),
      model: $("welcome-model").value || "deepseek-chat",
    };
    try {
      await window.emrg.saveSettings(config);
      if ($("welcome-dialog").open) $("welcome-dialog").close();
      Chat.addSystemMessage(_t("dlg.starting"));
      App.state.apiKeyConfigured = true;
      await App.boot();
    } catch (e) {
      Chat.addSystemMessage(_t("dlg.initFailed", { msg: e.message }));
    }
  }

  // ── 重命名对话（右键菜单 → 重命名） ────
  let renameSid = null;
  function showRename(sid, currentTitle) {
    renameSid = sid;
    const input = $("rename-input");
    input.value = (currentTitle && currentTitle !== sid) ? currentTitle : "";
    $("rename-dialog").showModal();
    input.focus();
    input.select();
  }

  async function submitRename() {
    if (!renameSid) return;
    const input = $("rename-input");
    const title = input.value.trim();
    if (!title) return; // 空名不提交
    try {
      await window.emrg.renameSession({ sessionId: renameSid, title });
      renameSid = null;
      $("rename-dialog").close();
      await App.refreshSessions();
    } catch (e) {
      Chat.addSystemMessage(_t("dlg.renameFailed", { msg: e.message }));
    }
  }

  function initRenameDialog() {
    $("rename-cancel").addEventListener("click", () => {
      renameSid = null;
      $("rename-dialog").close();
    });
    $("rename-ok").addEventListener("click", submitRename);
    $("rename-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submitRename();
      }
    });
  }

  // ── GitHub 连接（Windows GCM rant Stage 2：设置页授权） ────
  function initGithubSection() {
    $("github-connect-btn").addEventListener("click", async () => {
      const token = $("set-github-token").value.trim();
      if (!token) {
        // 首选 device flow（不开终端、不弹 GCM）；PAT 为受限环境兜底
        await startDeviceFlow();
        return;
      }
      $("github-connect-btn").disabled = true;
      $("github-connect-btn").textContent = _t("settings.githubConnecting");
      try {
        const res = await window.emrg.githubConnect({ token });
        if (res && res.ok) {
          $("set-github-token").value = "";
          Chat.addSystemMessage(_t("settings.githubConnected", { user: res.user || "" }));
          await refreshGithubStatus();
        } else {
          Chat.addSystemMessage(_t("settings.githubConnectFailed", { msg: (res && res.error) || _t("app.unknownError") }));
        }
      } catch (e) {
        Chat.addSystemMessage(_t("settings.githubConnectFailed", { msg: e.message }));
      } finally {
        $("github-connect-btn").disabled = false;
        $("github-connect-btn").textContent = _t("settings.githubConnect");
      }
    });
    $("github-disconnect-btn").addEventListener("click", async () => {
      try {
        const res = await window.emrg.githubDisconnect();
        if (res && res.ok) {
          Chat.addSystemMessage(_t("settings.githubDisconnected"));
          await refreshGithubStatus();
        } else {
          Chat.addSystemMessage(_t("settings.githubDisconnectFailed", { msg: (res && res.error) || _t("app.unknownError") }));
        }
      } catch (e) {
        Chat.addSystemMessage(_t("settings.githubDisconnectFailed", { msg: e.message }));
      }
    });
  }

  // ── GitHub device flow（Stage 2b：gh auth login --web） ────
  let _devicePollTimer = null;
  function stopDevicePolling() {
    if (_devicePollTimer) {
      clearInterval(_devicePollTimer);
      _devicePollTimer = null;
    }
  }

  async function startDeviceFlow() {
    const dlg = $("github-device-dialog");
    const codeEl = $("github-device-code");
    if (!dlg || !codeEl) return; // 元素缺失（测试桩）时忽略
    stopDevicePolling();
    codeEl.textContent = "…";
    dlg.showModal();
    try {
      const res = await window.emrg.githubConnectWeb();
      if (!res || !res.ok) {
        dlg.close();
        Chat.addSystemMessage(_t("settings.githubDeviceFailed", { msg: (res && res.error) || _t("app.unknownError") }));
        return;
      }
      if (res.code && res.url) {
        codeEl.textContent = res.code;
        const openBtn = $("github-device-open");
        if (openBtn) {
          openBtn.onclick = () => window.emrg.openExternal({ url: res.url });
        }
      } else {
        // already authenticated
        dlg.close();
        Chat.addSystemMessage(_t("settings.githubConnected", { user: res.user || "" }));
        await refreshGithubStatus();
        return;
      }
    } catch (e) {
      dlg.close();
      Chat.addSystemMessage(_t("settings.githubDeviceFailed", { msg: e.message }));
      return;
    }
    // 轮询 github_status 直到授权完成（daemon 侧 300s 超时兜底）
    _devicePollTimer = setInterval(async () => {
      try {
        const s = await window.emrg.githubStatus();
        if (s && s.authenticated) {
          stopDevicePolling();
          if (dlg.open) dlg.close();
          Chat.addSystemMessage(_t("settings.githubConnected", { user: s.user || "" }));
          await refreshGithubStatus();
        }
      } catch { /* 网络抖动忽略，下一轮再试 */ }
    }, 3000);
  }

  function initDeviceDialog() {
    const dlg = $("github-device-dialog");
    if (!dlg) return;
    $("github-device-close").addEventListener("click", () => {
      stopDevicePolling();
      dlg.close();
    });
  }

  async function refreshGithubStatus() {
    const statusEl = $("github-status");
    const authRow = $("github-auth-row");
    if (!statusEl || !authRow) return; // 元素缺失（测试桩）时忽略
    statusEl.textContent = _t("settings.githubChecking");
    try {
      const s = await window.emrg.githubStatus();
      const connected = Boolean(s && s.authenticated);
      const user = (s && s.user) || "";
      statusEl.textContent = connected
        ? _t("settings.githubConnectedStatus", { user })
        : _t("settings.githubNotConnected");
      $("github-disconnect-btn").classList.toggle("hidden", !connected);
      $("set-github-token").classList.toggle("hidden", connected);
      $("github-connect-btn").classList.toggle("hidden", connected);
    } catch {
      statusEl.textContent = _t("settings.githubStatusFailed");
    }
  }


  // ── 定时任务管理（rant 2026-08-12T18:23:15 P3：GUI 任务/自定义类型 CRUD） ──
  // 决策点执行：①内置类型只读 ②被任务引用的自定义类型拒绝删除 ③项目仅限已注册
  // ④热重载（daemon apply_tasks）⑤间隔 ≥60 ⑥内置模板只读。
  let taskTypes = []; // [{name, builtin}] — 类型下拉选项（内置 + 自定义）
  let taskProjects = []; // listProjects 结果 — 项目下拉仅已注册项目（决策点③）
  let editingTask = null; // 正在编辑的任务名（null = 新增；daemon 以 name 定位 → 名称不可改）
  let editingTemplate = null; // 正在编辑的自定义类型名（null = 新增；名称不可改）
  // rant 2026-08-15T10:36:39：任务状态 + 下次运行倒计时
  // 倒计时以"渲染时快照的 deadline"为基准每秒递减（直接改 DOM 文本，不重渲染整行防闪烁/滚动丢失）
  let taskCountdownTimer = null; // setInterval id（面板激活时启动，离开视图时清除防泄漏）
  let taskCountdowns = []; // [{name, deadline, span}] — 每 1s 由 updateTaskCountdowns 更新

  // 倒计时格式化：≤60s "43s"；≤1h "1m23s"；>1h "1h05m"；负数/非数钳制为 0
  function formatCountdown(totalSeconds) {
    const s = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    if (s < 60) return `${s}s`;
    if (s < 3600) {
      const m = Math.floor(s / 60);
      const sec = s % 60;
      return `${m}m${String(sec).padStart(2, "0")}s`;
    }
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return `${h}h${String(m).padStart(2, "0")}m`;
  }

  // rant 2026-08-18T10:45:52：相对时间显示（"5m ago" / "3h ago" / 日期）——
  // 用于任务行"上次运行"元信息。ISO 时间戳解析失败时回退为原文。
  function formatRelativeTime(isoStr) {
    if (!isoStr) return "";
    const t = Date.parse(isoStr);
    if (Number.isNaN(t)) return String(isoStr);
    const diffSec = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (diffSec < 60) return `${diffSec}s ago`;
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
    return `${Math.floor(diffSec / 86400)}d ago`;
  }

  function updateTaskCountdowns() {
    let expired = false;
    for (const e of taskCountdowns) {
      const rem = Math.max(0, Math.ceil((e.deadline - Date.now()) / 1000));
      e.span.textContent = _t("app.taskNextRun", { n: formatCountdown(rem) });
      // rant 2026-08-18T11:16:32：倒计时归零后任务状态不会自动更新（pending → running 永不反映到 UI）。
      // 检测到 deadline 已过 → 重新拉取任务状态；e.expired 防止同一任务重复触发。
      if (rem <= 0 && !e.expired) {
        e.expired = true;
        expired = true;
      }
    }
    if (expired) {
      renderTaskList().catch(() => {});
    }
  }

  function stopTaskCountdown() {
    if (taskCountdownTimer !== null) {
      clearInterval(taskCountdownTimer);
      taskCountdownTimer = null;
    }
    taskCountdowns = [];
  }

  function startTaskCountdown() {
    // 幂等重启：只清旧计时器，不清 taskCountdowns（stopTaskCountdown 会清引用，供离开视图用）
    if (taskCountdownTimer !== null) {
      clearInterval(taskCountdownTimer);
      taskCountdownTimer = null;
    }
    if (taskCountdowns.length === 0) return;
    taskCountdownTimer = setInterval(updateTaskCountdowns, 1000);
  }

  async function loadTaskMeta() {
    try {
      const templates = await window.emrg.taskTemplateList();
      taskTypes = Array.isArray(templates)
        ? templates.map((t) => ({ name: t.name, builtin: Boolean(t.builtin) }))
        : [];
    } catch { taskTypes = []; }
    try {
      const projects = await window.emrg.listProjects();
      taskProjects = Array.isArray(projects) ? projects : [];
    } catch { taskProjects = []; }
  }

  // rant 2026-08-18T21:32:32：任务卡点击 → 手风琴展开最近运行子表
  // （时间 / 干了什么 / 降频）。数据来自 daemon handler.status().recent_runs
  // （最多 5 条）。Rant 2026-08-19T07:06:45（宿主定稿）：无 summary 不再
  // fallback impact 机器串——空则显示 "-"；无记录 → 占位文案。
  function buildTaskRunDetail(t) {
    const wrap = el("div", { class: "task-run-detail hidden" });
    const runs = Array.isArray(t.recent_runs) ? t.recent_runs : [];
    if (runs.length === 0) {
      wrap.appendChild(el("div", { class: "task-run-empty" }, _t("app.taskRunsEmpty")));
      return wrap;
    }
    const head = el("div", { class: "task-run-head" });
    head.appendChild(el("span", {}, _t("app.taskRunsColTime")));
    head.appendChild(el("span", {}, _t("app.taskRunsColDone")));
    head.appendChild(el("span", {}, _t("app.taskRunsColThrottle")));
    head.appendChild(el("span", {}, _t("app.taskRunsColReason")));
    wrap.appendChild(head);
    for (const r of runs) {
      const row = el("div", { class: "task-run-row" });
      row.appendChild(el("span", { class: "task-run-time" }, formatRelativeTime(r.timestamp)));
      // rant 2026-08-20T10:58:55：字段统一 work（原 summary/done）
      let done = (typeof r.work === "string" && r.work) ? r.work : "";
      row.appendChild(el("span", { class: "task-run-done" }, done || "-"));
      const flagCell = el("span", { class: "task-run-flag" });
      if (r.recommend_slowdown) {
        flagCell.appendChild(el("span", { class: "task-badge task-run-badge-warn" }, _t("app.taskRunThrottle")));
      }
      row.appendChild(flagCell);
      // rant 2026-08-19T18:25:14：原因列 —— 降频判断的自然语言理由（vibe check
      // slowdown_reason，rant 2026-08-20T10:58:55 改名）。
      let reason = (typeof r.slowdown_reason === "string" && r.slowdown_reason) ? r.slowdown_reason : "";
      row.appendChild(el("span", { class: "task-run-reason" }, reason || "-"));
      wrap.appendChild(row);
    }
    return wrap;
  }

  async function renderTaskList() {
    const list = $("task-list");
    if (!list) return; // 元素缺失（测试桩）时忽略
    stopTaskCountdown(); // 清旧计时器 + 旧倒计时引用（空列表/失败路径也不会泄漏）
    list.innerHTML = "";
    let tasks = [];
    try {
      tasks = await window.emrg.listTasks();
    } catch (e) {
      list.innerHTML = `<div class="task-empty">${_t("settings.taskEmpty")}</div>`;
      return;
    }
    if (!tasks || tasks.length === 0) {
      list.innerHTML = `<div class="task-empty">${_t("settings.taskEmpty")}</div>`;
      return;
    }
    const countdowns = []; // 本地收集 → 渲染完成后一次性挂载（startTaskCountdown 幂等重启）
    for (const t of tasks) {
      const row = el("div", { class: "task-row" });
      row.appendChild(el("span", { class: "task-name" }, t.name || "?"));
      row.appendChild(el("span", { class: "task-badge" }, t.type || "evolution"));
      // rant 09:23:10：running 状态徽标（演化任务 60s 一轮几乎常驻 running，从源头减少误点）
      // rant 10:36:39：等待中 → "待运行"淡色徽标 + 下次运行倒计时；无 next 且启用 → "待调度"
      if (t.running) {
        const runBadge = el("span", { class: "task-badge task-running-badge" }, _t("app.taskRunningBadge"));
        row.appendChild(runBadge);
      } else if (t.next_run_in_seconds != null) {
        row.appendChild(el("span", { class: "task-badge task-pending-badge" }, _t("app.taskPendingBadge")));
        const nextSpan = el("span", { class: "task-next-run" }, _t("app.taskNextRun", { n: formatCountdown(t.next_run_in_seconds) }));
        row.appendChild(nextSpan);
        countdowns.push({ name: t.name, deadline: Date.now() + Math.max(0, t.next_run_in_seconds) * 1000, span: nextSpan });
      } else if (t.enabled !== false) {
        row.appendChild(el("span", { class: "task-badge task-idle-badge" }, _t("app.taskIdleBadge")));
      }
      const cfg = (t.config && typeof t.config === "object") ? t.config : {};
      const hints = [];
      if (cfg.project) hints.push(cfg.project);
      if (t.interval != null) hints.push(_t("app.taskInterval", { n: t.interval ?? "-" }));
      if (t.enabled === false) hints.push(_t("app.taskDisabled"));
      row.appendChild(el("span", { class: "task-hint" }, hints.join(" · ")));
      // rant 2026-08-18T10:45:52：上次执行维度 —— 最近一次运行时间 + 降频（saturation）标识。
      // rant 2026-08-19T18:25:14：一级列表不再显示"干了什么"（last_cycle_summary）——
      // 执行记录（时间/工作/降频建议/原因）统一由点击展开的二级列表（buildTaskRunDetail）呈现。
      // 数据来自 daemon list_tasks → handler.status()（last_run_at/saturation/recent_runs）。
      const meta = el("div", { class: "task-meta" });
      if (t.last_run_at) {
        const runTxt = el("span", { class: "task-meta-item" }, _t("app.taskLastRun", { n: formatRelativeTime(t.last_run_at) }));
        meta.appendChild(runTxt);
      } else {
        meta.appendChild(el("span", { class: "task-meta-item" }, _t("app.taskNoRunYet")));
      }
      // rant 2026-08-20T10:58:55：降频徽标改为只认 heartbeat_active（不再用
      // empty_cycles）—— 文案「已降频 · heartbeat {m}s」（app.taskThrottled）。
      const sat = (t.saturation && typeof t.saturation === "object") ? t.saturation : null;
      if (sat && sat.heartbeat_active) {
        meta.appendChild(el("span", { class: "task-badge task-saturation-badge" }, _t("app.taskThrottled", { m: formatCountdown(sat.heartbeat_interval) })));
      }
      row.appendChild(meta);
      const actions = el("span", { class: "task-actions" });
      // 触发（复用 /trigger 语义；rant 09:23:10：running 时给 info 反馈而非假成功）
      const trigBtn = el("button", { type: "button", class: "model-action-btn", title: _t("settings.taskTrigger") }, _t("settings.taskTrigger"));
      if (t.running) trigBtn.disabled = true;
      trigBtn.addEventListener("click", async () => {
        try {
          const res = await window.emrg.triggerTask({ name: t.name });
          if (res && res.error) {
            Chat.addSystemMessage(_t("app.triggerFailed", { msg: res.error }));
            showToast(_t("app.triggerFailed", { msg: res.error }), { type: "error" });
          } else if (res && res.result === "running") {
            Chat.addSystemMessage(_t("app.taskRunning", { n: t.name }));
            showToast(_t("app.taskRunning", { n: t.name }), { type: "info", durationMs: 4000 });
          } else {
            Chat.addSystemMessage(_t("app.triggered", { n: t.name }));
            showToast(_t("app.triggered", { n: t.name }), { type: "success" });
          }
        } catch (e) {
          Chat.addSystemMessage(_t("app.triggerFailed", { msg: e.message }));
          showToast(_t("app.triggerFailed", { msg: e.message }), { type: "error" });
        }
      });
      actions.appendChild(trigBtn);
      // 编辑（表单预填 → taskUpdate）
      const editBtn = el("button", { type: "button", class: "model-action-btn", title: _t("settings.taskEdit") }, _t("settings.taskEdit"));
      editBtn.addEventListener("click", () => { openTaskForm(t).catch(() => {}); });
      actions.appendChild(editBtn);
      // 删除（确认弹窗 → taskDelete → 热重载由 daemon apply_tasks 完成）
      const delBtn = el("button", { type: "button", class: "model-action-btn danger", title: _t("settings.taskDelete") }, _t("settings.taskDelete"));
      delBtn.addEventListener("click", () => {
        showConfirm(_t("settings.taskDelete"), _t("settings.taskDeleteConfirm", { name: t.name }), {
          okText: _t("settings.taskDelete"),
          danger: true,
          onOk: async () => {
            try {
              await window.emrg.taskDelete({ name: t.name });
              Chat.addSystemMessage(_t("settings.taskDeleted"));
              showToast(_t("settings.taskDeleted"), { type: "success" });
              await renderTaskList();
            } catch (e) {
              Chat.addSystemMessage(_t("app.tasksFailed", { msg: e.message }));
              showToast(_t("app.tasksFailed", { msg: e.message }), { type: "error" });
            }
          },
        });
      });
      actions.appendChild(delBtn);
      row.appendChild(actions);
      // rant 2026-08-18T21:32:32：点击任务卡（非按钮）→ 手风琴展开最近运行子表。
      // 真实 DOM 中按钮点击通过 e.target.closest("button") 拦截（不触发展开）；
      // 测试沙箱的 click() 无 target → 直接切换（沙箱中按钮点击不冒泡，互不影响）。
      const runDetail = buildTaskRunDetail(t);
      row.appendChild(runDetail);
      let runDetailOpen = false;
      row.addEventListener("click", (e) => {
        if (e && e.target && typeof e.target.closest === "function" && e.target.closest("button")) return;
        runDetailOpen = !runDetailOpen;
        runDetail.classList.toggle("hidden", !runDetailOpen);
      });
      list.appendChild(row);
    }
    taskCountdowns = countdowns;
    startTaskCountdown(); // rant 10:36:39：面板激活即启动 1s 倒计时（离开视图由 app.js stopTaskCountdown）
  }

  async function openTaskForm(task = null) {
    // rant 2026-08-14T15:41:52：点"＋ 添加任务"时元数据未加载完 → 下拉为空 → 保存报 invalid type
    // 幂等快路径：openTasksPanel/保存刷新已加载过 → 同步填（无闪烁）；未完成 → 等 IPC 返回再填
    if (taskTypes.length === 0 && taskProjects.length === 0) {
      await loadTaskMeta();
    }
    editingTask = task ? task.name : null;
    $("task-form-name").value = task ? task.name : "";
    $("task-form-name").disabled = Boolean(task); // 决策点：daemon 以 name 定位 → 不可改名
    // 类型下拉（内置 + 自定义；决策点⑥内置模板只读体现在模板管理区）
    const typeSel = $("task-form-type");
    typeSel.innerHTML = "";
    for (const tp of taskTypes) {
      const opt = el("option", { value: tp.name }, tp.name);
      typeSel.appendChild(opt);
    }
    const curType = (task && task.type) || (taskTypes[0] && taskTypes[0].name) || "evolution";
    typeSel.value = taskTypes.some((tp) => tp.name === curType) ? curType : (taskTypes[0] ? taskTypes[0].name : "evolution");
    // 项目下拉（决策点③：仅已注册项目）
    const projSel = $("task-form-project");
    projSel.innerHTML = "";
    for (const p of taskProjects) {
      const pv = p.name || p.path || "";
      projSel.appendChild(el("option", { value: pv }, pv));
    }
    const cfg = (task && task.config && typeof task.config === "object") ? task.config : {};
    const curProj = cfg.project || (taskProjects[0] ? taskProjects[0].name || taskProjects[0].path || "" : "");
    projSel.value = taskProjects.some((p) => (p.name || p.path || "") === curProj)
      ? curProj
      : (taskProjects[0] ? taskProjects[0].name || taskProjects[0].path || "" : "");
    $("task-form-interval").value = task && task.interval != null ? task.interval : 1800;
    $("task-form-enabled").checked = task ? task.enabled !== false : true;
    $("task-form-repo").value = cfg.repo || "";
    $("task-form").classList.remove("hidden");
    $("task-form-name").focus();
  }

  function closeTaskForm() {
    $("task-form").classList.add("hidden");
    editingTask = null;
    $("task-form-name").disabled = false;
  }

  async function saveTaskForm() {
    const name = $("task-form-name").value.trim();
    if (!name) {
      showConfirm(_t("dlg.stepTitle"), _t("dlg.nameRequiredBody"), { okText: _t("dlg.gotIt"), danger: false });
      return;
    }
    // 决策点⑤：间隔最小值 60s（客户端先行校验，daemon 兜底）
    const interval = parseInt($("task-form-interval").value, 10);
    if (!Number.isFinite(interval) || interval < 60) {
      showConfirm(_t("dlg.stepTitle"), _t("settings.taskIntervalInvalid"), { okText: _t("dlg.gotIt"), danger: false });
      return;
    }
    const payload = {
      name,
      type: $("task-form-type").value,
      project: $("task-form-project").value,
      interval,
      enabled: $("task-form-enabled").checked,
      repo: $("task-form-repo").value.trim() || undefined,
    };
    try {
      if (editingTask === null) await window.emrg.taskCreate(payload);
      else await window.emrg.taskUpdate(payload);
      Chat.addSystemMessage(_t("settings.taskSaved"));
      showToast(_t("settings.taskSaved"), { type: "success" });
      closeTaskForm();
      await renderTaskList();
    } catch (e) {
      Chat.addSystemMessage(_t("app.tasksFailed", { msg: e.message }));
      showToast(_t("app.tasksFailed", { msg: e.message }), { type: "error" });
    }
  }

  // ── 自定义类型管理（决策点①⑥：内置只读；决策点②：被引用拒绝删除） ──
  // rant 2026-08-15T09:17:45/09:20:12：提示词编辑器升级 Monaco（本地 vendor，零网络）。
  // 测试/无 monaco 环境回退轻量 shim（读写 host 元素的 .value），行为等价。
  let templateEditor = null; // monaco.editor 实例或 shim
  let templateMonacoInit = false;
  const templateEditorWaiters = [];

  function currentMonacoTheme() {
    try {
      const root = document.documentElement;
      const t = root && root.getAttribute ? root.getAttribute("data-theme") : null;
      if (t === "light") return "vs";
      if (t === "dark") return "vs-dark";
      return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "vs-dark" : "vs";
    } catch { return "vs"; }
  }

  function createTemplateEditorShim() {
    const host = $("task-template-prompt");
    return {
      setValue(v) { if (host) host.value = v || ""; },
      getValue() { return host ? (host.value || "") : ""; },
      setReadOnly() {},
      focus() { if (host && host.focus) host.focus(); },
      layout() {},
      dispose() {},
    };
  }

  function flushTemplateEditorWaiters() {
    const waiters = templateEditorWaiters.splice(0);
    waiters.forEach((fn) => { try { fn(templateEditor); } catch { /* ignore */ } });
  }

  function fallbackTemplateEditorShim(reason) {
    // eslint-disable-next-line no-console
    console.warn("[dialogs] Monaco unavailable (" + reason + ") — using textarea shim");
    templateEditor = createTemplateEditorShim();
    flushTemplateEditorWaiters();
  }

  function initTemplateMonaco() {
    if (templateMonacoInit) return;
    templateMonacoInit = true;
    try {
      // preferScriptTags: sandboxed renderer (sandbox:true, nodeIntegration:false) exposes
      // process.versions.electron + process.type==='renderer' → loader would pick the Node
      // loader branch which needs nodeRequire (undefined in sandbox) → load fails silently.
      // Script-tag loader needs no eval/nodeRequire and works under CSP 'self' + file://.
      // (pm25coder Windows host review, #801)
      window.require.config({ paths: { vs: "../vendor/monaco/vs" }, preferScriptTags: true });
      // errback + timeout: if editor.main fails to load, degrade to the shim instead of
      // leaving a dead empty .monaco-host (waiters would accumulate, save reads "" → confusing
      // "templateInvalid" toast). (#801 review finding 2)
      const t = setTimeout(() => {
        if (!templateEditor) fallbackTemplateEditorShim("timeout");
      }, 8000);
      window.require(["vs/editor/editor.main"], () => {
        clearTimeout(t);
        const host = $("task-template-prompt");
        if (!host || !window.monaco || !window.monaco.editor) {
          fallbackTemplateEditorShim("editor.main missing");
          return;
        }
        templateEditor = window.monaco.editor.create(host, {
          value: "",
          language: "markdown",
          theme: currentMonacoTheme(),
          lineNumbers: "on",
          wordWrap: "on",
          minimap: { enabled: false },
          scrollBeyondLastLine: false,
          automaticLayout: true,
          fontSize: 13,
          lineHeight: 20,
          padding: { top: 6, bottom: 6 },
        });
        // system 主题下跟随 OS 深浅切换（settings 内切换走 initThemeButtons → sync）
        if (window.matchMedia) {
          try {
            window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncTemplateEditorTheme);
          } catch { /* ignore */ }
        }
        flushTemplateEditorWaiters();
      }, (err) => {
        clearTimeout(t);
        fallbackTemplateEditorShim(String(err || "module load error"));
      });
    } catch (e) {
      fallbackTemplateEditorShim(String(e));
    }
  }

  function withTemplateEditor(fn) {
    if (templateEditor) return fn(templateEditor);
    if (!(window.monaco && window.monaco.editor) && !window.require) {
      // 无 monaco（测试沙箱等）→ 立即用 shim，行为与 textarea 时代等价
      templateEditor = createTemplateEditorShim();
      return fn(templateEditor);
    }
    templateEditorWaiters.push(fn);
    initTemplateMonaco();
    return null;
  }

  function syncTemplateEditorTheme() {
    const ed = templateEditor;
    if (ed && window.monaco && window.monaco.editor) {
      try { window.monaco.editor.setTheme(currentMonacoTheme()); } catch { /* ignore */ }
    }
  }

  async function renderTemplateList() {
    const wrap = $("task-template-list");
    if (!wrap) return;
    wrap.innerHTML = "";
    let templates = [];
    try {
      templates = await window.emrg.taskTemplateList();
    } catch (e) {
      wrap.innerHTML = `<div class="task-empty">${_t("settings.templateEmpty")}</div>`;
      return;
    }
    if (!templates || templates.length === 0) {
      wrap.innerHTML = `<div class="task-empty">${_t("settings.templateEmpty")}</div>`;
      return;
    }
    for (const t of templates) {
      const row = el("div", { class: "task-row" });
      row.appendChild(el("span", { class: "task-name" }, t.name));
      row.appendChild(el("span", { class: "task-badge" }, t.builtin ? _t("settings.taskBuiltin") : _t("settings.taskCustom")));
      const body = t.prompt || t.template || "";
      row.appendChild(el("span", { class: "task-hint" }, body.length > 60 ? body.slice(0, 60) + "…" : body));
      // rant 09:17:45：内置类型提供只读"查看"（Monaco readOnly 同样高亮/行号/滚动）；
      // 自定义类型在查看之外保留编辑/删除（决策点①⑥：内置不可增删改）
      const actions = el("span", { class: "task-actions" });
      const viewBtn = el("button", { type: "button", class: "model-action-btn", title: _t("settings.templateView") }, _t("settings.templateView"));
      viewBtn.addEventListener("click", () => openTemplateForm(t));
      actions.appendChild(viewBtn);
      if (!t.builtin) {
        const editBtn = el("button", { type: "button", class: "model-action-btn", title: _t("settings.templateEdit") }, _t("settings.templateEdit"));
        editBtn.addEventListener("click", () => openTemplateForm(t));
        actions.appendChild(editBtn);
        const delBtn = el("button", { type: "button", class: "model-action-btn danger", title: _t("settings.templateDelete") }, _t("settings.templateDelete"));
        delBtn.addEventListener("click", () => {
          showConfirm(_t("settings.templateDelete"), _t("settings.templateDeleteConfirm", { name: t.name }), {
            okText: _t("settings.templateDelete"),
            danger: true,
            onOk: async () => {
              try {
                await window.emrg.taskTemplateDelete({ name: t.name });
                Chat.addSystemMessage(_t("settings.templateDeleted"));
                showToast(_t("settings.templateDeleted"), { type: "success" });
                await loadTaskMeta(); // 刷新类型选项（任务表单下拉）
                await renderTemplateList();
              } catch (e) {
                // 决策点②：被任务引用的类型 daemon 拒绝删除（错误信息含任务数）
                Chat.addSystemMessage(_t("settings.templateDeleteFailed", { msg: e.message }));
                showToast(_t("settings.templateDeleteFailed", { msg: e.message }), { type: "error" });
              }
            },
          });
        });
        actions.appendChild(delBtn);
      }
      row.appendChild(actions);
      wrap.appendChild(row);
    }
    // 底部"＋ 添加类型"（仅自定义类型可增删改）
    const addRow = el("div", { style: "display:flex;justify-content:center;margin-top:6px;" });
    const addBtn = el("button", { type: "button", class: "btn btn-ghost", style: "padding:4px 12px;" }, _t("settings.templateAdd"));
    addBtn.addEventListener("click", () => openTemplateForm(null));
    addRow.appendChild(addBtn);
    wrap.appendChild(addRow);
  }

  function openTemplateForm(tpl = null) {
    editingTemplate = tpl ? tpl.name : null;
    const readOnly = Boolean(tpl && tpl.builtin);
    $("task-template-name").value = tpl ? tpl.name : "";
    $("task-template-name").disabled = Boolean(tpl); // daemon 以 name 定位 → 不可改名
    $("task-template-form").classList.remove("hidden");
    // 内置类型只读查看：隐藏保存按钮（编辑器 readOnly 高亮/行号照常）
    const saveBtn = $("task-template-save");
    if (saveBtn) saveBtn.classList.toggle("hidden", readOnly);
    const applyEditor = (ed) => {
      ed.setValue(tpl ? (tpl.prompt || tpl.template || "") : "");
      ed.setReadOnly(readOnly);
      syncTemplateEditorTheme();
      try { ed.focus(); } catch { /* ignore */ }
    };
    const ed = templateEditor;
    if (ed) applyEditor(ed);
    else withTemplateEditor(applyEditor);
    if (!readOnly) $("task-template-name").focus();
  }

  function closeTemplateForm() {
    $("task-template-form").classList.add("hidden");
    editingTemplate = null;
    $("task-template-name").disabled = false;
    $("task-template-name").value = "";
    const saveBtn = $("task-template-save");
    if (saveBtn) saveBtn.classList.remove("hidden");
    const ed = templateEditor;
    if (ed) {
      try { ed.setValue(""); ed.setReadOnly(false); } catch { /* ignore */ }
    } else if ($("task-template-prompt")) {
      $("task-template-prompt").value = "";
    }
  }

  function getTemplateEditorValue() {
    const ed = templateEditor;
    if (ed) return ed.getValue();
    const host = $("task-template-prompt");
    return host ? (host.value || "") : "";
  }

  async function saveTemplateForm() {
    const name = $("task-template-name").value.trim();
    const prompt = getTemplateEditorValue();
    if (!name || !prompt.trim()) {
      showConfirm(_t("dlg.stepTitle"), _t("settings.templateInvalid"), { okText: _t("dlg.gotIt"), danger: false });
      return;
    }
    try {
      if (editingTemplate === null) await window.emrg.taskTemplateCreate({ name, prompt });
      else await window.emrg.taskTemplateUpdate({ name, prompt });
      Chat.addSystemMessage(_t("settings.templateSaved"));
      showToast(_t("settings.templateSaved"), { type: "success" });
      closeTemplateForm();
      await loadTaskMeta();
      await renderTemplateList();
    } catch (e) {
      Chat.addSystemMessage(_t("settings.templateSaveFailed", { msg: e.message }));
      showToast(_t("settings.templateSaveFailed", { msg: e.message }), { type: "error" });
    }
  }

  function initTaskManagement() {
    const addBtn = $("task-add-btn");
    if (addBtn) addBtn.addEventListener("click", () => { openTaskForm(null).catch(() => {}); });
    const saveBtn = $("task-form-save");
    if (saveBtn) saveBtn.addEventListener("click", saveTaskForm);
    const cancelBtn = $("task-form-cancel");
    if (cancelBtn) cancelBtn.addEventListener("click", closeTaskForm);
    const tmgrBtn = $("task-template-mgr-btn");
    if (tmgrBtn) {
      tmgrBtn.addEventListener("click", async () => {
        const wrap = $("task-template-list");
        if (!wrap) return;
        if (wrap.classList.contains("hidden")) {
          wrap.classList.remove("hidden");
          closeTemplateForm(); // 收起表单，避免与列表并列
          await loadTaskMeta();
          await renderTemplateList();
        } else {
          wrap.classList.add("hidden");
        }
      });
    }
    const tSave = $("task-template-save");
    if (tSave) tSave.addEventListener("click", saveTemplateForm);
    const tCancel = $("task-template-cancel");
    if (tCancel) tCancel.addEventListener("click", closeTemplateForm);
    const nameInput = $("task-form-name");
    if (nameInput) {
      nameInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); saveTaskForm(); }
        else if (e.key === "Escape") { e.preventDefault(); closeTaskForm(); }
      });
    }
    const tplInput = $("task-template-name");
    if (tplInput) {
      tplInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); saveTemplateForm(); }
        else if (e.key === "Escape") { e.preventDefault(); closeTemplateForm(); }
      });
    }
  }

  // ── 确认对话框（替代 confirm/alert） ────
  let confirmCb = null;
  function showConfirm(title, message, opts = {}) {
    $("confirm-title").textContent = title;
    $("confirm-message").textContent = message;
    const ok = $("confirm-ok");
    ok.textContent = opts.okText || _t("dlg.delete");
    ok.className = opts.danger === false ? "btn btn-primary" : "btn btn-danger";
    confirmCb = opts.onOk || null;
    $("confirm-dialog").showModal();
  }

  // ── P5（rant 15:07:19）：打开会话对话框（两步：选项目 → 选会话，跨项目） ──
  async function showOpenSessionDialog() {
    const list = $("open-session-list");
    const dialog = $("open-session-dialog");
    if (!list || !dialog) return;
    list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("dlg.loading")}</span></div>`;
    dialog.showModal();
    try {
      const projects = await window.emrg.listProjects();
      list.innerHTML = "";
      if (!projects || projects.length === 0) {
        list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("openSession.noProjects")}</span></div>`;
        return;
      }
      // 第一步：项目列表（按最近活跃倒序——daemon 已排；底部"新建项目…"按钮）
      projects.forEach((p) => {
        const row = el("div", { class: "help-row", style: "display:flex;align-items:center;gap:var(--sp-2);" });
        const pick = el("button", { type: "button", style: "flex:1;text-align:left;cursor:pointer;background:none;border:none;display:flex;flex-direction:column;align-items:flex-start;padding:0;" });
        const name = el("span", { class: "help-cmd" }, p.name || p.path || "");
        const hint = el("span", { class: "help-hint" }, p.path || "");
        pick.appendChild(name);
        pick.appendChild(hint);
        // P6 验收补完：项目行显示"最近活跃"相对时间（消费 daemon latest_session_at）
        const act = relTime(p.latest_session_at);
        if (act) pick.appendChild(el("span", { class: "help-hint" }, act));
        // P5 slice 2：点击打开会话（带项目路径 → resume 用该项目 cwd）
        pick.addEventListener("click", () => showProjectSessions(p));
        row.appendChild(pick);
        // 删除项目（受保护项目不可删 → 提示；非受保护 → 确认弹窗）
        const del = el("button", { type: "button", class: "btn btn-ghost", style: "padding:2px 8px;flex-shrink:0;", title: _t("deleteProject.delete") }, _t("deleteProject.delete"));
        del.addEventListener("click", () => confirmDeleteProject(p));
        row.appendChild(del);
        list.appendChild(row);
      });
    } catch (e) {
      list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("openSession.loadFailed", { msg: e.message })}</span></div>`;
    }
  }

  // P5 slice 2：受保护项目（内置 project emrg / 内置 task emrg-task → 演化依赖，不可删）
  function isProtectedProject(p) {
    return p && (p.name === "emrg" || p.name === "emrg-task");
  }

  function confirmDeleteProject(p, onDone) {
    if (isProtectedProject(p)) {
      showConfirm(_t("deleteProject.title"), _t("deleteProject.protectedBody", { name: p.name }), { okText: _t("dlg.gotIt"), danger: false });
      return;
    }
    showConfirm(_t("deleteProject.title"), _t("deleteProject.body", { name: p.name }), {
      okText: _t("dlg.delete"),
      danger: true,
      onOk: async () => {
        try {
          const res = await window.emrg.removeProject({ name: p.name, path: p.path });
          if (!res || !res.ok) {
            Chat.addSystemMessage(_t("deleteProject.failed", { msg: (res && res.error) || _t("app.unknownError") }));
            return;
          }
          Chat.addSystemMessage(_t("deleteProject.removed", { name: p.name }));
          // 该项目已打开的会话被关闭 → 激活会话被关则切相邻
          const closed = res.closed || [];
          if (closed.length > 0 && closed.includes(App.state.sessionId)) {
            const remaining = (App.state.openSessions || []).filter((s) => !closed.includes(s.sid));
            if (remaining.length > 0) await App.switchSession(remaining[0].sid, { silent: true });
            else await App.newSession();
          }
          if (typeof onDone === "function") onDone();
          else showOpenSessionDialog(); // 刷新项目列表
        } catch (e) {
          Chat.addSystemMessage(_t("deleteProject.failed", { msg: e.message }));
        }
      },
    });
  }

  // 第二步：该项目会话列表（created_at 倒序）→ 点击打开（switchSession 复用连接）
  async function showProjectSessions(project) {
    const list = $("open-session-list");
    list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("dlg.loading")}</span></div>`;
    $("open-session-title").textContent = _t("openSession.titleProject", { project: project.name || project.path || "" });
    try {
      const frame = await window.emrg.listProjectSessions({ projectPath: project.path });
      const sessions = frame.sessions || [];
      list.innerHTML = "";
      if (sessions.length === 0) {
        list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("openSession.noSessions")}</span></div>`;
        return;
      }
      sessions.forEach((s) => {
        const row = el("button", { class: "help-row", type: "button", style: "width:100%;text-align:left;cursor:pointer;background:none;border:none;" });
        const name = el("span", { class: "help-cmd" }, s.title || _t("app.unnamed"));
        const hint = el("span", { class: "help-hint" }, s.session_id === App.state.sessionId ? _t("app.current") : "");
        row.appendChild(name);
        row.appendChild(hint);
        row.addEventListener("click", async () => {
          $("open-session-dialog").close();
          // P5 slice 2：带项目路径切换（resume 用该项目 cwd，非全局 projectDir）
          await App.switchSession(s.session_id, { projectPath: project.path });
        });
        list.appendChild(row);
      });
    } catch (e) {
      list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("openSession.loadFailed", { msg: e.message })}</span></div>`;
    }
  }

  function initOpenSessionDialog() {
    $("open-session-cancel").addEventListener("click", () => $("open-session-dialog").close());
    $("open-session-new").addEventListener("click", async () => {
      // P5：新建项目 = 选目录 → 轻量命令注册（daemon 隐式 _touch_project）
      try {
        const res = await window.emrg.pickProjectDir();
        if (res && res.path) {
          await window.emrg.registerProject({ path: res.path });
          Chat.addSystemMessage(_t("openSession.projectCreated", { path: res.path }));
          showOpenSessionDialog(); // 刷新项目列表
        }
      } catch (e) {
        Chat.addSystemMessage(_t("openSession.projectFailed", { msg: e.message }));
      }
    });
  }

  // ── Rant 面板（rant 14:10:14 P4：列表筛选 + 详情 + 新建） ──
  let rantFilter = ""; // "" | pending | in_progress | completed

  function setRantFilter(status) {
    rantFilter = status || "";
    // tab 高亮
    for (const f of ["all", "pending", "inprogress", "completed"]) {
      const btn = $(`rant-filter-${f}`);
      if (btn) btn.classList.toggle("active", f === (status === "" ? "all" : status === "in_progress" ? "inprogress" : status));
    }
    renderRantList();
  }

  async function renderRantList() {
    const list = $("rant-list");
    if (!list) return;
    list.innerHTML = "";
    list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("dlg.loading")}</span></div>`;
    try {
      const rants = await window.emrg.listRants({ status: rantFilter });
      list.innerHTML = "";
      if (!rants || rants.length === 0) {
        list.innerHTML = `<div class="task-empty">${_t(rantFilter ? "rants.emptyFiltered" : "rants.empty")}</div>`;
        return;
      }
      for (const r of rants) {
        // rant 21:36:01：5 列对齐（时间/项目/状态/进度/内容），与 .rant-head 列头同列宽类
        const row = el("div", { class: "task-row rant-row" });
        const ts = String(r.timestamp || "").slice(0, 16).replace("T", " ");
        row.appendChild(el("span", { class: "rant-col-time" }, ts || "—"));
        row.appendChild(el("span", { class: "rant-col-project" }, r.project || "—"));
        // 状态徽标三态配色（completed 绿 / in_progress 琥珀 / pending 灰）
        const st = r.status || "pending";
        const stText = st === "completed" ? _t("rants.statusCompleted") : st === "in_progress" ? _t("rants.statusInProgress") : _t("rants.statusPending");
        const badgeCls = st === "completed" ? "badge-done" : st === "in_progress" ? "badge-warn" : "badge-muted";
        row.appendChild(el("span", { class: `task-badge ${badgeCls}` }, stText));
        row.appendChild(el("span", { class: "rant-col-progress" }, r.progress ? String(r.progress) : "—"));
        // 内容列：message 首行摘要（去 md 标题/列表标记）
        const msg = r.message || "";
        const firstLine = (msg.split("\n").find((l) => l.trim() !== "") || "").replace(/^#{1,6}\s+/, "").replace(/^[>*\-\s]+/, "");
        row.appendChild(el("span", { class: "rant-col-content" }, firstLine || "—"));
        // 详情展开/收起（rant 10:41:43：点击已展开的本行 → 收起，不再无条件重建）
        row.addEventListener("click", async () => {
          const existing = row.nextElementSibling;
          const isSelfDetail = existing && existing.classList.contains("rant-detail");
          // 先移除所有已展开的详情（含其他行的）
          list.querySelectorAll(".rant-detail").forEach((d) => d.remove());
          if (isSelfDetail) return; // 点自己已展开 → 收起
          const detailRow = el("div", { class: "rant-detail", style: "padding:6px 8px;border-top:1px solid var(--border);font-size:var(--fs-secondary);" });
          detailRow.appendChild(el("div", { class: "rant-meta" }, `${ts || "—"} · ${r.project || "—"} · ${stText}`));
          const md = el("div", { class: "msg-body rant-md" });
          try {
            if (window.emrgMarkdown && window.emrgMarkdown.renderMarkdown) {
              // rant 10:41:43：【】标题预处理（markdown 视觉层次；不破坏原文）
              md.innerHTML = await window.emrgMarkdown.renderMarkdown(preprocessRantMarkdown(msg));
            } else {
              md.textContent = msg;
            }
          } catch {
            md.textContent = msg;
          }
          detailRow.appendChild(md);
          if (r.progress) detailRow.appendChild(el("div", { class: "rant-progress" }, `${_t("rants.statusInProgress")}: ${r.progress}`));
          else detailRow.appendChild(el("div", { class: "rant-progress" }, _t("rants.noProgress")));
          row.after(detailRow);
        });
        list.appendChild(row);
      }
    } catch (e) {
      list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("rants.loadFailed", { msg: e.message })}</span></div>`;
    }
  }

  // rant 10:41:43：rant 详情 markdown 预处理 —— 行首 【xxx】 段标记映射为标题级
  // （【任务】→####），让整篇有视觉层次；仅短行（≤60 字符）转标题，长内容行保持正文。
  // 原文保留（只加 #### 前缀，不删除/改写原文）。
  function preprocessRantMarkdown(text) {
    if (!text) return text || "";
    return String(text).split("\n").map((line) => {
      const t = line.trim();
      if (/^【[^】]{1,24}】/.test(t) && t.length <= 60) {
        return `#### ${t}`;
      }
      return line;
    }).join("\n");
  }

  function openRantForm() {
    const form = $("rant-form");
    if (!form) return;
    if (!form.classList.contains("hidden")) { form.classList.add("hidden"); return; }
    $("rant-form-message").value = "";
    // 项目下拉（复用 listProjects）
    const sel = $("rant-form-project");
    sel.innerHTML = "";
    const opt = el("option", { value: "" }, _t("rants.project"));
    sel.appendChild(opt);
    window.emrg.listProjects().then((projects) => {
      for (const p of projects || []) {
        const o = el("option", { value: p.name || p.path || "" }, p.name || p.path || "");
        sel.appendChild(o);
      }
    }).catch(() => {});
    form.classList.remove("hidden");
    $("rant-form-message").focus();
  }

  async function submitRantForm() {
    const msg = $("rant-form-message").value.trim();
    const proj = $("rant-form-project").value;
    if (!msg) {
      showConfirm(_t("dlg.stepTitle"), _t("rants.message"), { okText: _t("dlg.gotIt"), danger: false });
      return;
    }
    try {
      const res = await window.emrg.sendRant({ message: msg, project: proj });
      Chat.addSystemMessage(_t("rants.sent", { count: res && res.count ? res.count : "" }));
      $("rant-form").classList.add("hidden");
      $("rant-form-message").value = "";
      await renderRantList();
    } catch (e) {
      Chat.addSystemMessage(_t("rants.sendFailed", { msg: e.message }));
    }
  }

  function initRantPanel() {
    // 状态筛选 tab
    for (const f of ["all", "pending", "inprogress", "completed"]) {
      const btn = $(`rant-filter-${f}`);
      if (btn) btn.addEventListener("click", () => setRantFilter(btn.dataset.rantFilter));
    }
    const newBtn = $("rant-new-btn");
    if (newBtn) newBtn.addEventListener("click", openRantForm);
    const submit = $("rant-form-submit");
    if (submit) submit.addEventListener("click", submitRantForm);
    const cancel = $("rant-form-cancel");
    if (cancel) cancel.addEventListener("click", () => $("rant-form").classList.add("hidden"));
  }

  // ── 项目面板（rant 14:10:14 P5：侧边栏项目入口，复用项目 IPC） ──
  /** 项目面板：列表（名称/路径/auto_evolve 徽标/最近活跃）+ 查看会话 + 删除 + 添加。 */
  async function renderProjectList() {
    const list = $("project-list");
    if (!list) return;
    list.innerHTML = ""; // 显式清空（测试沙箱 innerHTML 非空赋值不清 children；真实 DOM 语义一致）
    list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("dlg.loading")}</span></div>`;
    try {
      // auto_evolve 徽标：tasks.yml 中有 config.project === 项目名的任务（含 evolution 类型）
      const [projects, tasks] = await Promise.all([
        window.emrg.listProjects(),
        window.emrg.listTasks().catch(() => []),
      ]);
      list.innerHTML = "";
      if (!projects || projects.length === 0) {
        list.innerHTML = `<div class="task-empty">${_t("projects.empty")}</div>`;
        return;
      }
      const evolveProjects = new Set(
        (tasks || []).filter((t) => t && t.config && t.config.project).map((t) => t.config.project)
      );
      projects.forEach((p) => {
        const row = el("div", { class: "task-row" });
        row.appendChild(el("span", { class: "task-name" }, p.name || p.path || "?"));
        if (evolveProjects.has(p.name)) {
          row.appendChild(el("span", { class: "task-badge" }, `⚡ ${_t("projects.autoEvolve")}`));
        }
        const hints = [];
        if (p.path) hints.push(p.path);
        const act = relTime(p.latest_session_at);
        if (act) hints.push(act);
        row.appendChild(el("span", { class: "task-hint" }, hints.join(" · ")));
        const actions = el("span", { class: "task-actions" });
        // 查看会话（复用 listProjectSessions）
        const sessBtn = el("button", { type: "button", class: "model-action-btn", title: _t("projects.viewSessions") }, _t("projects.viewSessions"));
        sessBtn.addEventListener("click", () => showProjectSessionsInPanel(p));
        actions.appendChild(sessBtn);
        // 删除（受保护守卫 + 确认；删除后刷新面板）
        const delBtn = el("button", { type: "button", class: "model-action-btn danger", title: _t("deleteProject.delete") }, _t("deleteProject.delete"));
        delBtn.addEventListener("click", () => confirmDeleteProject(p, () => renderProjectList()));
        actions.appendChild(delBtn);
        row.appendChild(actions);
        list.appendChild(row);
      });
    } catch (e) {
      list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("projects.addFailed", { msg: e.message })}</span></div>`;
    }
  }

  /** 项目面板内嵌：该项目会话列表（created_at 倒序）→ 点击切换会话（带项目 cwd）。 */
  async function showProjectSessionsInPanel(project) {
    const list = $("project-list");
    if (!list) return;
    list.innerHTML = ""; // 显式清空（测试沙箱 innerHTML 非空赋值不清 children；真实 DOM 语义一致）
    list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("dlg.loading")}</span></div>`;
    // 返回按钮 + 标题
    const title = el("div", { class: "task-row" });
    const backBtn = el("button", { type: "button", class: "model-action-btn", style: "flex-shrink:0;" }, _t("projects.back"));
    backBtn.addEventListener("click", () => renderProjectList());
    title.appendChild(backBtn);
    title.appendChild(el("span", { class: "task-name" }, _t("projects.sessionsOf", { project: project.name || project.path || "" })));
    list.appendChild(title);
    try {
      const frame = await window.emrg.listProjectSessions({ projectPath: project.path });
      const sessions = frame.sessions || [];
      if (sessions.length === 0) {
        list.appendChild(el("div", { class: "task-empty" }, _t("projects.noSessions")));
        return;
      }
      sessions.forEach((s) => {
        const row = el("div", { class: "task-row", style: "cursor:pointer;" });
        row.appendChild(el("span", { class: "task-name" }, s.title || _t("app.unnamed")));
        if (s.session_id === App.state.sessionId) row.appendChild(el("span", { class: "task-badge" }, _t("projects.current")));
        const hint = el("span", { class: "task-hint" }, s.session_id || "");
        row.appendChild(hint);
        row.addEventListener("click", async () => {
          // 带项目路径切换（resume 用该项目 cwd，非全局 projectDir）
          await App.switchSession(s.session_id, { projectPath: project.path });
        });
        list.appendChild(row);
      });
    } catch (e) {
      list.appendChild(el("div", { class: "task-empty" }, _t("openSession.loadFailed", { msg: e.message })));
    }
  }

  // ── P5 slice 2：新建会话对话框（选已有项目 → 新建；或新建项目 → 新建） ──
  async function showNewSessionDialog() {
    const list = $("new-session-list");
    const dialog = $("new-session-dialog");
    if (!list || !dialog) return;
    list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("dlg.loading")}</span></div>`;
    dialog.showModal();
    try {
      const projects = await window.emrg.listProjects();
      list.innerHTML = "";
      if (!projects || projects.length === 0) {
        list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("openSession.noProjects")}</span></div>`;
        return;
      }
      // 已有项目（活跃排序）→ 点选即在该项目新建会话
      projects.forEach((p) => {
        const row = el("button", { class: "help-row", type: "button", style: "width:100%;text-align:left;cursor:pointer;background:none;border:none;" });
        const name = el("span", { class: "help-cmd" }, p.name || p.path || "");
        const hint = el("span", { class: "help-hint" }, p.path || "");
        row.appendChild(name);
        row.appendChild(hint);
        // P6 验收补完：项目行显示"最近活跃"相对时间
        const act = relTime(p.latest_session_at);
        if (act) row.appendChild(el("span", { class: "help-hint" }, act));
        row.addEventListener("click", async () => {
          $("new-session-dialog").close();
          await App.newSession({ projectPath: p.path });
        });
        list.appendChild(row);
      });
    } catch (e) {
      list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("newSession.loadFailed", { msg: e.message })}</span></div>`;
    }
  }

  function initNewSessionDialog() {
    $("new-session-cancel").addEventListener("click", () => $("new-session-dialog").close());
    $("new-session-new").addEventListener("click", async () => {
      // 新建项目 = 选目录 → 注册（轻量命令隐式 _touch_project，不调 init_auto_evolve）→ 新建会话
      try {
        const res = await window.emrg.pickProjectDir();
        if (res && res.path) {
          await window.emrg.registerProject({ path: res.path });
          $("new-session-dialog").close();
          await App.newSession({ projectPath: res.path });
        }
      } catch (e) {
        Chat.addSystemMessage(_t("newSession.projectFailed", { msg: e.message }));
      }
    });
  }

  function closeConfirm() {
    $("confirm-dialog").close();
    confirmCb = null;
  }

  /** 确认按钮：执行回调后关闭 */
  function confirmOk() {
    const cb = confirmCb;
    $("confirm-dialog").close();
    confirmCb = null;
    if (cb) cb();
  }

  return {
    initThemeButtons,
    initLangButtons,
    renderLangOptions,
    initModelForm,
    initRenameDialog,
    initGithubSection,
    initDeviceDialog,
    refreshGithubStatus,
    initOpenSessionDialog, // P5：打开会话对话框初始化
    showOpenSessionDialog, // P5：两步打开会话
    initNewSessionDialog, // P5 slice 2：新建会话对话框初始化
    showNewSessionDialog, // P5 slice 2：新建会话（选项目）
    initTaskManagement, // rant 18:23:15 P3：定时任务/自定义类型管理初始化
    loadTaskMeta, // rant 18:23:15 P3：任务/类型元数据加载（面板打开/测试复用）
    renderTaskList, // rant 18:23:15 P3：任务列表渲染（测试/刷新复用）
    formatCountdown, // rant 10:36:39：倒计时格式化（≤60s "43s" / ≤1h "1m23s" / >1h "1h05m"；测试复用）
    updateTaskCountdowns, // rant 10:36:39：1s tick 更新倒计时文本（测试直接调用模拟走秒）
    startTaskCountdown, // rant 10:36:39：启动 1s 倒计时（幂等；renderTaskList 自动调用）
    stopTaskCountdown, // rant 10:36:39：停止并清引用（离开任务视图时 app.js 调用防泄漏）
    renderProjectList, // rant 14:10:14 P5：项目面板列表渲染（测试/刷新复用）
    showProjectSessionsInPanel, // rant 14:10:14 P5：项目面板内嵌会话列表（测试复用）
    initRantPanel, // rant 14:10:14 P4：rant 面板初始化
    renderRantList, // rant 14:10:14 P4：rant 列表渲染（测试/刷新复用）
    preprocessRantMarkdown, // rant 10:41:43：rant 详情 markdown 【】标题预处理（测试复用）
    setRantFilter, // rant 14:10:14 P4：rant 状态筛选（测试复用）
    openRantForm, // rant 14:10:14 P4：新建 rant 表单（测试复用）
    submitRantForm, // rant 14:10:14 P4：rant 表单提交（测试复用）
    saveTaskForm, // rant 18:23:15 P3：任务表单提交（测试复用）
    saveTemplateForm, // rant 18:23:15 P3：类型表单提交（测试复用）
    openTemplateForm, // rant 09:17:45：类型表单打开（内置只读查看 / 自定义编辑，测试复用）
    closeTemplateForm, // rant 09:17:45：类型表单关闭（测试复用）
    showRename,
    submitRename,
    showSettings,
    saveSettings,
    showWelcome,
    saveWelcome,
    showConfirm,
    closeConfirm,
    confirmOk,
    get theme() {
      return theme;
    },
    set theme(v) {
      theme = v;
    },
  };
})();

window.EMRG_Dialogs = Dialogs;
