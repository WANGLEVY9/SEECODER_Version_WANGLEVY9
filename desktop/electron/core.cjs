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

module.exports = { buildBackendInvocation, parseEventLine };
