"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("seecoderDesktop", {
  chooseWorkspace: () => ipcRenderer.invoke("seecoder:choose-workspace"),
  startRun: (payload) => ipcRenderer.invoke("seecoder:start-run", payload),
  stopRun: () => ipcRenderer.invoke("seecoder:stop-run"),
  onRunnerEvent: (callback) => ipcRenderer.on("seecoder:runner-event", (_event, value) => callback(value)),
  onRunnerStderr: (callback) => ipcRenderer.on("seecoder:runner-stderr", (_event, value) => callback(value)),
});
