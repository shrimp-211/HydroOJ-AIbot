import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {existsSync, mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {createServer} from 'node:http';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root = mkdtempSync(join(tmpdir(), 'hydroai-smoke-'));
const profile = join(root, 'edge');
const python = process.env.PYTHON || 'python';
const edge = process.argv[2];
const dummyKey = 'sk-offline#demo';
const requests = [];
let web, browser, cdp, mock;

async function listen(server) {
  await new Promise((done, fail) => server.once('error', fail).listen(0, '127.0.0.1', done));
  return server.address().port;
}
async function until(work, label, timeout = 15000) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    try { if (last = await work()) return last; } catch (err) { last = err; }
    await delay(100);
  }
  throw new Error(`${label}未完成：${last || ''}`);
}

try {
  mock = createServer((req, res) => {
    requests.push([req.method, req.url]);
    if (req.method === 'GET' && req.url === '/v1/models') {
      if (req.headers.authorization !== `Bearer ${dummyKey}`) { res.writeHead(401).end(); return; }
      res.setHeader('content-type', 'application/json');
      res.end(JSON.stringify({data: [{id: 'mock-fast'}, {id: 'mock-model'}]}));
    } else if (req.method === 'POST' && req.url === '/login') {
      let body = '';
      req.on('data', chunk => body += chunk);
      req.on('end', () => {
        const form = new URLSearchParams(body);
        if (form.get('uname') !== 'oj-test' || form.get('password') !== 'oj-test-pass') {
          res.writeHead(401).end('密码错误'); return;
        }
        res.writeHead(302, {Location: '/', 'Set-Cookie': 'sid=dummy; Path=/'}).end();
      });
    } else if (req.method === 'GET' && req.url === '/d/system/p/1000') {
      res.setHeader('content-type', 'application/json');
      res.end(JSON.stringify({pdoc: {pid: '1000', title: '离线冒烟题目', content: '输入一个整数，输出同一个整数。'}}));
    } else if (req.method === 'GET' && ['/', '/login'].includes(req.url)) {
      res.end('本地模拟 HydroOJ');
    } else {
      res.writeHead(404).end();
    }
  });
  const upstream = `http://127.0.0.1:${await listen(mock)}`;
  const address = createServer();
  const port = await listen(address);
  await new Promise(resolveClose => address.close(resolveClose));
  const origin = `http://127.0.0.1:${port}`;
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(OJ_|AI_|WEBUI_|PRICING_|DEEPSEEK_|ZHIPU_)/i.test(key)));
  Object.assign(env, {OJ_DATA_DIR: root, WEBUI_USERNAME: '管理员', WEBUI_PASSWORD: '浏览器测试密码',
    OJ_USERNAME: 'oj-test', OJ_PASSWORD: 'oj-test-pass', WEBUI_AUTOSTART_DAEMON: '0',
    NO_PROXY: '127.0.0.1,localhost', PYTHONIOENCODING: 'utf-8'});
  const init = spawnSync(python, [resolve('config_cli.py'), 'init', '--model', 'seed',
    '--base-url', upstream + '/v1', '--oj-root', upstream, '--profile', 'economy'], {env, cwd: root, encoding: 'utf8'});
  assert.equal(init.status, 0, init.stderr);
  web = spawn(python, ['-u', resolve('webui.py'), '--port', String(port)], {env, cwd: root});
  web.stdout.resume();
  web.stderr.resume();
  await until(async () => (await fetch(origin + '/status')).ok, 'Web 启动');

  browser = spawn(edge, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
    '--disable-sync', `--user-data-dir=${profile}`, '--remote-debugging-port=0', 'about:blank'], {stdio: 'ignore'});
  const devtoolsPort = await until(() => {
    const file = join(profile, 'DevToolsActivePort');
    return existsSync(file) && readFileSync(file, 'utf8').split(/\r?\n/)[0];
  }, 'Edge 调试端口');
  const tabs = await (await fetch(`http://127.0.0.1:${devtoolsPort}/json/list`)).json();
  cdp = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
  await new Promise((done, fail) => { cdp.addEventListener('open', done, {once: true}); cdp.addEventListener('error', fail, {once: true}); });
  let id = 0;
  const pending = new Map();
  const pageErrors = [];
  cdp.addEventListener('message', event => {
    const payload = JSON.parse(event.data);
    if (payload.method === 'Runtime.exceptionThrown') pageErrors.push(payload.params.exceptionDetails);
    if (pending.has(payload.id)) {
      const {done, fail} = pending.get(payload.id);
      pending.delete(payload.id);
      if (payload.error) fail(payload.error); else done(payload.result);
    }
  });
  const cmd = (method, params = {}) => new Promise((done, fail) => {
    pending.set(++id, {done, fail}); cdp.send(JSON.stringify({id, method, params}));
  });
  await cmd('Runtime.enable');
  const evaluate = async expression => {
    const data = await cmd('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (data.exceptionDetails) throw new Error(data.exceptionDetails.exception?.description || data.exceptionDetails.text);
    return data.result.value;
  };
  const click = selector => evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
  const value = (selector, val) => evaluate(`(() => {const el = document.querySelector(${JSON.stringify(selector)}); el.value = ${JSON.stringify(val)}; el.dispatchEvent(new Event('input', {bubbles: true}));})()`);
  const wait = (expr, label) => until(() => evaluate(expr), label);

  await cmd('Page.navigate', {url: origin + '/'});
  await wait("location.pathname === '/login' && document.querySelector('#username') !== null", '登录页');
  await value('#username', env.WEBUI_USERNAME);
  await value('#password', env.WEBUI_PASSWORD);
  await click('button[type=submit]');
  await wait("document.querySelector('#stream-status')?.textContent.includes('已连接')", '登录与 SSE 握手');
  await click('#nav-config');
  await wait("document.querySelector('#cfg-ai-model').value === 'seed'", '加载基本配置');
  assert(await evaluate("document.querySelector('#cfg-oj-pass').disabled"));
  await value('#cfg-contest-page', '4');
  await click('#cfg-save');
  await wait("document.querySelector('#cfg-msg').textContent.includes('配置已保存')", '保存基本配置');
  await click('details.disclosure:last-of-type summary');
  await click('#advanced-load');
  await wait("!document.querySelector('#cfg-advanced-json').readOnly", '载入高级配置');
  assert(!await evaluate("document.querySelector('#cfg-advanced-json').value.includes('oj-test-pass')"));
  await value('#cfg-advanced-json', '{"contest_start_page":7}');
  await click('#manage-models');
  await wait("document.querySelectorAll('.preset-card').length > 0", '模型供应商加载');
  await value('#provider-search', '自定义 OpenAI');
  await click('.preset-card');
  await value('#preset-name', 'offline');
  await value('#preset-base', upstream + '/v1');
  await value('#preset-key-env', 'SMOKE_PROVIDER_KEY');
  await value('#preset-api-key', dummyKey);
  await click('#preset-discover');
  await wait("document.querySelectorAll('#preset-model-picker option').length === 2", '发现本地模拟模型');
  await click('#provider-form button[type=submit]');
  await wait("document.querySelector('#provider-current').textContent === '当前：offline'", '新增并切换默认模型');
  assert.equal(JSON.parse(readFileSync(join(root, 'config.json'), 'utf8')).models.offline.api_key, 'env:SMOKE_PROVIDER_KEY');
  assert(!readFileSync(join(root, 'config.json'), 'utf8').includes(dummyKey));
  await click('#nav-config');
  await wait("document.querySelector('#cfg-ai-model').value === 'offline'", '配置页同步供应商');
  assert(await evaluate("document.querySelector('#advanced-save').disabled && document.querySelector('#cfg-advanced-json').value === '{\"contest_start_page\":7}'"));
  await click('#nav-usage');
  await wait("!document.querySelector('#usage-tiles').textContent.includes('正在读取')", '用量页面加载');
  await click('#nav-solve');
  await value('#url', '1000');
  await click('button[value="dry-run"]');
  await wait("document.querySelector('#console-lines').textContent.includes('离线冒烟题目')", '运行真实求解入口并读取 SSE 输出');
  await wait("document.querySelector('#console-lines').textContent.includes('进程结束 (code 0)')", '子进程正常退出');
  assert(requests.some(([method, path]) => method === 'POST' && path === '/login'));
  assert(requests.every(([method, path]) => method !== 'POST' || path === '/login'));
  assert.equal(JSON.parse(readFileSync(join(root, 'config.json'), 'utf8')).contest_start_page, 4);
  await click('form[action="/logout"] button[type=submit]');
  await wait("location.pathname === '/login' && document.querySelector('#username') !== null", '退出后返回登录页');
  assert.deepEqual(pageErrors, []);
  console.log('浏览器冒烟通过：登录与退出、基础配置、高级草稿保护、发现/添加/切换模型、用量、只读求解和 SSE。');
} finally {
  cdp?.close();
  for (const child of [browser, web]) {
    if (child?.pid && child.exitCode === null) {
      if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F']);
      else child.kill();
    }
  }
  mock?.closeAllConnections();
  mock?.close();
  rmSync(root, {recursive: true, force: true, maxRetries: 3, retryDelay: 100});
}
