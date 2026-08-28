"use strict";

const { app, BrowserWindow, dialog, ipcMain } = require("electron");
const { spawn, execFile } = require("node:child_process");
const { promisify } = require("node:util");
const path = require("node:path");
const fs = require("node:fs/promises");
const { buildChatInvocation, parseEventLine, parseGitEnvironment, parseUnifiedDiff, desktopCapabilities, validateWorkspaceFolderName } = require("./core.cjs");

const projectRoot = path.resolve(__dirname, "../..");
let mainWindow;
const activeChats = new Map();
const execFileAsync = promisify(execFile);

function send(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, payload);
}

function findUv() {
  return process.env.SEECODER_UV || "uv";
}

function consumeLines(stream, channel, sessionId) {
  let remainder = "";
  stream.setEncoding("utf8");
  stream.on("data", (chunk) => {
    remainder += chunk;
    const lines = remainder.split(/\r?\n/);
    remainder = lines.pop() || "";
    for (const line of lines) {
      if (!line) continue;
      const event = parseEventLine(line);
      send(channel, { ...(event || { event: "unstructured_output", data: { text: line } }), sessionId });
    }
  });
  stream.on("end", () => {
    if (!remainder) return;
    const event = parseEventLine(remainder);
    send(channel, { ...(event || { event: "unstructured_output", data: { text: remainder } }), sessionId });
  });
}

function stopActiveChat(sessionId) {
  const active = activeChats.get(sessionId);
  if (!active || active.child.exitCode !== null) return false;
  try {
    if (process.platform !== "win32") process.kill(-active.child.pid, "SIGTERM");
    else active.child.kill("SIGTERM");
  } catch {
    active.child.kill("SIGTERM");
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
  for (const sessionId of activeChats.keys()) stopActiveChat(sessionId);
  if (process.platform !== "darwin") app.quit();
});

ipcMain.handle("seecoder:choose-workspace", async () => {
  const result = await dialog.showOpenDialog(mainWindow, { properties: ["openDirectory", "createDirectory"] });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("seecoder:choose-workspace-parent", async () => {
  const result = await dialog.showOpenDialog(mainWindow, { properties: ["openDirectory"] });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("seecoder:create-workspace", async (_event, payload) => {
  const rawParent = payload?.parentPath;
  const name = validateWorkspaceFolderName(payload?.name);
  if (typeof rawParent !== "string" || !rawParent.trim() || !name) return { ok: false, error: "请输入有效的文件夹名称，并选择父目录。" };
  const parent = path.resolve(rawParent);
  try {
    if (!(await fs.stat(parent)).isDirectory()) return { ok: false, error: "父目录不可用。" };
  } catch {
    return { ok: false, error: "父目录不可用。" };
  }
  const target = path.resolve(parent, name);
  if (path.dirname(target) !== parent) return { ok: false, error: "新文件夹必须直接位于所选父目录中。" };
  try {
    await fs.mkdir(target);
    return { ok: true, workspace: target };
  } catch (error) {
    return { ok: false, error: error?.code === "EEXIST" ? "同名文件夹已存在。" : "无法创建该文件夹。" };
  }
});

ipcMain.handle("seecoder:capabilities", () => desktopCapabilities());

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

function safeWorkspaceRelativePath(workspace, rawPath) {
  if (typeof rawPath !== "string" || !rawPath.trim() || path.isAbsolute(rawPath)) return null;
  const root = path.resolve(workspace);
  const candidate = path.resolve(root, rawPath);
  const relative = path.relative(root, candidate);
  if (!relative || relative === ".." || relative.startsWith(".." + path.sep) || path.isAbsolute(relative)) return null;
  return relative;
}

ipcMain.handle("seecoder:read-diff", async (_event, payload) => {
  const rawWorkspace = payload?.workspace;
  if (typeof rawWorkspace !== "string" || !rawWorkspace.trim()) return { ok: false, error: "请选择有效工作区。" };
  const workspace = path.resolve(rawWorkspace);
  try {
    if (!(await fs.stat(workspace)).isDirectory()) return { ok: false, error: "工作区目录不可用。" };
  } catch {
    return { ok: false, error: "工作区目录不可用。" };
  }
  const relativePath = safeWorkspaceRelativePath(workspace, payload?.path);
  if (!relativePath) return { ok: false, error: "只允许读取工作区内的相对文件路径。" };
  const diff = await gitOutput(workspace, ["diff", "--no-ext-diff", "--unified=3", "--", relativePath]);
  if (diff === null) return { ok: false, error: "无法读取该工作区的本地 Git 差异。" };
  return { ok: true, path: relativePath, lines: parseUnifiedDiff(diff) };
});

function safeSessionId(value) {
  return typeof value === "string" && /^[a-zA-Z0-9-]{8,80}$/.test(value) ? value : null;
}

ipcMain.handle("seecoder:start-chat", async (_event, payload) => {
  const sessionId = safeSessionId(payload?.sessionId);
  if (!sessionId) throw new Error("会话标识无效。");
  const existing = activeChats.get(sessionId);
  if (existing && existing.child.exitCode === null) return { started: false, resumed: true };
  const directory = path.join(app.getPath("userData"), "seecoder-sessions");
  await fs.mkdir(directory, { recursive: true });
  const sessionPath = path.join(directory, sessionId + ".json");
  const hasSavedSession = await fs.access(sessionPath).then(() => true).catch(() => false);
  const { command, args } = buildChatInvocation(findUv(), payload?.workspace, payload?.mode, sessionPath, hasSavedSession);
  const child = spawn(command, args, {
    cwd: projectRoot,
    detached: process.platform !== "win32",
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  activeChats.set(sessionId, { child, workspace: payload.workspace, mode: payload.mode });
  send("seecoder:runner-event", { event: "chat_started", data: { workspace: payload.workspace }, sessionId });
  consumeLines(child.stdout, "seecoder:runner-event", sessionId);
  consumeLines(child.stderr, "seecoder:runner-stderr", sessionId);
  child.on("error", (error) => send("seecoder:runner-event", { event: "runner_error", data: { message: error.message }, sessionId }));
  child.on("close", (code, signal) => {
    send("seecoder:runner-event", { event: "chat_exit", data: { code, signal }, sessionId });
    activeChats.delete(sessionId);
  });
  return { started: true, resumed: hasSavedSession };
});

ipcMain.handle("seecoder:send-chat-task", (_event, payload) => {
  const sessionId = safeSessionId(payload?.sessionId);
  const task = typeof payload?.task === "string" ? payload.task.trim() : "";
  const active = sessionId ? activeChats.get(sessionId) : null;
  if (!active || !active.child.stdin || !task) return { handled: false };
  active.child.stdin.write(task + "\n");
  return { handled: true };
});

ipcMain.handle("seecoder:approve", (_event, payload) => {
  const sessionId = safeSessionId(payload?.sessionId);
  const active = sessionId ? activeChats.get(sessionId) : null;
  if (!active || !active.child.stdin) return { handled: false };
  const decision = payload?.decision;
  const line = decision === true || decision === "approve" ? "y\n" : "n\n";
  active.child.stdin.write(line);
  return { handled: true };
});

ipcMain.handle("seecoder:stop-chat", (_event, sessionId) => ({ stopped: Boolean(safeSessionId(sessionId)) && stopActiveChat(sessionId) }));
