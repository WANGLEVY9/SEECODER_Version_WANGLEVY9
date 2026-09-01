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

function validChangesetId(value) {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(value);
}

function changesetDirectory() {
  return path.join(projectRoot, "runs", "changesets");
}

ipcMain.handle("seecoder:list-changesets", async (_event, rawWorkspace) => {
  if (typeof rawWorkspace !== "string" || !rawWorkspace.trim()) return { ok: false, error: "请选择有效工作区。", changesets: [] };
  const workspace = path.resolve(rawWorkspace);
  try {
    if (!(await fs.stat(workspace)).isDirectory()) return { ok: false, error: "工作区目录不可用。", changesets: [] };
    const names = await fs.readdir(changesetDirectory());
    const changesets = [];
    for (const name of names.filter((value) => value.endsWith(".json")).slice(0, 200)) {
      try {
        const raw = JSON.parse(await fs.readFile(path.join(changesetDirectory(), name), "utf8"));
        if (!validChangesetId(raw?.id) || path.resolve(String(raw?.workspace || "")) !== workspace) continue;
        changesets.push({ id: raw.id, run_id: raw.run_id, workspace, created_at: raw.created_at, records: Array.isArray(raw.records) ? raw.records.map((record) => ({ path: record.path, tool: record.tool, before_exists: record.before_exists, after_exists: record.after_exists, before_hash: record.before_hash, after_hash: record.after_hash })) : [], directory_operations: Array.isArray(raw.directory_operations) ? raw.directory_operations : [] });
      } catch { /* Ignore incomplete or untrusted journal entries. */ }
    }
    changesets.sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)));
    return { ok: true, changesets };
  } catch {
    return { ok: true, changesets: [] };
  }
});

ipcMain.handle("seecoder:rollback-changeset", async (_event, payload) => {
  const rawWorkspace = payload?.workspace;
  const id = payload?.changesetId;
  if (typeof rawWorkspace !== "string" || !rawWorkspace.trim() || !validChangesetId(id)) return { ok: false, error: "工作区或 ChangeSet 标识无效。" };
  const workspace = path.resolve(rawWorkspace);
  try {
    if (!(await fs.stat(workspace)).isDirectory()) return { ok: false, error: "工作区目录不可用。" };
  } catch { return { ok: false, error: "工作区目录不可用。" }; }
  try {
    const { stdout } = await execFileAsync(findUv(), ["run", "seecoder", "rollback-changeset", "--workspace", workspace, "--journal-dir", changesetDirectory(), "--changeset-id", id], { cwd: projectRoot, timeout: 10_000, maxBuffer: 128 * 1024, windowsHide: true, env: { ...process.env, GIT_TERMINAL_PROMPT: "0" } });
    return JSON.parse(stdout);
  } catch (error) {
    try { return JSON.parse(error?.stdout || ""); } catch { return { ok: false, error: error?.message || "无法执行 ChangeSet 回退。" }; }
  }
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

async function gitOutputAllowFailure(workspace, args) {
  try {
    const { stdout } = await execFileAsync("git", ["-C", workspace, ...args], {
      timeout: 3_000,
      maxBuffer: 512 * 1024,
      windowsHide: true,
      env: { ...process.env, GIT_OPTIONAL_LOCKS: "0", GIT_TERMINAL_PROMPT: "0" },
    });
    return stdout;
  } catch (error) {
    return typeof error?.stdout === "string" ? error.stdout : null;
  }
}

async function countUntrackedLines(workspace, listing) {
  const counts = {};
  for (const rawPath of String(listing || "").split(/\r?\n/).map((value) => value.trim()).filter(Boolean)) {
    const relative = safeWorkspaceRelativePath(workspace, rawPath);
    if (!relative) continue;
    try {
      const content = await fs.readFile(path.join(workspace, relative), "utf8");
      counts[relative] = { added: content ? content.split(/\r?\n/).length - (content.endsWith("\n") ? 1 : 0) : 0, deleted: 0 };
    } catch {
      // Binary or unreadable files still appear in the file list with zero counts.
    }
  }
  return counts;
}

ipcMain.handle("seecoder:inspect-environment", async (_event, rawWorkspace) => {
  if (typeof rawWorkspace !== "string" || !rawWorkspace.trim()) return { isRepository: false, files: [] };
  const workspace = path.resolve(rawWorkspace);
  try {
    if (!(await fs.stat(workspace)).isDirectory()) return { isRepository: false, files: [] };
  } catch {
    return { isRepository: false, files: [] };
  }
  const [repository, branch, headNameStatus, headNumstat, untracked] = await Promise.all([
    gitOutput(workspace, ["rev-parse", "--is-inside-work-tree"]),
    gitOutput(workspace, ["branch", "--show-current"]),
    gitOutput(workspace, ["diff", "HEAD", "--name-status"]),
    gitOutput(workspace, ["diff", "HEAD", "--numstat"]),
    gitOutput(workspace, ["ls-files", "--others", "--exclude-standard"]),
  ]);
  if (repository?.trim() !== "true" || untracked === null) return { isRepository: false, files: [] };
  // An unborn repository has no HEAD yet, and a detached repository has an
  // empty branch name. In both cases it is still a real Git workspace.
  const nameStatus = headNameStatus ?? await gitOutput(workspace, ["diff", "--name-status"]) ?? "";
  const numstat = headNumstat ?? await gitOutput(workspace, ["diff", "--numstat"]) ?? "";
  return parseGitEnvironment({ isRepository: true, branch, nameStatus, numstat, untracked, untrackedCounts: await countUntrackedLines(workspace, untracked) });
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
  let diff = await gitOutput(workspace, ["diff", "HEAD", "--no-ext-diff", "--unified=3", "--", relativePath]);
  if (diff === null) {
    // Non-Git workspaces still need an auditable review surface.  There is no
    // repository baseline, so expose the current UTF-8 file as a synthetic
    // add-only diff and label it as such in the renderer.
    try {
      const content = await fs.readFile(path.join(workspace, relativePath), "utf8");
      const lines = content.split(/\r?\n/);
      if (lines.at(-1) === "") lines.pop();
      diff = `--- /dev/null\n+++ ${relativePath}\n@@ -0,0 +1,${lines.length} @@\n${lines.map((line) => `+${line}`).join("\n")}`;
    } catch {
      return { ok: false, error: "无法读取该文件的本地内容。" };
    }
  }
  if (!diff.trim()) diff = await gitOutputAllowFailure(workspace, ["diff", "--no-index", "--no-ext-diff", "--unified=3", "/dev/null", relativePath]) || "";
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
  const { command, args } = buildChatInvocation(findUv(), payload?.workspace, payload?.mode, sessionPath, hasSavedSession, sessionId);
  const child = spawn(command, args, {
    cwd: projectRoot,
    detached: process.platform !== "win32",
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  activeChats.set(sessionId, {
    child,
    workspace: payload.workspace,
    mode: payload.mode,
    // A renderer click and its keyboard shortcut may arrive as two IPC
    // messages. Remember the last frame briefly so the same logical task is
    // never written twice to the long-lived chat process.
    lastTask: null,
    lastTaskAt: 0,
  });
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
  if (!active || !active.child.stdin || active.child.stdin.destroyed || active.child.stdin.writableEnded || !task) {
    return { handled: false, error: "本地会话尚未准备好接收任务。" };
  }
  const now = Date.now();
  if (active.lastTask === task && now - active.lastTaskAt < 2_000) {
    return { handled: true, deduplicated: true };
  }
  try {
    active.child.stdin.write(task + "\n");
    active.lastTask = task;
    active.lastTaskAt = now;
    return { handled: true };
  } catch (error) {
    return { handled: false, error: error?.message || "无法写入本地会话。" };
  }
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
