const list = document.querySelector('#report-list');
const message = document.querySelector('#page-message');
const search = document.querySelector('#search');
const projectFilter = document.querySelector('#project-filter');
const statusFilter = document.querySelector('#status-filter');
const resetButton = document.querySelector('#reset-filters');

let reports = [];

const projectNames = {
  __full__: '全量测试',
  book: 'Book',
  chat: 'Chat',
  knowledge: 'Knowledge',
  marketplace: 'Marketplace',
  node: 'Node',
  orchestrator: 'Orchestrator',
  project: 'Project',
  router: 'Router',
  social: 'Social',
  wallet: 'Wallet',
  warehouse: 'Warehouse',
};

const statusNames = {
  passed: '通过',
  failed: '失败',
  deploy_or_runner_failed: '运行异常',
  runner_failed: '运行异常',
  blocked: '未执行',
};

function projectName(value) {
  return projectNames[value] || value || '未知项目';
}

function statusKind(value) {
  if (value === 'passed') return 'passed';
  if (value === 'failed') return 'failed';
  return 'other';
}

function statusName(value) {
  return statusNames[value] || '其他结果';
}

function formatDate(value) {
  if (!value) return '时间未知';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(date);
}

function updateSummary() {
  const passed = reports.filter((item) => statusKind(item.status) === 'passed').length;
  const failed = reports.filter((item) => statusKind(item.status) === 'failed').length;
  const other = reports.filter((item) => statusKind(item.status) === 'other').length;
  document.querySelector('#total-count').textContent = String(reports.length);
  document.querySelector('#passed-count').textContent = String(passed);
  document.querySelector('#failed-count').textContent = String(failed);
  document.querySelector('#other-count').textContent = String(other);
  document.querySelector('#updated-at').textContent =
    `更新于 ${formatDate(new Date().toISOString())}`;
}

function populateProjects() {
  const options = [...new Set(reports.map((item) => item.project))].sort((left, right) =>
    projectName(left).localeCompare(projectName(right), 'zh-CN'),
  );

  for (const project of options) {
    const option = document.createElement('option');
    option.value = project;
    option.textContent = projectName(project);
    projectFilter.append(option);
  }
}

function addCount(parent, label, count, className) {
  const item = document.createElement('span');
  item.className = className;
  item.textContent = `${label} ${Number(count || 0)}`;
  parent.append(item);
}

function renderReport(item) {
  const row = document.createElement('article');
  row.className = 'report-row';

  const project = document.createElement('div');
  project.className = 'report-project';
  const title = document.createElement('strong');
  title.textContent = projectName(item.project);
  const run = document.createElement('span');
  run.textContent = `运行 ${item.run || '未知'}`;
  project.append(title, run);

  const time = document.createElement('time');
  time.className = 'run-time';
  time.dateTime = item.finished_at || item.started_at || '';
  time.textContent = formatDate(item.finished_at || item.started_at);

  const counts = document.createElement('div');
  counts.className = 'result-counts';
  addCount(counts, '通过', item.passed, 'passed');
  addCount(counts, '失败', item.failed, 'failed');
  addCount(counts, '跳过', item.skipped, 'skipped');

  const state = document.createElement('span');
  const kind = statusKind(item.status);
  state.className = `result-state result-state-${kind}`;
  state.textContent = statusName(item.status);

  row.append(project, time, counts, state);
  if (typeof item.report === 'string' && item.report.startsWith('/report/')) {
    const link = document.createElement('a');
    link.className = 'button button-secondary report-open';
    link.href = item.report;
    link.textContent = '打开报告';
    row.append(link);
  }
  return row;
}

function render() {
  const query = search.value.trim().toLocaleLowerCase('zh-CN');
  const project = projectFilter.value;
  const status = statusFilter.value;
  const visibleReports = reports.filter((item) => {
    const matchesProject = !project || item.project === project;
    const matchesStatus =
      !status ||
      (status === 'other'
        ? statusKind(item.status) === 'other'
        : statusKind(item.status) === status);
    const searchable = `${projectName(item.project)} ${item.project} ${item.run} ${formatDate(item.finished_at)}`;
    const matchesQuery = !query || searchable.toLocaleLowerCase('zh-CN').includes(query);
    return matchesProject && matchesStatus && matchesQuery;
  });

  list.replaceChildren();
  visibleReports.forEach((item) => list.append(renderReport(item)));
  document.querySelector('#result-count').textContent =
    `显示 ${visibleReports.length} / ${reports.length} 份报告`;
  resetButton.hidden = !project && !status && !query;

  if (visibleReports.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    const heading = document.createElement('strong');
    heading.textContent = reports.length ? '没有符合条件的报告' : '还没有测试报告';
    const explanation = document.createElement('span');
    explanation.textContent = reports.length
      ? '调整项目、结果或搜索词后再试。'
      : '运行一次测试后，报告会出现在这里。';
    empty.append(heading, explanation);
    list.append(empty);
  }
}

function showMessage(text, needsLogin = false) {
  message.replaceChildren();
  message.hidden = false;
  message.append(document.createTextNode(text));
  if (needsLogin) {
    message.append(document.createTextNode(' '));
    const link = document.createElement('a');
    link.href = '/';
    link.textContent = '返回 Tester 登录';
    message.append(link);
  }
}

async function loadReports() {
  try {
    const response = await fetch('/api/reports', { cache: 'no-store' });
    if (response.status === 401) {
      showMessage('登录后才能查看测试报告。', true);
      document.querySelector('#result-count').textContent = '需要钱包身份登录';
      return;
    }
    if (!response.ok) throw new Error('报告列表暂时无法读取，请稍后重试。');

    const payload = await response.json();
    reports = Array.isArray(payload.reports) ? payload.reports : [];
    updateSummary();
    populateProjects();
    render();
  } catch {
    showMessage('报告列表暂时无法读取，请检查 Tester 服务后重试。');
    document.querySelector('#result-count').textContent = '报告加载失败';
  }
}

search.addEventListener('input', render);
projectFilter.addEventListener('change', render);
statusFilter.addEventListener('change', render);
resetButton.addEventListener('click', () => {
  search.value = '';
  projectFilter.value = '';
  statusFilter.value = '';
  render();
  search.focus();
});
document.querySelector('#filters').addEventListener('submit', (event) => event.preventDefault());

loadReports();
