#!/usr/bin/env node
/**
 * discover-chatgpt-routes.js — 从 chatgpt.com 前端 bundle 提取真实 backend-api 路由
 *
 * 为什么需要:猜路径是无效的(本 session 猜了 30+ 个全部 404)。
 * 真实路由写在前端 JS 里,爬下来 grep 一次就有了 —— 包括
 * aip/connectors/mcp 这种关键入口,以及 feature flag 的枚举名。
 *
 * 用法:
 *   node discover-chatgpt-routes.js --session sess.json
 *   node discover-chatgpt-routes.js --session sess.json --grep connector
 *   node discover-chatgpt-routes.js --session sess.json --enum DeveloperMode
 *
 * 缓存在 --cache 目录(默认 /tmp/cgpt-bundles),二次运行不重复下载。
 */

'use strict';

const fs = require('fs');
const path = require('path');

const BASE = 'https://chatgpt.com';

function log(s) { process.stdout.write(s + '\n'); }
function die(s) { log('错误: ' + s); process.exit(2); }

function parseArgs() {
  const a = process.argv.slice(2);
  const o = { cache: '/tmp/cgpt-bundles', rounds: '2' };
  for (let i = 0; i < a.length; i++) {
    if (a[i].startsWith('--')) o[a[i].slice(2)] = a[++i];
  }
  return o;
}

async function fetchHome(sessionHeaders) {
  const h = Object.assign({}, sessionHeaders);
  h['accept'] = '*/*';
  // 首页不要带路由头 —— 那是 backend-api 专用的
  delete h['x-openai-target-path'];
  delete h['x-openai-target-route'];
  delete h['content-type'];

  const r = await fetch(BASE + '/', { headers: h });
  const html = await r.text();
  if (r.status !== 200) die(`首页返回 ${r.status},session 可能过期`);
  return html;
}

/**
 * 递归爬 lazy chunk。入口 bundle 只有十几个,但它们内部引用了
 * 上千个按需加载的 chunk —— connector 相关代码就在里面。
 */
async function crawl(html, cacheDir, rounds, ua) {
  fs.mkdirSync(cacheDir, { recursive: true });

  let queue = [...new Set(html.match(/\/cdn\/assets\/[a-zA-Z0-9_.-]+\.js/g) || [])];
  if (!queue.length) die('首页里没找到 /cdn/assets/*.js —— 前端结构可能变了');

  const seen = new Set();
  for (let round = 0; round < rounds; round++) {
    const next = new Set();
    let fetched = 0;

    for (const url of queue) {
      if (seen.has(url)) continue;
      seen.add(url);

      const file = path.join(cacheDir, url.split('/').pop());
      let body;
      if (fs.existsSync(file)) {
        body = fs.readFileSync(file, 'utf8');
      } else {
        try {
          const r = await fetch(BASE + url, { headers: { 'user-agent': ua } });
          if (!r.ok) continue;
          body = await r.text();
          fs.writeFileSync(file, body);
          fetched++;
        } catch (e) { continue; }
      }
      // chunk 文件名形如 name-<16位hash>.js
      for (const m of body.match(/[a-zA-Z0-9_.-]+-[a-z0-9]{16}\.js/g) || []) {
        next.add('/cdn/assets/' + m);
      }
    }
    log(`round ${round}: 已知 ${seen.size} 个,新下载 ${fetched} 个,发现 ${next.size} 个引用`);
    queue = [...next];
  }
  return cacheDir;
}

function scan(cacheDir, opts) {
  const files = fs.readdirSync(cacheDir).filter(f => f.endsWith('.js'));
  if (!files.length) die('缓存目录是空的');

  const routes = new Set();
  const enums = new Set();
  const needle = opts.grep ? new RegExp(opts.grep, 'i') : null;
  const enumName = opts.enum;

  for (const f of files) {
    const t = fs.readFileSync(path.join(cacheDir, f), 'utf8');

    for (const m of t.match(/aip\/[a-zA-Z0-9_/{}$.-]+/g) || []) routes.add(m);
    for (const m of t.match(/backend-api\/[a-zA-Z0-9_/{}$.-]+/g) || []) routes.add(m);

    if (enumName) {
      // 形如 e.DeveloperMode=`developer_mode`
      const re = new RegExp('\\.' + enumName + '\\s*=\\s*[`"\']([a-z0-9_]+)[`"\']', 'g');
      let m;
      while ((m = re.exec(t))) enums.add(m[1]);
    }
  }

  const out = [...routes].filter(r => !needle || needle.test(r)).sort();
  log(`\n=== 路由 (${out.length}${needle ? `, 过滤 /${opts.grep}/i` : ''}) ===`);
  out.forEach(r => log('  ' + r));

  if (enumName) {
    log(`\n=== 枚举 ${enumName} ===`);
    if (enums.size) enums.forEach(e => log('  ' + e));
    else log('  (未找到)');
  }
  return out.length;
}

async function main() {
  const opts = parseArgs();
  if (!opts.session) die('需要 --session <sess.json>');
  if (!fs.existsSync(opts.session)) die(`session 不存在: ${opts.session}`);

  const s = JSON.parse(fs.readFileSync(opts.session, 'utf8'));
  if (!s || typeof s !== 'object' || Array.isArray(s)) {
    die('session 顶层必须是 object(应是含 headers 的 bundle)。');
  }
  const h = (s.headers && typeof s.headers === 'object') ? s.headers : s;
  if (!h.authorization) die('session 里没有 authorization');

  log('拉取首页...');
  const html = await fetchHome(h);
  log(`首页 ${html.length} 字节`);

  log('爬 bundle(首次较慢,之后走缓存)...');
  // 夹到 [1,4]:负数/NaN 会让爬取一轮不跑然后在空缓存上 die,
  // 过大只是白下载(第 3 轮之后基本没有新 chunk)。
  const rounds = Math.min(4, Math.max(1, parseInt(opts.rounds, 10) || 2));
  await crawl(html, opts.cache, rounds, h['user-agent'] || 'Mozilla/5.0');

  const n = scan(opts.cache, opts);
  return n > 0 ? 0 : 1;
}

main()
  .then(c => process.exit(c))
  .catch(e => { log('未捕获异常: ' + (e && e.stack || e)); process.exit(3); });
