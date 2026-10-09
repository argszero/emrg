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
  // rant 2026-09-30T09:35:04 要求 4：系统给的图不受白名单所限——粘贴收到的是 renderer
  // 解不了的格式（macOS 的 TIFF）时，向 main 要剪贴板里的图（NSImage 解码 → PNG）。
  readClipboardImage: () => ipcRenderer.invoke("emrg:readClipboardImage"),
  listSessions: () => ipcRenderer.invoke("emrg:listSessions"),
  restartDaemon: () => ipcRenderer.invoke("emrg:restartDaemon"),
  relaunchGui: () => ipcRenderer.invoke("emrg:relaunchGui"),
  switchSession: (payload) => ipcRenderer.invoke("emrg:switchSession", payload),
  newSession: (payload) => ipcRenderer.invoke("emrg:newSession", payload),
  deleteSession: (payload) => ipcRenderer.invoke("emrg:deleteSession", payload),
  closeSession: (payload) => ipcRenderer.invoke("emrg:closeSession", payload),
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
  rewindSession: (payload) => ipcRenderer.invoke("emrg:rewindSession", payload),
  listMemories: (payload) => ipcRenderer.invoke("emrg:listMemories", payload),
  readMemory: (payload) => ipcRenderer.invoke("emrg:readMemory", payload),
  listFiles: (payload) => ipcRenderer.invoke("emrg:listFiles", payload), // 右栏工作区 P1：目录树
  readFile: (payload) => ipcRenderer.invoke("emrg:readFile", payload), // 右栏工作区 P1：文件查看器
  previewHtml: (payload) => ipcRenderer.invoke("emrg:previewHtml", payload), // P2.3：HTML 预览（WebContentsView）
  closePreview: (payload) => ipcRenderer.invoke("emrg:closePreview", payload), // P2.3：关闭/切走 HTML 预览
  panelResized: (payload) => ipcRenderer.invoke("emrg:panelResized", payload), // P2.3：面板布局上报 → bounds 同步
  getPreviewState: () => ipcRenderer.invoke("emrg:getPreviewState"), // P2.3：renderer 崩溃恢复拉取
  listSkills: () => ipcRenderer.invoke("emrg:listSkills"),
  listProjects: () => ipcRenderer.invoke("emrg:listProjects"),
  listProjectSessions: (payload) => ipcRenderer.invoke("emrg:listProjectSessions", payload),
  registerProject: (payload) => ipcRenderer.invoke("emrg:registerProject", payload),
  removeProject: (payload) => ipcRenderer.invoke("emrg:removeProject", payload),
  listTasks: () => ipcRenderer.invoke("emrg:listTasks"),
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
  evolutionSummary: (payload) => ipcRenderer.invoke("emrg:evolutionSummary", payload),
  githubStatus: () => ipcRenderer.invoke("emrg:githubStatus"),
  githubConnect: (payload) => ipcRenderer.invoke("emrg:githubConnect", payload),
  githubDisconnect: () => ipcRenderer.invoke("emrg:githubDisconnect"),
  githubConnectWeb: () => ipcRenderer.invoke("emrg:githubConnectWeb"),
  openExternal: (payload) => ipcRenderer.invoke("emrg:openExternal", payload),
  listModels: () => ipcRenderer.invoke("emrg:listModels"),
  openFile: (payload) => ipcRenderer.invoke("emrg:openFile", payload),
  saveSettings: (payload) => ipcRenderer.invoke("emrg:saveSettings", payload),
  getSettings: () => ipcRenderer.invoke("emrg:getSettings"),
  cancel: (sessionId) => ipcRenderer.invoke("emrg:cancel", sessionId),
  pickProjectDir: () => ipcRenderer.invoke("emrg:pickProjectDir"),
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
