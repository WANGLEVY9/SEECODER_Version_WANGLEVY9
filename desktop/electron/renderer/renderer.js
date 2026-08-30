"use strict";

const STORAGE_KEY = "seecoder-electron-sessions-v1";
const MODE_KEY = "seecoder-electron-mode-v1";
const defaultWorkspace = "";
const SUGGESTIONS = [
  { icon: "🔎", label: "探索并理解代码", task: "请先通读当前工作区的核心源码，梳理模块结构，并解释数据是如何流动的。" },
  { icon: "🧩", label: "构建新功能、应用或工具", task: "查看当前工作区结构与依赖，实现一个小而完整的新功能，并运行测试验证。" },
  { icon: "🧐", label: "审查代码并提出修改建议", task: "审查当前工作区的代码质量，指出潜在 bug、边界与安全问题，并给出可落地的修改建议。" },
  { icon: "🔥", label: "修复问题和失败", task: "定位当前工作区里失败的测试或缺陷，做最小修复，然后运行测试确认通过。" },
];
const state = { sessions: loadSessions(), currentId: null, running: false, submitting: false, mode: loadMode(), usageTotal: 0, lastRun: null, lastSubmission: null, reviewAvailable: false, desktopMessage: '正在检查桌面内核…', review: { open: false, path: null, lines: [], loading: false, error: "" } };

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
const workspaceDialog = $('#workspace-dialog');
const workspaceParentLabel = $('#workspace-parent-label');
const workspaceName = $('#workspace-name');
const workspaceDialogError = $('#workspace-dialog-error');
const inspectorPages = { run: $('#run-inspector'), tools: $('#tools-inspector'), skills: $('#skills-inspector') };
const TOOL_CATALOG = [
  ['list_files', '浏览工作区目录', '只读'], ['read_file', '读取文件内容', '只读'], ['search_files', '搜索文件内容', '只读'],
  ['find_files', '按 glob 查找文件', '只读'], ['project_overview', '概览项目结构', '只读'], ['search_code', '检索代码符号', '只读'],
  ['git_diff', '读取本地差异', '只读'], ['git_status', '查看工作树状态', '只读'], ['git_log', '查看提交历史', '只读'],
  ['git_show', '查看提交摘要', '只读'], ['list_skills', '列出本地 Skills', '只读'], ['delete_file', '安全删除单个临时文件', '受策略控制'], ['create_directory', '创建工作区目录', '受策略控制'], ['copy_file', '复制工作区文件', '受策略控制'], ['move_file', '移动或重命名文件', '受策略控制'], ['rename_directory', '重命名代码目录', '受策略控制'],
  ['write_file', '原子写入文件', '受策略控制'], ['apply_patch', '精确应用补丁', '受策略控制'], ['run_command', '受限 argv 命令', '受策略控制'],
];
const SKILL_CATALOG = [
  ['上下文管理', '历史裁剪、长度预算与回合保留'], ['工具安全', '工作区边界、敏感文件与命令白名单'],
  ['代码工作流', '浏览、修改、测试与 Git 差异追踪'], ['本地记忆', '会话持久化，不保存 API key'],
];
let selectedWorkspaceParent = '';
let inspectorPage = 'run';

function loadSessions() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    return Array.isArray(saved) ? saved : [];
  } catch { return []; }
}
function loadMode() {
  const value = localStorage.getItem(MODE_KEY);
  return ["ask", "plan", "auto"].includes(value) ? value : "auto";
}
function persist() { localStorage.setItem(STORAGE_KEY, JSON.stringify(state.sessions)); }
function makeSession(workspace = defaultWorkspace) { return { id: crypto.randomUUID(), title: '新对话', workspace, createdAt: Date.now(), updatedAt: Date.now(), messages: [], environment: null, localChanges: [] }; }
function current() { return state.sessions.find((session) => session.id === state.currentId); }
function ensureSession() { if (!state.sessions.length) state.sessions.push(makeSession()); if (!current()) state.currentId = state.sessions[0].id; persist(); }
function escapeText(value) { const element = document.createElement('span'); element.textContent = value; return element.innerHTML; }
function setBadge(label, kind = 'ready') { stateBadge.textContent = label; stateBadge.className = 'state-badge ' + (kind === 'ready' ? '' : kind); }
function setCost(value) { costBadge.textContent = 'tokens ' + Number(value || 0).toLocaleString(); }
function renderSessions() {
  sessionList.innerHTML = '';
  const groups = new Map();
  for (const session of state.sessions) { const key = session.workspace || ''; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(session); }
  [...groups.entries()].sort(([left], [right]) => left ? (right ? shortPath(left).localeCompare(shortPath(right)) : -1) : 1).forEach(([workspace, sessions]) => {
    const group = document.createElement('section'); group.className = 'project-group';
    const header = document.createElement('div'); header.className = 'project-header';
    const title = document.createElement('span'); title.className = 'project-title'; title.textContent = workspace ? shortPath(workspace) : '未选择项目';
    const icon = document.createElement('span'); icon.className = 'project-icon'; icon.textContent = workspace ? '▱' : '⌂';
    header.append(icon, title);
    if (workspace) {
      const add = document.createElement('button'); add.className = 'project-add'; add.type = 'button'; add.textContent = '+'; add.title = '在项目中创建会话';
      add.addEventListener('click', () => { if (!state.running) { const session = makeSession(workspace); state.sessions.unshift(session); state.currentId = session.id; persist(); render(); renderEnvironment(); taskInput.focus(); } });
      header.append(add);
    }
    group.append(header);
    sessions.slice().sort((a, b) => b.updatedAt - a.updatedAt).forEach((session) => {
      const row = document.createElement('div'); row.className = 'session-row' + (session.archived ? ' archived' : '');
      const button = document.createElement('button'); button.className = 'session-item' + (session.id === state.currentId ? ' active' : '');
      button.innerHTML = '<span class="session-item-icon">◌</span><span><strong></strong><small></small></span>';
      button.querySelector('strong').textContent = session.title; button.querySelector('small').textContent = session.archived ? '已归档' : (workspace ? shortPath(workspace) : '未选择工作区');
      button.addEventListener('click', () => { if (!state.running) { state.currentId = session.id; state.review = { open: false, path: null, lines: [], loading: false, error: '' }; setReviewOpen(false); persist(); render(); renderEnvironment(); } });
      const menu = document.createElement('button'); menu.className = 'session-menu'; menu.type = 'button'; menu.textContent = '•••'; menu.title = '会话操作';
      menu.addEventListener('click', (event) => { event.stopPropagation(); if (state.running) return; const action = window.prompt('输入操作：rename 重命名，archive 归档/恢复，delete 删除', 'rename'); if (action === 'rename') { const title = window.prompt('新的会话名称', session.title)?.trim(); if (title) { session.title = title; session.updatedAt = Date.now(); persist(); render(); } } else if (action === 'archive') { session.archived = !session.archived; session.updatedAt = Date.now(); persist(); render(); } else if (action === 'delete' && window.confirm('删除此会话及其本地记录？')) { state.sessions = state.sessions.filter((item) => item.id !== session.id); ensureSession(); render(); renderEnvironment(); } });
      row.append(button, menu); group.append(row);
    });
    sessionList.append(group);
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
function localChangeFiles(session) { return Array.isArray(session?.localChanges) ? session.localChanges : []; }
function changeFiles(environment, session) {
  if (environment?.isRepository && Array.isArray(environment.files) && environment.files.length) return environment.files;
  return localChangeFiles(session);
}
function changeTotals(files) { return files.reduce((totals, file) => ({ added: totals.added + (Number(file.added) || 0), deleted: totals.deleted + (Number(file.deleted) || 0) }), { added: 0, deleted: 0 }); }
function changeCard(environment, session) {
  const filesForCard = changeFiles(environment, session);
  if (!filesForCard.length) return '';
  const totals = changeTotals(filesForCard);
  const files = filesForCard.slice(0, 6).map((file) => '<li><button class="change-file" data-diff-path="' + escapeText(file.path) + '"><code>' + escapeText(file.path) + '</code><span><b>+' + (Number(file.added) || 0) + '</b> <i>−' + (Number(file.deleted) || 0) + '</i><small>查看差异 →</small></span></button></li>').join('');
  const remaining = filesForCard.length - 6;
  const sourceHint = environment?.isRepository ? '' : '<small class="change-source">本轮 Agent 编辑记录（工作区未检测到 Git 基线）</small>';
  return '<section class="change-card"><header><div class="change-icon">▣</div><div><strong>已编辑 ' + filesForCard.length + ' 个文件</strong><small><b>+' + totals.added + '</b> −' + totals.deleted + '</small>' + sourceHint + '</div></header><ul>' + files + (remaining > 0 ? '<li class="more-files">另有 ' + remaining + ' 个文件</li>' : '') + '</ul></section>';
}
function renderWelcome() {
  const session = current();
  if (!session?.workspace) {
    conversation.innerHTML = '<section class="workspace-onboarding"><div class="onboarding-mark"><img src="assets/seecoder-logo.png" alt="SEECODER" /></div><span class="eyebrow">本地 Coding Agent</span><h1>先选择一个开发区域</h1><p>选择已有文件夹，或在本地创建一个新的会话工作区。所有代码读取、修改、命令和 Git 操作都会限制在你选定的目录中。</p><div class="onboarding-actions"><button class="onboarding-primary" data-workspace-action="open">⌁ 选择本地文件夹</button><button class="onboarding-secondary" data-workspace-action="create">＋ 新建会话工作区</button></div><small>工作区可随时在顶部或左侧切换。</small></section>';
    conversation.querySelectorAll('[data-workspace-action]').forEach((button) => button.addEventListener('click', () => button.dataset.workspaceAction === 'open' ? chooseWorkspace() : openWorkspaceDialog()));
    return;
  }
  const cards = SUGGESTIONS.map((item) => '<button class="suggestion-card" data-task="' + escapeText(item.task) + '"><span class="s-icon">' + item.icon + '</span>' + escapeText(item.label) + '</button>').join('');
  conversation.innerHTML = '<section class="welcome"><div><div class="welcome-mark"><img src="assets/seecoder-logo.png" alt="SEECODER" /></div><h1>从一个真实任务开始</h1><p>选择你的工作区，描述希望完成的修改。SEECODER 会在本地读取文件、执行受限命令并给出可审计的结果。</p><div class="suggestion-grid">' + cards + '</div><span class="hint">⌘ ↵ 发送任务</span></div></section>';
  conversation.querySelectorAll('.suggestion-card').forEach((card) => card.addEventListener('click', () => { taskInput.value = card.dataset.task; taskInput.focus(); }));
}
function renderConversation() {
  // Rendering a different persisted session invalidates the transient
  // streaming element from the previous run.
  liveAgentEl = null; liveAgentText = '';
  const session = current(); $('#session-title').textContent = session.title; $('#workspace-label').textContent = session.workspace || '尚未选择本地开发区域';
  if (!session.messages.length) { renderWelcome(); return; }
  conversation.innerHTML = session.messages.map((message) => { const label = { user: '你', agent: 'SEECODER', system: '本地状态' }[message.role] || '本地状态'; const body = message.role === 'agent' ? markdownToHtml(message.content) : escapeText(message.content); return '<article class="message ' + message.role + '"><div class="message-meta"><span class="dot"></span>' + label + '</div><div class="message-body' + (message.role === 'agent' ? ' markdown' : '') + '">' + body + '</div></article>'; }).join('') + changeCard(session.environment, session);
  conversation.querySelectorAll('[data-diff-path]').forEach((button) => button.addEventListener('click', () => openReview(button.dataset.diffPath)));
  conversation.scrollTop = conversation.scrollHeight;
}
function updateComposerAvailability() {
  const hasWorkspace = Boolean(current()?.workspace);
  const busy = state.running || state.submitting;
  if (!busy) { sendButton.disabled = !hasWorkspace; taskInput.disabled = !hasWorkspace; }
  else { sendButton.disabled = true; taskInput.disabled = true; }
  taskInput.placeholder = hasWorkspace ? '描述一个真实的编程任务…' : '先选择或创建一个本地开发区域…';
}
function render() { renderSessions(); renderConversation(); renderReview(); updateComposerAvailability(); if (modeSelect) modeSelect.value = state.mode; }
function renderInspectorPage() {
  Object.entries(inspectorPages).forEach(([name, element]) => { if (element) element.hidden = name !== inspectorPage; });
  document.querySelectorAll('[data-inspector-page]').forEach((button) => button.classList.toggle('active', button.dataset.inspectorPage === inspectorPage));
}
function renderManagementCatalogs() {
  const toolList = $('#tool-list'); const skillList = $('#skill-list');
  if (toolList) toolList.innerHTML = TOOL_CATALOG.map(([name, detail, badge]) => '<div class="management-row"><span class="management-icon">⌘</span><div><b>' + escapeText(name) + '</b><small>' + escapeText(detail) + '</small></div><em>' + escapeText(badge) + '</em></div>').join('');
  if (skillList) skillList.innerHTML = SKILL_CATALOG.map(([name, detail]) => '<div class="management-row"><span class="management-icon">✦</span><div><b>' + escapeText(name) + '</b><small>' + escapeText(detail) + '</small></div><em>内置</em></div>').join('');
}
function normalizeWorkspacePath(value) {
  return String(value || '').replace(/[\\/]+$/, '');
}
function applyWorkspaceRename(oldPath, newPath) {
  const oldValue = normalizeWorkspacePath(oldPath);
  const newValue = normalizeWorkspacePath(newPath);
  if (!oldValue || !newValue || oldValue === newValue) return false;
  let changed = false;
  for (const session of state.sessions) {
    if (normalizeWorkspacePath(session.workspace) !== oldValue) continue;
    session.workspace = newValue;
    session.environment = null;
    session.updatedAt = Date.now();
    changed = true;
  }
  if (!changed) return false;
  persist();
  addActivity('Agent 已重命名工作区', shortPath(newValue), 'ok');
  render();
  renderEnvironment();
  refreshEnvironment();
  return true;
}
let liveAgentEl = null;
let liveAgentText = '';
function ensureLiveAgent() {
  if (!liveAgentEl) {
    conversation.insertAdjacentHTML('beforeend', '<article class="message agent"><div class="message-meta"><span class="dot"></span>SEECODER</div><div class="message-body" data-live></div></article>');
    liveAgentEl = conversation.querySelector('[data-live]');
    liveAgentText = '';
    conversation.scrollTop = conversation.scrollHeight;
  }
  return liveAgentEl;
}
function addActivity(title, detail = '', kind = '') { const entry = document.createElement('div'); entry.className = 'activity-entry ' + kind; entry.innerHTML = '<strong>' + escapeText(title) + '</strong>' + (detail ? '<small>' + escapeText(detail) + '</small>' : ''); activityList.prepend(entry); }
function recordLocalChange(name, result) {
  const session = current(); if (!session || !result || !['write_file', 'apply_patch', 'delete_file', 'copy_file', 'move_file'].includes(name)) return;
  const paths = [];
  if (typeof result.path === 'string' && result.path !== '.') paths.push(result.path);
  if (typeof result.destination === 'string') paths.push(result.destination);
  if (!paths.length) return;
  if (!Array.isArray(session.localChanges)) session.localChanges = [];
  const added = Number(result.added_lines ?? result.added) || 0;
  const deleted = Number(result.deleted_lines ?? result.deleted) || 0;
  for (const path of paths) {
    const existing = session.localChanges.find((item) => item.path === path);
    if (existing) { existing.added = Math.max(Number(existing.added) || 0, added); existing.deleted = Math.max(Number(existing.deleted) || 0, deleted); }
    else session.localChanges.push({ path, added, deleted });
  }
  session.updatedAt = Date.now(); persist(); renderConversation(); renderReview(); renderEnvironment();
}
function setRunning(running) {
  state.running = running;
  const busy = running || state.submitting;
  sendButton.disabled = busy || !current()?.workspace;
  stopButton.disabled = !running;
  taskInput.disabled = busy || !current()?.workspace;
  if (modeSelect) modeSelect.disabled = busy;
  if (!running) hideApproval();
  setBadge(running ? '运行中' : '就绪', running ? 'running' : 'ready');
}
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
  if (!environment.isRepository) {
    const files = localChangeFiles(session); const totals = changeTotals(files);
    environmentDetails.innerHTML = '<div class="environment-row"><span>⌂</span><div><b>本地工作区</b><small>未检测到 Git 仓库 · Agent 编辑记录仍会保留</small></div></div>' + (files.length ? '<div class="environment-row"><span>▣</span><div><b>本轮编辑</b><small>' + files.length + ' 个文件</small></div><em><b>+' + totals.added + '</b> <i>−' + totals.deleted + '</i></em></div>' : '') + '<div class="environment-row"><span>◌</span><div><b>' + escapeText(state.mode) + ' 模式</b><small>' + (state.running ? '本地任务运行中' : '等待下一次任务') + '</small></div></div>'; return;
  }
  const changeText = environment.files.length ? '<b>+' + environment.added + '</b> <i>−' + environment.deleted + '</i>' : '<span class="environment-muted">无未提交变更</span>';
  environmentDetails.innerHTML = '<div class="environment-row"><span>▣</span><div><b>变更</b><small>' + environment.files.length + ' 个文件</small></div><em>' + changeText + '</em></div><div class="environment-row"><span>⌂</span><div><b>本地</b><small>' + escapeText(shortPath(session.workspace)) + '</small></div></div><div class="environment-row"><span>⌘</span><div><b>' + escapeText(environment.branch || 'detached HEAD') + '</b><small>当前分支</small></div></div><div class="environment-row"><span>◌</span><div><b>' + escapeText(state.mode) + ' 模式</b><small>' + (state.running ? '本地任务运行中' : '等待下一次任务') + '</small></div></div>';
}
function diffLineHtml(line) {
  return '<div class="diff-line ' + escapeText(line.kind || 'context') + '"><span>' + String(line.number || '').padStart(3, ' ') + '</span><code>' + escapeText(line.text || ' ') + '</code></div>';
}
function renderReview() {
  const session = current(); const environment = session?.environment;
  const files = changeFiles(environment, session);
  const review = state.review;
  reviewFiles.innerHTML = files.length ? files.map((file) => '<button class="review-file' + (file.path === review.path ? ' active' : '') + '" data-review-path="' + escapeText(file.path) + '"><code>' + escapeText(file.path) + '</code><span><b>+' + (Number(file.added) || 0) + '</b> −' + (Number(file.deleted) || 0) + '</span></button>').join('') : '<p class="review-muted">当前工作区没有可审阅的 Agent 编辑记录。</p>';
  reviewFiles.querySelectorAll('[data-review-path]').forEach((button) => button.addEventListener('click', () => openReview(button.dataset.reviewPath)));
  if (review.loading) {
    reviewSummary.textContent = environment?.isRepository ? '正在从本地 Git 读取差异…' : '正在读取 Agent 编辑后的文件内容…'; diffTitle.textContent = review.path || '正在读取'; diffView.innerHTML = '<p class="review-muted">正在加载只读差异。</p>'; return;
  }
  if (review.error) {
    reviewSummary.textContent = review.error; diffTitle.textContent = review.path || '无法显示差异'; diffView.innerHTML = '<p class="review-muted">未修改文件内容，也未运行外部差异程序。</p>'; return;
  }
  if (!review.path) {
    reviewSummary.textContent = files.length ? (environment?.isRepository ? '选择一个文件，查看本地 Git 未提交差异。' : '选择一个文件，查看 Agent 本轮编辑记录。') : '当前工作区没有可审阅的 Agent 编辑记录。'; diffTitle.textContent = '尚未选择文件'; diffView.innerHTML = '<p class="review-muted">文件差异会在这里以只读形式显示。</p>'; return;
  }
  reviewSummary.textContent = environment?.isRepository ? '以下为 ' + review.path + ' 的本地未提交差异。' : '以下为 ' + review.path + ' 的 Agent 编辑记录；工作区未检测到 Git 基线。'; diffTitle.textContent = review.path;
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
    const local = localChangeFiles(session).find((file) => file.path === rawPath);
    if (local && (Number(local.deleted) || 0) > 0 && !(Number(local.added) || 0)) {
      state.review = { open: true, path: rawPath, lines: [{ number: 1, kind: 'context', text: '该文件已由 Agent 删除；当前工作区没有可读取的文件内容。' }], loading: false, error: '' };
    } else state.review = { open: true, path: rawPath, lines: [], loading: false, error: normalizeReviewError(error) };
  }
  renderReview();
}
async function refreshEnvironment() {
  const session = current(); if (!session?.workspace) return;
  environmentDetails.innerHTML = '<span class="environment-muted">正在读取本地 Git 状态…</span>';
  try { session.environment = await window.seecoderDesktop.inspectEnvironment(session.workspace); persist(); renderEnvironment(); renderConversation(); renderReview(); }
  catch { session.environment = { isRepository: false, files: [] }; renderEnvironment(); }
}
async function applyWorkspace(picked, activityTitle) { if (!picked) return; const session = current(); if (!session) return; if (session.workspace !== picked) session.localChanges = []; session.workspace = picked; session.environment = null; session.updatedAt = Date.now(); state.review = { open: false, path: null, lines: [], loading: false, error: '' }; setReviewOpen(false); persist(); render(); addActivity(activityTitle, shortPath(picked), 'ok'); await refreshEnvironment(); taskInput.focus(); }
async function chooseWorkspace() { await applyWorkspace(await window.seecoderDesktop.chooseWorkspace(), '已选择工作区'); }
function openWorkspaceDialog() { if (state.running) return; workspaceDialogError.textContent = ''; workspaceName.value = ''; selectedWorkspaceParent = ''; workspaceParentLabel.textContent = '尚未选择'; workspaceDialog.showModal(); }
async function chooseWorkspaceParent() { const picked = await window.seecoderDesktop.chooseWorkspaceParent(); if (!picked) return; selectedWorkspaceParent = picked; workspaceParentLabel.textContent = picked; workspaceDialogError.textContent = ''; }
async function createWorkspace() {
  const name = workspaceName.value.trim();
  if (!selectedWorkspaceParent || !name) { workspaceDialogError.textContent = '请先选择父目录并输入文件夹名称。'; return; }
  const result = await window.seecoderDesktop.createWorkspace({ parentPath: selectedWorkspaceParent, name });
  if (!result?.ok) { workspaceDialogError.textContent = result?.error || '无法创建工作区。'; return; }
  workspaceDialog.close(); await applyWorkspace(result.workspace, '已新建工作区');
}
async function sendTask() {
  const task = taskInput.value.trim(); const session = current();
  // The textarea can receive both a click and a Cmd/Ctrl+Enter event in the
  // same run-loop turn. Keep a small client-side idempotency window so one
  // logical submission creates exactly one user message and one stdin write.
  if (!task || state.running || state.submitting) return;
  if (!session.workspace) return;
  const fingerprint = session.id + '\u0000' + task;
  if (state.lastSubmission && state.lastSubmission.fingerprint === fingerprint && Date.now() - state.lastSubmission.at < 2_000) return;
  state.submitting = true;
  state.lastSubmission = { fingerprint, at: Date.now() };
  appendMessage('user', task); taskInput.value = ''; activityList.innerHTML = ''; hideApproval(); render(); renderEnvironment(); setRunning(true); addActivity('启动本地 AgentRunner', '模式：' + state.mode + ' · 受限 argv 执行', 'ok');
  state.lastRun = { task, workspace: session.workspace, mode: state.mode };
  try {
    await window.seecoderDesktop.startChat({ sessionId: session.id, workspace: session.workspace, mode: state.mode });
    const accepted = await window.seecoderDesktop.sendChatTask({ sessionId: session.id, task });
    if (!accepted?.handled) throw new Error('本地会话未准备好接收任务。');
  } catch (error) { state.lastSubmission = null; appendMessage('system', '无法启动本地会话：' + error.message); addActivity('启动失败', error.message, 'error'); setRunning(false); setBadge('需处理', 'error'); render(); }
  finally { state.submitting = false; updateComposerAvailability(); }
}
function approveCurrent(decision) { const session = current(); if (session) window.seecoderDesktop.approve({ sessionId: session.id, decision }); }
function handleRunnerEvent(payload) {
  if (payload?.sessionId && payload.sessionId !== state.currentId) return;
  const { event, data } = payload || {};
  if (event === 'usage') { state.usageTotal = data?.total_tokens || state.usageTotal; setCost(state.usageTotal); return; }
  if (event === 'token') { const el = ensureLiveAgent(); if (el) { liveAgentText += (data?.text || ''); el.classList.add('markdown'); el.innerHTML = markdownToHtml(liveAgentText); conversation.scrollTop = conversation.scrollHeight; } return; }
  if (event === 'reasoning') { addActivity('模型思考', data?.text || '', ''); return; }
  if (event === 'tool_result' && data?.ok && data?.name === 'rename_directory') {
    const result = data?.data || {};
    recordLocalChange(data.name, result);
    if (result.workspace_renamed) applyWorkspaceRename(result.old_path, result.workspace_path || result.new_path);
  }
  if (event === 'tool_result' && data?.ok && ['write_file', 'apply_patch', 'delete_file', 'create_directory', 'copy_file', 'move_file'].includes(data?.name)) recordLocalChange(data.name, data?.data || {});
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
      liveAgentEl = null; liveAgentText = ''; setBadge('待批准', 'running'); render(); return;
    }
    hideApproval();
    appendMessage('agent', data?.final_text || '任务结束，但没有收到可显示的总结。');
    const recoverable = data?.recoverable || ['failed_model', 'failed_protocol', 'stop_context_budget'].includes(stateName);
    const activityDetail = recoverable && stateName !== 'final'
      ? (data?.steps ?? 0) + ' 步 · 上一轮已保留，可继续发送下一条指令或重试'
      : (data?.steps ?? 0) + ' 步';
    addActivity('完成：' + stateName, activityDetail, stateName === 'final' ? 'ok' : 'error'); setRunning(false); liveAgentEl = null; liveAgentText = ''; setBadge(stateName === 'final' ? '已完成' : (recoverable ? '可继续' : '需处理'), stateName === 'final' ? 'ready' : 'error'); render(); refreshEnvironment(); return;
  }
  const summaries = {
    chat_started: ['本地会话已连接', data?.workspace], run_started: ['任务已启动', data?.workspace], model_request: ['请求模型', '第 ' + (data?.step ?? '?') + ' 步'], tool_dispatch: ['准备工具调用', (data?.count ?? 0) + ' 个工具'],
    tool_result: [data?.ok ? '完成工具：' + (data?.name || 'unknown') : '工具失败：' + (data?.name || 'unknown'), data?.error || '', data?.ok ? 'ok' : 'error'],
    plan_proposal: ['计划一步', data?.description || data?.name || '', 'ok'],
    configuration_error: ['配置错误', data?.message || '', 'error'], runner_error: ['本地进程错误', data?.message || '', 'error'], chat_exit: ['本地会话已退出', 'code=' + (data?.code ?? 'null')],
  };
  const summary = summaries[event]; if (summary) addActivity(summary[0], summary[1], summary[2]); else if (event === 'unstructured_output') addActivity('本地输出', data?.text || '');
  if (event === 'tool_result' && data?.ok && ['write_file', 'apply_patch', 'delete_file', 'create_directory', 'copy_file', 'move_file', 'rename_directory'].includes(data?.name)) refreshEnvironment();
  if (event === 'chat_exit') { setRunning(false); hideApproval(); render(); }
}

$('#new-session').addEventListener('click', async () => {
  if (state.running) return;
  // A new top-level conversation always starts from an explicit local
  // project, matching the native flow and preventing unscoped edits.
  const picked = await window.seecoderDesktop.chooseWorkspace();
  if (!picked) return;
  hideApproval();
  const session = makeSession(picked);
  state.sessions.unshift(session); state.currentId = session.id;
  state.review = { open: false, path: null, lines: [], loading: false, error: '' };
  setReviewOpen(false); persist(); render(); renderEnvironment(); await refreshEnvironment(); taskInput.focus();
});
$('#choose-workspace').addEventListener('click', chooseWorkspace); $('#top-workspace').addEventListener('click', chooseWorkspace); $('#composer-workspace').addEventListener('click', chooseWorkspace); $('#create-workspace').addEventListener('click', openWorkspaceDialog); sendButton.addEventListener('click', sendTask);
stopButton.addEventListener('click', async () => { const session = current(); const r = session ? await window.seecoderDesktop.stopChat(session.id) : null; if (r?.stopped) addActivity('已请求停止', '正在终止本地会话'); });
if (modeSelect) modeSelect.addEventListener('change', () => { state.mode = modeSelect.value; localStorage.setItem(MODE_KEY, state.mode); addActivity('切换工作模式', state.mode); });
taskInput.addEventListener('keydown', (event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); sendTask(); } });
$('#about-button').addEventListener('click', () => $('#about-dialog').showModal()); $('#close-about').addEventListener('click', () => $('#about-dialog').close());
$('#close-workspace-dialog').addEventListener('click', () => workspaceDialog.close()); $('#cancel-workspace').addEventListener('click', () => workspaceDialog.close()); $('#choose-workspace-parent').addEventListener('click', chooseWorkspaceParent); $('#confirm-workspace').addEventListener('click', createWorkspace);
$('#refresh-environment').addEventListener('click', refreshEnvironment);
document.querySelectorAll('[data-inspector-page]').forEach((button) => button.addEventListener('click', () => { inspectorPage = button.dataset.inspectorPage; renderInspectorPage(); }));
$('#open-review').addEventListener('click', () => { setReviewOpen(true); renderReview(); });
$('#close-review').addEventListener('click', () => setReviewOpen(false));
window.seecoderDesktop.onRunnerEvent(handleRunnerEvent); window.seecoderDesktop.onRunnerStderr((payload) => addActivity('CLI 提示', payload?.data?.text || '', 'error'));
ensureSession(); renderManagementCatalogs(); renderInspectorPage(); render(); renderEnvironment(); verifyDesktopCapabilities(); refreshEnvironment(); addActivity('桌面端已就绪', '默认模式 ' + state.mode, 'ok');
