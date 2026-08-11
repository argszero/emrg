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
      "sidebar.openChat": "打开会话",
      "sidebar.openChatTitle": "打开会话（跨项目）",
      "sidebar.settings": "设置",
      "sidebar.statusTitle": "连接状态",
      "sidebar.backToBottom": "回到底部",

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
      "settings.modelNamePlaceholder": "如 deepseek-v3",
      "settings.modelId": "模型 ID（选填）",
      "settings.modelIdPlaceholder": "默认同名称",
      "settings.vision": "支持图片",
      "settings.cancel": "取消",
      "settings.aboutTitle": "关于",
      "settings.aboutDesc": "EMRG 是一个会自我进化的 AI 智能体——每次改进都会自动汇报，你可以随时在这里看到它的成长。",
      "settings.recentTitle": "最近改进",
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
      // Windows GCM rant Stage 2：GitHub 连接
      "settings.groupGithub": "GitHub 连接",
      "settings.githubTokenPlaceholder": "GitHub Personal Access Token",
      "settings.githubConnect": "连接",
      "settings.githubDisconnect": "断开",
      "settings.githubHint": "用于自进化推送 PR；授权后自动执行 gh auth setup-git，git 操作不再弹 GCM",
      "settings.githubChecking": "检查中…",
      "settings.githubConnectedStatus": "已连接 @{user}",
      "settings.githubNotConnected": "未连接",
      "settings.githubStatusFailed": "状态获取失败",
      "settings.updateAvailable": "发现新版本 v{latest} —— 点击前往 Releases 下载（不会自动安装）",
      "settings.checkUpdate": "检查更新",
      "settings.checkingUpdate": "检查中…",
      "app.updateAvailable": "发现新版本 v{latest} —— 点击前往 Releases 下载（不会自动安装）：https://github.com/argszero/emrg/releases",
      "settings.githubTokenEmpty": "请先粘贴 GitHub Personal Access Token",
      "settings.githubConnecting": "连接中…",
      "settings.githubConnected": "已连接 GitHub：@{user}（gh auth setup-git 已执行）",
      "settings.githubConnectFailed": "GitHub 连接失败：{msg}",
      "settings.githubDisconnected": "已断开 GitHub 连接",
      "settings.githubDisconnectFailed": "断开失败：{msg}",
      // Windows GCM rant Stage 2b：device flow
      "settings.githubDeviceTitle": "连接 GitHub",
      "settings.githubDeviceDesc": "在浏览器中打开 GitHub 并输入下方一次性代码以完成授权。",
      "settings.githubDeviceOpen": "打开浏览器",
      "settings.githubDeviceCancel": "取消",
      "settings.githubDeviceWait": "等待浏览器中确认…",
      "settings.githubDeviceFailed": "GitHub 连接失败：{msg}",
      // Windows GCM rant Stage 2：连接横幅
      "settings.githubBannerMsg": "启用自进化需连接 GitHub →",
      "settings.githubBannerConnect": "去连接",
      "settings.githubBannerDismiss": "关闭",

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
      "openSession.title": "打开会话",
      "openSession.desc": "选择项目后选择要打开的会话（跨项目多开）。",
      "openSession.titleProject": "打开会话 — {project}",
      "openSession.noProjects": "还没有项目。点下方「新建项目…」选择一个文件夹。",
      "openSession.noSessions": "该项目还没有会话，发送第一条消息会自动创建。",
      "openSession.loadFailed": "加载失败：{msg}",
      "openSession.newProject": "＋ 新建项目…",
      "openSession.projectCreated": "项目已注册：{path}",
      "openSession.projectFailed": "新建项目失败：{msg}",
      "newSession.openButton": "＋ 新建会话…",
      "newSession.title": "新建会话",
      "newSession.desc": "选择项目新建会话（跨项目多开）。",
      "newSession.newProject": "＋ 新建项目…",
      "newSession.loadFailed": "加载失败：{msg}",
      "newSession.projectFailed": "新建会话失败：{msg}",
      "deleteProject.delete": "删除",
      "deleteProject.title": "删除项目",
      "deleteProject.body": "删除项目「{name}」？只删除项目记录，磁盘数据保留（可重新注册）。",
      "deleteProject.protectedBody": "「{name}」是系统项目，不可删除。",
      "deleteProject.removed": "项目已删除：{name}",
      "deleteProject.failed": "删除项目失败：{msg}",
      "relTime.justNow": "刚刚",
      "relTime.minutesAgo": "{n} 分钟前",
      "relTime.hoursAgo": "{n} 小时前",
      "relTime.daysAgo": "{n} 天前",
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
      "cmd.open.hint": "打开会话（跨项目）",
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
      "copy.growthCardTitle": "EMRG 每次自我改进都会自动汇报",
      "copy.growthCount": "已自我进化 {n} 次",
      "copy.growthCountPrefix": "已自我进化",
      "copy.times": "次",
      "copy.growthNote": "边工作边学习，越用越懂你",
      "copy.evolutionToastTitle": "EMRG 刚刚完成一次自我进化！",
      "copy.evolutionToastMsg": "这是它的第 {n} 次自我改进，现在更好用了。",
      "copy.evolutionToastMsgStatic": "它学会了新的东西，变得更好用了。",
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

      // 聊天区（chat.js）
      "chat.copyCode": "复制",
      "chat.copied": "已复制 ✓",
      "chat.copyFailed": "复制失败",
      "chat.fromOtherClient": "（来自其他客户端）",
      "chat.timeoutWarn": "⚠️ 响应超时了，我还没说完——可以再试一次。",
      "chat.maxRoundsHint": "⚠️ 这次任务因为工具调用次数上限中断了，结果可能不完整——输入“继续”我可以接着做。",
      "chat.elapsed": "耗时 {s}",
      "chat.expand": "展开全文",
      // 时间分组（utils.js/sidebar.js）
      "util.groupToday": "今天",
      "util.groupYesterday": "昨天",
      "util.groupEarlier": "更早",
      // 结果面板（result-panel.js）
      "panel.openFailed": "无法打开文件",
      "panel.copied": "✓ 已复制",
      // Markdown 代码块（markdown.js）
      "md.copyCode": "复制代码",
      // 对话框动态文案（dialogs.js）
      "dlg.notConfigured": "未配置",
      "dlg.noOtherModels": "暂无其他模型，点下方按钮添加",
      "dlg.currentDefault": "当前默认模型",
      "dlg.setDefault": "设为默认",
      "dlg.defaultBadge": "默认",
      "dlg.supportsImages": "🖼 支持图片",
      "dlg.edit": "编辑",
      "dlg.delete": "删除",
      "dlg.deleteModelTitle": "删除这个模型？",
      "dlg.deleteModelBody": "「{name}」将从可用模型里移除。",
      "dlg.stepTitle": "还差一步",
      "dlg.nameRequiredBody": "模型名称是必填的哦。",
      "dlg.duplicateTitle": "重名了",
      "dlg.duplicateBody": "这个名称已经有模型了，换个名字吧。",
      "dlg.duplicateDefaultBody": "这个名称是默认模型，请在默认行编辑。",
      "dlg.gotIt": "知道了",
      "app.authKeyRequired": "API Key 是连接模型服务必需的，填上才能继续哦。",
      "dlg.saved": "设置已保存。",
      "dlg.loading": "加载中…",
      "dlg.starting": "设置完成，正在启动…",
      "dlg.initFailed": "初始化失败了：{msg}",
      "dlg.renameFailed": "重命名失败了：{msg}",
      // 应用动态文案（app.js）
      "app.workdirInvalid": "工作目录不可用，请到设置里改一下。",
      "app.bootFailed": "启动遇到了问题：{msg}",
      "app.needSession": "请先创建一个对话。",
      "app.recentImprovements": "最近改进",
      "app.noImprovements": "还没有改进记录，输入 /rant 驱动第一次进化吧",
      "app.cmdUnknown": "指令 {cmd} 暂未开放。",
      "app.cleared": "已清空当前对话。",
      "app.compacted": "已压缩当前对话历史。",
      "app.imagePaste": "请直接粘贴图片到输入框（Ctrl+V / ⌘V）。",
      "app.cmdFailed": "指令 {cmd} 执行失败：{msg}",
      "app.helpNoSessions": "还没有对话，输入内容即可开始。",
      "app.unnamed": "(未命名)",
      "app.current": "当前",
      "app.noHistory": "没有可回退的历史消息。",
      "app.historyFailed": "加载历史失败：{msg}",
      "app.rewound": "已回退到消息点 #{index}，移除了 {n} 条记录。",
      "app.rewindFailed": "回退失败：{msg}",
      "app.noMemories": "还没有{scope}记忆。",
      "app.sessionMem": "会话",
      "app.projectMem": "项目",
      "app.readMemFailed": "读取记忆失败：{msg}",
      "app.memFailed": "加载记忆失败：{msg}",
      "app.noSkills": "还没有加载技能。",
      "app.skillsFailed": "加载技能失败：{msg}",
      "app.globalAll": "（全局 — 所有项目）",
      "app.rantEmpty": "写点内容再提交吧。",
      "app.rantReceived": "✓ 收到！EMRG 会据此进化。",
      "app.rantCount": "（已累计 {n} 条反馈）",
      "app.rantFailed": "提交失败了：{msg}",
      "app.noTasks": "没有可触发的任务。",
      "app.taskDisabled": "已停用",
      "app.taskInterval": "间隔 {n}s",
      "app.tasksFailed": "加载任务失败：{msg}",
      "app.triggerFailed": "触发失败：{msg}",
      "app.triggered": "已触发任务 {n}。",
      "app.deletedSwitch": "这个对话已被删除，已帮你切到最近的对话。",
      "app.switched": "已切换对话。",
      "app.sessionDisconnected": "该会话连接已断开，正在自动重连…",
      "app.switchFailed": "切换对话失败了：{msg}",
      "app.tooManyOpenSessions": "打开的会话已达上限（20），请先关闭一些再打开。",
      "app.newFailed": "新建对话失败了：{msg}",
      "app.deleteFailed": "删除失败了：{msg}",
      "app.rename": "✏️ 重命名",
      "app.deleteConv": "🗑 删除对话",
      "app.closeSession": "❌ 关闭会话（保留数据）",
      "app.closeFailed": "关闭会话失败了：{msg}",
      "sidebar.openSessions": "打开的会话",
      "sidebar.noOpenSessions": "暂无打开的会话",
      "sidebar.openSessionOf": "{project} / {title}",
      "app.noModels": "还没有配置模型",
      "app.goSettings": "去设置添加",
      "app.modelSwitchFailed": "切换模型失败了：{msg}",
      "app.modelListFailed": "读取模型列表失败了：{msg}",
      "app.askModeNotice": "Ask 模式：我只对话，不执行工具。输入内容问我就好。",
      "app.unknownResult": "结果未知——连接中断",
      "app.error": "出了点问题：{msg}",
      "app.unknownError": "未知错误",
      "app.unknown": "未知",
      "app.authFailed": "认证失败了，请检查设置里的 API Key。",
      "app.daemonStopped": "daemon 启动失败（已停止自动重试）。请在终端运行 `emrg server` 排查；\n{msg}",
      "app.versionInfo": "EMRG GUI v{ver} · 实例 {id} · 模型 {model} · 已进化 {n} 次",
    },

    en: {
      // Sidebar
      "sidebar.newChat": "＋ New chat",
      "sidebar.newChatTitle": "New chat (⌘N)",
      "sidebar.openChat": "Open session",
      "sidebar.openChatTitle": "Open session (cross-project)",
      "sidebar.settings": "Settings",
      "sidebar.statusTitle": "Connection status",
      "sidebar.backToBottom": "Back to bottom",

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
      "settings.modelNamePlaceholder": "e.g. deepseek-v3",
      "settings.modelId": "Model ID (optional)",
      "settings.modelIdPlaceholder": "Defaults to name",
      "settings.vision": "Supports images",
      "settings.cancel": "Cancel",
      "settings.aboutTitle": "About",
      "settings.aboutDesc": "EMRG is a self-evolving AI agent — it reports every improvement, and you can watch it grow right here.",
      "settings.recentTitle": "Recent improvements",
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
      // Windows GCM rant Stage 2: GitHub connection
      "settings.groupGithub": "GitHub connection",
      "settings.githubTokenPlaceholder": "GitHub Personal Access Token",
      "settings.githubConnect": "Connect",
      "settings.githubDisconnect": "Disconnect",
      "settings.githubHint": "Used for self-evolution PR pushes; runs gh auth setup-git after auth so git operations never pop up GCM",
      "settings.githubChecking": "Checking…",
      "settings.githubConnectedStatus": "Connected as @{user}",
      "settings.githubNotConnected": "Not connected",
      "settings.githubStatusFailed": "Failed to load status",
      "settings.updateAvailable": "New version v{latest} available — click to visit Releases (no auto-install)",
      "settings.checkUpdate": "Check for updates",
      "settings.checkingUpdate": "Checking…",
      "app.updateAvailable": "New version v{latest} available — https://github.com/argszero/emrg/releases (no auto-install)",
      "settings.githubTokenEmpty": "Please paste a GitHub Personal Access Token first",
      "settings.githubConnecting": "Connecting…",
      "settings.githubConnected": "Connected to GitHub: @{user} (gh auth setup-git done)",
      "settings.githubConnectFailed": "GitHub connect failed: {msg}",
      "settings.githubDisconnected": "Disconnected from GitHub",
      "settings.githubDisconnectFailed": "Disconnect failed: {msg}",
      // Windows GCM rant Stage 2b: device flow
      "settings.githubDeviceTitle": "Connect GitHub",
      "settings.githubDeviceDesc": "Open GitHub in your browser and enter the one-time code below to finish authorization.",
      "settings.githubDeviceOpen": "Open browser",
      "settings.githubDeviceCancel": "Cancel",
      "settings.githubDeviceWait": "Waiting for confirmation in browser…",
      "settings.githubDeviceFailed": "GitHub connect failed: {msg}",
      // Windows GCM rant Stage 2: connect banner
      "settings.githubBannerMsg": "Connect GitHub to enable self-evolution →",
      "settings.githubBannerConnect": "Connect",
      "settings.githubBannerDismiss": "Dismiss",

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
      "openSession.title": "Open session",
      "openSession.desc": "Pick a project, then pick a session to open (multi-project tabs).",
      "openSession.titleProject": "Open session — {project}",
      "openSession.noProjects": "No projects yet. Use \"＋ New project…\" below to pick a folder.",
      "openSession.noSessions": "No sessions in this project yet — the first message creates one.",
      "openSession.loadFailed": "Failed to load: {msg}",
      "openSession.newProject": "＋ New project…",
      "openSession.projectCreated": "Project registered: {path}",
      "openSession.projectFailed": "Failed to create project: {msg}",
      "newSession.openButton": "＋ New session…",
      "newSession.title": "New session",
      "newSession.desc": "Pick a project to create a new session (multi-project tabs).",
      "newSession.newProject": "＋ New project…",
      "newSession.loadFailed": "Failed to load: {msg}",
      "newSession.projectFailed": "Failed to create session: {msg}",
      "deleteProject.delete": "Delete",
      "deleteProject.title": "Delete project",
      "deleteProject.body": "Delete project \"{name}\"? Only the project record is removed; on-disk data is kept (re-registerable).",
      "deleteProject.protectedBody": "\"{name}\" is a system project and cannot be deleted.",
      "deleteProject.removed": "Project removed: {name}",
      "deleteProject.failed": "Failed to delete project: {msg}",
      "relTime.justNow": "just now",
      "relTime.minutesAgo": "{n} min ago",
      "relTime.hoursAgo": "{n} h ago",
      "relTime.daysAgo": "{n} d ago",
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
      "cmd.open.hint": "Open session (cross-project)",
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
      "copy.growthCardTitle": "EMRG reports every self-improvement automatically",
      "copy.growthCount": "Self-evolved {n} times",
      "copy.growthCountPrefix": "Self-evolved",
      "copy.times": "times",
      "copy.growthNote": "Learning as I work — getting better with use",
      "copy.evolutionToastTitle": "EMRG just completed a self-evolution!",
      "copy.evolutionToastMsg": "That's its {n}th self-improvement — it's even better now.",
      "copy.evolutionToastMsgStatic": "It learned something new and is even better now.",
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

      // Chat area (chat.js)
      "chat.copyCode": "Copy",
      "chat.copied": "Copied ✓",
      "chat.copyFailed": "Copy failed",
      "chat.fromOtherClient": "(from another client)",
      "chat.timeoutWarn": "⚠️ Response timed out — I wasn't done. Try again?",
      "chat.maxRoundsHint": "⚠️ This task hit the tool-call limit and may be incomplete — type \"continue\" and I'll keep going.",
      "chat.elapsed": "took {s}",
      "chat.expand": "Expand full text",
      // Time groups (utils.js/sidebar.js)
      "util.groupToday": "Today",
      "util.groupYesterday": "Yesterday",
      "util.groupEarlier": "Earlier",
      // Result panel (result-panel.js)
      "panel.openFailed": "Could not open file",
      "panel.copied": "✓ Copied",
      // Markdown code block (markdown.js)
      "md.copyCode": "Copy code",
      // Dialog dynamic copy (dialogs.js)
      "dlg.notConfigured": "Not configured",
      "dlg.noOtherModels": "No other models — add one below",
      "dlg.currentDefault": "Current default model",
      "dlg.setDefault": "Set as default",
      "dlg.defaultBadge": "Default",
      "dlg.supportsImages": "🖼 Supports images",
      "dlg.edit": "Edit",
      "dlg.delete": "Delete",
      "dlg.deleteModelTitle": "Delete this model?",
      "dlg.deleteModelBody": "\"{name}\" will be removed from available models.",
      "dlg.stepTitle": "One more step",
      "dlg.nameRequiredBody": "A model name is required.",
      "dlg.duplicateTitle": "Name taken",
      "dlg.duplicateBody": "A model with this name already exists — try another.",
      "dlg.duplicateDefaultBody": "This is the default model — edit it in the default row.",
      "dlg.gotIt": "Got it",
      "app.authKeyRequired": "An API Key is required to connect to the model service — fill it in to continue.",
      "dlg.saved": "Settings saved.",
      "dlg.loading": "Loading…",
      "dlg.starting": "Settings saved — starting…",
      "dlg.initFailed": "Initialization failed: {msg}",
      "dlg.renameFailed": "Rename failed: {msg}",
      // App dynamic copy (app.js)
      "app.workdirInvalid": "Working directory unavailable — update it in Settings.",
      "app.bootFailed": "Startup failed: {msg}",
      "app.needSession": "Start a conversation first.",
      "app.recentImprovements": "Recent improvements",
      "app.noImprovements": "No improvements recorded yet — type /rant to drive the first evolution",
      "app.cmdUnknown": "Command {cmd} is not available yet.",
      "app.cleared": "Current conversation cleared.",
      "app.compacted": "Conversation history compacted.",
      "app.imagePaste": "Paste the image into the input (Ctrl+V / ⌘V).",
      "app.cmdFailed": "Command {cmd} failed: {msg}",
      "app.helpNoSessions": "No conversations yet — start typing.",
      "app.unnamed": "(untitled)",
      "app.current": "current",
      "app.noHistory": "No history points to rewind to.",
      "app.historyFailed": "Failed to load history: {msg}",
      "app.rewound": "Rewound to message point #{index}, removed {n} records.",
      "app.rewindFailed": "Rewind failed: {msg}",
      "app.noMemories": "No {scope} memories yet.",
      "app.sessionMem": "conversation",
      "app.projectMem": "project",
      "app.readMemFailed": "Failed to read memory: {msg}",
      "app.memFailed": "Failed to load memories: {msg}",
      "app.noSkills": "No skills loaded yet.",
      "app.skillsFailed": "Failed to load skills: {msg}",
      "app.globalAll": "(Global — all projects)",
      "app.rantEmpty": "Write something first.",
      "app.rantReceived": "✓ Got it! EMRG will evolve accordingly.",
      "app.rantCount": "({n} feedback items so far)",
      "app.rantFailed": "Submission failed: {msg}",
      "app.noTasks": "No tasks to trigger.",
      "app.taskDisabled": "Disabled",
      "app.taskInterval": "every {n}s",
      "app.tasksFailed": "Failed to load tasks: {msg}",
      "app.triggerFailed": "Trigger failed: {msg}",
      "app.triggered": "Task {n} triggered.",
      "app.deletedSwitch": "This conversation was deleted — switched to the most recent one.",
      "app.switched": "Conversation switched.",
      "app.sessionDisconnected": "This session's connection is lost — reconnecting automatically…",
      "app.switchFailed": "Failed to switch: {msg}",
      "app.tooManyOpenSessions": "Too many open sessions (20) — close some first.",
      "app.newFailed": "Failed to create conversation: {msg}",
      "app.deleteFailed": "Delete failed: {msg}",
      "app.rename": "✏️ Rename",
      "app.deleteConv": "🗑 Delete conversation",
      "app.closeSession": "❌ Close session (keep data)",
      "app.closeFailed": "Failed to close session: {msg}",
      "sidebar.openSessions": "Open sessions",
      "sidebar.noOpenSessions": "No open sessions",
      "sidebar.openSessionOf": "{project} / {title}",
      "app.noModels": "No models configured",
      "app.goSettings": "Add in Settings",
      "app.modelSwitchFailed": "Failed to switch model: {msg}",
      "app.modelListFailed": "Failed to load models: {msg}",
      "app.askModeNotice": "Ask mode: I only chat — no tools. Just ask.",
      "app.unknownResult": "Result unknown — connection lost",
      "app.error": "Something went wrong: {msg}",
      "app.unknownError": "Unknown error",
      "app.unknown": "unknown",
      "app.authFailed": "Authentication failed — check your API Key in Settings.",
      "app.daemonStopped": "daemon failed to start (auto-retry stopped). Run `emrg server` in a terminal to debug;\n{msg}",
      "app.versionInfo": "EMRG GUI v{ver} · Instance {id} · Model {model} · Evolved {n} times",
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
