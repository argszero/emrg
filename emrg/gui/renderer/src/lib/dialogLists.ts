/**
 * dialogLists.ts — 列表式对话框纯逻辑（Batch 4 slice 2，P4 slice 6）。
 * 从 vanilla `js/app.js` showMemoryDialog/showSkillsDialog 迁出的行格式化逻辑：
 * - memoryRowLabel/Hint：标题 40 字符截断 + 摘要 50 字符截断（vanilla 同款）
 * - skillRowLabel/Hint：名称 + source·description 拼接（vanilla 同款）
 * - truncate：通用截断工具
 */

/** 通用截断：超长取前 n 字符（vanilla String.slice(0, n) 语义） */
export function truncate(s: string, n: number): string {
  return String(s).slice(0, n);
}

export interface MemoryRow {
  id: string;
  title?: string;
  summary?: string;
  content?: string;
}

export interface MemoryRowView {
  /** 点击读取详情的回调标识（memory id） */
  id: string;
  label: string;
  hint: string;
}

/** 记忆行格式化：label = title||id||unnamed（截 40）；hint = summary||content（截 50） */
export function memoryRowView(m: MemoryRow, unnamedText: string): MemoryRowView {
  const title = m.title || m.id || unnamedText;
  const hint = (m.summary || m.content || "").slice(0, 50);
  return { id: m.id, label: truncate(String(title), 40), hint };
}

export interface SkillRow {
  name?: string;
  source?: string;
  description?: string;
}

export interface SkillRowView {
  label: string;
  hint: string;
}

/** 技能行格式化：label = name||unnamed；hint = source + " · " + description(截 50) */
export function skillRowView(s: SkillRow, unnamedText: string): SkillRowView {
  const label = s.name || unnamedText;
  const hint = `${s.source || ""}${s.description ? ` · ${s.description.slice(0, 50)}` : ""}`;
  return { label, hint };
}

export interface HelpRowView {
  cmd: string;
  hint: string;
}

/** 帮助行：COMMANDS 条目 → {cmd, hint}（hint 经 i18n 解析，vanilla hintText 语义） */
export function helpRows(
  commands: Record<string, unknown>,
  hintOf: (cmd: string) => string,
): HelpRowView[] {
  return Object.entries(commands).map(([cmd]) => ({ cmd, hint: hintOf(cmd) }));
}
