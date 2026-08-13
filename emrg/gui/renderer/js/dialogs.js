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
      // Auto update-check prompt (rant 2026-08-10T07:12:12): about area shows
      // a one-time non-intrusive line when a newer release exists.
      // rant 2026-08-11T09:18:16：手动"检查更新"按钮（force 重新检查）
      initUpdateCheckButton();
      await refreshUpdateCheck();
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

  // Auto update-check prompt (rant 2026-08-10T07:12:12): display-only, one
  // line in the about area, never a modal.
  // force (rant 2026-08-11T09:18:16): settings manual check button — run a
  // fresh GitHub fetch instead of returning the daemon's cached result.
  // rant 2026-08-12T12:10:12: when the daemon already auto-downloaded a
  // verified installer (downloaded_version), show a one-click install button
  // instead of the plain Releases link.
  // ⚠️ 局部变量命名 updEl（勿用 el——会遮蔽模块级 el() 元素工厂，el("a",…)
  // 抛 TypeError → catch 吞掉 → 更新行永远 hidden，正是 #602 隐藏缺陷）
  async function refreshUpdateCheck({ force = false } = {}) {
    const updEl = $("about-update");
    if (!updEl) return; // 元素缺失（测试桩）时忽略
    try {
      const u = await window.emrg.updateCheck({ force });
      if (!u || !u.enabled) {
        updEl.classList.add("hidden");
        updEl.textContent = "";
        return;
      }
      updEl.textContent = "";
      let shown = false;
      // ① 已自动下载 + 校验通过 → 一键安装按钮（rant 2026-08-12T12:10:12）
      if (u.downloaded_version && u.downloaded_version !== u.current_version) {
        const btn = el("button", {
          type: "button",
          class: "btn btn-sm btn-primary",
        }, _t("settings.updateReady", { latest: u.downloaded_version }));
        btn.addEventListener("click", () => showUpdateInstallConfirm(u));
        updEl.appendChild(btn);
        shown = true;
      }
      // ② 有新版但未下载 → Releases 链接（幂等：同版本只提示一次）
      if (!shown && u.has_update && u.latest_version && u.latest_version !== u.prompted_version) {
        const link = el("a", {
          href: "https://github.com/argszero/emrg/releases",
          target: "_blank",
          rel: "noopener",
        }, _t("settings.updateAvailable", { latest: u.latest_version }));
        updEl.appendChild(link);
        shown = true;
        try { await window.emrg.updateCheckPrompted({ version: u.latest_version }); } catch { /* ignore */ }
      }
      updEl.classList.toggle("hidden", !shown);
    } catch {
      updEl.classList.add("hidden");
      updEl.textContent = "";
    }
  }

  // Auto-update one-click install (rant 2026-08-12T12:10:12): the daemon
  // downloaded + SHA256-verified the installer; the user clicks install →
  // confirm (SmartScreen hint on Windows) → launch installer + quit EMRG
  // (the installer stops any remaining EMRG processes itself).
  function showUpdateInstallConfirm(u) {
    const isWin = /win/i.test((navigator.platform || "") + (navigator.userAgent || ""));
    showConfirm(
      _t("settings.installTitle"),
      _t(isWin ? "settings.installConfirmWin" : "settings.installConfirm", { latest: u.downloaded_version || "" }),
      {
        okText: _t("settings.installNow"),
        danger: false,
        onOk: async () => {
          try {
            const res = await window.emrg.updateInstall({ path: u.downloaded_path, version: u.downloaded_version });
            if (res && res.ok) {
              Chat.addSystemMessage(_t("settings.installStarted", { latest: u.downloaded_version || "" }));
            } else {
              Chat.addSystemMessage(_t("settings.installFailed", { msg: (res && res.error) || "" }));
            }
          } catch {
            Chat.addSystemMessage(_t("settings.installFailed", { msg: "" }));
          }
        },
      },
    );
  }

  // 设置页"检查更新"手动按钮（rant 2026-08-11T09:18:16）：点击立即强制
  // 重新检查（不等 TTL 轮询），检查中显示"检查中…"，完成后恢复按钮。
  function initUpdateCheckButton() {
    const btn = $("about-update-check-btn");
    if (!btn) return; // 元素缺失（测试桩）时忽略
    btn.addEventListener("click", async () => {
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = _t("settings.checkingUpdate");
      try {
        await refreshUpdateCheck({ force: true });
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    });
  }

  // 启动主动更新提示（rant 2026-08-11T09:18:16）：GUI 启动成功路径调用，
  // 不依赖打开设置对话框——有新版本且未提示过时输出一条非阻塞系统消息
  // （对齐 TUI 启动 system 行）。rant 2026-08-12T12:10:12：若 daemon 已
  // 自动下载好安装包，则提示"已就绪，点击安装"。boot 时 daemon 可能未
  // 就绪 → 静默失败。
  async function promptUpdateAtStartup() {
    try {
      const u = await window.emrg.updateCheck({ force: false });
      if (!u || !u.enabled) return;
      // 已自动下载 + 校验通过 → "已就绪，点击安装"（设置 → 关于）
      if (u.downloaded_version && u.downloaded_version !== u.current_version) {
        Chat.addSystemMessage(
          _t("app.updateReady", { latest: u.downloaded_version }),
        );
        return;
      }
      if (!u.has_update || !u.latest_version) return;
      if (u.latest_version === u.prompted_version) return;
      Chat.addSystemMessage(
        _t("app.updateAvailable", { latest: u.latest_version }),
      );
      // 幂等：同版本只提示一次
      try { await window.emrg.updateCheckPrompted({ version: u.latest_version }); } catch { /* ignore */ }
    } catch { /* boot-time daemon may not be ready — silent */ }
  }

  // ── 定时任务管理（rant 2026-08-12T18:23:15 P3：GUI 任务/自定义类型 CRUD） ──
  // 决策点执行：①内置类型只读 ②被任务引用的自定义类型拒绝删除 ③项目仅限已注册
  // ④热重载（daemon apply_tasks）⑤间隔 ≥60 ⑥内置模板只读。
  let taskTypes = []; // [{name, builtin}] — 类型下拉选项（内置 + 自定义）
  let taskProjects = []; // listProjects 结果 — 项目下拉仅已注册项目（决策点③）
  let editingTask = null; // 正在编辑的任务名（null = 新增；daemon 以 name 定位 → 名称不可改）
  let editingTemplate = null; // 正在编辑的自定义类型名（null = 新增；名称不可改）

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

  async function renderTaskList() {
    const list = $("task-list");
    if (!list) return; // 元素缺失（测试桩）时忽略
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
    for (const t of tasks) {
      const row = el("div", { class: "task-row" });
      row.appendChild(el("span", { class: "task-name" }, t.name || "?"));
      row.appendChild(el("span", { class: "task-badge" }, t.type || "evolution"));
      const cfg = (t.config && typeof t.config === "object") ? t.config : {};
      const hints = [];
      if (cfg.project) hints.push(cfg.project);
      if (t.interval != null) hints.push(_t("app.taskInterval", { n: t.interval ?? "-" }));
      if (t.enabled === false) hints.push(_t("app.taskDisabled"));
      row.appendChild(el("span", { class: "task-hint" }, hints.join(" · ")));
      const actions = el("span", { class: "task-actions" });
      // 触发（复用 /trigger 语义）
      const trigBtn = el("button", { type: "button", class: "model-action-btn", title: _t("settings.taskTrigger") }, _t("settings.taskTrigger"));
      trigBtn.addEventListener("click", async () => {
        try {
          const res = await window.emrg.triggerTask({ name: t.name });
          if (res && res.error) Chat.addSystemMessage(_t("app.triggerFailed", { msg: res.error }));
          else Chat.addSystemMessage(_t("app.triggered", { n: t.name }));
        } catch (e) {
          Chat.addSystemMessage(_t("app.triggerFailed", { msg: e.message }));
        }
      });
      actions.appendChild(trigBtn);
      // 编辑（表单预填 → taskUpdate）
      const editBtn = el("button", { type: "button", class: "model-action-btn", title: _t("settings.taskEdit") }, _t("settings.taskEdit"));
      editBtn.addEventListener("click", () => openTaskForm(t));
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
              await renderTaskList();
            } catch (e) {
              Chat.addSystemMessage(_t("app.tasksFailed", { msg: e.message }));
            }
          },
        });
      });
      actions.appendChild(delBtn);
      row.appendChild(actions);
      list.appendChild(row);
    }
  }

  function openTaskForm(task = null) {
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
      closeTaskForm();
      await renderTaskList();
    } catch (e) {
      Chat.addSystemMessage(_t("app.tasksFailed", { msg: e.message }));
    }
  }

  // ── 自定义类型管理（决策点①⑥：内置只读；决策点②：被引用拒绝删除） ──
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
      // 内置只读（决策点①⑥）：不渲染任何操作按钮
      if (!t.builtin) {
        const actions = el("span", { class: "task-actions" });
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
                await loadTaskMeta(); // 刷新类型选项（任务表单下拉）
                await renderTemplateList();
              } catch (e) {
                // 决策点②：被任务引用的类型 daemon 拒绝删除（错误信息含任务数）
                Chat.addSystemMessage(_t("settings.templateDeleteFailed", { msg: e.message }));
              }
            },
          });
        });
        actions.appendChild(delBtn);
        row.appendChild(actions);
      }
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
    $("task-template-name").value = tpl ? tpl.name : "";
    $("task-template-name").disabled = Boolean(tpl); // daemon 以 name 定位 → 不可改名
    $("task-template-prompt").value = tpl ? (tpl.prompt || "") : "";
    $("task-template-form").classList.remove("hidden");
    $("task-template-name").focus();
  }

  function closeTemplateForm() {
    $("task-template-form").classList.add("hidden");
    editingTemplate = null;
    $("task-template-name").disabled = false;
    $("task-template-name").value = "";
    $("task-template-prompt").value = "";
  }

  async function saveTemplateForm() {
    const name = $("task-template-name").value.trim();
    const prompt = $("task-template-prompt").value;
    if (!name || !prompt.trim()) {
      showConfirm(_t("dlg.stepTitle"), _t("settings.templateInvalid"), { okText: _t("dlg.gotIt"), danger: false });
      return;
    }
    try {
      if (editingTemplate === null) await window.emrg.taskTemplateCreate({ name, prompt });
      else await window.emrg.taskTemplateUpdate({ name, prompt });
      Chat.addSystemMessage(_t("settings.templateSaved"));
      closeTemplateForm();
      await loadTaskMeta();
      await renderTemplateList();
    } catch (e) {
      Chat.addSystemMessage(_t("settings.templateSaveFailed", { msg: e.message }));
    }
  }

  function initTaskManagement() {
    const addBtn = $("task-add-btn");
    if (addBtn) addBtn.addEventListener("click", () => openTaskForm(null));
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
        const row = el("div", { class: "task-row" });
        const ts = String(r.timestamp || "").slice(0, 16).replace("T", " ");
        row.appendChild(el("span", { class: "task-name" }, ts));
        // 状态徽标
        const st = r.status || "pending";
        const stText = st === "completed" ? _t("rants.statusCompleted") : st === "in_progress" ? _t("rants.statusInProgress") : _t("rants.statusPending");
        row.appendChild(el("span", { class: `task-badge ${st === "completed" ? "badge-done" : ""}` }, stText));
        // 项目 + 进度摘要
        const hints = [];
        if (r.project) hints.push(r.project);
        if (r.progress) hints.push(String(r.progress).slice(0, 40) + (String(r.progress).length > 40 ? "…" : ""));
        row.appendChild(el("span", { class: "task-hint" }, hints.join(" · ") || "—"));
        // 详情展开（完整内容 + progress）
        const msg = r.message || "";
        row.addEventListener("click", () => {
          const detail = list.querySelector(".rant-detail");
          if (detail) detail.remove();
          const detailRow = el("div", { class: "rant-detail", style: "padding:6px 8px;border-top:1px solid var(--border);font-size:var(--fs-secondary);" });
          detailRow.appendChild(el("div", {}, `${_t("rants.detail")}: ${msg}`));
          if (r.progress) detailRow.appendChild(el("div", {}, `${_t("rants.statusInProgress")}: ${r.progress}`));
          else detailRow.appendChild(el("div", {}, _t("rants.noProgress")));
          row.after(detailRow);
        });
        list.appendChild(row);
      }
    } catch (e) {
      list.innerHTML = `<div class="help-row"><span class="help-hint">${_t("rants.loadFailed", { msg: e.message })}</span></div>`;
    }
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
    refreshUpdateCheck,
    initUpdateCheckButton,
    promptUpdateAtStartup,
    showUpdateInstallConfirm, // rant 12:10:12：已下载安装包 → 一键安装确认
    initOpenSessionDialog, // P5：打开会话对话框初始化
    showOpenSessionDialog, // P5：两步打开会话
    initNewSessionDialog, // P5 slice 2：新建会话对话框初始化
    showNewSessionDialog, // P5 slice 2：新建会话（选项目）
    initTaskManagement, // rant 18:23:15 P3：定时任务/自定义类型管理初始化
    loadTaskMeta, // rant 18:23:15 P3：任务/类型元数据加载（面板打开/测试复用）
    renderTaskList, // rant 18:23:15 P3：任务列表渲染（测试/刷新复用）
    renderProjectList, // rant 14:10:14 P5：项目面板列表渲染（测试/刷新复用）
    showProjectSessionsInPanel, // rant 14:10:14 P5：项目面板内嵌会话列表（测试复用）
    initRantPanel, // rant 14:10:14 P4：rant 面板初始化
    renderRantList, // rant 14:10:14 P4：rant 列表渲染（测试/刷新复用）
    setRantFilter, // rant 14:10:14 P4：rant 状态筛选（测试复用）
    openRantForm, // rant 14:10:14 P4：新建 rant 表单（测试复用）
    submitRantForm, // rant 14:10:14 P4：rant 表单提交（测试复用）
    saveTaskForm, // rant 18:23:15 P3：任务表单提交（测试复用）
    saveTemplateForm, // rant 18:23:15 P3：类型表单提交（测试复用）
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
