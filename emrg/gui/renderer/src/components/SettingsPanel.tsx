import { useEffect, useReducer, useRef, useState } from "react";
import { useI18n, setLocale, type Locale } from "../lib/i18n";
import { dialogReducer, initialDialogState } from "../lib/dialog";
import { ConfirmDialog } from "./ConfirmDialog";

/**
 * SettingsPanel — Batch 5 slice 6：设置面板接线（vanilla dialogs.js showSettings/
 * saveSettings/renderModelList 语义；类名与 vanilla CSS 一致：.panel-tabs/.panel-tab/
 * .panel-tab-body/.settings-group/.model-item/.model-form/.theme-options）。
 *
 * - 数据经 window.emrg 加载（getSettings/saveSettings/githubStatus/githubConnect/
 *   githubDisconnect）；window.emrg 缺失（jsdom / 未接 preload）时降级为表单直显、
 *   保存 no-op —— 与 DialogHost 同款降级策略。
 * - 五个 tab：模型服务 / GitHub / 外观 / 语言 / 关于；model tab 支持
 *   默认模型 + [[llm.models]] 增删改设默认（vanilla renderModelList/saveModelForm 对等）。
 * - 主题三选（vanilla applyTheme 对等：data-theme attr）；语言选择（i18n setLocale）。
 */
export type SettingsTab = "model" | "github" | "appearance" | "language" | "about" | "templates";

export interface SettingsPanelProps {
  /** 版本号（Shell appState.currentVersion 注入） */
  version?: string;
  /** 进化计数（Shell appState.evolutionCount 注入） */
  evolutionCount?: number | null;
}

interface ModelRec {
  name: string;
  model: string;
  vision: boolean;
}

/** 自定义任务类型（daemon task_template_* 对等：name 为定位键，builtin 只读） */
interface TemplateRec {
  name: string;
  prompt?: string;
  template?: string;
  builtin?: boolean;
}

interface GetSettingsResult {
  apiKey?: string;
  baseUrl?: string;
  model?: string;
  modelDetails?: { name: string; model?: string; vision?: boolean }[];
  theme?: string;
  lang?: string;
}

/** SettingsPanel 用到的 preload 桥方法（与 preload.js 通道对齐） */
interface EmrgBridge {
  getSettings(): Promise<GetSettingsResult>;
  saveSettings(cfg: unknown): Promise<unknown>;
  githubStatus(): Promise<{ authenticated?: boolean; user?: string | null }>;
  githubConnect(p: { token: string }): Promise<{ ok?: boolean; user?: string; error?: string }>;
  githubDisconnect(): Promise<unknown>;
  taskTemplateList(): Promise<TemplateRec[]>;
  taskTemplateCreate(p: { name: string; prompt: string }): Promise<unknown>;
  taskTemplateUpdate(p: { name: string; prompt: string }): Promise<unknown>;
  taskTemplateDelete(p: { name: string }): Promise<unknown>;
}

function bridge(): EmrgBridge | undefined {
  return (window as unknown as { emrg?: EmrgBridge }).emrg;
}

const TABS: { id: SettingsTab; labelKey: string }[] = [
  { id: "model", labelKey: "settings.groupModel" },
  { id: "github", labelKey: "settings.groupGithub" },
  { id: "appearance", labelKey: "settings.groupAppearance" },
  { id: "language", labelKey: "settings.groupLanguage" },
  { id: "about", labelKey: "settings.aboutTitle" },
  { id: "templates", labelKey: "settings.taskTemplates" },
];

export function SettingsPanel({ version = "", evolutionCount = null }: SettingsPanelProps) {
  const { t } = useI18n();
  const [tab, setTab] = useState<SettingsTab>("model");
  // model tab 表单态
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [defaultName, setDefaultName] = useState("");
  const [extraModels, setExtraModels] = useState<ModelRec[]>([]);
  // 主题 / GitHub 状态
  const [theme, setTheme] = useState<string>("system");
  const [github, setGithub] = useState<{ authenticated: boolean; user: string | null } | null>(null);
  const [githubToken, setGithubToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  // model 行内表单态（vanilla openModelForm/saveModelForm）
  const [formOpen, setFormOpen] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [formName, setFormName] = useState("");
  const [formId, setFormId] = useState("");
  const [formVision, setFormVision] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);
  // templates tab 状态（vanilla renderTemplateList/openTemplateForm/saveTemplateForm）
  const [templates, setTemplates] = useState<TemplateRec[] | null>(null);
  const [tplFormOpen, setTplFormOpen] = useState(false);
  const [editingTpl, setEditingTpl] = useState<string | null>(null);
  const [tplBuiltinView, setTplBuiltinView] = useState(false);
  const [tplName, setTplName] = useState("");
  const [tplPrompt, setTplPrompt] = useState("");
  // 删除确认（lib/dialog reducer + ConfirmDialog，vanilla showConfirm 对等）
  const [dialogState, dispatch] = useReducer(dialogReducer, initialDialogState);

  // 打开 settings 面板 → 加载设置（vanilla showSettings）
  useEffect(() => {
    const b = bridge();
    if (!b?.getSettings) return;
    b.getSettings()
      .then((s) => {
        setApiKey(s.apiKey || "");
        setBaseUrl(s.baseUrl || "");
        setDefaultName(s.model || "");
        setTheme(s.theme || "system");
        setExtraModels(
          (s.modelDetails || []).map((m) => ({
            name: m.name || "",
            model: m.model && m.model !== m.name ? m.model : "",
            vision: Boolean(m.vision),
          })),
        );
        applyTheme(s.theme || "system");
        // 语言持久化（rant 22:22:50）：config.toml 有 lang → 应用；""（跟随系统/旧配置）→ 不动
        if (s.lang) setLocale(s.lang as "" | Locale);
      })
      .catch((e: Error) => setMsg({ kind: "err", text: t("settings.readFailed", { msg: e.message }) }));
    void refreshGithubStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // templates tab 激活 → 加载类型列表（vanilla initTaskManagement 每次打开刷新）
  useEffect(() => {
    if (tab === "templates") void loadTemplates();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  async function refreshGithubStatus() {
    const b = bridge();
    if (!b?.githubStatus) return;
    try {
      const s = await b.githubStatus();
      setGithub({ authenticated: Boolean(s.authenticated), user: s.user || null });
    } catch {
      setGithub({ authenticated: false, user: null });
    }
  }

  function applyTheme(mode: string) {
    const root = document.documentElement;
    if (mode === "system" || !mode) root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", mode);
  }

  /** 外观偏好即点即持久化（rant 22:22:50）：theme/lang 点击 → saveSettings 落盘
   *  config.toml（重开面板/重启 GUI 不回退）。成功静默；失败显示错误消息。 */
  async function persistAppearance(patch: { theme?: string; lang?: string }) {
    const b = bridge();
    if (!b?.saveSettings) return;
    try {
      await b.saveSettings(patch);
    } catch (e: unknown) {
      setMsg({ kind: "err", text: t("settings.saveFailed", { msg: (e as Error).message }) });
    }
  }

  // ── model tab ─────────────────────────────
  function openModelForm(name: string | null, isDefault: boolean) {
    setEditingName(isDefault ? name : name);
    setFormName(name || "");
    const found = extraModels.find((m) => m.name === name);
    setFormId(found?.model && found.model !== found.name ? found.model : "");
    setFormVision(Boolean(found?.vision));
    setFormOpen(true);
    setTimeout(() => nameRef.current?.focus(), 0);
  }

  function closeModelForm() {
    setFormOpen(false);
    setEditingName(null);
    setFormName("");
    setFormId("");
    setFormVision(false);
  }

  function saveModelForm() {
    const name = formName.trim();
    if (!name) {
      setMsg({ kind: "err", text: t("dlg.nameRequiredBody") });
      return;
    }
    const modelId = formId.trim() || name;
    const vision = formVision;
    if (editingName === null) {
      // 添加新模型
      if (name === defaultName || extraModels.some((m) => m.name === name)) {
        setMsg({ kind: "err", text: t("dlg.duplicateBody") });
        return;
      }
      setExtraModels((prev) => [...prev, { name, model: modelId, vision }]);
    } else if (editingName === defaultName) {
      // 编辑默认模型：更新默认名 + 配置项
      setExtraModels((prev) => [
        ...prev.filter((m) => m.name !== editingName),
        { name, model: modelId, vision },
      ]);
      setDefaultName(name);
    } else {
      if (name === defaultName) {
        setMsg({ kind: "err", text: t("dlg.duplicateDefaultBody") });
        return;
      }
      if (name !== editingName && extraModels.some((m) => m.name === name)) {
        setMsg({ kind: "err", text: t("dlg.duplicateBody") });
        return;
      }
      setExtraModels((prev) =>
        prev.map((m) => (m.name === editingName ? { name, model: modelId, vision } : m)),
      );
    }
    closeModelForm();
  }

  function deleteModel(name: string) {
    setExtraModels((prev) => prev.filter((m) => m.name !== name));
  }

  function setDefault(name: string) {
    setDefaultName(name);
  }

  async function saveSettings() {
    if (!apiKey.trim()) {
      setMsg({ kind: "err", text: t("app.authKeyRequired") });
      return;
    }
    const b = bridge();
    if (!b?.saveSettings) {
      setMsg({ kind: "ok", text: t("dlg.saved") });
      return;
    }
    setBusy(true);
    try {
      await b.saveSettings({
        apiKey: apiKey.trim(),
        baseUrl: baseUrl.trim(),
        model: defaultName,
        theme,
        models: [
          ...(defaultName && !extraModels.some((m) => m.name === defaultName)
            ? [{ name: defaultName, model: defaultName, vision: false }]
            : []),
          ...extraModels,
        ],
      });
      setMsg({ kind: "ok", text: t("dlg.saved") });
    } catch (e: unknown) {
      setMsg({ kind: "err", text: t("settings.saveFailed", { msg: (e as Error).message }) });
    } finally {
      setBusy(false);
    }
  }

  // ── github tab ────────────────────────────
  async function connectGithub() {
    const b = bridge();
    if (!b?.githubConnect) return;
    setBusy(true);
    try {
      const res = await b.githubConnect({ token: githubToken });
      if (res.ok) {
        setMsg({ kind: "ok", text: t("settings.githubConnected", { user: res.user || "" }) });
        setGithub({ authenticated: true, user: res.user || null });
        setGithubToken("");
      } else {
        setMsg({ kind: "err", text: t("settings.githubConnectFailed", { msg: res.error || t("app.unknownError") }) });
      }
    } catch (e: unknown) {
      setMsg({ kind: "err", text: t("settings.githubConnectFailed", { msg: (e as Error).message }) });
    } finally {
      setBusy(false);
    }
  }

  async function disconnectGithub() {
    const b = bridge();
    if (!b?.githubDisconnect) return;
    setBusy(true);
    try {
      await b.githubDisconnect();
      setMsg({ kind: "ok", text: t("settings.githubDisconnected") });
      setGithub({ authenticated: false, user: null });
    } catch (e: unknown) {
      setMsg({ kind: "err", text: t("settings.githubDisconnectFailed", { msg: (e as Error).message }) });
    } finally {
      setBusy(false);
    }
  }

  // ── templates tab（自定义任务类型管理，vanilla dialogs.js 语义） ──
  async function loadTemplates() {
    const b = bridge();
    if (!b?.taskTemplateList) return;
    try {
      setTemplates(await b.taskTemplateList());
    } catch {
      setTemplates([]); // 列表加载失败 → 空态（vanilla 同款 templateEmpty 兜底）
    }
  }

  function openTplForm(tpl: TemplateRec | null) {
    setEditingTpl(tpl ? tpl.name : null);
    setTplBuiltinView(Boolean(tpl && tpl.builtin)); // 内置只读查看（隐藏保存）
    setTplName(tpl ? tpl.name : "");
    setTplPrompt(tpl ? tpl.prompt || tpl.template || "" : "");
    setTplFormOpen(true);
  }

  function closeTplForm() {
    setTplFormOpen(false);
    setEditingTpl(null);
    setTplBuiltinView(false);
    setTplName("");
    setTplPrompt("");
  }

  async function saveTplForm() {
    const name = tplName.trim();
    const prompt = tplPrompt.trim();
    if (!name || !prompt) {
      setMsg({ kind: "err", text: t("settings.templateInvalid") });
      return;
    }
    const b = bridge();
    if (!b?.taskTemplateCreate || !b?.taskTemplateUpdate) return;
    try {
      if (editingTpl === null) await b.taskTemplateCreate({ name, prompt });
      else await b.taskTemplateUpdate({ name, prompt });
      setMsg({ kind: "ok", text: t("settings.templateSaved") });
      closeTplForm();
      await loadTemplates();
    } catch (e: unknown) {
      setMsg({ kind: "err", text: t("settings.templateSaveFailed", { msg: (e as Error).message }) });
    }
  }

  function requestDeleteTpl(tpl: TemplateRec) {
    dispatch({
      type: "open-confirm",
      payload: {
        title: t("settings.templateDelete"),
        message: t("settings.templateDeleteConfirm", { name: tpl.name }),
        okText: t("settings.templateDelete"),
        danger: true,
        onOk: () => doDeleteTpl(tpl.name),
      },
    });
  }

  async function doDeleteTpl(name: string) {
    const b = bridge();
    if (!b?.taskTemplateDelete) return;
    try {
      await b.taskTemplateDelete({ name });
      setMsg({ kind: "ok", text: t("settings.templateDeleted") });
      await loadTemplates();
    } catch (e: unknown) {
      // 决策点②：被任务引用的类型 daemon 拒绝删除（错误信息含任务数）
      setMsg({ kind: "err", text: t("settings.templateDeleteFailed", { msg: (e as Error).message }) });
    }
  }

  // ── 渲染 ─────────────────────────────────
  const seen = new Set<string>([defaultName]);

  return (
    <section className="workspace-view" data-view="settings" data-testid="panel-settings">
      <div className="workspace-view-body">
        <h2 className="workspace-view-title">{t("settings.title")}</h2>
        <div className="panel-tabs" role="tablist" data-testid="settings-tabs">
          {TABS.map(({ id, labelKey }) => (
            <button
              key={id}
              type="button"
              className={`panel-tab${tab === id ? " active" : ""}`}
              data-settings-tab={id}
              data-testid={`settings-tab-${id}`}
              onClick={() => setTab(id)}
            >
              {t(labelKey)}
            </button>
          ))}
        </div>

        {msg && (
          <div className={`settings-msg ${msg.kind}`} data-testid="settings-msg">
            {msg.text}
          </div>
        )}

        {/* ── 模型服务 ── */}
        {tab === "model" && (
          <div className="panel-tab-body" data-settings-body="model" data-testid="settings-body-model">
            <div className="settings-group">
              <label>
                <span>{t("settings.apiKey")}</span>
                <input
                  type="password"
                  data-testid="set-api-key"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                />
              </label>
              <label>
                <span>{t("settings.baseUrl")}</span>
                <input
                  type="text"
                  data-testid="set-base-url"
                  placeholder="https://api.deepseek.com"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                />
              </label>
              <div className="settings-group-title" style={{ marginTop: 14 }}>
                {t("settings.availableModels")}
              </div>
              <div className="model-list" data-testid="model-list">
                {/* 默认行 */}
                <ModelItem
                  name={defaultName || t("dlg.notConfigured")}
                  isDefault
                  vision={extraModels.find((m) => m.name === defaultName)?.vision ?? false}
                  modelId={extraModels.find((m) => m.name === defaultName)?.model ?? ""}
                  t={t}
                  onEdit={() => openModelForm(defaultName, true)}
                />
                {/* 额外模型行（去重） */}
                {extraModels.map((m) => {
                  if (seen.has(m.name)) return null;
                  seen.add(m.name);
                  return (
                    <ModelItem
                      key={m.name}
                      name={m.name}
                      isDefault={false}
                      vision={m.vision}
                      modelId={m.model}
                      t={t}
                      onSetDefault={() => setDefault(m.name)}
                      onEdit={() => openModelForm(m.name, false)}
                      onDelete={() => deleteModel(m.name)}
                    />
                  );
                })}
                {extraModels.filter((m) => m.name !== defaultName).length === 0 && (
                  <div className="model-list-empty">{t("dlg.noOtherModels")}</div>
                )}
              </div>
              <button
                type="button"
                className="btn btn-ghost"
                style={{ width: "100%", marginTop: 6 }}
                data-testid="add-model-btn"
                onClick={() => openModelForm(null, false)}
              >
                {t("settings.addModel")}
              </button>
              {formOpen && (
                <div className="model-form" data-testid="model-form">
                  <div className="model-form-row">
                    <label className="model-form-label">
                      {t("settings.name")}
                      <input
                        ref={nameRef}
                        type="text"
                        data-testid="model-form-name"
                        placeholder={t("settings.modelNamePlaceholder")}
                        maxLength={40}
                        value={formName}
                        onChange={(e) => setFormName(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") { e.preventDefault(); saveModelForm(); }
                          else if (e.key === "Escape") { e.preventDefault(); closeModelForm(); }
                        }}
                      />
                    </label>
                    <label className="model-form-label">
                      {t("settings.modelId")}
                      <input
                        type="text"
                        data-testid="model-form-id"
                        placeholder={t("settings.modelIdPlaceholder")}
                        maxLength={60}
                        value={formId}
                        onChange={(e) => setFormId(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") { e.preventDefault(); saveModelForm(); }
                          else if (e.key === "Escape") { e.preventDefault(); closeModelForm(); }
                        }}
                      />
                    </label>
                  </div>
                  <div className="model-form-footer">
                    <label className="model-form-vision">
                      <input
                        type="checkbox"
                        data-testid="model-form-vision"
                        checked={formVision}
                        onChange={(e) => setFormVision(e.target.checked)}
                      />{" "}
                      <span>{t("settings.vision")}</span>
                    </label>
                    <div style={{ display: "flex", gap: 8, marginLeft: "auto" }}>
                      <button type="button" className="btn btn-ghost" style={{ padding: "6px 12px", minHeight: 30 }} onClick={closeModelForm}>
                        {t("settings.cancel")}
                      </button>
                      <button type="button" className="btn btn-primary" style={{ padding: "6px 12px", minHeight: 30 }} data-testid="model-form-save" onClick={saveModelForm}>
                        {t("settings.saveModel")}
                      </button>
                    </div>
                  </div>
                </div>
              )}
              <div className="hint" style={{ marginTop: 8 }}>{t("settings.defaultHint")}</div>
            </div>
          </div>
        )}

        {/* ── GitHub ── */}
        {tab === "github" && (
          <div className="panel-tab-body" data-settings-body="github" data-testid="settings-body-github">
            <div className="settings-group">
              <div className="hint" style={{ marginBottom: 6 }} data-testid="github-status">
                {github === null
                  ? "—"
                  : github.authenticated
                    ? t("settings.githubConnectedStatus", { user: github.user || "" })
                    : t("settings.githubNotConnected")}
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  type="password"
                  placeholder="GitHub Personal Access Token"
                  style={{ flex: 1, minWidth: 0 }}
                  data-testid="set-github-token"
                  value={githubToken}
                  onChange={(e) => setGithubToken(e.target.value)}
                />
                <button
                  type="button"
                  className="btn btn-primary"
                  data-testid="github-connect-btn"
                  disabled={busy || !githubToken.trim()}
                  onClick={connectGithub}
                >
                  {t("settings.githubConnect")}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  data-testid="github-disconnect-btn"
                  disabled={busy}
                  onClick={disconnectGithub}
                >
                  {t("settings.githubDisconnect")}
                </button>
              </div>
              <div className="hint" style={{ marginTop: 6 }}>{t("settings.githubHint")}</div>
            </div>
          </div>
        )}

        {/* ── 外观 ── */}
        {tab === "appearance" && (
          <div className="panel-tab-body" data-settings-body="appearance" data-testid="settings-body-appearance">
            <div className="settings-group">
              <label>
                <span>{t("settings.theme")}</span>
                <div className="theme-options" data-testid="theme-options">
                  {(["light", "dark", "system"] as const).map((m) => (
                    <button
                      key={m}
                      type="button"
                      className={`theme-option${theme === m ? " active" : ""}`}
                      data-theme={m}
                      data-testid={`theme-${m}`}
                      onClick={() => {
                        setTheme(m);
                        applyTheme(m);
                        void persistAppearance({ theme: m });
                      }}
                    >
                      {t(`settings.theme${m[0].toUpperCase()}${m.slice(1)}`)}
                    </button>
                  ))}
                </div>
              </label>
            </div>
          </div>
        )}

        {/* ── 语言 ── */}
        {tab === "language" && (
          <div className="panel-tab-body" data-settings-body="language" data-testid="settings-body-language">
            <div className="settings-group">
              <label>
                <span>{t("settings.groupLanguage")}</span>
                <div className="theme-options" data-testid="lang-options">
                  {(["", "zh", "en"] as const).map((loc) => (
                    <button
                      key={loc || "system"}
                      type="button"
                      className="theme-option"
                      data-lang={loc}
                      data-testid={`lang-${loc || "system"}`}
                      onClick={() => {
                        setLocale(loc);
                        void persistAppearance({ lang: loc });
                      }}
                    >
                      {t(
                        loc === "" ? "settings.langFollowSystem" : loc === "zh" ? "settings.langZh" : "settings.langEn",
                      )}
                    </button>
                  ))}
                </div>
              </label>
            </div>
          </div>
        )}

        {/* ── 关于 ── */}
        {tab === "about" && (
          <div className="panel-tab-body" data-settings-body="about" data-testid="settings-body-about">
            <div className="settings-group">
              <div className="about-box">
                <div className="about-line" data-testid="about-line">
                  EMRG <span>v{version || "0.2.8"}</span> ·{" "}
                  <span>
                    🌱 {t("copy.growthCountPrefix")} {evolutionCount ?? 0} {t("copy.times")}
                  </span>
                </div>
                <div className="hint" style={{ marginTop: 6 }}>{t("settings.aboutDesc")}</div>
              </div>
            </div>
          </div>
        )}

        {/* ── 自定义类型（task templates，vanilla renderTemplateList 对等） ── */}
        {tab === "templates" && (
          <div className="panel-tab-body" data-settings-body="templates" data-testid="settings-body-templates">
            <div className="settings-group">
              <div className="settings-group-title">{t("settings.taskTemplates")}</div>
              <div className="task-list" data-testid="template-list">
                {!templates || templates.length === 0 ? (
                  <div className="task-empty" data-testid="template-empty">{t("settings.templateEmpty")}</div>
                ) : (
                  templates.map((tpl) => {
                    const body = tpl.prompt || tpl.template || "";
                    return (
                      <div className="task-row" key={tpl.name} data-testid="template-row">
                        <span className="task-name">{tpl.name}</span>
                        <span className="task-badge">{tpl.builtin ? t("settings.taskBuiltin") : t("settings.taskCustom")}</span>
                        <span className="task-hint">{body.length > 60 ? `${body.slice(0, 60)}…` : body}</span>
                        <span className="task-actions">
                          <button type="button" className="model-action-btn" data-testid={`template-view-${tpl.name}`} onClick={() => openTplForm(tpl)}>
                            {t("settings.templateView")}
                          </button>
                          {!tpl.builtin && (
                            <>
                              <button type="button" className="model-action-btn" data-testid={`template-edit-${tpl.name}`} onClick={() => openTplForm(tpl)}>
                                {t("settings.templateEdit")}
                              </button>
                              <button type="button" className="model-action-btn danger" data-testid={`template-delete-${tpl.name}`} onClick={() => requestDeleteTpl(tpl)}>
                                {t("settings.templateDelete")}
                              </button>
                            </>
                          )}
                        </span>
                      </div>
                    );
                  })
                )}
              </div>
              <button
                type="button"
                className="btn btn-ghost"
                style={{ width: "100%", marginTop: 6 }}
                data-testid="add-template-btn"
                onClick={() => openTplForm(null)}
              >
                {t("settings.templateAdd")}
              </button>
              {tplFormOpen && (
                <div className="model-form" data-testid="template-form">
                  <div className="model-form-row">
                    <label className="model-form-label">
                      {t("settings.templateName")}
                      <input
                        type="text"
                        data-testid="template-form-name"
                        maxLength={40}
                        disabled={editingTpl !== null} // daemon 以 name 定位 → 不可改名
                        value={tplName}
                        onChange={(e) => setTplName(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") { e.preventDefault(); saveTplForm(); }
                          else if (e.key === "Escape") { e.preventDefault(); closeTplForm(); }
                        }}
                      />
                    </label>
                  </div>
                  <label className="model-form-label">
                    {t("settings.templatePrompt")}
                    {/* vanilla 无 monaco 时回退轻量 shim（读写 .value）——textarea 即等价物 */}
                    <textarea
                      rows={6}
                      data-testid="template-form-prompt"
                      value={tplPrompt}
                      onChange={(e) => setTplPrompt(e.target.value)}
                    />
                  </label>
                  <div className="model-form-footer">
                    <div style={{ display: "flex", gap: 8, marginLeft: "auto" }}>
                      <button type="button" className="btn btn-ghost" style={{ padding: "6px 12px", minHeight: 30 }} onClick={closeTplForm}>
                        {t("settings.cancel")}
                      </button>
                      {!tplBuiltinView && (
                        <button type="button" className="btn btn-primary" style={{ padding: "6px 12px", minHeight: 30 }} data-testid="template-form-save" onClick={saveTplForm}>
                          {t("settings.templateSave")}
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        <div className="panel-actions" style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12 }}>
          <button type="button" className="btn btn-primary" data-testid="settings-save" disabled={busy} onClick={saveSettings}>
            {t("settings.save")}
          </button>
        </div>
      </div>
      <ConfirmDialog request={dialogState.confirm} onDismiss={() => dispatch({ type: "close-confirm" })} />
    </section>
  );
}

/** 模型行（vanilla renderModelItem 对等） */
function ModelItem({
  name,
  isDefault,
  vision,
  modelId,
  t,
  onSetDefault,
  onEdit,
  onDelete,
}: {
  name: string;
  isDefault: boolean;
  vision: boolean;
  modelId: string;
  t: (k: string, v?: Record<string, unknown>) => string;
  onSetDefault?: () => void;
  onEdit: () => void;
  onDelete?: () => void;
}) {
  return (
    <div className={`model-item${isDefault ? " default" : ""}`} data-testid="model-item">
      <button
        type="button"
        className={`model-radio${isDefault ? " checked" : ""}`}
        title={isDefault ? t("dlg.currentDefault") : t("dlg.setDefault")}
        data-testid={isDefault ? "model-radio-default" : "model-radio"}
        onClick={() => !isDefault && onSetDefault?.()}
      >
        {isDefault ? "●" : "○"}
      </button>
      <span className="model-name">
        {name}
        {isDefault && <span className="model-badge">{t("dlg.defaultBadge")}</span>}
      </span>
      {modelId && modelId !== name && <span className="model-id">{modelId}</span>}
      {vision && <span className="model-vision">{t("dlg.supportsImages")}</span>}
      <span className="model-actions">
        <button type="button" className="model-action-btn" title={t("dlg.edit")} onClick={onEdit}>
          {t("dlg.edit")}
        </button>
        {!isDefault && onDelete && (
          <button type="button" className="model-action-btn danger" title={t("dlg.delete")} onClick={onDelete}>
            {t("dlg.delete")}
          </button>
        )}
      </span>
    </div>
  );
}
