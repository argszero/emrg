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
      $("set-project-dir").value = s.projectDir || "";
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
    $("settings-dialog").showModal();
  }

  async function saveSettings() {
    const config = {
      apiKey: $("set-api-key").value.trim(),
      baseUrl: $("set-base-url").value.trim(),
      model: defaultName,
      projectDir: $("set-project-dir").value.trim(),
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
      if ($("settings-dialog").open) $("settings-dialog").close();
      Chat.addSystemMessage(_t("dlg.saved"));
      App.state.apiKeyConfigured = true;
      App.state.projectDir = config.projectDir || App.state.projectDir;
      App.state.model = defaultName;
      App.updateModelSwitcher();
      await App.boot();
    } catch (e) {
      Chat.addSystemMessage(_t("settings.saveFailed", { msg: e.message }));
    }
  }

  // ── 首启引导 ─────────────────────────────
  function showWelcome() {
    $("welcome-project-dir").value = "";
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
      projectDir: $("welcome-project-dir").value.trim() || "",
    };
    try {
      await window.emrg.saveSettings(config);
      if ($("welcome-dialog").open) $("welcome-dialog").close();
      Chat.addSystemMessage(_t("dlg.starting"));
      App.state.apiKeyConfigured = true;
      App.state.projectDir = config.projectDir || App.state.projectDir;
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
