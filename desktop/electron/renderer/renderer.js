"use strict";

const STORAGE_KEY = "seecoder-electron-sessions-v1";
const defaultWorkspace = "demo_workspace";
const SUGGESTIONS = [
  { icon: "🔎", label: "探索并理解代码", task: "请先通读当前工作区的核心源码，梳理模块结构，并解释数据是如何流动的。" },
  { icon: "🧩", label: "构建新功能、应用或工具", task: "查看当前工作区结构与依赖，实现一个小而完整的新功能，并运行测试验证。" },
  { icon: "🧐", label: "审查代码并提出修改建议", task: "审查当前工作区的代码质量，指出潜在 bug、边界与安全问题，并给出可落地的修改建议。" },
  { icon: "🔥", label: "修复问题和失败", task: "定位当前工作区里失败的测试或缺陷，做最小修复，然后运行测试确认通过。" },
];
const state = { sessions: loadSessions(), currentId: null, running: false, mode: "ask", usageTotal: 0, lastRun: null, reviewAvailable: false, desktopMessage: '正在检查桌面内核…', review: { open: false, path: null, lines: [], loading: false, error: "" } };

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
const environmentDetails = $('#environment-details');
const appShell = $('.app-shell');
const activityContent = $('.activity-content');
const reviewPane = $('#review-pane');
const reviewFiles = $('#review-files');
const reviewSummary = $('#review-summary');
const diffTitle = $('#diff-title');
const diffView = $('#diff-view');

function loadSessions() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    return Array.isArray(saved) ? saved : [];
  } catch { return []; }
}
function persist() { localStorage.setItem(STORAGE_KEY, JSON.stringify(state.sessions)); }
function makeSession(workspace = defaultWorkspace) { return { id: crypto.randomUUID(), title: '新对话', workspace, createdAt: Date.now(), updatedAt: Date.now(), messages: [], environment: null }; }
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
    button.addEventListener('click', () => { if (!state.running) { state.currentId = session.id; state.review = { open: false, path: null, lines: [], loading: false, error: '' }; setReviewOpen(false); persist(); render(); renderEnvironment(); } });
    sessionList.append(button);
  });
}
function shortPath(value) { const parts = String(value).split('/').filter(Boolean); return parts.slice(-2).join('/') || value; }
function appendMessage(role, content) { const session = current(); if (!session) return; session.messages.push({ role, content, createdAt: Date.now() }); session.updatedAt = Date.now(); if (role === 'user' && session.title === '新对话') session.title = content.replace(/\s+/g, ' ').slice(0, 22) || '新对话'; persist(); }
function inlineMarkdown(value) {
  return escapeText(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
}
function markdownToHtml(source) {
  const lines = String(source || '').replace(/\r\n/g, '\n').split('\n');
  const blocks = []; let paragraph = []; let list = []; let ordered = false; let code = []; let inCode = false;
  const flushParagraph = () => { if (paragraph.length) { blocks.push('<p>' + paragraph.map(inlineMarkdown).join('<br>') + '</p>'); paragraph = []; } };
  const flushList = () => { if (list.length) { const tag = ordered ? 'ol' : 'ul'; blocks.push('<' + tag + '>' + list.map((item) => '<li>' + inlineMarkdown(item) + '</li>').join('') + '</' + tag + '>'); list = []; } };
  const flushCode = () => { if (code.length || inCode) { blocks.push('<pre><code>' + escapeText(code.join('\n')) + '</code></pre>'); code = []; } };
  for (const line of lines) {
    if (line.startsWith('```')) { if (inCode) { flushCode(); inCode = false; } else { flushParagraph(); flushList(); inCode = true; } continue; }
    if (inCode) { code.push(line); continue; }
    const heading = line.match(/^(#{1,3})\s+(.+)$/); const bullet = line.match(/^[-*+]\s+(.+)$/); const numbered = line.match(/^\d+\.\s+(.+)$/);
    if (heading) { flushParagraph(); flushList(); blocks.push('<h' + heading[1].length + '>' + inlineMarkdown(heading[2]) + '</h' + heading[1].length + '>'); continue; }
    if (bullet || numbered) { flushParagraph(); const nextOrdered = Boolean(numbered); if (list.length && ordered !== nextOrdered) flushList(); ordered = nextOrdered; list.push((bullet || numbered)[1]); continue; }
    if (!line.trim()) { flushParagraph(); flushList(); continue; }
    paragraph.push(line);
  }
  if (inCode) flushCode(); flushParagraph(); flushList();
  return blocks.join('') || '<p></p>';
}
function changeCard(environment) {
  if (!environment?.isRepository || !environment.files?.length) return '';
  const files = environment.files.slice(0, 6).map((file) => '<li><button class="change-file" data-diff-path="' + escapeText(file.path) + '"><code>' + escapeText(file.path) + '</code><span><b>+' + file.added + '</b> <i>−' + file.deleted + '</i><small>查看差异 →</small></span></button></li>').join('');
  const remaining = environment.files.length - 6;
  return '<section class="change-card"><header><div class="change-icon">▣</div><div><strong>已编辑 ' + environment.files.length + ' 个文件</strong><small><b>+' + environment.added + '</b> −' + environment.deleted + '</small></div></header><ul>' + files + (remaining > 0 ? '<li class="more-files">另有 ' + remaining + ' 个文件</li>' : '') + '</ul></section>';
}
function renderWelcome() {
  const cards = SUGGESTIONS.map((item) => '<button class="suggestion-card" data-task="' + escapeText(item.task) + '"><span class="s-icon">' + item.icon + '</span>' + escapeText(item.label) + '</button>').join('');
  conversation.innerHTML = '<section class="welcome"><div><div class="welcome-mark"><img src="assets/seecoder-logo.png" alt="SEECODER" /></div><h1>从一个真实任务开始</h1><p>选择你的工作区，描述希望完成的修改。SEECODER 会在本地读取文件、执行受限命令并给出可审计的结果。</p><div class="suggestion-grid">' + cards + '</div><span class="hint">⌘ ↵ 发送任务</span></div></section>';
  conversation.querySelectorAll('.suggestion-card').forEach((card) => card.addEventListener('click', () => { taskInput.value = card.dataset.task; taskInput.focus(); }));
}
function renderConversation() {
  const session = current(); $('#session-title').textContent = session.title; $('#workspace-label').textContent = session.workspace === defaultWorkspace ? '默认演示工作区 · demo_workspace' : session.workspace;
  if (!session.messages.length) { renderWelcome(); return; }
  conversation.innerHTML = session.messages.map((message) => { const label = { user: '你', agent: 'SEECODER', system: '本地状态' }[message.role] || '本地状态'; const body = message.role === 'agent' ? markdownToHtml(message.content) : escapeText(message.content); return '<article class="message ' + message.role + '"><div class="message-meta"><span class="dot"></span>' + label + '</div><div class="message-body' + (message.role === 'agent' ? ' markdown' : '') + '">' + body + '</div></article>'; }).join('') + changeCard(session.environment);
  conversation.querySelectorAll('[data-diff-path]').forEach((button) => button.addEventListener('click', () => openReview(button.dataset.diffPath)));
  conversation.scrollTop = conversation.scrollHeight;
}
function render() { renderSessions(); renderConversation(); renderReview(); }
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
function renderEnvironment() {
  const session = current(); const environment = session?.environment;
  if (!environment) { environmentDetails.innerHTML = '<span class="environment-muted">尚未读取环境信息</span>'; return; }
  if (!environment.isRepository) { environmentDetails.innerHTML = '<div class="environment-row"><span>⌂</span><div><b>本地工作区</b><small>未检测到 Git 仓库</small></div></div>'; return; }
  const changeText = environment.files.length ? '<b>+' + environment.added + '</b> <i>−' + environment.deleted + '</i>' : '<span class="environment-muted">无未提交变更</span>';
  environmentDetails.innerHTML = '<div class="environment-row"><span>▣</span><div><b>变更</b><small>' + environment.files.length + ' 个文件</small></div><em>' + changeText + '</em></div><div class="environment-row"><span>⌂</span><div><b>本地</b><small>' + escapeText(shortPath(session.workspace)) + '</small></div></div><div class="environment-row"><span>⌘</span><div><b>' + escapeText(environment.branch || 'detached HEAD') + '</b><small>当前分支</small></div></div><div class="environment-row"><span>◌</span><div><b>' + escapeText(state.mode) + ' 模式</b><small>' + (state.running ? '本地任务运行中' : '等待下一次任务') + '</small></div></div>';
}
function diffLineHtml(line) {
  return '<div class="diff-line ' + escapeText(line.kind || 'context') + '"><span>' + String(line.number || '').padStart(3, ' ') + '</span><code>' + escapeText(line.text || ' ') + '</code></div>';
}
function renderReview() {
  const session = current(); const environment = session?.environment;
  const files = environment?.isRepository ? (environment.files || []) : [];
  const review = state.review;
  reviewFiles.innerHTML = files.length ? files.map((file) => '<button class="review-file' + (file.path === review.path ? ' active' : '') + '" data-review-path="' + escapeText(file.path) + '"><code>' + escapeText(file.path) + '</code><span><b>+' + file.added + '</b> −' + file.deleted + '</span></button>').join('') : '<p class="review-muted">当前工作区没有可审阅的未提交变更。</p>';
  reviewFiles.querySelectorAll('[data-review-path]').forEach((button) => button.addEventListener('click', () => openReview(button.dataset.reviewPath)));
  if (review.loading) {
    reviewSummary.textContent = '正在从本地 Git 读取差异…'; diffTitle.textContent = review.path || '正在读取'; diffView.innerHTML = '<p class="review-muted">正在加载只读差异。</p>'; return;
  }
  if (review.error) {
    reviewSummary.textContent = review.error; diffTitle.textContent = review.path || '无法显示差异'; diffView.innerHTML = '<p class="review-muted">未修改文件内容，也未运行外部差异程序。</p>'; return;
  }
  if (!review.path) {
    reviewSummary.textContent = files.length ? '选择一个文件，查看本地 Git 未提交差异。' : '当前工作区没有可审阅的未提交变更。'; diffTitle.textContent = '尚未选择文件'; diffView.innerHTML = '<p class="review-muted">文件差异会在这里以只读形式显示。</p>'; return;
  }
  reviewSummary.textContent = '以下为 ' + review.path + ' 的本地未提交差异。'; diffTitle.textContent = review.path;
  diffView.innerHTML = review.lines.length ? review.lines.map(diffLineHtml).join('') : '<p class="review-muted">Git 未返回可显示的文本差异。该文件可能只有暂存区变更，或工作区状态已更新。</p>';
}
function setReviewOpen(open) {
  state.review.open = open; reviewPane.hidden = !open; activityContent.hidden = open; appShell.classList.toggle('review-open', open);
}
function desktopRestartMessage() {
  return '当前窗口连接的是旧版桌面内核，尚未加载本地差异接口。请完全退出所有 SEECODER 窗口后重新运行 desktop/run_desktop_electron.sh。';
}
function normalizeReviewError(error) {
  const message = error?.message || String(error || '无法读取本地差异。');
  return /No handler registered|is not a function|seecoder:read-diff/.test(message) ? desktopRestartMessage() : message;
}
async function verifyDesktopCapabilities() {
  try {
    const result = await window.seecoderDesktop.getCapabilities();
    const features = Array.isArray(result?.features) ? result.features : [];
    state.reviewAvailable = Number(result?.protocolVersion) >= 2 && features.includes('local_git_diff');
    state.desktopMessage = state.reviewAvailable ? '本地 Git 审阅已就绪。' : desktopRestartMessage();
  } catch {
    state.reviewAvailable = false; state.desktopMessage = desktopRestartMessage();
  }
  renderReview();
}
async function openReview(rawPath) {
  const session = current(); if (!session?.workspace || !rawPath) return;
  setReviewOpen(true);
  if (!state.reviewAvailable) {
    state.review = { open: true, path: rawPath, lines: [], loading: false, error: state.desktopMessage || desktopRestartMessage() }; renderReview(); return;
  }
  state.review = { open: true, path: rawPath, lines: [], loading: true, error: '' }; renderReview();
  try {
    const result = await window.seecoderDesktop.readDiff({ workspace: session.workspace, path: rawPath });
    if (!result?.ok) throw new Error(result?.error || '无法读取本地差异。');
    if (current()?.id !== session.id) return;
    state.review = { open: true, path: result.path, lines: Array.isArray(result.lines) ? result.lines : [], loading: false, error: '' };
  } catch (error) {
    state.review = { open: true, path: rawPath, lines: [], loading: false, error: normalizeReviewError(error) };
  }
  renderReview();
}
async function refreshEnvironment() {
  const session = current(); if (!session?.workspace) return;
  environmentDetails.innerHTML = '<span class="environment-muted">正在读取本地 Git 状态…</span>';
  try { session.environment = await window.seecoderDesktop.inspectEnvironment(session.workspace); persist(); renderEnvironment(); renderConversation(); renderReview(); }
  catch { session.environment = { isRepository: false, files: [] }; renderEnvironment(); }
}
async function chooseWorkspace() { const picked = await window.seecoderDesktop.chooseWorkspace(); if (!picked) return; current().workspace = picked; current().environment = null; current().updatedAt = Date.now(); state.review = { open: false, path: null, lines: [], loading: false, error: '' }; setReviewOpen(false); persist(); render(); addActivity('已选择工作区', shortPath(picked), 'ok'); await refreshEnvironment(); }
async function sendTask() {
  const task = taskInput.value.trim(); const session = current(); if (!task || state.running) return; if (!session.workspace) return;
  appendMessage('user', task); taskInput.value = ''; activityList.innerHTML = ''; hideApproval(); render(); renderEnvironment(); setRunning(true); addActivity('启动本地 AgentRunner', '模式：' + state.mode + ' · 受限 argv 执行', 'ok');
  state.lastRun = { task, workspace: session.workspace, mode: state.mode };
  try {
    await window.seecoderDesktop.startChat({ sessionId: session.id, workspace: session.workspace, mode: state.mode });
    const accepted = await window.seecoderDesktop.sendChatTask({ sessionId: session.id, task });
    if (!accepted?.handled) throw new Error('本地会话未准备好接收任务。');
  } catch (error) { appendMessage('system', '无法启动本地会话：' + error.message); addActivity('启动失败', error.message, 'error'); setRunning(false); setBadge('需处理', 'error'); render(); }
}
function approveCurrent(decision) { const session = current(); if (session) window.seecoderDesktop.approve({ sessionId: session.id, decision }); }
function handleRunnerEvent(payload) {
  if (payload?.sessionId && payload.sessionId !== state.currentId) return;
  const { event, data } = payload || {};
  if (event === 'usage') { state.usageTotal = data?.total_tokens || state.usageTotal; setCost(state.usageTotal); return; }
  if (event === 'token') { const el = ensureLiveAgent(); if (el) { el.textContent += (data?.text || ''); conversation.scrollTop = conversation.scrollHeight; } return; }
  if (event === 'approval_request') {
    showApproval('批准工具调用：' + (data?.name || '未知工具') + '？', () => approveCurrent(true), () => approveCurrent(false));
    addActivity('等待批准', data?.name || '', 'running'); return;
  }
  if (event === 'turn_outcome') {
    const stateName = data?.state || 'unknown';
    const planSteps = Array.isArray(data?.plan) ? data.plan : [];
    if (stateName === 'plan_proposed') {
      const planLines = planSteps.map((s) => '- ' + (s.description || s.tool)).join('\n');
      appendMessage('agent', planLines ? (data?.final_text || '') + '\n' + planLines : (data?.final_text || '计划已生成。'));
      addActivity('计划已生成，等待批准', planSteps.length + ' 步计划', 'running');
      showApproval('批准该计划并继续执行？', () => approveCurrent(true), () => approveCurrent(false));
      liveAgentEl = null; setBadge('待批准', 'running'); render(); return;
    }
    hideApproval();
    appendMessage('agent', data?.final_text || '任务结束，但没有收到可显示的总结。');
    addActivity('完成：' + stateName, (data?.steps ?? 0) + ' 步', stateName === 'final' ? 'ok' : 'error'); setRunning(false); liveAgentEl = null; setBadge(stateName === 'final' ? '已完成' : '需处理', stateName === 'final' ? 'ready' : 'error'); render(); refreshEnvironment(); return;
  }
  const summaries = {
    chat_started: ['本地会话已连接', data?.workspace], run_started: ['任务已启动', data?.workspace], model_request: ['请求模型', '第 ' + (data?.step ?? '?') + ' 步'], tool_dispatch: ['准备工具调用', (data?.count ?? 0) + ' 个工具'],
    tool_result: [data?.ok ? '完成工具：' + (data?.name || 'unknown') : '工具失败：' + (data?.name || 'unknown'), data?.error || '', data?.ok ? 'ok' : 'error'],
    plan_proposal: ['计划一步', data?.description || data?.name || '', 'ok'],
    configuration_error: ['配置错误', data?.message || '', 'error'], runner_error: ['本地进程错误', data?.message || '', 'error'], chat_exit: ['本地会话已退出', 'code=' + (data?.code ?? 'null')],
  };
  const summary = summaries[event]; if (summary) addActivity(summary[0], summary[1], summary[2]); else if (event === 'unstructured_output') addActivity('本地输出', data?.text || '');
  if (event === 'tool_result' && data?.ok && ['write_file', 'apply_patch'].includes(data?.name)) refreshEnvironment();
  if (event === 'chat_exit') { setRunning(false); hideApproval(); render(); }
}

$('#new-session').addEventListener('click', () => { if (state.running) return; hideApproval(); state.sessions.unshift(makeSession(current()?.workspace || defaultWorkspace)); state.currentId = state.sessions[0].id; state.review = { open: false, path: null, lines: [], loading: false, error: '' }; setReviewOpen(false); persist(); render(); renderEnvironment(); taskInput.focus(); });
$('#choose-workspace').addEventListener('click', chooseWorkspace); $('#top-workspace').addEventListener('click', chooseWorkspace); $('#composer-workspace').addEventListener('click', chooseWorkspace); sendButton.addEventListener('click', sendTask);
stopButton.addEventListener('click', async () => { const session = current(); const r = session ? await window.seecoderDesktop.stopChat(session.id) : null; if (r?.stopped) addActivity('已请求停止', '正在终止本地会话'); });
if (modeSelect) modeSelect.addEventListener('change', () => { state.mode = modeSelect.value; addActivity('切换工作模式', state.mode); });
taskInput.addEventListener('keydown', (event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); sendTask(); } });
$('#about-button').addEventListener('click', () => $('#about-dialog').showModal()); $('#close-about').addEventListener('click', () => $('#about-dialog').close());
$('#refresh-environment').addEventListener('click', refreshEnvironment);
$('#open-review').addEventListener('click', () => { setReviewOpen(true); renderReview(); });
$('#close-review').addEventListener('click', () => setReviewOpen(false));
window.seecoderDesktop.onRunnerEvent(handleRunnerEvent); window.seecoderDesktop.onRunnerStderr((payload) => addActivity('CLI 提示', payload?.data?.text || '', 'error'));
ensureSession(); render(); renderEnvironment(); verifyDesktopCapabilities(); refreshEnvironment(); addActivity('桌面端已就绪', '默认模式 ' + state.mode, 'ok');
