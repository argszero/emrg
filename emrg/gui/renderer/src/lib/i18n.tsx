import { createContext, useContext, useMemo, type ReactNode } from "react";

/**
 * i18n.ts — React i18n context（Batch 0 骨架）。
 * 设计文档 §5 Batch 0 项 3：i18n context 先就位；完整词典（934 行 zh/en）
 * 原样迁移在 Batch 1（i18n.js → lib/i18n.ts），届时 dicts prop 传入真实词典。
 * 本文件仅含 Shell 占位所需的最小词典 + {n} 占位替换。
 */
export type TranslateFn = (key: string, params?: Record<string, unknown>) => string;

interface I18nContextValue {
  lang: string;
  t: TranslateFn;
}

/** Batch 0 最小词典（完整词典 Batch 1 迁移） */
const DEFAULT_DICTS: Record<string, Record<string, string>> = {
  zh: {
    "shell.batch0Notice": "EMRG React 骨架（Batch 0）— 迁移进行中",
    "shell.placeholder": "React 组件树迁移分 6 批进行（Batch 0–5），当前为基建批次。",
  },
  en: {
    "shell.batch0Notice": "EMRG React shell (Batch 0) — migration in progress",
    "shell.placeholder": "The React component tree migrates in 6 batches (Batch 0–5); this is the infrastructure batch.",
  },
};

const I18nContext = createContext<I18nContextValue | null>(null);

function detectLang(lang?: string): "zh" | "en" {
  const l = (lang ?? (typeof navigator !== "undefined" ? navigator.language : "zh-CN")) || "";
  return l.toLowerCase().startsWith("zh") ? "zh" : "en";
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
  /** 自定义词典（Batch 1 起传入完整 zh/en 词典；与默认词典合并） */
  dicts?: Record<string, Record<string, string>>;
  children: ReactNode;
}) {
  const l = detectLang(lang);
  const value = useMemo<I18nContextValue>(() => {
    const dict = { ...(DEFAULT_DICTS[l] ?? DEFAULT_DICTS.en), ...(dicts?.[l] ?? {}) };
    return {
      lang: l,
      t: (key, params) => interpolate(dict[key] ?? key, params),
    };
  }, [l, dicts]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

/** 取词 hook；无 Provider 时回退默认词典（保证任何组件可安全调用） */
export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (ctx) return ctx;
  const l = detectLang();
  const dict = DEFAULT_DICTS[l] ?? DEFAULT_DICTS.en;
  return {
    lang: l,
    t: (key, params) => interpolate(dict[key] ?? key, params),
  };
}
