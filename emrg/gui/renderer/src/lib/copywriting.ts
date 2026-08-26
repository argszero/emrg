import type { TranslateFn } from "./utils";

/**
 * copywriting.ts — 去黑话文案映射（vanilla renderer/js/copywriting.js 迁移，Batch 1）。
 * 纯逻辑：t 由调用方注入（i18n context），不依赖 window.EMRG_I18N。
 * 契约与 vanilla 版一致：toolPhrases / buildCopy / refresh / TOOL_FAIL_TEXT。
 */
export interface ToolPhrases {
  doing: string;
  done: string;
}

export interface CopyDict {
  disconnected: string;
  reconnected: string;
  sessionBusy: string;
  sendFailed: string;
  deleteConfirmTitle: string;
  deleteConfirmBody: string;
  noSessions: string;
  aboutEvolution: (n: number) => string;
  growthCount: (n: number) => string;
  growthNote: string;
  evolutionToastTitle: string;
  evolutionToastMsg: (n: number) => string;
  evolutionToastSee: string;
  evolutionToastDismiss: string;
}

/** 工具名 → 进行中 / 完成 短语（渐进披露：默认折叠，点开展示原始输出） */
export function toolPhrases(name: string | undefined | null, t: TranslateFn): ToolPhrases {
  const base = name || "fallback";
  return {
    doing: t(`tool.${base}.doing`),
    done: t(`tool.${base}.done`),
  };
}

/** 系统状态文案（locale 敏感：切换语言后 refresh 重建；含 #501 P3 成长卡/toast 键） */
export function buildCopy(t: TranslateFn): CopyDict {
  return {
    disconnected: t("copy.disconnected"),
    reconnected: t("copy.reconnected"),
    sessionBusy: t("copy.sessionBusy"),
    sendFailed: t("copy.sendFailed"),
    deleteConfirmTitle: t("copy.deleteConfirmTitle"),
    deleteConfirmBody: t("copy.deleteConfirmBody"),
    noSessions: t("copy.noSessions"),
    aboutEvolution: (n) =>
      n ? t("copy.aboutEvolution", { n }) : t("copy.aboutEvolutionEmpty"),
    // WorkBuddy P3（#501）：成长卡 + 进化 toast
    growthCount: (n) => t("copy.growthCount", { n }),
    growthNote: t("copy.growthNote"),
    evolutionToastTitle: t("copy.evolutionToastTitle"),
    evolutionToastMsg: (n) => t("copy.evolutionToastMsg", { n }),
    evolutionToastSee: t("copy.evolutionToastSee"),
    evolutionToastDismiss: t("copy.evolutionToastDismiss"),
  };
}

/** 实例工厂：持有当前 COPY + failText，refresh() 重建（locale 切换时调用） */
export interface Copywriting {
  toolPhrases: (name: string | undefined | null) => ToolPhrases;
  COPY: CopyDict;
  refresh: () => void;
  /** 工具失败文案（字符串属性，chat 区直接赋值用） */
  TOOL_FAIL_TEXT: string;
}

export function createCopywriting(t: TranslateFn): Copywriting {
  let COPY = buildCopy(t);
  let failText = t("tool.failText");

  function refresh(): void {
    COPY = buildCopy(t);
    failText = t("tool.failText");
  }

  return {
    toolPhrases: (name) => toolPhrases(name, t),
    get COPY() {
      return COPY;
    },
    refresh,
    get TOOL_FAIL_TEXT() {
      return failText;
    },
  };
}
