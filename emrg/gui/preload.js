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
  listModels: () => ipcRenderer.invoke("emrg:listModels"),
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
