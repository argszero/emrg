"use strict";
/**
 * copywriting.js — 去黑话文案映射（非开发者友好，rant 21:19 起经 i18n 词典取词）。
 * 工具名 → 友好的动词短语；系统级状态 → 口语化鼓励性文案。
 * refresh()：locale 切换后重建 COPY（i18n.apply 会调用）。
 * 注意：EMRG_Copy 需在 i18n.js 之后加载（引用 EMRG_I18N.t）。
 */

const EMRG_Copy = (() => {
  const _t = (key, params) => {
    try {
      return window.EMRG_I18N ? window.EMRG_I18N.t(key, params) : key;
    } catch { return key; }
  };

  /** 工具名 → 进行中 / 完成 短语（渐进披露：默认折叠，点开展示原始输出） */
  function toolPhrases(name) {
    const base = name || "fallback";
    return {
      doing: _t(`tool.${base}.doing`),
      done: _t(`tool.${base}.done`),
    };
  }

  /** 系统状态文案（locale 敏感：切换语言后 refresh 重建；含 #501 P3 成长卡/toast 键） */
  function buildCopy() {
    return {
      disconnected: _t("copy.disconnected"),
      reconnected: _t("copy.reconnected"),
      sessionBusy: _t("copy.sessionBusy"),
      sendFailed: _t("copy.sendFailed"),
      deleteConfirmTitle: _t("copy.deleteConfirmTitle"),
      deleteConfirmBody: _t("copy.deleteConfirmBody"),
      noSessions: _t("copy.noSessions"),
      aboutEvolution: (n) =>
        n ? _t("copy.aboutEvolution", { n }) : _t("copy.aboutEvolutionEmpty"),
      // WorkBuddy P3（#501）：成长卡 + 进化 toast
      growthCount: (n) => _t("copy.growthCount", { n }),
      growthNote: _t("copy.growthNote"),
      evolutionToastTitle: _t("copy.evolutionToastTitle"),
      evolutionToastMsg: (n) => _t("copy.evolutionToastMsg", { n }),
      evolutionToastSee: _t("copy.evolutionToastSee"),
      evolutionToastDismiss: _t("copy.evolutionToastDismiss"),
    };
  }
  let COPY = buildCopy();
  let failText = _t("tool.failText"); // 工具失败文案（字符串属性，chat.js 直接赋值用）

  function refresh() {
    COPY = buildCopy();
    failText = _t("tool.failText");
  }

  return {
    toolPhrases,
    COPY,
    refresh,
    get TOOL_FAIL_TEXT() { return failText; },
  };
})();

window.EMRG_Copy = EMRG_Copy;
