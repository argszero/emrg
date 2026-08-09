"use strict";
/**
 * preload.js — contextBridge：renderer ↔ main 安全 IPC 桥（G67）。
 * renderer 零网络权限（sandbox + contextIsolation），所有操作经白名单 API。
 */

const { contextBridge, ipcRenderer } = require("electron");

const api = {
  init: () => ipcRenderer.invoke("emrg:init"),
  sendMessage: (payload) => ipcRenderer.invoke("emrg:sendMessage", payload),
  listSessions: () => ipcRenderer.invoke("emrg:listSessions"),
  switchSession: (payload) => ipcRenderer.invoke("emrg:switchSession", payload),
  newSession: () => ipcRenderer.invoke("emrg:newSession"),
  deleteSession: (payload) => ipcRenderer.invoke("emrg:deleteSession", payload),
  renameSession: (payload) => ipcRenderer.invoke("emrg:renameSession", payload),
  setModel: (payload) => ipcRenderer.invoke("emrg:setModel", payload),
  clearSession: (payload) => ipcRenderer.invoke("emrg:clearSession", payload),
  compactSession: (payload) => ipcRenderer.invoke("emrg:compactSession", payload),
  listHistory: (payload) => ipcRenderer.invoke("emrg:listHistory", payload),
  rewindSession: (payload) => ipcRenderer.invoke("emrg:rewindSession", payload),
  listMemories: (payload) => ipcRenderer.invoke("emrg:listMemories", payload),
  readMemory: (payload) => ipcRenderer.invoke("emrg:readMemory", payload),
  listSkills: () => ipcRenderer.invoke("emrg:listSkills"),
  listProjects: () => ipcRenderer.invoke("emrg:listProjects"),
  listTasks: () => ipcRenderer.invoke("emrg:listTasks"),
  triggerTask: (payload) => ipcRenderer.invoke("emrg:triggerTask", payload),
  sendRant: (payload) => ipcRenderer.invoke("emrg:sendRant", payload),
  evolutionSummary: (payload) => ipcRenderer.invoke("emrg:evolutionSummary", payload),
  githubStatus: () => ipcRenderer.invoke("emrg:githubStatus"),
  updateCheck: () => ipcRenderer.invoke("emrg:updateCheck"),
  updateCheckPrompted: (payload) => ipcRenderer.invoke("emrg:updateCheckPrompted", payload),
  githubConnect: (payload) => ipcRenderer.invoke("emrg:githubConnect", payload),
  githubDisconnect: () => ipcRenderer.invoke("emrg:githubDisconnect"),
  githubConnectWeb: () => ipcRenderer.invoke("emrg:githubConnectWeb"),
  openExternal: (payload) => ipcRenderer.invoke("emrg:openExternal", payload),
  listModels: () => ipcRenderer.invoke("emrg:listModels"),
  openFile: (payload) => ipcRenderer.invoke("emrg:openFile", payload),
  saveSettings: (payload) => ipcRenderer.invoke("emrg:saveSettings", payload),
  getSettings: () => ipcRenderer.invoke("emrg:getSettings"),
  cancel: () => ipcRenderer.invoke("emrg:cancel"),
  pickProjectDir: () => ipcRenderer.invoke("emrg:pickProjectDir"),
  onEvent: (cb) => {
    const listener = (_e, evt) => cb(evt);
    ipcRenderer.on("emrg:event", listener);
    return () => ipcRenderer.removeListener("emrg:event", listener);
  },
};

contextBridge.exposeInMainWorld("emrg", api);
