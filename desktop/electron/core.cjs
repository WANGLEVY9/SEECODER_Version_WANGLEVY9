"use strict";

function buildBackendInvocation(uv, task, workspace, mode) {
  if (typeof uv !== "string" || !uv.trim()) throw new Error("未找到 uv；请安装 uv 或设置 SEECODER_UV。");
  if (typeof task !== "string" || !task.trim()) throw new Error("任务不能为空。");
  if (typeof workspace !== "string" || !workspace.trim()) throw new Error("请先选择一个已存在的工作区。");
  const allowed = { auto: "auto", plan: "plan", ask: "ask" };
  const selected = allowed[mode] || "auto";
  const args = ["run", "seecoder", "run", task.trim(), "--workspace", workspace, "--event-json"];
  if (selected !== "auto") args.push("--mode", selected);
  return { command: uv, args };
}

function buildChatInvocation(uv, workspace, mode, sessionPath, resume) {
  if (typeof uv !== "string" || !uv.trim()) throw new Error("未找到 uv；请安装 uv 或设置 SEECODER_UV。");
  if (typeof workspace !== "string" || !workspace.trim()) throw new Error("请先选择一个已存在的工作区。");
  if (typeof sessionPath !== "string" || !sessionPath.trim()) throw new Error("会话存储路径不能为空。");
  const allowed = { auto: "auto", plan: "plan", ask: "ask" };
  const selected = allowed[mode] || "auto";
  const args = ["run", "seecoder", "chat", "--workspace", workspace, "--event-json", "--save", sessionPath];
  if (resume) args.push("--resume", sessionPath);
  if (selected !== "auto") args.push("--mode", selected);
  return { command: uv, args };
}

function parseEventLine(line) {
  try {
    const payload = JSON.parse(line);
    if (typeof payload.event !== "string" || !payload.data || typeof payload.data !== "object" || Array.isArray(payload.data)) {
      return null;
    }
    return { event: payload.event, data: payload.data };
  } catch {
    return null;
  }
}

function parseGitEnvironment({ branch = "", nameStatus = "", numstat = "" }) {
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
  return { isRepository: Boolean(branch), branch: branch.trim(), added, deleted, files };
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
  return { protocolVersion: 2, features: ["local_git_diff"] };
}

module.exports = { buildBackendInvocation, buildChatInvocation, parseEventLine, parseGitEnvironment, parseUnifiedDiff, desktopCapabilities };
