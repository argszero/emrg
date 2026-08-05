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
        defaultName || "未配置",
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
      const hint = el("div", { class: "model-list-empty" }, "暂无其他模型，点下方按钮添加");
      list.appendChild(hint);
    }
  }

  function renderModelItem(name, isDefault, vision, modelId) {
    const row = el("div", { class: "model-item" + (isDefault ? " default" : "") });
    // 单选圆点 → 设为默认
    const radio = el("button", { class: "model-radio" + (isDefault ? " checked" : ""), title: isDefault ? "当前默认模型" : "设为默认" });
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
    if (isDefault) title.appendChild(el("span", { class: "model-badge" }, "默认"));
    row.appendChild(title);
    if (modelId) row.appendChild(el("span", { class: "model-id" }, modelId));
    if (vision) row.appendChild(el("span", { class: "model-vision" }, "🖼 支持图片"));
    // 操作：编辑（非默认行 → 打开表单填本条）；删除（默认不可删）
    const actions = el("span", { class: "model-actions" });
    const editBtn = el("button", { class: "model-action-btn", title: "编辑" }, "编辑");
    editBtn.addEventListener("click", () => openModelForm(name, isDefault));
    actions.appendChild(editBtn);
    if (!isDefault) {
      const delBtn = el("button", { class: "model-action-btn danger", title: "删除" }, "删除");
      delBtn.addEventListener("click", () => {
        showConfirm("删除这个模型？", `「${name}」将从可用模型里移除。`, {
          okText: "删除",
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
      showConfirm("还差一步", "模型名称是必填的哦。", { okText: "知道了", danger: false });
      return;
    }
    const modelId = $("model-form-id").value.trim() || name;
    const vision = $("model-form-vision").checked;

    if (editingName === null) {
      // 添加新模型
      if (name === defaultName || extraModels.some((m) => m.name === name)) {
        showConfirm("重名了", "这个名称已经有模型了，换个名字吧。", { okText: "知道了", danger: false });
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
        showConfirm("重名了", "这个名称是默认模型，请在默认行编辑。", { okText: "知道了", danger: false });
        return;
      }
      if (name !== editingName && extraModels.some((m) => m.name === name)) {
        showConfirm("重名了", "这个名称已经有模型了，换个名字吧。", { okText: "知道了", danger: false });
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
      defaultName = s.model || "";
      extraModels = (s.modelDetails || []).map((m) => ({
        name: m.name || "",
        model: m.model || m.name || "",
        vision: Boolean(m.vision),
      }));
      renderModelList();
    } catch (e) {
      Chat.addSystemMessage(`读取设置失败了：${e.message}`);
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
      showConfirm("还差一步", "API Key 是连接模型服务必需的，填上才能继续哦。", {
        okText: "知道了",
        danger: false,
      });
      return;
    }
    try {
      await window.emrg.saveSettings(config);
      if ($("settings-dialog").open) $("settings-dialog").close();
      Chat.addSystemMessage("设置已保存。");
      App.state.apiKeyConfigured = true;
      App.state.projectDir = config.projectDir || App.state.projectDir;
      App.state.model = defaultName;
      App.updateModelSwitcher();
      await App.boot();
    } catch (e) {
      Chat.addSystemMessage(`保存失败了：${e.message}`);
    }
  }

  // ── 首启引导 ─────────────────────────────
  function showWelcome() {
    $("welcome-project-dir").value = "";
    $("welcome-api-key").value = "";
    $("welcome-base-url").value = "";
    $("welcome-model").innerHTML = "<option value=''>加载中…</option>";
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
      showConfirm("还差一步", "API Key 是连接模型服务必需的，填上才能继续哦。", {
        okText: "知道了",
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
      Chat.addSystemMessage("设置完成，正在启动…");
      App.state.apiKeyConfigured = true;
      App.state.projectDir = config.projectDir || App.state.projectDir;
      await App.boot();
    } catch (e) {
      Chat.addSystemMessage(`初始化失败了：${e.message}`);
    }
  }

  // ── 确认对话框（替代 confirm/alert） ────
  let confirmCb = null;
  function showConfirm(title, message, opts = {}) {
    $("confirm-title").textContent = title;
    $("confirm-message").textContent = message;
    const ok = $("confirm-ok");
    ok.textContent = opts.okText || "删除";
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
    initModelForm,
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
