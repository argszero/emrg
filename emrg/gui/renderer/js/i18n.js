"use strict";
/**
 * i18n.js — 轻量国际化（零依赖，rant 21:19 Stage 1）。
 * - 词典 { zh, en }，键为文案 ID
 * - detectLocale()：navigator.language 检测（zh* → 中文，其他 → 英文）
 * - getLocale()/setLocale()：localStorage "emrg.locale" 手动覆盖（""=跟随系统）
 * - t(key, params)：取词，支持 {var} 插值；zh 兜底
 * - apply()：应用 data-i18n / data-i18n-placeholder / data-i18n-title 静态文案，
 *   并刷新 EMRG_Copy 词典与 App.refreshLocale()（动态文案）
 * 测试环境（vm 沙箱）可能没有 navigator / localStorage → 全部守卫。
 */

const I18N = (() => {
  const LOCALE_KEY = "emrg.locale";

  // ── 词典 ────────────────────────────────
  const DICTS = {
    zh: {
      // 侧边栏
      "sidebar.newChat": "＋ 新对话",
      "sidebar.newChatTitle": "新对话 (⌘N)",
      "sidebar.settings": "设置",

      // 空状态欢迎屏
      "empty.hello": "你好，我是 EMRG",
      "empty.sub": "你的 AI 工作伙伴。问我任何事——写东西、整理文件、规划任务，都可以。",
      "empty.example1": "帮我写一份本周的工作周报",
      "empty.example2": "帮我整理这个文件夹里的文件",
      "empty.example3": "帮我规划一次周末旅行",

      // 输入区
      "composer.modelLoading": "加载中…",
      "composer.switchModel": "切换模型",
      "composer.modeTitle": "工作模式：Ask 只对话 / Auto 自动执行",
      "composer.modeAsk": "Ask",
      "composer.modeAuto": "Auto",
      "composer.placeholder": "发消息给 EMRG… (Enter 发送 / Shift+Enter 换行)",
      "composer.send": "发送",
      "composer.stop": "停止回复",
      "composer.hint": "EMRG 可能会犯错，请核对重要信息",

      // 结果面板
      "result.title": "产物",
      "result.collapse": "折叠/展开 (⌘\\)",
      "result.empty": "还没有产物，对话中生成的文件会出现在这里",

      // 设置
      "settings.title": "设置",
      "settings.groupModel": "模型服务",
      "settings.apiKey": "API Key",
      "settings.baseUrl": "接口地址",
      "settings.availableModels": "可用模型",
      "settings.addModel": "＋ 添加模型",
      "settings.name": "名称",
      "settings.modelId": "模型 ID（选填）",
      "settings.modelIdPlaceholder": "默认同名称",
      "settings.vision": "支持图片",
      "settings.cancel": "取消",
      "settings.saveModel": "保存模型",
      "settings.defaultHint": "点左侧圆点可设为默认模型，切换后下一条消息即生效",
      "settings.groupWorkdir": "工作目录",
      "settings.workdirDesc": "EMRG 在哪里帮你干活",
      "settings.choose": "选择…",
      "settings.groupAppearance": "外观",
      "settings.theme": "主题",
      "settings.themeLight": "浅色",
      "settings.themeDark": "深色",
      "settings.themeSystem": "跟随系统",
      "settings.groupLanguage": "语言",
      "settings.langFollowSystem": "跟随系统",
      "settings.langZh": "中文",
      "settings.langEn": "English",
      "settings.save": "保存",
      "settings.readFailed": "读取设置失败了：{msg}",
      "settings.saveFailed": "保存失败了：{msg}",

      // 首启引导
      "welcome.title": "欢迎使用 EMRG",
      "welcome.sub": "我是你的 AI 工作伙伴。先做两步简单设置，马上就能开始。",
      "welcome.step1": "第一步：告诉我 API Key",
      "welcome.step1Hint": "用于连接你的 AI 模型服务",
      "welcome.baseUrl": "接口地址",
      "welcome.defaultModel": "默认模型",
      "welcome.step2": "第二步：选择工作目录（可跳过）",
      "welcome.projectPlaceholder": "默认 ~/.emrg/evolution",
      "welcome.step2Hint": "EMRG 会在这个目录里帮你干活",
      "welcome.saveStart": "保存并开始",

      // 确认 / 重命名
      "confirm.title": "确认",
      "confirm.delete": "删除",
      "rename.title": "重命名对话",
      "rename.label": "给这段对话起个好记的名字",
      "rename.placeholder": "例如：周报草稿",

      // 指令帮助
      "help.title": "/ 指令帮助",
      "help.desc": "以 / 开头的指令在输入框执行，或点击补全菜单选择。",
      "help.close": "关闭",

      // 会话 / 回退 / 记忆 / 技能对话框
      "sessions.title": "切换对话",
      "sessions.desc": "点击切换，或输入 /resume <id> 直接切换。",
      "rewind.title": "回退到历史消息点",
      "rewind.desc": "选择要保留到的消息点，之后的对话将被移除。",
      "rewind.cancel": "取消",
      "memory.title": "记忆",
      "memory.desc": "EMRG 记住的长期信息，点击查看详情。",
      "skills.title": "技能",
      "skills.desc": "EMRG 已加载的技能。",

      // 进化 / 任务
      "rant.title": "🧬 进化 — 告诉 EMRG 往哪里走",
      "rant.desc": "你的输入会驱动 EMRG 的自我进化——它会认真读，并据此改进自己。",
      "rant.project": "项目（可选）",
      "rant.message": "你的想法",
      "rant.placeholder": "哪里不好用、想要什么新功能、或者希望它怎么改进…",
      "rant.submit": "驱动进化",
      "tasks.title": "后台任务",
      "tasks.desc": "点击任务立即触发一次运行。",

      // / 指令 hint（commands.js 补全菜单）
      "cmd.clear.hint": "清空当前对话",
      "cmd.compact.hint": "压缩当前对话历史",
      "cmd.version.hint": "显示版本与实例信息",
      "cmd.help.hint": "查看全部指令说明",
      "cmd.image.hint": "发送图片（请直接粘贴）",
      "cmd.delete.hint": "删除当前对话",
      "cmd.rename.hint": "重命名当前对话",
      "cmd.resume.hint": "切换/恢复对话",
      "cmd.rewind.hint": "回退到历史消息点",
      "cmd.sessions.hint": "查看全部对话",
      "cmd.model.hint": "切换模型",
      "cmd.memory.hint": "浏览记忆",
      "cmd.skills.hint": "查看已加载技能",
      "cmd.rant.hint": "驱动 EMRG 进化",
      "cmd.trigger.hint": "触发后台任务",

      // 系统状态文案（copywriting.js）
      "copy.disconnected": "连接中断了，正在重新连接…",
      "copy.reconnected": "回来了，我们继续 ✦",
      "copy.sessionBusy": "我还在处理上一条，稍等一下哦",
      "copy.sendFailed": "没发送成功，你的话我还留着，再试一次？",
      "copy.deleteConfirmTitle": "删除这段对话？",
      "copy.deleteConfirmBody": "删除后无法恢复。",
      "copy.noSessions": "还没有对话",
      "copy.aboutEvolution": "EMRG 已自我成长 {n} 次，感谢你的每一次反馈",
      "copy.aboutEvolutionEmpty": "EMRG 正在成长中",
      // WorkBuddy P3（#501）：成长卡 + 进化 toast
      "copy.growthCount": "已自我进化 {n} 次",
      "copy.growthNote": "边工作边学习，越用越懂你",
      "copy.evolutionToastTitle": "EMRG 刚刚完成一次自我进化！",
      "copy.evolutionToastMsg": "这是它的第 {n} 次自我改进，现在更好用了。",
      "copy.evolutionToastSee": "去看看",
      "copy.evolutionToastDismiss": "知道了",

      // 工具短语（copywriting.js）
      "tool.bash.doing": "正在运行命令…",
      "tool.bash.done": "已运行命令",
      "tool.read.doing": "正在读取文件…",
      "tool.read.done": "已读取文件",
      "tool.write.doing": "正在写入文件…",
      "tool.write.done": "已写入文件",
      "tool.edit.doing": "正在修改文件…",
      "tool.edit.done": "已修改文件",
      "tool.glob.doing": "正在查找文件…",
      "tool.glob.done": "已找到文件",
      "tool.grep.doing": "正在搜索内容…",
      "tool.grep.done": "已完成搜索",
      "tool.fallback.doing": "正在处理…",
      "tool.fallback.done": "已完成",
      "tool.failText": "这一步没成功，我换个方法试试",
    },

    en: {
      // Sidebar
      "sidebar.newChat": "＋ New chat",
      "sidebar.newChatTitle": "New chat (⌘N)",
      "sidebar.settings": "Settings",

      // Empty state
      "empty.hello": "Hi, I'm EMRG",
      "empty.sub": "Your AI work partner. Ask me anything — write, organize files, plan tasks.",
      "empty.example1": "Write this week's work report",
      "empty.example2": "Organize the files in this folder",
      "empty.example3": "Plan a weekend trip",

      // Composer
      "composer.modelLoading": "Loading…",
      "composer.switchModel": "Switch model",
      "composer.modeTitle": "Work mode: Ask (chat only) / Auto (run tools)",
      "composer.modeAsk": "Ask",
      "composer.modeAuto": "Auto",
      "composer.placeholder": "Message EMRG… (Enter to send / Shift+Enter for new line)",
      "composer.send": "Send",
      "composer.stop": "Stop reply",
      "composer.hint": "EMRG may make mistakes — please verify important information",

      // Result panel
      "result.title": "Artifacts",
      "result.collapse": "Collapse/expand (⌘\\)",
      "result.empty": "No artifacts yet — files generated in the conversation will appear here",

      // Settings
      "settings.title": "Settings",
      "settings.groupModel": "Model service",
      "settings.apiKey": "API Key",
      "settings.baseUrl": "Base URL",
      "settings.availableModels": "Available models",
      "settings.addModel": "＋ Add model",
      "settings.name": "Name",
      "settings.modelId": "Model ID (optional)",
      "settings.modelIdPlaceholder": "Defaults to name",
      "settings.vision": "Supports images",
      "settings.cancel": "Cancel",
      "settings.saveModel": "Save model",
      "settings.defaultHint": "Click the dot on the left to set the default model — takes effect on the next message",
      "settings.groupWorkdir": "Working directory",
      "settings.workdirDesc": "Where EMRG does work for you",
      "settings.choose": "Choose…",
      "settings.groupAppearance": "Appearance",
      "settings.theme": "Theme",
      "settings.themeLight": "Light",
      "settings.themeDark": "Dark",
      "settings.themeSystem": "System",
      "settings.groupLanguage": "Language",
      "settings.langFollowSystem": "System",
      "settings.langZh": "中文",
      "settings.langEn": "English",
      "settings.save": "Save",
      "settings.readFailed": "Failed to read settings: {msg}",
      "settings.saveFailed": "Failed to save settings: {msg}",

      // Welcome / onboarding
      "welcome.title": "Welcome to EMRG",
      "welcome.sub": "I'm your AI work partner. Two quick steps and you're ready to go.",
      "welcome.step1": "Step 1: enter your API Key",
      "welcome.step1Hint": "Used to connect to your AI model service",
      "welcome.baseUrl": "Base URL",
      "welcome.defaultModel": "Default model",
      "welcome.step2": "Step 2: choose a working directory (optional)",
      "welcome.projectPlaceholder": "Default: ~/.emrg/evolution",
      "welcome.step2Hint": "EMRG will work for you in this directory",
      "welcome.saveStart": "Save & start",

      // Confirm / rename
      "confirm.title": "Confirm",
      "confirm.delete": "Delete",
      "rename.title": "Rename conversation",
      "rename.label": "Give this conversation a memorable name",
      "rename.placeholder": "e.g. Weekly report draft",

      // Commands help
      "help.title": "/ Commands help",
      "help.desc": "Type a / command in the input box, or pick one from the autocomplete menu.",
      "help.close": "Close",

      // Sessions / rewind / memory / skills dialogs
      "sessions.title": "Switch conversation",
      "sessions.desc": "Click to switch, or type /resume <id> to switch directly.",
      "rewind.title": "Rewind to a history point",
      "rewind.desc": "Choose the message point to keep — later messages will be removed.",
      "rewind.cancel": "Cancel",
      "memory.title": "Memory",
      "memory.desc": "Long-term information EMRG remembers. Click to view details.",
      "skills.title": "Skills",
      "skills.desc": "Skills EMRG has loaded.",

      // Evolution / tasks
      "rant.title": "🧬 Evolve — tell EMRG where to go",
      "rant.desc": "Your input drives EMRG's self-evolution — it reads carefully and improves itself.",
      "rant.project": "Project (optional)",
      "rant.message": "Your thoughts",
      "rant.placeholder": "What's not working, what features you want, or how it should improve…",
      "rant.submit": "Evolve",
      "tasks.title": "Background tasks",
      "tasks.desc": "Click a task to trigger it immediately.",

      // / command hints
      "cmd.clear.hint": "Clear current conversation",
      "cmd.compact.hint": "Compact current conversation history",
      "cmd.version.hint": "Show version & instance info",
      "cmd.help.hint": "View all commands",
      "cmd.image.hint": "Send an image (just paste it)",
      "cmd.delete.hint": "Delete current conversation",
      "cmd.rename.hint": "Rename current conversation",
      "cmd.resume.hint": "Switch / resume a conversation",
      "cmd.rewind.hint": "Rewind to a history point",
      "cmd.sessions.hint": "View all conversations",
      "cmd.model.hint": "Switch model",
      "cmd.memory.hint": "Browse memory",
      "cmd.skills.hint": "View loaded skills",
      "cmd.rant.hint": "Drive EMRG's evolution",
      "cmd.trigger.hint": "Trigger a background task",

      // System status copy
      "copy.disconnected": "Connection lost — reconnecting…",
      "copy.reconnected": "I'm back, let's continue ✦",
      "copy.sessionBusy": "Still working on the previous message — one moment",
      "copy.sendFailed": "Message not sent — I kept your text, try again?",
      "copy.deleteConfirmTitle": "Delete this conversation?",
      "copy.deleteConfirmBody": "This cannot be undone.",
      "copy.noSessions": "No conversations yet",
      "copy.aboutEvolution": "EMRG has self-evolved {n} times — thanks for every bit of feedback",
      "copy.aboutEvolutionEmpty": "EMRG is still growing",
      // WorkBuddy P3 (#501): growth card + evolution toast
      "copy.growthCount": "Self-evolved {n} times",
      "copy.growthNote": "Learning as I work — getting better with use",
      "copy.evolutionToastTitle": "EMRG just completed a self-evolution!",
      "copy.evolutionToastMsg": "That's its {n}th self-improvement — it's even better now.",
      "copy.evolutionToastSee": "See details",
      "copy.evolutionToastDismiss": "Got it",

      // Tool phrases
      "tool.bash.doing": "Running a command…",
      "tool.bash.done": "Command run",
      "tool.read.doing": "Reading a file…",
      "tool.read.done": "File read",
      "tool.write.doing": "Writing a file…",
      "tool.write.done": "File written",
      "tool.edit.doing": "Editing a file…",
      "tool.edit.done": "File edited",
      "tool.glob.doing": "Finding files…",
      "tool.glob.done": "Files found",
      "tool.grep.doing": "Searching content…",
      "tool.grep.done": "Search done",
      "tool.fallback.doing": "Working on it…",
      "tool.fallback.done": "Done",
      "tool.failText": "That step didn't work — let me try another approach",
    },
  };

  // ── 工具函数 ─────────────────────────────
  function detectLocale() {
    try {
      const lang = (typeof navigator !== "undefined" && navigator.language) || "";
      return /^zh/i.test(lang) ? "zh" : "en";
    } catch { return "zh"; }
  }

  function getLocale() {
    let saved = null;
    try {
      if (typeof localStorage !== "undefined") saved = localStorage.getItem(LOCALE_KEY);
    } catch { /* ignore */ }
    if (saved === "zh" || saved === "en") return saved;
    return detectLocale();
  }

  /** 手动覆盖语言：""=跟随系统 / "zh" / "en" */
  function setLocale(loc) {
    try {
      if (typeof localStorage !== "undefined") {
        localStorage.setItem(LOCALE_KEY, loc || "");
      }
    } catch { /* ignore */ }
    apply();
  }

  /** 取词：t(key, {var: value})；zh 兜底 */
  function t(key, params) {
    const dict = DICTS[getLocale()] || DICTS.zh;
    let s = key in dict ? dict[key] : (DICTS.zh[key] ?? key);
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        s = s.replaceAll(`{${k}}`, String(v));
      }
    }
    return s;
  }

  /** 应用静态文案：data-i18n / data-i18n-placeholder / data-i18n-title，并刷新动态文案 */
  function apply() {
    try {
      if (typeof document === "undefined" || !document.querySelectorAll) return;
      document.querySelectorAll("[data-i18n]").forEach((node) => {
        const key = node.getAttribute("data-i18n");
        if (key) node.textContent = t(key);
      });
      document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
        const key = node.getAttribute("data-i18n-placeholder");
        if (key) node.placeholder = t(key);
      });
      document.querySelectorAll("[data-i18n-title]").forEach((node) => {
        const key = node.getAttribute("data-i18n-title");
        if (key) node.title = t(key);
      });
    } catch { /* 沙箱/低版本环境忽略 */ }
    // 刷新动态文案（copywriting 词典重建 + app 动态渲染）
    try {
      if (typeof window !== "undefined" && window.EMRG_Copy && window.EMRG_Copy.refresh) {
        window.EMRG_Copy.refresh();
      }
      if (typeof window !== "undefined" && window.App && window.App.refreshLocale) {
        window.App.refreshLocale();
      }
    } catch { /* ignore */ }
  }

  return { DICTS, detectLocale, getLocale, setLocale, t, apply };
})();

window.EMRG_I18N = I18N;
