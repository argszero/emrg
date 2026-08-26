import { createContext, useContext, useMemo, type ReactNode } from "react";
import { DICTS } from "./i18n-dicts";

/**
 * i18n.tsx — React i18n context（Batch 1 remainder：完整词典 + 原生逻辑移植）。
 * 设计文档 §5 Batch 1 项 3：i18n.js → lib/i18n.ts，词典原样搬，detectLocale/apply 逻辑保留。
 * - 完整词典 373 keys（zh/en 严格对齐）位于 ./i18n-dicts.ts（由 vanilla i18n.js 机械生成）
 * - t(key, params)：zh 兜底 + {var} 插值（与 vanilla t() 语义一致）
 * - detectLocale/getLocale/setLocale：navigator 检测 + localStorage "emrg.locale" 覆盖
 * - I18nProvider 仍支持注入自定义词典（测试/主题覆盖），与默认全词典合并
 */
export type TranslateFn = (key: string, params?: Record<string, unknown>) => string;

interface I18nContextValue {
  lang: string;
  t: TranslateFn;
}

export const LOCALE_KEY = "emrg.locale";
export type Locale = "zh" | "en";

/** 完整词典（373 keys，zh/en 严格对齐）——由 vanilla i18n.js 机械生成，见 ./i18n-dicts.ts */
export { DICTS, ZH_DICT, EN_DICT } from "./i18n-dicts";

/** React 骨架专属 key（vanilla 词典无此分区；Batch 2+ 聊天区落地后并入组件词典） */
const SHELL_DICT: Record<string, Record<string, string>> = {
  zh: {
    "shell.batch0Notice": "EMRG React 骨架（Batch 0）— 迁移进行中",
    "shell.placeholder": "React 组件树迁移分 6 批进行（Batch 0–5），当前为基建批次。",
  },
  en: {
    "shell.batch0Notice": "EMRG React shell (Batch 0) — migration in progress",
    "shell.placeholder": "The React component tree migrates in 6 batches (Batch 0–5); this is the infrastructure batch.",
  },
};

/** 默认全词典 = vanilla 373 keys + React 骨架扩展 */
export const DEFAULT_DICTS: Record<string, Record<string, string>> = {
  zh: { ...DICTS.zh, ...SHELL_DICT.zh },
  en: { ...DICTS.en, ...SHELL_DICT.en },
};

const I18nContext = createContext<I18nContextValue | null>(null);

/** navigator.language 检测（zh* → 中文，其他 → 英文）；navigator 异常 → zh 兜底（vanilla 同语义） */
export function detectLocale(lang?: string): Locale {
  try {
    const l = (lang ?? (typeof navigator !== "undefined" ? navigator.language : "")) || "";
    return /^zh/i.test(l) ? "zh" : "en";
  } catch {
    return "zh";
  }
}

/** localStorage 手动覆盖（""=跟随系统）；不可用时回退系统检测 */
export function getLocale(): Locale {
  let saved: string | null = null;
  try {
    if (typeof localStorage !== "undefined") saved = localStorage.getItem(LOCALE_KEY);
  } catch {
    /* ignore */
  }
  if (saved === "zh" || saved === "en") return saved;
  return detectLocale();
}

/** 手动覆盖语言：""=跟随系统 / "zh" / "en"；返回生效的 locale */
export function setLocale(loc: "" | Locale): Locale {
  try {
    if (typeof localStorage !== "undefined") localStorage.setItem(LOCALE_KEY, loc || "");
  } catch {
    /* ignore */
  }
  return getLocale();
}

/** 取词：t(key, {var: value})；当前语言缺失 → zh 兜底 → 原样返回 key（vanilla 同语义） */
export function t(
  key: string,
  params?: Record<string, unknown>,
  lang?: string,
  dicts: Record<string, Record<string, string>> = DICTS,
): string {
  const locale = (lang as Locale) || getLocale();
  const dict = dicts[locale] || dicts.zh;
  let s = key in dict ? dict[key] : (dicts.zh?.[key] ?? key);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      s = s.replaceAll(`{${k}}`, String(v));
    }
  }
  return s;
}

function interpolate(template: string, params?: Record<string, unknown>): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (m, k) => (k in params ? String(params[k]) : m));
}

export function I18nProvider({
  lang,
  dicts,
  children,
}: {
  /** 语言提示（缺省用 navigator.language；测试可注入） */
  lang?: string;
  /** 自定义词典（与默认全词典合并；测试/主题覆盖用） */
  dicts?: Record<string, Record<string, string>>;
  children: ReactNode;
}) {
  const l = detectLocale(lang);
  const value = useMemo<I18nContextValue>(() => {
    const dict = { ...(DEFAULT_DICTS[l] ?? DEFAULT_DICTS.en), ...(dicts?.[l] ?? {}) };
    return {
      lang: l,
      t: (key, params) => interpolate(key in dict ? dict[key] : (DICTS.zh[key] ?? key), params),
    };
  }, [l, dicts]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

/** 取词 hook；无 Provider 时回退默认词典（保证任何组件可安全调用） */
export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (ctx) return ctx;
  const l = detectLocale();
  const dict = DEFAULT_DICTS[l] ?? DEFAULT_DICTS.en;
  return {
    lang: l,
    t: (key, params) => interpolate(key in dict ? dict[key] : (DICTS.zh[key] ?? key), params),
  };
}
