"use strict";

const STORAGE_KEY = "seecoder-electron-sessions-v1";
const defaultWorkspace = "demo_workspace";
const SUGGESTIONS = [
  { icon: "🔎", label: "探索并理解代码", task: "请先通读当前工作区的核心源码，梳理模块结构，并解释数据是如何流动的。" },
  { icon: "🧩", label: "构建新功能、应用或工具", task: "查看当前工作区结构与依赖，实现一个小而完整的新功能，并运行测试验证。" },
  { icon: "🧐", label: "审查代码并提出修改建议", task: "审查当前工作区的代码质量，指出潜在 bug、边界与安全问题，并给出可落地的修改建议。" },
  { icon: "🔥", label: "修复问题和失败", task: "定位当前工作区里失败的测试或缺陷，做最小修复，然后运行测试确认通过。" },
];
const state = { sessions: loadSessions(), currentId: null, running: false, mode: "ask", usageTotal: 0, lastRun: null };

const $ = (selector) => document.querySelector(selector);
const sessionList = $('#session-list');
const conversation = $('#conversation');
const activityList = $('#activity-list');
const taskInput = $('#task-input');
const sendButton = $('#send-task');
const stopButton = $('#stop-run');
const stateBadge = $('#state-badge');
const costBadge = $('#cost-badge');
const modeSelect = $('#mode-select');
const approvalBanner = $('#approval-banner');
const approvalText = $('#approval-text');

function loadSessions() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    return Array.isArray(saved) ? saved : [];
  } catch { return []; }
}
function persist() { localStorage.setItem(STORAGE_KEY, JSON.stringify(state.sessions)); }
function makeSession(workspace = defaultWorkspace) { return { id: crypto.randomUUID(), title: '新对话', workspace, createdAt: Date.now(), updatedAt: Date.now(), messages: [] }; }
function current() { return state.sessions.find((session) => session.id === state.currentId); }
function ensureSession() { if (!state.sessions.length) state.sessions.push(makeSession()); if (!current()) state.currentId = state.sessions[0].id; persist(); }
function escapeText(value) { const element = document.createElement('span'); element.textContent = value; return element.innerHTML; }
function setBadge(label, kind = 'ready') { stateBadge.textContent = label; stateBadge.className = 'state-badge ' + (kind === 'ready' ? '' : kind); }
function setCost(value) { costBadge.textContent = 'tokens ' + Number(value || 0).toLocaleString(); }
function renderSessions() {
  sessionList.innerHTML = '';
  state.sessions.slice().sort((a, b) => b.updatedAt - a.updatedAt).forEach((session) => {
    const button = document.createElement('button');
    button.className = 'session-item' + (session.id === state.currentId ? ' active' : '');
    button.innerHTML = '<span class="session-item-icon">◌</span><span><strong>' + escapeText(session.title) + '</strong><small>' + escapeText(shortPath(session.workspace)) + '</small></span>';
    button.addEventListener('click', () => { if (!state.running) { state.currentId = session.id; persist(); render(); } });
    sessionList.append(button);
  });
}
function shortPath(value) { const parts = String(value).split('/').filter(Boolean); return parts.slice(-2).join('/') || value; }
function appendMessage(role, content) { const session = current(); if (!session) return; session.messages.push({ role, content, createdAt: Date.now() }); session.updatedAt = Date.now(); if (role === 'user' && session.title === '新对话') session.title = content.replace(/\s+/g, ' ').slice(0, 22) || '新对话'; persist(); }
function renderWelcome() {
  const cards = SUGGESTIONS.map((item) => '<button class="suggestion-card" data-task="' + escapeText(item.task) + '"><span class="s-icon">' + item.icon + '</span>' + escapeText(item.label) + '</button>').join('');
  conversation.innerHTML = '<section class="welcome"><div><div class="welcome-mark"><img src="assets/seecoder-logo.png" alt="SEECODER" /></div><h1>从一个真实任务开始</h1><p>选择你的工作区，描述希望完成的修改。SEECODER 会在本地读取文件、执行受限命令并给出可审计的结果。</p><div class="suggestion-grid">' + cards + '</div><span class="hint">⌘ ↵ 发送任务</span></div></section>';
  conversation.querySelectorAll('.suggestion-card').forEach((card) => card.addEventListener('click', () => { taskInput.value = card.dataset.task; taskInput.focus(); }));
}
function renderConversation() {
  const session = current(); $('#session-title').textContent = session.title; $('#workspace-label').textContent = session.workspace === defaultWorkspace ? '默认演示工作区 · demo_workspace' : session.workspace;
  if (!session.messages.length) { renderWelcome(); return; }
  conversation.innerHTML = session.messages.map((message) => { const label = { user: '你', agent: 'SEECODER', system: '本地状态' }[message.role] || '本地状态'; return '<article class="message ' + message.role + '"><div class="message-meta"><span class="dot"></span>' + label + '</div><div class="message-body">' + escapeText(message.content) + '</div></article>'; }).join('');
  conversation.scrollTop = conversation.scrollHeight;
}
function render() { renderSessions(); renderConversation(); }
let liveAgentEl = null;
function ensureLiveAgent() {
  if (!liveAgentEl) {
    conversation.insertAdjacentHTML('beforeend', '<article class="message agent"><div class="message-meta"><span class="dot"></span>SEECODER</div><div class="message-body" data-live></div></article>');
    liveAgentEl = conversation.querySelector('[data-live]');
    conversation.scrollTop = conversation.scrollHeight;
  }
  return liveAgentEl;
}
function addActivity(title, detail = '', kind = '') { const entry = document.createElement('div'); entry.className = 'activity-entry ' + kind; entry.innerHTML = '<strong>' + escapeText(title) + '</strong>' + (detail ? '<small>' + escapeText(detail) + '</small>' : ''); activityList.prepend(entry); }
function setRunning(running) { state.running = running; sendButton.disabled = running; stopButton.disabled = !running; taskInput.disabled = running; if (modeSelect) modeSelect.disabled = running; if (!running) hideApproval(); setBadge(running ? '运行中' : '就绪', running ? 'running' : 'ready'); }
function showApproval(text, onApprove, onDeny) {
  approvalText.textContent = text; approvalBanner.hidden = false;
  const approveBtn = $('#approve-btn'); const denyBtn = $('#deny-btn');
  approveBtn.onclick = () => { approvalBanner.hidden = true; if (onApprove) onApprove(); };
  denyBtn.onclick = () => { approvalBanner.hidden = true; if (onDeny) onDeny(); };
}
function hideApproval() { approvalBanner.hidden = true; }
async function chooseWorkspace() { const picked = await window.seecoderDesktop.chooseWorkspace(); if (!picked) return; current().workspace = picked; current().updatedAt = Date.now(); persist(); render(); addActivity('已选择工作区', shortPath(picked), 'ok'); }
async function sendTask() {
  const task = taskInput.value.trim(); const session = current(); if (!task || state.running) return; if (!session.workspace) return;
  appendMessage('user', task); taskInput.value = ''; activityList.innerHTML = ''; hideApproval(); render(); setRunning(true); addActivity('启动本地 AgentRunner', '模式：' + state.mode + ' · 受限 argv 执行', 'ok');
  state.lastRun = { task, workspace: session.workspace, mode: state.mode };
  try { await window.seecoderDesktop.startRun({ task, workspace: session.workspace, mode: state.mode }); } catch (error) { appendMessage('system', '无法启动任务：' + error.message); addActivity('启动失败', error.message, 'error'); setRunning(false); setBadge('需处理', 'error'); render(); }
}
function executePlanAgain() {
  if (!state.lastRun || state.running) return;
  const copy = Object.assign({}, state.lastRun); hideApproval();
  state.mode = 'auto'; if (modeSelect) modeSelect.value = 'auto';
  addActivity('已批准计划', '以自动模式重新执行', 'ok');
  setRunning(true);
  window.seecoderDesktop.startRun({ task: copy.task, workspace: copy.workspace, mode: 'auto' }).catch((error) => { setRunning(false); addActivity('执行失败', error.message, 'error'); });
}
function handleRunnerEvent(payload) {
  const { event, data } = payload || {};
  if (event === 'usage') { state.usageTotal = data?.total_tokens || state.usageTotal; setCost(state.usageTotal); return; }
  if (event === 'token') { const el = ensureLiveAgent(); if (el) { el.textContent += (data?.text || ''); conversation.scrollTop = conversation.scrollHeight; } return; }
  if (event === 'approval_request') {
    showApproval('批准工具调用：' + (data?.name || '未知工具') + '？', () => window.seecoderDesktop.approve(true), () => window.seecoderDesktop.approve(false));
    addActivity('等待批准', data?.name || '', 'running'); return;
  }
  if (event === 'run_outcome') {
    const stateName = data?.state || 'unknown';
    const planSteps = Array.isArray(data?.plan) ? data.plan : [];
    if (stateName === 'plan_proposed') {
      const planLines = planSteps.map((s) => '- ' + (s.description || s.tool)).join('\n');
      appendMessage('agent', planLines ? (data?.final_text || '') + '\n' + planLines : (data?.final_text || '计划已生成。'));
      addActivity('计划已生成，等待批准', planSteps.length + ' 步计划', 'running');
      showApproval('批准该计划并以自动模式执行？', () => executePlanAgain(), () => setRunning(false));
      liveAgentEl = null; setBadge('待批准', 'running'); render(); return;
    }
    hideApproval();
    appendMessage('agent', data?.final_text || '任务结束，但没有收到可显示的总结。');
    addActivity('完成：' + stateName, (data?.steps ?? 0) + ' 步', stateName === 'final' ? 'ok' : 'error'); setRunning(false); liveAgentEl = null; setBadge(stateName === 'final' ? '已完成' : '需处理', stateName === 'final' ? 'ready' : 'error'); render(); return;
  }
  const summaries = {
    run_started: ['任务已启动', data?.workspace], model_request: ['请求模型', '第 ' + (data?.step ?? '?') + ' 步'], tool_dispatch: ['准备工具调用', (data?.count ?? 0) + ' 个工具'],
    tool_result: [data?.ok ? '完成工具：' + (data?.name || 'unknown') : '工具失败：' + (data?.name || 'unknown'), data?.error || '', data?.ok ? 'ok' : 'error'],
    plan_proposal: ['计划一步', data?.description || data?.name || '', 'ok'],
    configuration_error: ['配置错误', data?.message || '', 'error'], runner_error: ['本地进程错误', data?.message || '', 'error'], process_exit: ['本地进程已退出', 'code=' + (data?.code ?? 'null')],
  };
  const summary = summaries[event]; if (summary) addActivity(summary[0], summary[1], summary[2]); else if (event === 'unstructured_output') addActivity('本地输出', data?.text || '');
  if (event === 'process_exit') { setRunning(false); hideApproval(); render(); }
}

$('#new-session').addEventListener('click', () => { if (state.running) return; hideApproval(); state.sessions.unshift(makeSession(current()?.workspace || defaultWorkspace)); state.currentId = state.sessions[0].id; persist(); render(); taskInput.focus(); });
$('#choose-workspace').addEventListener('click', chooseWorkspace); $('#top-workspace').addEventListener('click', chooseWorkspace); $('#composer-workspace').addEventListener('click', chooseWorkspace); sendButton.addEventListener('click', sendTask);
stopButton.addEventListener('click', async () => { const r = await window.seecoderDesktop.stopRun(); if (r?.stopped) addActivity('已请求停止', '正在终止本地任务'); });
if (modeSelect) modeSelect.addEventListener('change', () => { state.mode = modeSelect.value; addActivity('切换工作模式', state.mode); });
taskInput.addEventListener('keydown', (event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); sendTask(); } });
$('#about-button').addEventListener('click', () => $('#about-dialog').showModal()); $('#close-about').addEventListener('click', () => $('#about-dialog').close());
window.seecoderDesktop.onRunnerEvent(handleRunnerEvent); window.seecoderDesktop.onRunnerStderr((payload) => addActivity('CLI 提示', payload?.data?.text || '', 'error'));
ensureSession(); render(); addActivity('桌面端已就绪', '默认模式 ' + state.mode, 'ok');
