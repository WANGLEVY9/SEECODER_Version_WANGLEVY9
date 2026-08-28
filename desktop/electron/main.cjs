"use strict";

const { app, BrowserWindow, dialog, ipcMain } = require("electron");
const { spawn, execFile } = require("node:child_process");
const { promisify } = require("node:util");
const path = require("node:path");
const fs = require("node:fs/promises");
const { buildBackendInvocation, parseEventLine, parseGitEnvironment } = require("./core.cjs");

const projectRoot = path.resolve(__dirname, "../..");
let mainWindow;
let activeRun;
const execFileAsync = promisify(execFile);

function send(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, payload);
}

function findUv() {
  return process.env.SEECODER_UV || "uv";
}

function consumeLines(stream, channel) {
  let remainder = "";
  stream.setEncoding("utf8");
  stream.on("data", (chunk) => {
    remainder += chunk;
    const lines = remainder.split(/\r?\n/);
    remainder = lines.pop() || "";
    for (const line of lines) {
      if (!line) continue;
      const event = parseEventLine(line);
      send(channel, event || { event: "unstructured_output", data: { text: line } });
    }
  });
  stream.on("end", () => {
    if (!remainder) return;
    const event = parseEventLine(remainder);
    send(channel, event || { event: "unstructured_output", data: { text: remainder } });
  });
}

function stopActiveRun() {
  if (!activeRun || activeRun.child.exitCode !== null) return false;
  try {
    if (process.platform !== "win32") process.kill(-activeRun.child.pid, "SIGTERM");
    else activeRun.child.kill("SIGTERM");
  } catch {
    activeRun.child.kill("SIGTERM");
  }
  return true;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1500,
    height: 920,
    minWidth: 1120,
    minHeight: 700,
    title: "SEECODER",
    backgroundColor: "#f7f4ef",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    trafficLightPosition: process.platform === "darwin" ? { x: 20, y: 21 } : undefined,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  mainWindow.on("closed", () => { mainWindow = undefined; });
}

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on("window-all-closed", () => {
  stopActiveRun();
  if (process.platform !== "darwin") app.quit();
});

ipcMain.handle("seecoder:choose-workspace", async () => {
  const result = await dialog.showOpenDialog(mainWindow, { properties: ["openDirectory", "createDirectory"] });
  return result.canceled ? null : result.filePaths[0];
});

async function gitOutput(workspace, args) {
  try {
    const { stdout } = await execFileAsync("git", ["-C", workspace, ...args], {
      timeout: 3_000,
      maxBuffer: 512 * 1024,
      windowsHide: true,
      env: { ...process.env, GIT_OPTIONAL_LOCKS: "0" },
    });
    return stdout;
  } catch {
    return null;
  }
}

ipcMain.handle("seecoder:inspect-environment", async (_event, rawWorkspace) => {
  if (typeof rawWorkspace !== "string" || !rawWorkspace.trim()) return { isRepository: false, files: [] };
  const workspace = path.resolve(rawWorkspace);
  try {
    if (!(await fs.stat(workspace)).isDirectory()) return { isRepository: false, files: [] };
  } catch {
    return { isRepository: false, files: [] };
  }
  const [branch, nameStatus, numstat] = await Promise.all([
    gitOutput(workspace, ["branch", "--show-current"]),
    gitOutput(workspace, ["diff", "--name-status"]),
    gitOutput(workspace, ["diff", "--numstat"]),
  ]);
  if (branch === null || nameStatus === null || numstat === null) return { isRepository: false, files: [] };
  return parseGitEnvironment({ branch, nameStatus, numstat });
});

ipcMain.handle("seecoder:start-run", (_event, payload) => {
  if (activeRun && activeRun.child.exitCode === null) throw new Error("已有任务正在运行，请先停止或等待它完成。");
  const { command, args } = buildBackendInvocation(findUv(), payload?.task, payload?.workspace, payload?.mode);
  const child = spawn(command, args, {
    cwd: projectRoot,
    detached: process.platform !== "win32",
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  activeRun = { child };
  send("seecoder:runner-event", { event: "run_started", data: { workspace: payload.workspace } });
  consumeLines(child.stdout, "seecoder:runner-event");
  consumeLines(child.stderr, "seecoder:runner-stderr");
  child.on("error", (error) => send("seecoder:runner-event", { event: "runner_error", data: { message: error.message } }));
  child.on("close", (code, signal) => {
    send("seecoder:runner-event", { event: "process_exit", data: { code, signal } });
    activeRun = undefined;
  });
  return { started: true };
});

ipcMain.handle("seecoder:approve", (_event, decision) => {
  if (!activeRun || !activeRun.child.stdin) return { handled: false };
  const line = decision === true || decision === "approve" ? "y\n" : "n\n";
  activeRun.child.stdin.write(line);
  return { handled: true };
});

ipcMain.handle("seecoder:stop-run", () => ({ stopped: stopActiveRun() }));
