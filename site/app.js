const navItems = [...document.querySelectorAll('.nav-item[data-section]')];
const observedSections = [...document.querySelectorAll('[data-observe]')];
const currentScope = document.querySelector('#currentScope');
const inspectorLinks = [...document.querySelectorAll('.inspector-toc a')];

const labels = {
  overview: 'SYSTEM OVERVIEW',
  loop: 'RUNTIME LOOP',
  modules: 'ARCHITECTURE',
  evidence: 'EVIDENCE',
  guardrails: 'BOUNDARIES',
  roadmap: 'ROADMAP',
  'source-map': 'SOURCE MAP',
};

function setActive(id) {
  navItems.forEach((item) => item.classList.toggle('active', item.dataset.section === id));
  inspectorLinks.forEach((item) => item.classList.toggle('active', item.getAttribute('href') === `#${id}`));
  if (currentScope && labels[id]) currentScope.textContent = labels[id];
}

const observer = new IntersectionObserver((entries) => {
  const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
  if (visible) setActive(visible.target.dataset.observe);
}, { rootMargin: '-18% 0px -65% 0px', threshold: [0.05, 0.25, 0.5] });
observedSections.forEach((section) => observer.observe(section));

const sidebar = document.querySelector('#sidebar');
document.querySelector('#mobileMenu')?.addEventListener('click', () => sidebar?.classList.toggle('open'));
navItems.forEach((item) => item.addEventListener('click', () => sidebar?.classList.remove('open')));

const themeToggle = document.querySelector('#themeToggle');
themeToggle?.addEventListener('click', () => {
  const light = document.body.classList.toggle('light');
  themeToggle.textContent = light ? '切换深色控制室' : '切换浅色阅读';
  localStorage.setItem('seecoder-map-theme', light ? 'light' : 'dark');
});
if (localStorage.getItem('seecoder-map-theme') === 'light') {
  document.body.classList.add('light');
  if (themeToggle) themeToggle.textContent = '切换深色控制室';
}

const dialog = document.querySelector('#searchDialog');
const searchInput = document.querySelector('#searchInput');
const results = document.querySelector('#searchResults');
const searchTrigger = () => { dialog?.showModal(); searchInput?.focus(); };
document.querySelector('#searchTrigger')?.addEventListener('click', searchTrigger);
document.querySelector('#mobileSearch')?.addEventListener('click', searchTrigger);
document.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); searchTrigger(); }
  if (event.key === 'Escape' && sidebar?.classList.contains('open')) sidebar.classList.remove('open');
});

const searchable = [
  ['系统总览', '#overview', 'agent · local-first · audit'],
  ['一次任务怎样流动', '#loop', 'runner · context · model · tools'],
  ['核心模块与职责', '#modules', 'runner.py · session.py · tools/ · trace.py'],
  ['验证证据', '#evidence', '101/101 · regression · offline'],
  ['安全边界', '#guardrails', 'WorkspaceBoundary · Policy · restricted argv'],
  ['开发路线', '#roadmap', 'shipped · next · isolation · checkpoints'],
  ['源码入口', '#source-map', 'GitHub · source tree · docs'],
];
searchInput?.addEventListener('input', (event) => {
  const query = event.target.value.trim().toLowerCase();
  const matches = query ? searchable.filter((item) => item.join(' ').toLowerCase().includes(query)) : [];
  results.innerHTML = matches.length
    ? matches.map(([title, href, detail]) => `<a class="search-result" href="${href}"><strong>${title}</strong><br /><small>${detail}</small></a>`).join('')
    : '<p>输入关键词，跳转到对应的工程区域。</p>';
  results.querySelectorAll('a').forEach((link) => link.addEventListener('click', () => dialog?.close()));
});

window.addEventListener('hashchange', () => {
  const id = window.location.hash.replace('#', '');
  if (labels[id]) setActive(id);
});
