"use strict";
/**
 * preload.js — contextBridge：renderer ↔ main 安全 IPC 桥（G67）。
 * renderer 零网络权限（sandbox + contextIsolation），所有操作经白名单 API。
 */

const { contextBridge, ipcRenderer } = require("electron");

const api = {
  init: () => ipcRenderer.invoke("emrg:init"),
  sendMessage: (payload) => ipcRenderer.invoke("emrg:sendMessage", payload),
  // rant 2026-09-02T15:23:53：图片落盘（粘贴/拖拽 → base64 → main 写 <cwd>/.emrg/sessions/<sid>/images/）
  saveImage: (payload) => ipcRenderer.invoke("emrg:saveImage", payload),
  // issue #2920：任意文件附件落盘（同一条路径、另一个目录），返回绝对路径——
  // 图片走 vision，其它文件只把这行路径放进正文，由模型自己读。
  saveFile: (payload) => ipcRenderer.invoke("emrg:saveFile", payload),
  // rant 2026-09-30T09:35:04 要求 4：系统给的图不受白名单所限——粘贴收到的是 renderer
  // 解不了的格式（macOS 的 TIFF）时，向 main 要剪贴板里的图（NSImage 解码 → PNG）。
  readClipboardImage: () => ipcRenderer.invoke("emrg:readClipboardImage"),
  listSessions: () => ipcRenderer.invoke("emrg:listSessions"),
  // rant 2026-11-19T09:31:00：最近 24 小时的三段 token 读数（daemon 记账）
  usageWindow: (payload) => ipcRenderer.invoke("emrg:usageWindow", payload),
  // issue #3212：这台机器跑的是不是最新版。按钮按一次问一次；daemon 侧缓存住读数，
  // 所以连按几次不会连打几次 GitHub。
  checkUpdate: (payload) => ipcRenderer.invoke("emrg:checkUpdate", payload),
  restartDaemon: () => ipcRenderer.invoke("emrg:restartDaemon"),
  relaunchGui: () => ipcRenderer.invoke("emrg:relaunchGui"),
  switchSession: (payload) => ipcRenderer.invoke("emrg:switchSession", payload),
  newSession: (payload) => ipcRenderer.invoke("emrg:newSession", payload),
  deleteSession: (payload) => ipcRenderer.invoke("emrg:deleteSession", payload),
  closeSession: (payload) => ipcRenderer.invoke("emrg:closeSession", payload),
  // issue #3022：侧边栏拖拽调序——顺序的唯一真源在主进程，渲染层只上交新顺序。
  reorderOpenSessions: (payload) => ipcRenderer.invoke("emrg:reorderOpenSessions", payload),
  getOpenSessions: () => ipcRenderer.invoke("emrg:getOpenSessions"),
  renameSession: (payload) => ipcRenderer.invoke("emrg:renameSession", payload),
  setModel: (payload) => ipcRenderer.invoke("emrg:setModel", payload),
  // rant 2026-09-30T09:30:16（GUI 半边）：会话 sandbox 档位——客户端只发意图，
  // daemon 落盘并广播 `sandbox_set`（自己那份回执与其他客户端收到的广播同形）。
  setSandbox: (payload) => ipcRenderer.invoke("emrg:setSandbox", payload),
  // rant 2026-10-09T09:43:39（GUI 半边）：会话的额外可写根——宿主点名一个路径、
  // daemon 按当前档位的规则裁定并落盘，回帧是唯一的列表来源（客户端不自己记）。
  setSandboxRoots: (payload) => ipcRenderer.invoke("emrg:setSandboxRoots", payload),
  clearSession: (payload) => ipcRenderer.invoke("emrg:clearSession", payload),
  compactSession: (payload) => ipcRenderer.invoke("emrg:compactSession", payload),
  // Approval channel (rant 2026-09-29T15:52:38.987951+08:00, requirement 1):
  // the daemon holds a confined call open until a client answers.
  respondApproval: (payload) => ipcRenderer.invoke("emrg:respondApproval", payload),
  listHistory: (payload) => ipcRenderer.invoke("emrg:listHistory", payload),
  // Issue #2349 part 2/2: the prompts the host typed in this session, owned by the
  // daemon (per session, durable, shared between clients). The composer's up/down
  // arrow recall reads from here rather than from a list of its own.
  listInputHistory: (payload) => ipcRenderer.invoke("emrg:listInputHistory", payload),
  rewindSession: (payload) => ipcRenderer.invoke("emrg:rewindSession", payload),
  listMemories: (payload) => ipcRenderer.invoke("emrg:listMemories", payload),
  readMemory: (payload) => ipcRenderer.invoke("emrg:readMemory", payload),
  listFiles: (payload) => ipcRenderer.invoke("emrg:listFiles", payload), // 右栏工作区 P1：目录树
  // issue #3026：右栏两个文件树的两个根（agent 工作目录 / 该会话的会话目录）
  sessionRoots: (payload) => ipcRenderer.invoke("emrg:sessionRoots", payload),
  readFile: (payload) => ipcRenderer.invoke("emrg:readFile", payload), // 右栏工作区 P1：文件查看器
  // rant 2026-11-20T18:01:52：右栏编辑器的两条文件通道——探测磁盘有没有变、把改动写回
  statFile: (payload) => ipcRenderer.invoke("emrg:statFile", payload),
  writeFile: (payload) => ipcRenderer.invoke("emrg:writeFile", payload),
  previewHtml: (payload) => ipcRenderer.invoke("emrg:previewHtml", payload), // P2.3：HTML 预览（WebContentsView）
  closePreview: (payload) => ipcRenderer.invoke("emrg:closePreview", payload), // P2.3：关闭/切走 HTML 预览
  panelResized: (payload) => ipcRenderer.invoke("emrg:panelResized", payload), // P2.3：面板布局上报 → bounds 同步
  getPreviewState: () => ipcRenderer.invoke("emrg:getPreviewState"), // P2.3：renderer 崩溃恢复拉取
  // rant 2026-11-25T11:10:45：HTML tab 的浏览器模式。show/hide 是页面本身的生命（隐藏不销毁），
  // navigate 是地址栏，bounds 是 renderer 量出的那一格——原生 view 盖在 DOM 上的位置只有它知道。
  showBrowser: (payload) => ipcRenderer.invoke("emrg:showBrowser", payload),
  hideBrowser: () => ipcRenderer.invoke("emrg:hideBrowser"),
  browserNavigate: (payload) => ipcRenderer.invoke("emrg:browserNavigate", payload),
  // issue #3229：第五个通道——重读当前地址。前四个都改不了屏幕上那一页，所以文件在磁盘上改了
  // 之后，界面里没有任何一处可以点。
  browserReload: (payload) => ipcRenderer.invoke("emrg:browserReload", payload),
  browserBounds: (payload) => ipcRenderer.invoke("emrg:browserBounds", payload),
  // `{ sessionId }` 决定 daemon 读哪个会话的技能树 —— 缺了参数，主进程只能当"没有会话"，
  // 于是每个会话看到的都是同一个根（issue #2569 判据 2）。
  listSkills: (p) => ipcRenderer.invoke("emrg:listSkills", p),
  listAgents: () => ipcRenderer.invoke("emrg:listAgents"),
  listAgentSessions: (payload) => ipcRenderer.invoke("emrg:listAgentSessions", payload),
  // issue #2978：搜索浮层的历史面 —— 全部 agent 的全部会话，一次请求，无参数
  listAllSessions: () => ipcRenderer.invoke("emrg:listAllSessions"),
  registerAgent: (payload) => ipcRenderer.invoke("emrg:registerAgent", payload),
  removeAgent: (payload) => ipcRenderer.invoke("emrg:removeAgent", payload),
  listTasks: () => ipcRenderer.invoke("emrg:listTasks"),
  // 间隔下限由 daemon 上报（issue #2490）——渲染层不留第二份数字
  taskLimits: () => ipcRenderer.invoke("emrg:taskLimits"),
  triggerTask: (payload) => ipcRenderer.invoke("emrg:triggerTask", payload),
  // Task/template CRUD (rant 2026-08-12T18:23:15 P3)
  taskCreate: (payload) => ipcRenderer.invoke("emrg:taskCreate", payload),
  taskUpdate: (payload) => ipcRenderer.invoke("emrg:taskUpdate", payload),
  taskDelete: (payload) => ipcRenderer.invoke("emrg:taskDelete", payload),
  taskTemplateList: () => ipcRenderer.invoke("emrg:taskTemplateList"),
  taskTemplateCreate: (payload) => ipcRenderer.invoke("emrg:taskTemplateCreate", payload),
  taskTemplateUpdate: (payload) => ipcRenderer.invoke("emrg:taskTemplateUpdate", payload),
  taskTemplateDelete: (payload) => ipcRenderer.invoke("emrg:taskTemplateDelete", payload),
  sendRant: (payload) => ipcRenderer.invoke("emrg:sendRant", payload),
  listRants: (payload) => ipcRenderer.invoke("emrg:listRants", payload),
  // issue #2959 item 7: rant 面板的行内删除（与 agent / task 行同一个确认手势）
  deleteRant: (payload) => ipcRenderer.invoke("emrg:deleteRant", payload),
  evolutionSummary: (payload) => ipcRenderer.invoke("emrg:evolutionSummary", payload),
  githubStatus: () => ipcRenderer.invoke("emrg:githubStatus"),
  githubConnect: (payload) => ipcRenderer.invoke("emrg:githubConnect", payload),
  githubDisconnect: () => ipcRenderer.invoke("emrg:githubDisconnect"),
  // issue #2774：aitokenpool 用 GitHub 账号登录。出码 / 轮询 / 写配置都在 main
  // （renderer 无网络权限），这里只开通道；服务地址有几处也由 main 说，界面不另存常量。
  atpBases: () => ipcRenderer.invoke("emrg:atpBases"),
  atpDeviceStart: (payload) => ipcRenderer.invoke("emrg:atpDeviceStart", payload),
  atpDevicePoll: (payload) => ipcRenderer.invoke("emrg:atpDevicePoll", payload),
  atpApply: (payload) => ipcRenderer.invoke("emrg:atpApply", payload),
  githubConnectWeb: () => ipcRenderer.invoke("emrg:githubConnectWeb"),
  openExternal: (payload) => ipcRenderer.invoke("emrg:openExternal", payload),
  listModels: () => ipcRenderer.invoke("emrg:listModels"),
  openFile: (payload) => ipcRenderer.invoke("emrg:openFile", payload),
  saveSettings: (payload) => ipcRenderer.invoke("emrg:saveSettings", payload),
  getSettings: () => ipcRenderer.invoke("emrg:getSettings"),
  cancel: (sessionId) => ipcRenderer.invoke("emrg:cancel", sessionId),
  // issue #3204：撤掉**一条**排队消息。此前每条删除路径都 pop 整个列表，客户端只能表达
  // 「整队丢掉」——所以界面上连一个删除控件都没有：按钮没有协议可发，就是按下去什么
  // 也不发生的按钮。
  cancelQueued: (payload) => ipcRenderer.invoke("emrg:cancelQueued", payload),
  pickAgentDir: () => ipcRenderer.invoke("emrg:pickAgentDir"),
  // 额外可写根的系统选择器（rant 2026-11-20T10:13:12）：只开对话框取一条路径，
  // 不写盘、不发命令——填进输入框不等于添加，那一跳仍由用户点「添加」。
  pickSandboxRoot: () => ipcRenderer.invoke("emrg:pickSandboxRoot"),
  // 渲染进程日志桥（rant 2026-08-24：打开会话链路排障——renderer 点击/调用步骤
  // 经 emrg:log IPC 上报，由 main 写入 ~/.emrg/emrg-gui.log，与 main 日志同文件时序对齐）
  log: (level, msg) => ipcRenderer.invoke("emrg:log", { level, msg }),
  onEvent: (cb) => {
    const listener = (_e, evt) => cb(evt);
    ipcRenderer.on("emrg:event", listener);
    return () => ipcRenderer.removeListener("emrg:event", listener);
  },
};

contextBridge.exposeInMainWorld("emrg", api);
