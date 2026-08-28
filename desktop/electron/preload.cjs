"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("seecoderDesktop", {
  chooseWorkspace: () => ipcRenderer.invoke("seecoder:choose-workspace"),
  inspectEnvironment: (workspace) => ipcRenderer.invoke("seecoder:inspect-environment", workspace),
  readDiff: (payload) => ipcRenderer.invoke("seecoder:read-diff", payload),
  startChat: (payload) => ipcRenderer.invoke("seecoder:start-chat", payload),
  sendChatTask: (payload) => ipcRenderer.invoke("seecoder:send-chat-task", payload),
  stopChat: (sessionId) => ipcRenderer.invoke("seecoder:stop-chat", sessionId),
  approve: (payload) => ipcRenderer.invoke("seecoder:approve", payload),
  onRunnerEvent: (callback) => ipcRenderer.on("seecoder:runner-event", (_event, value) => callback(value)),
  onRunnerStderr: (callback) => ipcRenderer.on("seecoder:runner-stderr", (_event, value) => callback(value)),
});
