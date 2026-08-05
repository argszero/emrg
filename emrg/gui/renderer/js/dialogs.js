"use strict";
/**
 * dialogs.js — 设置 / 首启引导 / 确认对话框。
 * - 设置：模型服务（API Key/接口地址/默认模型）、工作目录、外观（主题三选：浅色/深色/跟随系统）
 * - 首启：品牌欢迎语 + 口语化文案（P4 完善多模型管理）
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

  // ── 设置 ─────────────────────────────────
  async function showSettings() {
    try {
      const s = await window.emrg.getSettings();
      $("set-api-key").value = s.apiKey || "";
      $("set-base-url").value = s.baseUrl || "";
      $("set-project-dir").value = s.projectDir || "";
      if (s.theme) theme = s.theme;
      renderThemeOptions();
      const sel = $("set-model");
      sel.innerHTML = "";
      const models = s.models?.length ? s.models : s.model ? [s.model] : [];
      for (const m of models) {
        const opt = el("option", { value: m }, m);
        if (m === s.model) opt.selected = true;
        sel.appendChild(opt);
      }
      if (sel.selectedIndex < 0) sel.selectedIndex = 0; // G144
    } catch (e) {
      Chat.addSystemMessage(`读取设置失败了：${e.message}`);
    }
    $("settings-dialog").showModal();
  }

  async function saveSettings() {
    const config = {
      apiKey: $("set-api-key").value.trim(),
      baseUrl: $("set-base-url").value.trim(),
      model: $("set-model").value,
      projectDir: $("set-project-dir").value.trim(),
      theme,
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
      const models = s.models?.length ? s.models : ["deepseek-chat", "deepseek-v3", "gpt-4o"];
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
