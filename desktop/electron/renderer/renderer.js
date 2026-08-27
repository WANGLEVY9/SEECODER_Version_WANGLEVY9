"use strict";

const STORAGE_KEY = "seecoder-electron-sessions-v1";
const defaultWorkspace = "demo_workspace";
const state = { sessions: loadSessions(), currentId: null, running: false };

const $ = (selector) => document.querySelector(selector);
const sessionList = $("#session-list");
const conversation = $("#conversation");
const activityList = $("#activity-list");
const taskInput = $("#task-input");
const sendButton = $("#send-task");
const stopButton = $("#stop-run");
const stateBadge = $("#state-badge");

function loadSessions() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(saved) ? saved : [];
  } catch { return []; }
}
function persist() { localStorage.setItem(STORAGE_KEY, JSON.stringify(state.sessions)); }
function makeSession(workspace = defaultWorkspace) { return { id: crypto.randomUUID(), title: "新对话", workspace, createdAt: Date.now(), updatedAt: Date.now(), messages: [] }; }
function current() { return state.sessions.find((session) => session.id === state.currentId); }
function ensureSession() { if (!state.sessions.length) state.sessions.push(makeSession()); if (!current()) state.currentId = state.sessions[0].id; persist(); }
function escapeText(value) { const element = document.createElement("span"); element.textContent = value; return element.innerHTML; }
function setBadge(label, kind = "ready") { stateBadge.textContent = label; stateBadge.className = `state-badge ${kind === "ready" ? "" : kind}`; }
function renderSessions() {
  sessionList.innerHTML = "";
  state.sessions.slice().sort((a, b) => b.updatedAt - a.updatedAt).forEach((session) => {
    const button = document.createElement("button"); button.className = `session-item ${session.id === state.currentId ? "active" : ""}`;
    button.innerHTML = `<span class="session-item-icon">◌</span><span><strong>${escapeText(session.title)}</strong><small>${escapeText(shortPath(session.workspace))}</small></span>`;
    button.addEventListener("click", () => { if (!state.running) { state.currentId = session.id; persist(); render(); } });
    sessionList.append(button);
  });
}
function shortPath(value) { const parts = String(value).split("/").filter(Boolean); return parts.slice(-2).join("/") || value; }
function appendMessage(role, content) { const session = current(); if (!session) return; session.messages.push({ role, content, createdAt: Date.now() }); session.updatedAt = Date.now(); if (role === "user" && session.title === "新对话") session.title = content.replace(/\s+/g, " ").slice(0, 22) || "新对话"; persist(); }
function renderConversation() {
  const session = current(); $("#session-title").textContent = session.title; $("#workspace-label").textContent = session.workspace === defaultWorkspace ? "默认演示工作区 · demo_workspace" : session.workspace;
  if (!session.messages.length) { conversation.innerHTML = `<section class="welcome"><div><div class="welcome-mark"><img src="assets/seecoder-logo.png" alt="SEECODER" /></div><h1>从一个真实任务开始</h1><p>选择你的工作区，描述希望完成的修改。SEECODER 会在本地读取文件、执行受限命令并给出可审计的结果。</p><span class="hint">⌘ ↵ 发送任务</span></div></section>`; return; }
  conversation.innerHTML = session.messages.map((message) => { const label = { user: "你", agent: "SEECODER", system: "本地状态" }[message.role] || "本地状态"; return `<article class="message ${message.role}"><div class="message-meta"><span class="dot"></span>${label}</div><div class="message-body">${escapeText(message.content)}</div></article>`; }).join("");
  conversation.scrollTop = conversation.scrollHeight;
}
function render() { renderSessions(); renderConversation(); }
function addActivity(title, detail = "", kind = "") { const entry = document.createElement("div"); entry.className = `activity-entry ${kind}`; entry.innerHTML = `<strong>${escapeText(title)}</strong>${detail ? `<small>${escapeText(detail)}</small>` : ""}`; activityList.prepend(entry); }
function setRunning(running) { state.running = running; sendButton.disabled = running; stopButton.disabled = !running; taskInput.disabled = running; setBadge(running ? "运行中" : "就绪", running ? "running" : "ready"); }
async function chooseWorkspace() { const picked = await window.seecoderDesktop.chooseWorkspace(); if (!picked) return; current().workspace = picked; current().updatedAt = Date.now(); persist(); render(); addActivity("已选择工作区", shortPath(picked), "ok"); }
async function sendTask() {
  const task = taskInput.value.trim(); const session = current(); if (!task || state.running) return; if (!session.workspace || session.workspace === defaultWorkspace && !session.workspace) return;
  appendMessage("user", task); taskInput.value = ""; activityList.innerHTML = ""; render(); setRunning(true); addActivity("启动本地 AgentRunner", "默认受限 argv 执行模式", "ok");
  try { await window.seecoderDesktop.startRun({ task, workspace: session.workspace }); } catch (error) { appendMessage("system", `无法启动任务：${error.message}`); addActivity("启动失败", error.message, "error"); setRunning(false); setBadge("需处理", "error"); render(); }
}
function handleRunnerEvent(payload) {
  const { event, data } = payload || {}; const summaries = {
    run_started: ["任务已启动", data?.workspace], model_request: ["请求模型", `第 ${data?.step ?? "?"} 步`], tool_dispatch: ["准备工具调用", `${data?.count ?? 0} 个工具`],
    tool_result: [data?.ok ? `完成工具：${data?.name || "unknown"}` : `工具失败：${data?.name || "unknown"}`, data?.error || "", data?.ok ? "ok" : "error"],
    configuration_error: ["配置错误", data?.message || "", "error"], runner_error: ["本地进程错误", data?.message || "", "error"], process_exit: ["本地进程已退出", `code=${data?.code ?? "null"}`],
  };
  if (event === "run_outcome") { appendMessage("agent", data?.final_text || "任务结束，但没有收到可显示的总结。"); addActivity(`完成：${data?.state || "unknown"}`, `${data?.steps ?? 0} 步`, data?.state === "final" ? "ok" : "error"); setRunning(false); setBadge(data?.state === "final" ? "已完成" : "需处理", data?.state === "final" ? "ready" : "error"); render(); return; }
  const summary = summaries[event]; if (summary) addActivity(summary[0], summary[1], summary[2]); else if (event === "unstructured_output") addActivity("本地输出", data?.text || "");
  if (event === "process_exit" && state.running) { setRunning(false); render(); }
}

$("#new-session").addEventListener("click", () => { if (state.running) return; state.sessions.unshift(makeSession(current()?.workspace || defaultWorkspace)); state.currentId = state.sessions[0].id; persist(); render(); taskInput.focus(); });
$("#choose-workspace").addEventListener("click", chooseWorkspace); $("#top-workspace").addEventListener("click", chooseWorkspace); $("#composer-workspace").addEventListener("click", chooseWorkspace); sendButton.addEventListener("click", sendTask);
stopButton.addEventListener("click", async () => { const { stopped } = await window.seecoderDesktop.stopRun(); if (stopped) addActivity("已请求停止", "正在终止本地任务"); });
taskInput.addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") { event.preventDefault(); sendTask(); } });
$("#about-button").addEventListener("click", () => $("#about-dialog").showModal()); $("#close-about").addEventListener("click", () => $("#about-dialog").close());
window.seecoderDesktop.onRunnerEvent(handleRunnerEvent); window.seecoderDesktop.onRunnerStderr((payload) => addActivity("CLI 提示", payload?.data?.text || "", "error"));
ensureSession(); render(); addActivity("桌面端已就绪", "等待本地任务", "ok");
