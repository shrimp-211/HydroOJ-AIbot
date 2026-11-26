'use strict';

const $ = id => document.getElementById(id);
const apiRoot = document.documentElement.dataset.apiRoot.replace(/\/$/, '');

function readPreference(storage, key) {
  try { return window[storage].getItem(key) || ''; } catch { return ''; }
}
function writePreference(storage, key, value) {
  try { window[storage].setItem(key, value); } catch { /* 禁用存储时仍可在当前页面使用。 */ }
}

async function requestJSON(path, {body, method, signal, revision, snapshot = false} = {}) {
  const headers = new Headers();
  if (revision) headers.set('If-Match', revision);
  if (body !== undefined) headers.set('Content-Type', 'application/json');
  const response = await fetch(apiRoot + path, {method: method || (body === undefined ? 'GET' : 'POST'),
    headers, body: body === undefined ? undefined : JSON.stringify(body), signal});
  const data = await response.json().catch(() => null);
  if (response.status === 403 && data?.code === 'unauthorized') {
    location.assign(`${apiRoot}/login?next=${encodeURIComponent(location.pathname + location.hash)}`);
    throw new Error('登录已过期，请重新登录。');
  }
  if (!response.ok || data === null) {
    const details = Array.isArray(data?.errors) ? data.errors.join('；') : '';
    const error = new Error(details || data?.error || `服务响应异常（HTTP ${response.status}）`);
    error.status = response.status;
    throw error;
  }
  if (snapshot) return {data, revision: response.headers.get('ETag'),
    overrides: JSON.parse(response.headers.get('X-Config-Overrides') || '{}')};
  return data;
}

function message(id, text, state = 'idle') {
  const element = $(id);
  element.hidden = !text;
  element.textContent = text;
  element.dataset.state = state;
}

let toastTimer;
function toast(text, state = 'ok') {
  const element = $('toast');
  element.textContent = text;
  element.dataset.state = state;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.textContent = ''; }, state === 'error' ? 5500 : 3000);
}

async function withButton(id, work) {
  const button = $(id);
  if (button.disabled) return;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try { return await work(); }
  finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}

const tabs = [...document.querySelectorAll('[role="tab"]')];
let activePage;
function openPage(name, focus = false) {
  const selected = tabs.find(tab => tab.id === `nav-${name}`) || tabs[0];
  if (focus) selected.focus();
  if (activePage === selected.id) return;
  activePage = selected.id;
  for (const tab of tabs) {
    const active = tab === selected;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    $(tab.getAttribute('aria-controls')).hidden = !active;
  }
  const title = selected.textContent.trim();
  $('current-page').textContent = title;
  document.title = `${title} · HydroAI`;
  if (name === 'config' && (!configLoaded || configStale) && !changedFields.size) loadConfig();
  if (name === 'models' && !providersLoaded) loadProviders();
  if (name === 'usage' && Date.now() - usageLoadedAt > 30000) refreshUsage();
  if (selected === tabs[0]) refreshHistory();
}

function navigate(tab, focus = false) {
  const name = tab.id.slice(4);
  if (location.hash !== `#${name}`) history.pushState(null, '', `#${name}`);
  openPage(name, focus);
  window.scrollTo(0, 0);
}

const horizontalTabs = window.matchMedia('(max-width: 900px)');
function syncTabOrientation() {
  tabs[0].parentElement.setAttribute('aria-orientation', horizontalTabs.matches ? 'horizontal' : 'vertical');
}
syncTabOrientation();
horizontalTabs.addEventListener('change', syncTabOrientation);
for (const [index, tab] of tabs.entries()) {
  tab.addEventListener('click', () => navigate(tab));
  tab.addEventListener('keydown', event => {
    const next = horizontalTabs.matches ? 'ArrowRight' : 'ArrowDown';
    const prev = horizontalTabs.matches ? 'ArrowLeft' : 'ArrowUp';
    const targets = {[next]: (index + 1) % tabs.length, [prev]: (index + tabs.length - 1) % tabs.length,
      Home: 0, End: tabs.length - 1};
    if (!Object.hasOwn(targets, event.key)) return;
    event.preventDefault();
    navigate(tabs[targets[event.key]], true);
  });
}
window.addEventListener('hashchange', () => {
  if (location.hash !== '#workspace') openPage(location.hash.slice(1));
});

function setTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('theme-toggle').setAttribute('aria-pressed', String(dark));
}
setTheme(readPreference('localStorage', 'hydroai-theme') === 'dark');
$('theme-toggle').addEventListener('click', () => {
  const dark = document.documentElement.dataset.theme !== 'dark';
  setTheme(dark);
  writePreference('localStorage', 'hydroai-theme', dark ? 'dark' : 'light');
});

// 实时日志：重连时只保留最新的一次连接尝试。
const logView = $('console');
const logLines = $('console-lines');
const logFollow = $('log-follow');
const logLimit = 1000;

function logBottom() { logView.scrollTop = logView.scrollHeight; }
function syncLogEmpty() {
  $('console-empty').hidden = logLines.childElementCount > 0;
  $('copy-log').disabled = !logLines.childElementCount;
}
function appendStream(lines) {
  if (!Array.isArray(lines)) return;
  const fragment = document.createDocumentFragment();
  for (const line of lines.slice(-logLimit)) {
    if (typeof line !== 'string') continue;
    const row = document.createElement('div');
    row.textContent = line;
    if (/\[-\]|\bERROR\b/i.test(line)) row.className = 'log-error';
    else if (/\[!\]|\bWARN(?:ING)?\b/i.test(line)) row.className = 'log-warn';
    else if (/\[\+\]|成功|\bAC\b/.test(line)) row.className = 'log-ok';
    fragment.append(row);
  }
  logLines.append(fragment);
  while (logLines.childElementCount > logLimit) logLines.firstElementChild.remove();
  syncLogEmpty();
  if (logFollow.checked) logBottom();
}

function connectStream() {
  message('stream-status', '连接中…');
  // 由浏览器自动重连并发送 Last-Event-ID，避免重放已收到的日志。
  const connection = new EventSource(`${apiRoot}/stream`);
  connection.onopen = () => message('stream-status', '实时日志已连接', 'ok');
  connection.onmessage = event => {
    try { appendStream(JSON.parse(event.data)); } catch { /* 忽略格式异常的事件。 */ }
  };
  connection.onerror = () => {
    if (connection.readyState === EventSource.CLOSED) {
      message('stream-status', '日志连接已关闭，请刷新页面。', 'warn');
      requestJSON('/history').catch(() => {}); // 会话失效时回到登录页。
    } else {
      message('stream-status', '连接中断，正在重连…', 'warn');
    }
  };
}

logView.addEventListener('scroll', () => {
  if (logView.scrollHeight - logView.clientHeight - logView.scrollTop > 24) logFollow.checked = false;
});
logFollow.addEventListener('change', () => { if (logFollow.checked) logBottom(); });
$('copy-log').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText([...logLines.children].map(row => row.textContent).join('\n'));
    toast('已复制当前控制台日志');
  } catch {
    toast('浏览器未允许复制，可选中日志后手动复制。', 'error');
  }
});
$('clear-log').addEventListener('click', () => withButton('clear-log', async () => {
  try {
    await requestJSON('/clear', {method: 'POST'});
    logLines.replaceChildren();
    syncLogEmpty();
    toast('控制台已清空');
  } catch (error) { toast(error.message, 'error'); }
}));

// 求解状态以服务器为准，并在请求进行中锁定相关操作。
let taskRunning = false;
let daemonRunning = false;
let actionPending = false;
let statusPending = false;
let statusGeneration = 0;
let historyGeneration = 0;
let historyKey;
const taskStarts = document.querySelectorAll('[data-task-start]');

function syncTaskButtons() {
  for (const button of taskStarts) button.disabled = actionPending || taskRunning;
  $('btn-kill').disabled = actionPending || !taskRunning;
  $('btn-daemon-start').disabled = actionPending || daemonRunning;
  $('btn-daemon-stop').disabled = actionPending || !daemonRunning;
}
function renderStatus() {
  message('daemon-badge', daemonRunning ? '守护运行中' : '守护已停止', daemonRunning ? 'ok' : 'idle');
  message('task-badge', taskRunning ? '任务运行中' : '无运行任务', taskRunning ? 'ok' : 'idle');
  syncTaskButtons();
}

async function refreshStatus(force = false) {
  if (!force && (statusPending || actionPending || document.hidden)) return;
  const generation = ++statusGeneration;
  statusPending = true;
  try {
    const state = await requestJSON('/status');
    if (generation !== statusGeneration) return;
    taskRunning = state.task_running;
    daemonRunning = state.daemon_running;
    renderStatus();
  } catch {
    if (generation !== statusGeneration) return;
    message('daemon-badge', '守护状态不可用', 'warn');
    message('task-badge', '任务状态不可用', 'warn');
  } finally {
    if (generation === statusGeneration) statusPending = false;
  }
}

function empty(id, text, state = 'idle') {
  const p = document.createElement('p');
  p.className = 'empty';
  p.dataset.state = state;
  p.textContent = text;
  $(id).replaceChildren(p);
}

async function refreshHistory() {
  const generation = ++historyGeneration;
  try {
    const items = await requestJSON('/history');
    if (generation !== historyGeneration) return;
    const key = JSON.stringify(items);
    if (key === historyKey) return;
    historyKey = key;
    if (!items.length) { empty('history', '暂无任务记录。'); return; }
    const list = document.createElement('ul');
    for (const item of items.slice().reverse()) {
      const row = document.createElement('li');
      const time = document.createElement('time'); time.textContent = item.time;
      const command = document.createElement('code'); command.textContent = item.cmd;
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.dataset.state = item.code === 0 ? 'ok' : 'error';
      badge.textContent = item.code === 0 ? '完成' : `退出码 ${item.code}`;
      row.append(time, command, badge);
      list.append(row);
    }
    $('history').replaceChildren(list);
  } catch (error) {
    if (generation !== historyGeneration) return;
    historyKey = undefined;
    empty('history', error.message, 'error');
  }
}

function reportTaskAction(text, state) {
  message('task-message', text, state);
  const bounds = $('task-message').getBoundingClientRect();
  if (bounds.top > innerHeight || bounds.bottom < 0) toast(text, state);
}

async function performAction(path, success, updateState) {
  if (actionPending) return;
  actionPending = true;
  ++statusGeneration;
  syncTaskButtons();
  message('task-message', '正在发送操作请求…');
  try {
    const result = await requestJSON(path, {body: updateState.args || {}});
    if ('task' in updateState) taskRunning = updateState.task;
    if (typeof result.daemon_running === 'boolean') daemonRunning = result.daemon_running;
    renderStatus();
    reportTaskAction(success, 'ok');
  } catch (error) { reportTaskAction(error.message, 'error'); }
  finally {
    actionPending = false;
    syncTaskButtons();
    refreshStatus(true);
    refreshHistory();
  }
}

function submitTask(event, inputId, script, modeLabels) {
  event.preventDefault();
  if (taskRunning || actionPending) return;
  const input = $(inputId);
  const url = input.value.trim();
  if (!url) { input.focus(); message('task-message', '请输入有效的题目或比赛链接。', 'error'); return; }
  const mode = event.submitter?.value || 'submit';
  const args = {args: mode === 'submit' ? [url] : [url, `--${mode}`]};
  performAction('/run/' + script, modeLabels[mode], {args, task: true});
}
$('single-form').addEventListener('submit', event => submitTask(event, 'url', 'oj_solver', {
  submit: '已发起单题求解。', 'no-submit': '已开始生成题解，不会提交。', 'dry-run': '已开始抓取题目，不会调用模型。',
}));
$('contest-form').addEventListener('submit', event => submitTask(event, 'contest-url', 'contest_solver', {
  submit: '已发起比赛批量求解。', 'no-submit': '已开始批量生成题解，不会提交。',
}));
$('btn-daemon-start').addEventListener('click', () => performAction('/daemon/start', '已启动比赛守护。', {}));
$('btn-daemon-stop').addEventListener('click', () => performAction('/daemon/stop', '已发送停止守护信号。', {}));
$('btn-daemon-once').addEventListener('click', () => performAction('/daemon/once', '已开始单次检查。', {task: true}));
$('btn-kill').addEventListener('click', () => {
  if (window.confirm('确定终止当前任务？已经提交的结果不会撤回。')) {
    performAction('/kill', '已发送终止信号。', {});
  }
});

// 基本表单展示有效值，高级编辑器只读取并保存持久化配置。
const basicFields = {
  'oj-root': 'oj_root', 'oj-base': 'oj_base', 'oj-user': 'username',
  'ai-url': 'ai_base_url', 'ai-model': 'ai_model', 'ai-reasoning': 'ai_reasoning_effort',
  'ai-maxtok': 'ai_max_tokens', 'code-lang': 'code_lang', 'mon-interval': 'monitor_interval',
  'mon-state': 'monitor_state_file', 'contest-page': 'contest_start_page',
  'price-rate': 'pricing_usd_to_cny', 'price-hours': 'pricing_refresh_hours',
};
const environmentFields = {...basicFields, 'ai-key': 'ai_api_key', 'oj-pass': 'password',
  detect: 'difficulty_detect_enable'};
const changedFields = new Set();
let configLoaded = false;
let configBusy = false;
let configStale = false;
let configRevision = null;
let advancedRevision = null;
let advancedDirty = false;
let advancedStale = false;

function syncConfigControls() {
  $('config-fields').disabled = !configLoaded || configBusy;
  $('cfg-save').disabled = !configLoaded || configBusy || configStale;
  for (const id of ['cfg-reload', 'advanced-load']) $(id).disabled = configBusy;
  $('advanced-save').disabled = configBusy || advancedStale || !advancedRevision;
  $('cfg-advanced-json').readOnly = configBusy || !advancedRevision;
  message('config-state', configBusy ? '正在处理…' : configStale ? '配置已更新，请重新载入'
    : changedFields.size ? '有未保存的修改' : configLoaded ? '配置已载入' : '尚未载入',
  changedFields.size || configStale ? 'warn' : 'idle');
}

function populateConfig({data: c, revision, overrides}) {
  for (const [id, key] of Object.entries(basicFields)) $(`cfg-${id}`).value = c[key] ?? '';
  for (const [id, configured] of [['ai-key', c.ai_api_key_configured], ['oj-pass', c.password_configured]]) {
    const field = $(`cfg-${id}`);
    field.value = '';
    field.placeholder = configured ? '已配置，留空保留' : '尚未配置';
  }
  $('cfg-mon-domains').value = (c.monitor_domains || []).join(', ');
  for (const [name, defaultValue] of Object.entries({solve: 16384, plan: 4096, editorial: 8192})) {
    $(`cfg-${name}-budget`).value = c.ai_task_tokens?.[name] ?? c.ai_task_tokens?.default ?? defaultValue;
  }
  $('cfg-detect').checked = Boolean(c.difficulty_detect_enable);
  $('cfg-compact').checked = Boolean(c.compact_history);
  $('cfg-price-auto').checked = c.pricing_auto_update ?? true;
  const names = new Set();
  for (const [id, key] of Object.entries(environmentFields)) {
    const field = $(`cfg-${id}`);
    const source = overrides[key];
    field.disabled = Boolean(source);
    field.title = source ? `由 ${source} 控制` : '';
    if (source) names.add(source);
  }
  message('cfg-env-overrides', names.size ? `以下变量优先于文件配置：${[...names].join('、')}` : '');
  configRevision = revision;
  configLoaded = true;
  configStale = false;
  changedFields.clear();
}

function invalidateAdvanced() {
  if (!advancedRevision) return;
  advancedStale = true;
  message('cfg-advanced-msg', '服务器配置已更新。请先复制未保存的修改，再载入安全配置并合并。', 'warn');
}

function invalidateConfig() {
  configStale = true;
  invalidateAdvanced();
  syncConfigControls();
}

async function configOperation(statusId, work) {
  if (configBusy) return;
  configBusy = true;
  syncConfigControls();
  message(statusId, '正在处理…');
  try { await work(); }
  catch (error) {
    if (error.status === 409) invalidateConfig();
    message(statusId, error.message, 'error');
  } finally {
    configBusy = false;
    syncConfigControls();
  }
}

async function loadConfig() {
  return configOperation('cfg-msg', async () => {
    populateConfig(await requestJSON('/config', {snapshot: true}));
    if (advancedRevision && advancedRevision !== configRevision) invalidateAdvanced();
    message('cfg-msg', '');
  });
}

async function loadAdvanced() {
  const {data, revision} = await requestJSON('/config?view=stored', {snapshot: true});
  $('cfg-advanced-json').value = JSON.stringify(data, null, 2);
  advancedRevision = revision;
  advancedDirty = advancedStale = false;
}

async function saveConfig(body, statusId, success, revision) {
  await requestJSON('/config', {body, revision});
  const advanced = statusId === 'cfg-advanced-msg';
  providersLoaded = false;
  changedFields.clear();
  $('cfg-ai-key').value = '';
  $('cfg-oj-pass').value = '';
  try {
    populateConfig(await requestJSON('/config', {snapshot: true}));
    if (advanced || (advancedRevision && !advancedDirty)) await loadAdvanced();
    else invalidateAdvanced();
    message(statusId, success, 'ok');
  } catch {
    invalidateConfig();
    if (advanced) advancedDirty = false;
    message(statusId, `${success} 但未能重新读取，请重新载入配置。`, 'warn');
  }
}

$('manage-models').addEventListener('click', () => navigate($('nav-models'), true));
$('config-form').addEventListener('input', event => {
  if (!event.target.closest('#config-fields') || event.target.readOnly) return;
  changedFields.add(event.target.id.slice(4));
  message('cfg-msg', '');
  syncConfigControls();
});
$('cfg-reload').addEventListener('click', () => {
  if (!changedFields.size || window.confirm('重新载入将丢弃基本配置中未保存的修改，是否继续？')) loadConfig();
});
$('config-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!configLoaded || configStale) return;
  const value = id => $(`cfg-${id}`).value.trim();
  const numbers = new Set(['ai-maxtok', 'mon-interval', 'contest-page', 'price-rate', 'price-hours']);
  const payload = {};
  for (const [id, key] of Object.entries(basicFields)) {
    if (changedFields.has(id)) payload[key] = numbers.has(id) ? Number(value(id)) : value(id);
  }
  if (changedFields.has('mon-domains')) payload.monitor_domains = value('mon-domains').split(',').map(domain => domain.trim()).filter(Boolean);
  const budgets = ['solve', 'plan', 'editorial'].filter(name => changedFields.has(`${name}-budget`));
  if (budgets.length) payload.ai_task_tokens = Object.fromEntries(budgets.map(name => [name, Number(value(`${name}-budget`))]));
  for (const [id, key] of Object.entries({detect: 'difficulty_detect_enable', compact: 'compact_history', 'price-auto': 'pricing_auto_update'})) {
    if (changedFields.has(id)) payload[key] = $(`cfg-${id}`).checked;
  }
  if (value('ai-key')) payload.ai_api_key = value('ai-key');
  if ($('cfg-oj-pass').value) payload.password = $('cfg-oj-pass').value;
  if (!Object.keys(payload).length) { message('cfg-msg', '没有需要保存的修改。'); return; }
  configOperation('cfg-msg', () => saveConfig(payload, 'cfg-msg', '配置已保存。', configRevision));
});

$('cfg-advanced-json').addEventListener('input', () => { advancedDirty = true; });
$('advanced-load').addEventListener('click', () => {
  if (advancedDirty && !window.confirm('载入配置将覆盖 JSON 编辑区的修改，是否继续？')) return;
  configOperation('cfg-advanced-msg', async () => {
    await loadAdvanced();
    message('cfg-advanced-msg', '已载入安全配置，密钥不会回显。', 'ok');
  });
});
$('advanced-save').addEventListener('click', () => {
  if (changedFields.size) { message('cfg-advanced-msg', '请先保存或重新载入基本配置，再保存高级配置。', 'error'); return; }
  if (advancedStale || !advancedRevision) return;
  let doc;
  try {
    doc = JSON.parse($('cfg-advanced-json').value);
    if (!doc || typeof doc !== 'object' || Array.isArray(doc)) throw new Error('必须是 JSON 对象');
  } catch (error) { message('cfg-advanced-msg', `请检查 JSON 格式：${error.message}`, 'error'); return; }
  configOperation('cfg-advanced-msg', () => saveConfig(doc, 'cfg-advanced-msg', '高级配置已保存。', advancedRevision));
});
$('cfg-diagnose').addEventListener('click', () => withButton('cfg-diagnose', async () => {
  message('cfg-diagnostics', '正在检查已保存的配置（不调用 AI）…');
  try {
    const result = await requestJSON('/config/doctor');
    message('cfg-diagnostics', [result.ok ? '检查通过' : '请修正以下问题', ...result.errors, ...result.warnings,
      ...result.models.map(model => `${model.name}：${model.provider}，输出上限 ${model.output_limit ?? '未知'}`)].join('\n'), result.ok ? 'ok' : 'error');
  } catch (error) { message('cfg-diagnostics', error.message, 'error'); }
}));
let pricingPending = false;
async function showPrices(refresh) {
  if (pricingPending) return;
  pricingPending = true;
  $('price-show').disabled = $('price-refresh').disabled = true;
  message('cfg-prices', refresh ? '正在刷新公共价格目录…' : '正在读取价格缓存…');
  try {
    const data = await requestJSON(refresh ? '/pricing/refresh' : '/pricing', {method: refresh ? 'POST' : 'GET'});
    message('cfg-prices', [`目录：${data.count} 项${data.stale ? '（无缓存或已过期）' : ''}`, data.error,
      ...data.models.map(model => model.known
        ? `${model.name}：¥${model.pricing.input ?? 0} 输入 / ¥${model.pricing.output ?? 0} 输出（每百万 Token，${model.source === 'manual' ? '手工' : '自动'}）`
        : `${model.name}：${model.reason}`)].filter(Boolean).join('\n'), data.error ? 'error' : 'idle');
  } catch (error) { message('cfg-prices', error.message, 'error'); }
  finally {
    pricingPending = false;
    $('price-show').disabled = $('price-refresh').disabled = false;
  }
}
$('price-show').addEventListener('click', () => showPrices(false));
$('price-refresh').addEventListener('click', () => showPrices(true));

// 用量各区块独立加载：切换范围时只应用最新的响应。
const number = new Intl.NumberFormat('zh-CN');
const compact = new Intl.NumberFormat('en', {notation: 'compact', maximumFractionDigits: 2});
const fmtNum = n => compact.format(Number(n || 0));
const fmtCost = n => '¥' + Number(n || 0).toFixed(4);
const fmtPct = n => (Number(n || 0) * 100).toFixed(1) + '%';
const fixed = (n, digits = 1) => Number(n || 0).toFixed(digits);
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function fmtDuration(seconds) {
  const time = Math.max(0, Math.floor(seconds));
  const parts = [[Math.floor(time / 86400), ' 天'], [Math.floor(time % 86400 / 3600), ' 小时'], [Math.floor(time % 3600 / 60), ' 分钟']];
  return parts.filter(([value]) => value).map(([value, unit]) => value + unit).join(' ') || `${time} 秒`;
}

function dataTable(heading, rows, firstNumeric) {
  const table = node('table', null, 'data-table');
  const header = table.createTHead().insertRow();
  heading.forEach((label, i) => {
    const th = node('th', label, i >= firstNumeric ? 'num' : undefined);
    th.scope = 'col';
    header.append(th);
  });
  const body = table.createTBody();
  for (const values of rows) {
    const row = body.insertRow();
    values.forEach((value, i) => {
      const cell = row.insertCell();
      if (i >= firstNumeric) cell.className = 'num';
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value ?? '—';
    });
  }
  return table;
}

function seriesDetails(data, label) {
  const details = node('details', null, 'data-details');
  const wrap = node('div', null, 'table-wrap');
  wrap.tabIndex = 0;
  wrap.setAttribute('role', 'region');
  wrap.setAttribute('aria-label', label);
  wrap.append(dataTable(['日期', '请求数', 'Token', '预估费用'], data.slice().reverse().map(day => [day.day,
    number.format(day.requests), number.format(day.total_tokens), fmtCost(day.cost)]), 1));
  details.append(node('summary', '查看逐日数据'), wrap);
  return details;
}

function renderUsageTiles({summary: s, today: t}) {
  const items = [
    ['请求数', fmtNum(s.requests), `今日 ${fmtNum(t.requests)}`],
    ['输入 Token', fmtNum(s.input_tokens), `今日 ${fmtNum(t.input_tokens)}`],
    ['输出 Token', fmtNum(s.output_tokens), `今日 ${fmtNum(t.output_tokens)}`],
    ['缓存命中率', fmtPct(s.cache_rate), `缓存 ${fmtNum(s.cache_hit_tokens)} Token`],
    ['预估费用', fmtCost(s.cost), `今日 ${fmtCost(t.cost)}`],
    ['平均输出速度', `${fixed(s.avg_output_speed)} tok/s`, `平均延迟 ${fixed(s.avg_latency_s)} 秒`],
  ];
  $('usage-tiles').replaceChildren(...items.map(([label, value, hint]) => {
    const tile = node('div', null, 'tile');
    tile.append(node('div', label, 'k'), node('div', value, 'v'), node('div', hint, 's'));
    return tile;
  }));
  const unpriced = s.requests - s.priced_requests;
  const notes = [];
  if (unpriced > 0) notes.push(`${unpriced} 次请求缺少定价，预估费用可能偏低。`);
  if (s.failed_requests) notes.push(`${s.failed_requests} 次请求失败（计入请求数）。`);
  message('usage-note', notes.join(' '), unpriced > 0 ? 'warn' : 'idle');
}

function renderUsageChart(series) {
  const max = Math.max(0, ...series.map(day => day.total_tokens));
  if (!max) { empty('usage-chart', '所选时间范围内没有 Token 用量。'); return; }
  const chart = node('div', null, 'chart');
  chart.setAttribute('aria-hidden', 'true');
  for (const day of series) {
    const slot = node('div', null, 'bar-slot');
    slot.title = `${day.day} · ${number.format(day.total_tokens)} Token · ${fmtCost(day.cost)}`;
    const bar = node('div', null, 'bar');
    bar.style.height = `${day.total_tokens / max * 100}%`;
    slot.append(bar);
    chart.append(slot);
  }
  const axis = node('div', null, 'chart-axis');
  axis.append(node('span', series[0].day), node('span', `单日最高 ${fmtNum(max)} Token`), node('span', series.at(-1).day));
  $('usage-chart').replaceChildren(chart, axis, seriesDetails(series, '每日 Token 用量表'));
}

function renderCalendar(series) {
  const max = Math.max(0, ...series.map(day => day.requests));
  if (!max) { empty('usage-calendar', '最近 365 天内没有请求记录。'); return; }
  const grid = node('div', null, 'heatmap');
  grid.tabIndex = 0;
  grid.setAttribute('aria-label', '年度请求热力图，可横向滚动；下方可查看逐日数据。');
  grid.setAttribute('role', 'region');
  const offset = new Date(series[0].day + 'T00:00:00').getDay();
  for (let i = 0; i < offset; i++) grid.append(node('span'));
  for (const day of series) {
    const level = day.requests ? Math.ceil(day.requests / max * 4) : 0;
    const cell = node('span', null, `heatmap-cell l${level}`);
    cell.title = `${day.day} · ${number.format(day.requests)} 次请求 · ${number.format(day.total_tokens)} Token`;
    cell.setAttribute('aria-hidden', 'true');
    grid.append(cell);
  }
  const legend = node('div', null, 'heatmap-legend');
  legend.setAttribute('aria-label', `色阶表示请求量；单日最多 ${number.format(max)} 次。`);
  legend.append(node('span', '0 次'));
  for (let level = 0; level <= 4; level++) legend.append(node('i', null, `heatmap-cell l${level}`));
  legend.append(node('span', `${number.format(max)} 次`));
  $('usage-calendar').replaceChildren(grid, legend, seriesDetails(series, '每日请求数表'));
}

function renderUsageBreakdown(rows, by) {
  if (!rows.length) { empty('usage-models', '所选时间范围内没有用量记录。'); return; }
  const label = {model: '模型', provider: '供应商', task: '任务'}[by];
  $('usage-models').replaceChildren(dataTable([label, '请求', '输入', '输出', '缓存命中', '费用', 'tok/s'],
    rows.map(r => [r.key || '未知', fmtNum(r.requests), fmtNum(r.input_tokens), fmtNum(r.output_tokens),
      fmtPct(r.cache_rate), fmtCost(r.cost), fixed(r.avg_output_speed)]), 1));
}

function renderUsageRecent(rows) {
  if (!rows.length) { empty('usage-recent', '所选时间范围内没有请求记录。'); return; }
  $('usage-recent').replaceChildren(dataTable(['时间', '模型', '任务', '题目', '状态', '输入', '输出', '缓存', '费用', 'tok/s'],
    rows.map(r => {
      const status = node('span', r.ok ? '成功' : '失败', 'badge');
      status.dataset.state = r.ok ? 'ok' : 'error';
      return [r.time, r.model, r.task, r.problem_id || '—', status, fmtNum(r.input_tokens), fmtNum(r.output_tokens),
        fmtNum(r.cache_hit_tokens), r.cost_known ? fmtCost(r.cost) : '未知', fixed(r.output_speed)];
    }), 5));
}

function progress(value, label, state) {
  const bar = node('progress', null, 'progress');
  bar.max = 100;
  bar.value = Math.max(0, Math.min(100, value));
  bar.dataset.state = state;
  bar.setAttribute('aria-label', label);
  return bar;
}

function renderQuota(data) {
  if (data.error) { empty('quota-list', data.error, 'error'); return; }
  if (!data.items?.length) { empty('quota-list', '未配置额度。可在「配置 → 高级配置（JSON）」中设置 quotas。'); return; }
  const entries = data.items.map(item => {
    const card = node('div', null, 'quota');
    const head = node('div', null, 'top');
    const statuses = {low: ['额度较低', 'warn'], error: ['查询失败', 'error'], stale: ['数据已过期', 'warn']};
    const [status, state] = statuses[item.status] || [];
    head.append(node('span', item.id, 'name'));
    if (status) {
      const badge = node('span', status, 'badge');
      badge.dataset.state = state;
      head.append(badge);
    }
    let value, meta, percent;
    if (item.kind === 'plan' || item.unit === 'percent') {
      value = item.remaining == null ? '不可用' : `剩余 ${fixed(item.remaining)}%`;
      meta = item.plan_name || '套餐';
    } else if (item.kind === 'balance' || item.unit === 'currency') {
      value = item.remaining == null ? '不可用' : `${item.currency || 'CNY'} ${fixed(item.remaining, 4)}`;
      meta = item.limit ? `总额 ${fixed(item.limit, 2)}` : '余额';
      if (item.limit && item.remaining != null) percent = item.remaining / item.limit * 100;
    } else {
      value = `${item.used ?? '—'} / ${item.limit ?? '—'} ${item.unit || ''}`;
      meta = item.ratio == null ? '剩余额度未知' : `剩余 ${fmtPct(item.ratio)}`;
      if (item.ratio != null) percent = item.ratio * 100;
    }
    head.append(node('strong', value, 'val'));
    const reset = item.reset_in_s == null ? '' : `重置于 ${fmtDuration(item.reset_in_s)}后`;
    card.append(head, node('p', [meta, reset, item.source, item.error].filter(Boolean).join(' · '), 'meta'));
    if (percent != null) card.append(progress(percent, `${item.id} 剩余额度`, state || 'ok'));
    for (const tier of item.tiers || []) {
      const name = {five_hour: '5 小时', weekly: '每周', monthly: '每月', credits: '额度'}[tier.name] || tier.name;
      const section = node('div', null, 'tier');
      const row = node('div', null, 'row');
      const tierLow = tier.status === 'low';
      const remaining = tier.remaining_percent;
      row.append(node('span', name), node('span', (remaining == null ? '剩余未知' : `剩余 ${fixed(remaining)}%`) + (tierLow ? ' · 额度较低' : ''), 'pct'));
      section.append(row);
      if (remaining != null) section.append(progress(remaining, `${item.id} ${name} 剩余额度`, tierLow ? 'warn' : 'ok'));
      if (tier.reset_in_s != null) section.append(node('p', `重置于 ${fmtDuration(tier.reset_in_s)}后`, 'small'));
      card.append(section);
    }
    return card;
  });
  $('quota-list').replaceChildren(...entries);
}

const usageRequests = new Map();
let usageLoadedAt = 0;
let usageGeneration = 0;

async function loadUsagePart(key, targets, path, render, options = {}) {
  usageRequests.get(key)?.abort();
  const controller = new AbortController();
  usageRequests.set(key, controller);
  const ids = Array.isArray(targets) ? targets : [targets];
  ids.forEach(id => { empty(id, '正在读取…'); $(id).setAttribute('aria-busy', 'true'); });
  try {
    const data = await requestJSON(path, {...options, signal: controller.signal});
    if (controller.signal.aborted) return true;
    render(data);
    return !data.error;
  } catch (error) {
    if (controller.signal.aborted) return true;
    ids.forEach(id => empty(id, error.message, 'error'));
    return false;
  } finally {
    if (usageRequests.get(key) === controller) {
      usageRequests.delete(key);
      ids.forEach(id => $(id).removeAttribute('aria-busy'));
    }
  }
}

function loadBreakdown() {
  const by = $('usage-breakdown-by').value;
  const days = $('usage-days').value;
  return loadUsagePart('breakdown', 'usage-models', `/api/usage/breakdown?by=${by}&days=${days}`,
    data => renderUsageBreakdown(data.rows, by));
}

async function refreshUsage(all = true) {
  const generation = ++usageGeneration;
  const days = Number($('usage-days').value);
  message('usage-status', '正在读取用量…');
  message('usage-note', '');
  const requests = [
    loadUsagePart('summary', 'usage-tiles', `/api/usage/summary?days=${days}`, renderUsageTiles),
    loadUsagePart('series', 'usage-chart', `/api/usage/series?days=${days}`, data => renderUsageChart(data.series)),
    loadBreakdown(),
    loadUsagePart('recent', 'usage-recent', `/api/usage/recent?limit=30&days=${days}`, data => renderUsageRecent(data.requests)),
  ];
  if (all) {
    requests.push(loadUsagePart('calendar', 'usage-calendar', '/api/usage/series?days=365', data => renderCalendar(data.series)));
    if (!$('quota-refresh').disabled) requests.push(loadUsagePart('quota', 'quota-list', '/api/quota', renderQuota));
  }
  const results = await Promise.all(requests);
  if (generation !== usageGeneration) return;
  usageLoadedAt = Date.now();
  message('usage-status', results.every(Boolean) ? '' : '部分数据加载失败，请查看对应区块或刷新重试。', 'error');
}
$('usage-days').addEventListener('change', () => refreshUsage(false));
$('usage-refresh').addEventListener('click', () => withButton('usage-refresh', () => refreshUsage()));
$('usage-breakdown-by').addEventListener('change', loadBreakdown);
$('quota-refresh').addEventListener('click', () => withButton('quota-refresh', () =>
  loadUsagePart('quota', 'quota-list', '/api/quota/refresh', renderQuota, {method: 'POST'})));

// ── 模型供应商（cc-switch 风格）──
const providerState = {
  presets: [], categories: [], configured: [], roles: [], active: null,
  search: '', category: 'all', savedConnection: null, formId: 0,
};
let providersLoaded = false;
let providerBusy = false;

function providerInitial(name) {
  return String(name || '?').trim().slice(0, 1).toUpperCase();
}
function providerCategoryName(id) {
  return (providerState.categories.find(item => item.id === id) || {}).name || id;
}

async function loadProviders(force = false) {
  if (providersLoaded && !force) return;
  message('provider-current', '正在读取模型配置…');
  try {
    const data = await requestJSON('/api/providers');
    providerState.presets = data.presets || [];
    providerState.categories = data.categories || [];
    providerState.configured = data.configured || [];
    providerState.roles = data.roles || [];
    providerState.active = data.active || {};
    providersLoaded = true;
    renderProviderCategories();
    renderProviders();
  } catch (error) {
    message('provider-current', '加载失败：' + error.message, 'error');
  }
}

function renderProviderCategories() {
  const select = $('provider-category');
  const current = select.value || providerState.category || 'all';
  select.replaceChildren();
  const all = document.createElement('option');
  all.value = 'all';
  all.textContent = '全部供应商';
  select.appendChild(all);
  for (const category of providerState.categories) {
    const option = document.createElement('option');
    option.value = category.id;
    option.textContent = category.name;
    select.appendChild(option);
  }
  select.value = [...select.options].some(option => option.value === current) ? current : 'all';
  providerState.category = select.value;
}

function renderProviders() {
  const active = providerState.active || {};
  const current = $('provider-current');
  current.hidden = false;
  current.textContent = active.model ? `当前：${active.model}` : '未设置当前模型';
  const tiers = active.tiers || {};
  $('provider-active-tiers').textContent = Object.keys(tiers).length
    ? '路由：' + Object.entries(tiers).map(([key, value]) => `${key}=${value}`).join(' · ')
    : '尚未设置分层路由';

  const list = $('configured-models');
  list.replaceChildren();
  if (!providerState.configured.length) {
    const empty = document.createElement('p');
    empty.className = 'small';
    empty.textContent = '尚未接入模型。从右侧预置供应商开始添加。';
    list.appendChild(empty);
  }
  for (const item of providerState.configured) {
    const card = document.createElement('div');
    card.className = 'provider-card' + (item.current ? ' active' : '');
    const head = document.createElement('div');
    head.className = 'provider-card-head';
    const logo = document.createElement('span');
    logo.className = 'provider-logo';
    logo.textContent = providerInitial(item.name);
    const name = document.createElement('strong');
    name.textContent = item.name;
    head.append(logo, name);
    if (item.current) {
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.dataset.state = 'ok';
      badge.textContent = '当前';
      head.appendChild(badge);
    }
    card.appendChild(head);
    for (const text of [
      `模型：${item.model_id || item.name}`,
      `地址：${item.base_url || '未设置'}`,
      `${item.api_key_configured ? 'API Key：已配置' : 'API Key：缺失'}${item.proxy_configured ? ' · 代理：已配置' : ''}${item.tiers?.length ? ' · 路由：' + item.tiers.join('/') : ''}`,
    ]) {
      const line = document.createElement('div');
      line.className = 'provider-meta';
      line.textContent = text;
      card.appendChild(line);
    }
    const actions = document.createElement('div');
    actions.className = 'provider-actions';
    if (!item.current) {
      const switchButton = document.createElement('button');
      switchButton.type = 'button';
      switchButton.className = 'btn btn-primary';
      switchButton.textContent = '切换';
      switchButton.addEventListener('click', () => switchProvider(item.name, true));
      const defaultButton = document.createElement('button');
      defaultButton.type = 'button';
      defaultButton.className = 'btn btn-quiet';
      defaultButton.textContent = '仅设为默认';
      defaultButton.addEventListener('click', () => switchProvider(item.name, false));
      actions.append(switchButton, defaultButton);
    } else {
      const currentButton = document.createElement('button');
      currentButton.type = 'button';
      currentButton.className = 'btn btn-quiet';
      currentButton.disabled = true;
      currentButton.textContent = '当前使用中';
      actions.appendChild(currentButton);
    }
    const deleteButton = document.createElement('button');
    deleteButton.type = 'button';
    deleteButton.className = 'btn btn-danger';
    deleteButton.textContent = '删除';
    const inUse = (item.roles_using || []).filter(Boolean);
    deleteButton.disabled = item.current || inUse.length > 0;
    deleteButton.title = item.current
      ? '当前默认模型不能删除，请先切换'
      : inUse.length ? '正在被用途使用: ' + inUse.join('、') : '删除该模型连接';
    deleteButton.addEventListener('click', () => deleteProvider(item.name));
    actions.appendChild(deleteButton);
    if (item.managed) {
      const proxyButton = node('button', item.proxy_configured ? '修改代理' : '设置代理', 'btn btn-quiet');
      proxyButton.addEventListener('click', () => setProviderProxy(item.name));
      actions.append(proxyButton);
    }
    const discoverButton = node('button', '查询模型', 'btn btn-quiet');
    discoverButton.addEventListener('click', () => openPresetForm({
      id: item.name, name: item.name, base_url: item.base_url, model: item.model_id, provider: item.provider,
      note: '使用已保存的连接查询。添加其他模型 ID 时，请填写新的配置名称及密钥环境变量。',
    }, item));
    actions.append(discoverButton);
    card.appendChild(actions);
    list.appendChild(card);
  }
  renderRoles();
  renderPresets();
}

function renderRoles() {
  const list = $('model-roles');
  list.replaceChildren();
  const names = providerState.configured.map(item => item.name);
  for (const role of providerState.roles) {
    const row = document.createElement('div');
    row.className = 'role-row';
    const label = document.createElement('div');
    label.className = 'role-label';
    const title = document.createElement('span');
    title.textContent = role.name;
    const hint = document.createElement('span');
    hint.className = 'role-hint';
    hint.textContent = role.hint || '';
    label.append(title, hint);
    const select = document.createElement('select');
    select.setAttribute('aria-label', role.name);
    if (role.optional) {
      const empty = document.createElement('option');
      empty.value = '';
      empty.textContent = role.id === 'editorial' ? '跟随求解模型'
        : role.id === 'vision' ? '自动选择'
        : role.id === 'difficulty' ? '沿用路由首档'
        : '跟随默认模型';
      select.appendChild(empty);
    }
    const values = [...names];
    if (role.value && !values.includes(role.value)) values.unshift(role.value);
    for (const name of values) {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    }
    select.value = role.value || '';
    if (!role.optional && !select.value && values.length) select.value = values[0];
    select.addEventListener('change', () => switchRole(role.id, select.value));
    row.append(label, select);
    list.appendChild(row);
  }
}

async function changeProvider(path, body, success, statusId) {
  if (providerBusy) return false;
  providerBusy = true;
  try {
    await requestJSON(path, {body});
    invalidateConfig();
    await loadProviders(true);
    toast(success, 'ok');
    return true;
  } catch (error) {
    if (statusId) message(statusId, error.message, 'error');
    else toast(error.message, 'error');
    return false;
  } finally {
    providerBusy = false;
  }
}

async function switchRole(role, name) {
  const title = providerState.roles.find(item => item.id === role)?.name || role;
  if (!await changeProvider('/api/models/role', {role, name}, `已更新${title}`)) renderRoles();
}

function setProviderProxy(name) {
  const proxy = window.prompt(
    `为「${name}」设置代理（留空表示直连）\n支持 http://、https://、socks5://、socks5h://`, '');
  if (proxy === null) return;
  changeProvider('/api/models/proxy', {name, proxy: proxy.trim()}, proxy.trim() ? '已设置代理' : '已清除代理');
}

function deleteProvider(name) {
  if (!window.confirm(`确定删除模型「${name}」？`)) return;
  changeProvider('/api/models/delete', {name}, `已删除模型 ${name}`);
}

function providerPresetMatches(preset) {
  if (providerState.category !== 'all' && preset.category !== providerState.category) return false;
  const query = providerState.search.trim().toLowerCase();
  if (!query) return true;
  const haystack = [
    preset.id, preset.name, preset.base_url, preset.model,
    ...(preset.models || []), ...(preset.aliases || []), preset.website, preset.note,
  ].join(' ').toLowerCase();
  return haystack.includes(query);
}

function renderPresets() {
  const grid = $('provider-presets');
  grid.replaceChildren();
  const presets = providerState.presets.filter(providerPresetMatches);
  if (!presets.length) {
    const empty = document.createElement('p');
    empty.className = 'small';
    empty.textContent = '没有匹配的供应商，试试搜索 kimi、glm、openrouter，或使用“自定义 OpenAI 兼容”。';
    grid.appendChild(empty);
    return;
  }
  for (const preset of presets) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'preset-card';
    card.disabled = preset.addable === false;
    const name = document.createElement('div');
    name.className = 'name';
    const logo = document.createElement('span');
    logo.className = 'provider-logo';
    logo.textContent = providerInitial(preset.name);
    const label = document.createElement('span');
    label.textContent = preset.name;
    name.append(logo, label);
    const category = document.createElement('div');
    category.className = 'small';
    category.textContent = providerCategoryName(preset.category);
    const meta = document.createElement('div');
    meta.className = 'provider-meta';
    meta.textContent = preset.base_url || '需要填写 Base URL';
    const model = document.createElement('div');
    model.className = 'provider-meta';
    model.textContent = preset.model ? `示例模型：${preset.model}` : preset.note;
    const state = document.createElement('div');
    state.className = preset.addable === false ? 'preset-disabled' : 'preset-addable';
    state.textContent = preset.addable === false ? '账号登录方式，本项目不支持' : '点击添加';
    card.append(name, category, meta, model, state);
    if (preset.addable !== false) {
      card.addEventListener('click', () => openPresetForm(preset));
    }
    grid.appendChild(card);
  }
}

function openPresetForm(preset, saved = null) {
  providerState.savedConnection = saved;
  providerState.formId++;
  const name = saved ? '' : preset.id;
  $('preset-name').value = name;
  $('preset-model').value = preset.model || (preset.models || [])[0] || '';
  $('preset-base').value = preset.base_url || '';
  $('preset-key-env').value = preset.api_key_env || '';
  $('preset-provider').value = [...$('preset-provider').options]
    .some(option => option.value === preset.provider) ? preset.provider : 'compatible';
  $('preset-pricing').value = preset.pricing_provider || '';
  $('preset-set-default').checked = !saved;
  $('provider-form-title').textContent = saved ? `查询 ${saved.name} 或添加新连接` : `添加 ${preset.name}`;
  $('provider-form-note').textContent = preset.note || '模型 ID 为示例值，可按服务商控制台修改。';
  $('preset-api-key').value = '';
  $('preset-proxy').value = preset.proxy || '';
  $('preset-model-picker-wrap').hidden = true;
  $('preset-model-picker').replaceChildren();
  const duplicate = providerState.configured.some(item => item.managed && item.name === name);
  message('provider-msg', duplicate ? `名称「${name}」已存在，请修改配置名称或切换已有连接。` : '', 'error');
  $('provider-form').hidden = false;
  $('provider-form').scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

function closeProviderForm() {
  providerState.formId++;
  $('provider-form').hidden = true;
  $('preset-api-key').value = '';
  message('provider-msg', '');
}

const presetValue = field => $(`preset-${field}`).value.trim();

function connectionDraft() {
  return {base_url: presetValue('base'), api_key_env: presetValue('key-env'),
    api_key_value: presetValue('api-key'), provider: presetValue('provider'), proxy: presetValue('proxy')};
}

async function discoverPresetModels() {
  return withButton('preset-discover', async () => {
    const draft = connectionDraft();
    if (!draft.base_url) { message('provider-msg', '请先填写 Base URL。', 'error'); return; }
    const formId = providerState.formId;
    const saved = providerState.savedConnection;
    const body = saved && draft.base_url === saved.base_url && !draft.api_key_value && !draft.api_key_env && !draft.proxy
      ? {model: saved.name} : draft;
    message('provider-msg', '正在获取模型列表…');
    try {
      const data = await requestJSON('/models/discover', {body});
      if (formId !== providerState.formId) return;
      if (Object.keys(draft).some(key => draft[key] !== connectionDraft()[key])) {
        message('provider-msg', '连接参数已修改，请重新获取模型列表。', 'warn');
        return;
      }
      const models = (data.models || []).map(item => item.id).filter(Boolean);
      if (!models.length) throw new Error('服务返回了空模型列表');
      const select = $('preset-model-picker');
      select.replaceChildren(...models.map(id => new Option(id, id)));
      select.value = models.includes(presetValue('model')) ? presetValue('model') : models[0];
      $('preset-base').value = data.base_url;
      $('preset-model').value = select.value;
      $('preset-model-picker-wrap').hidden = false;
      message('provider-msg', `获取到 ${models.length} 个模型；列表不代表全部支持 Chat Completions。`, 'ok');
    } catch (error) {
      if (formId === providerState.formId) message('provider-msg', error.message, 'error');
    }
  });
}

async function addProvider(event) {
  event.preventDefault();
  const body = {...connectionDraft(), name: presetValue('name'), model_id: presetValue('model'),
    pricing_provider: presetValue('pricing'), set_default: $('preset-set-default').checked};
  if (providerState.configured.some(item => item.managed && item.name === body.name)) {
    message('provider-msg', `名称「${body.name}」已存在，请换一个名称。`, 'error');
    return;
  }
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  const formId = providerState.formId;
  submit.disabled = true;
  message('provider-msg', '正在添加…');
  try {
    if (await changeProvider('/models/add', body, `已添加 ${body.name}`, 'provider-msg') && formId === providerState.formId) {
      closeProviderForm();
    }
  } finally {
    submit.disabled = false;
  }
}

function switchProvider(name, single = true) {
  changeProvider('/api/models/switch', {name, single}, `已切换到 ${name}${single ? '（单模型路由）' : ''}`);
}

$('provider-search').addEventListener('input', event => {
  providerState.search = event.target.value;
  renderPresets();
});
$('provider-category').addEventListener('change', event => {
  providerState.category = event.target.value || 'all';
  renderPresets();
});
$('provider-form').addEventListener('submit', addProvider);
$('preset-discover').addEventListener('click', discoverPresetModels);
$('provider-form-close').addEventListener('click', closeProviderForm);
$('preset-model-picker').addEventListener('change', event => { $('preset-model').value = event.target.value; });

syncConfigControls();
openPage(location.hash.slice(1));
connectStream();
refreshStatus(true);
setInterval(() => {
  if (document.hidden) return;
  refreshStatus();
  if (activePage === 'nav-solve') refreshHistory();
}, 5000);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  refreshStatus();
  if (activePage === 'nav-solve') refreshHistory();
  if (logFollow.checked) logBottom();
});
