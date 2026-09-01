"use strict";

function buildBackendInvocation(uv, task, workspace, mode) {
  if (typeof uv !== "string" || !uv.trim()) throw new Error("未找到 uv；请安装 uv 或设置 SEECODER_UV。");
  if (typeof task !== "string" || !task.trim()) throw new Error("任务不能为空。");
  if (typeof workspace !== "string" || !workspace.trim()) throw new Error("请先选择一个已存在的工作区。");
  const allowed = { auto: "auto", plan: "plan", ask: "ask" };
  const selected = allowed[mode] || "auto";
  const args = ["run", "seecoder", "run", task.trim(), "--workspace", workspace, "--event-json", "--mode", selected];
  return { command: uv, args };
}

function buildChatInvocation(uv, workspace, mode, sessionPath, resume, sessionId) {
  if (typeof uv !== "string" || !uv.trim()) throw new Error("未找到 uv；请安装 uv 或设置 SEECODER_UV。");
  if (typeof workspace !== "string" || !workspace.trim()) throw new Error("请先选择一个已存在的工作区。");
  if (typeof sessionPath !== "string" || !sessionPath.trim()) throw new Error("会话存储路径不能为空。");
  const allowed = { auto: "auto", plan: "plan", ask: "ask" };
  const selected = allowed[mode] || "auto";
  const args = ["run", "seecoder", "chat", "--workspace", workspace, "--event-json", "--save", sessionPath, "--mode", selected];
  if (typeof sessionId === "string" && sessionId.trim()) args.push("--session-id", sessionId.trim());
  if (resume) args.push("--resume", sessionPath);
  return { command: uv, args };
}

function parseEventLine(line) {
  try {
    const payload = JSON.parse(line);
    if (typeof payload.event !== "string" || !payload.data || typeof payload.data !== "object" || Array.isArray(payload.data)) {
      return null;
    }
    const envelope = { event: payload.event, data: payload.data };
    if (payload.protocol_version !== undefined) {
      if (!Number.isInteger(payload.protocol_version) || payload.protocol_version < 1
          || typeof payload.session_id !== "string" || !payload.session_id
          || typeof payload.run_id !== "string" || !payload.run_id
          || !Number.isInteger(payload.sequence) || payload.sequence < 1) return null;
      envelope.protocolVersion = payload.protocol_version;
      envelope.sessionId = payload.session_id;
      envelope.runId = payload.run_id;
      envelope.sequence = payload.sequence;
    }
    return envelope;
  } catch {
    return null;
  }
}

function parseGitEnvironment({ branch = "", nameStatus = "", numstat = "", untracked = "", untrackedCounts = {}, isRepository = null }) {
  const counts = new Map();
  let added = 0;
  let deleted = 0;
  for (const line of String(numstat).split(/\r?\n/)) {
    const [plus, minus, file] = line.split("\t");
    if (!file) continue;
    const add = Number.parseInt(plus, 10) || 0;
    const remove = Number.parseInt(minus, 10) || 0;
    counts.set(file, { added: add, deleted: remove });
    added += add;
    deleted += remove;
  }
  const files = [];
  for (const line of String(nameStatus).split(/\r?\n/)) {
    const [rawStatus, ...paths] = line.split("\t");
    const file = paths.at(-1);
    if (!rawStatus || !file) continue;
    const count = counts.get(file) || { added: 0, deleted: 0 };
    files.push({ path: file, status: rawStatus, ...count });
  }
  for (const file of String(untracked).split(/\r?\n/).map((value) => value.trim()).filter(Boolean)) {
    if (files.some((entry) => entry.path === file)) continue;
    const count = untrackedCounts[file] || { added: 0, deleted: 0 };
    files.push({ path: file, status: "??", ...count });
    added += count.added;
    deleted += count.deleted;
  }
  return { isRepository: isRepository === null ? Boolean(branch) : Boolean(isRepository), branch: branch.trim(), added, deleted, files };
}

function parseUnifiedDiff(diff = "") {
  return String(diff).split(/\r?\n/).filter((text, index, lines) => text || index < lines.length - 1).map((text, index) => {
    let kind = "context";
    if (text.startsWith("@@")) kind = "hunk";
    else if (text.startsWith("+++ ") || text.startsWith("--- ")) kind = "file";
    else if (text.startsWith("+")) kind = "added";
    else if (text.startsWith("-")) kind = "removed";
    else if (text.startsWith("diff ") || text.startsWith("index ")) kind = "meta";
    return { number: index + 1, kind, text };
  });
}

function desktopCapabilities() {
  return { protocolVersion: 3, features: ["local_git_diff", "ordered_session_events", "persisted_approval", "changeset_journal"] };
}

function validateWorkspaceFolderName(value) {
  const name = typeof value === "string" ? value.trim() : "";
  return name && name.length <= 80 && name !== "." && name !== ".." && !/[\\/\0]/.test(name) ? name : null;
}

module.exports = { buildBackendInvocation, buildChatInvocation, parseEventLine, parseGitEnvironment, parseUnifiedDiff, desktopCapabilities, validateWorkspaceFolderName };
