#!/usr/bin/env node
/**
 * Company-wide LiteLLM daily actual spend report.
 *
 * For a completed LiteLLM 1d window, read `/spend/logs` once per backend. The
 * endpoint returns a server-side daily `users` map, so this stays deterministic
 * without the old slow per-key `/global/spend/logs` fan-out. This keeps the
 * Buyitian fix policy: raw actual LiteLLM spend, no internal adjustments,
 * grouped by person across all owned aliases.
 */
const fs = require('fs');
const https = require('https');
const path = require('path');

const ROOT = '/data/.openclaw/workspace';
const CONFIG_PATH = '/data/.openclaw/openclaw.json';
const SNAPSHOT_DIR = path.join(ROOT, 'data/spend-snapshots');
const TARGET_CHAT_ID = 'oc_bc97dd83f5eefcea1f4c91da4a64cc79';
const HISTORY_START_DATE = process.env.LITELLM_HISTORY_START || '2026-05-20';
const SPEND_LOGS_CHUNK_DAYS = Math.max(1, Number(process.env.LITELLM_SPEND_LOGS_CHUNK_DAYS) || 1);

const LITELLM_ENVS = [
  { label: '198pro', name: '198pro / Claude Max-CC', base: 'https://cc.auto-link.com.cn/pro', envFile: `${ROOT}/.env.litellm.198pro` },
  { label: 'aliyun', name: 'aliyun / Her-Codex', base: 'https://litellm.carher.net', envFile: `${ROOT}/.env.litellm.aliyun` },
];

const CODING_APP_TOKEN = 'DlT9bsrwMad12VsogEpcK9Ptncc';
const CODING_TABLE_ID = 'tblJT2s6Y6xjYj5A';
const HER_APP_TOKEN = 'DFqqwIMsIiLUWdkTfs4c1VqLnnh';
const HER_TABLE_ID = 'tblcvJPRIFV91yHy';
const STATS_APP_TOKEN = 'MBKKbBkGcaTLOPs7KuncM7Rkn6g';
const STATS_TABLE_ID = 'tblC38oelXfulkqh';

function utcDate(offsetDays = 0) {
  const d = new Date(Date.now() + offsetDays * 86400000);
  return d.toISOString().slice(0, 10);
}

function addDays(iso, n) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function parseArgs(argv) {
  const out = { dryRun: false, json: false, date: utcDate(-1), top: 30, chat: process.env.LITELLM_REPORT_CHAT_ID || TARGET_CHAT_ID };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dry-run') out.dryRun = true;
    else if (a === '--json') out.json = true;
    else if (a === '--date') out.date = argv[++i] || out.date;
    else if (a.startsWith('--date=')) out.date = a.slice('--date='.length);
    else if (a === '--top') out.top = Number(argv[++i] || out.top);
    else if (a.startsWith('--top=')) out.top = Number(a.slice('--top='.length));
    else if (a === '--chat') out.chat = argv[++i] || out.chat;
    else if (a.startsWith('--chat=')) out.chat = a.slice('--chat='.length);
    else if (a === '-h' || a === '--help') {
      console.log('Usage: litellm-company-daily-actual-report.js [--dry-run] [--json] [--date YYYY-MM-DD] [--top N] [--chat oc_xxx]');
      process.exit(0);
    } else {
      throw new Error(`unknown arg: ${a}`);
    }
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(out.date)) throw new Error(`bad --date: ${out.date}`);
  if (!Number.isFinite(out.top) || out.top <= 0) out.top = 30;
  return out;
}

function stripJson5(text) {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
    .replace(/([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)/g, '$1"$2"$3')
    .replace(/,(\s*[}\]])/g, '$1');
}

function resolveEnv(value) {
  if (typeof value !== 'string') return value;
  return value.replace(/\$\{([A-Z0-9_]+)\}/g, (_, name) => process.env[name] || '');
}

function loadConfig() {
  const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
  let config;
  try {
    const JSON5 = require('/app/node_modules/json5');
    config = JSON5.parse(raw);
  } catch {
    config = JSON.parse(stripJson5(raw));
  }
  const feishu = config.channels?.feishu || {};
  const masterKeyByLabel = {};
  for (const env of LITELLM_ENVS) {
    const text = fs.readFileSync(env.envFile, 'utf8');
    const m = text.match(/LITELLM_MASTER_KEY=(.+)/);
    if (m) masterKeyByLabel[env.label] = m[1].trim();
  }
  return {
    appId: resolveEnv(feishu.appId),
    appSecret: process.env.FEISHU_APP_SECRET || resolveEnv(feishu.appSecret),
    masterKeyByLabel,
  };
}

function requestJson(options, body) {
  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        if (res.statusCode < 200 || res.statusCode >= 300) {
          reject(new Error(`${res.statusCode}: ${data.slice(0, 300)}`));
          return;
        }
        try { resolve(JSON.parse(data)); }
        catch { reject(new Error(`Non-JSON: ${data.slice(0, 300)}`)); }
      });
    });
    req.setTimeout(120000, () => { req.destroy(); reject(new Error('timeout')); });
    req.on('error', reject);
    if (body) req.write(typeof body === 'string' ? body : JSON.stringify(body));
    req.end();
  });
}

async function retry(fn, retries = 3, delayMs = 1000) {
  let lastErr;
  for (let i = 0; i < retries; i++) {
    try { return await fn(); }
    catch (e) {
      lastErr = e;
      if (i < retries - 1) await new Promise((r) => setTimeout(r, delayMs * (i + 1)));
    }
  }
  throw lastErr;
}

async function getTenantToken(appId, appSecret) {
  const body = JSON.stringify({ app_id: appId, app_secret: appSecret });
  const res = await requestJson({
    hostname: 'open.feishu.cn',
    path: '/open-apis/auth/v3/tenant_access_token/internal',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
  }, body);
  if (!res.tenant_access_token) throw new Error(`tenant token failed: ${JSON.stringify(res).slice(0, 200)}`);
  return res.tenant_access_token;
}

async function fetchBitableAll(tenantToken, appToken, tableId, searchBody = {}) {
  const items = [];
  let pageToken = '';
  do {
    const body = JSON.stringify(searchBody);
    const url = `/open-apis/bitable/v1/apps/${appToken}/tables/${tableId}/records/search?page_size=500&user_id_type=open_id${pageToken ? `&page_token=${pageToken}` : ''}`;
    const res = await requestJson({
      hostname: 'open.feishu.cn',
      path: url,
      method: 'POST',
      headers: { Authorization: `Bearer ${tenantToken}`, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, body);
    if (res.code !== 0) throw new Error(`bitable ${tableId}: ${res.code} ${res.msg}`);
    items.push(...(res.data?.items || []));
    pageToken = res.data?.has_more ? res.data.page_token : '';
  } while (pageToken);
  return items;
}

function textField(value) {
  if (Array.isArray(value)) return value.map((x) => x.text || x.name || '').join('').trim();
  return String(value || '').trim();
}

function userField(value) {
  if (Array.isArray(value) && value[0]) return { openId: value[0].id || '', name: value[0].name || value[0].en_name || '' };
  return { openId: '', name: '' };
}

function normalizeName(name) {
  return String(name || '').replace(/的her$/i, '').trim();
}

function baseAliasOf(alias) {
  const a = String(alias || '').trim();
  const m = a.match(/^(claude-code|cursor)-([a-z0-9]+)$/i);
  if (m && /^[a-z]{4}$/i.test(m[2])) return a;
  return a.replace(/-[a-z0-9]{4}$/i, '');
}

function extractEmailPrefix(alias) {
  const base = baseAliasOf(alias);
  if (base.startsWith('cursor-')) return base.slice(7);
  if (base.startsWith('claude-code-')) return base.slice(12);
  return '';
}

function accountType(alias) {
  const base = baseAliasOf(alias);
  if (base.startsWith('claude-code-')) return 'Claude Code';
  if (base.startsWith('cursor-')) return 'Cursor';
  if (base.startsWith('carher-')) return 'CarHer';
  return '';
}

function accountTypeShort(type) {
  if (type === 'Claude Code') return 'CC';
  if (type === 'CarHer') return 'Her';
  return type;
}

function isTrackedAlias(alias) {
  return Boolean(accountType(alias));
}

function addAliasUser(map, nameToOpenId, alias, user, source) {
  const base = baseAliasOf(alias);
  if (!base) return;
  const name = normalizeName(user.name || '');
  const openId = user.openId || '';
  if (!map[base] || (openId && !map[base].openId)) map[base] = { name, openId, source };
  if (name && openId && !nameToOpenId[name]) nameToOpenId[name] = openId;
}

async function loadUserMappings(tenantToken) {
  const aliasToUser = {};
  const prefixToUser = {};
  const nameToOpenId = {};

  const [codingRows, herRows, statsRows] = await Promise.all([
    fetchBitableAll(tenantToken, CODING_APP_TOKEN, CODING_TABLE_ID).catch((e) => { console.error(`WARN coding map: ${e.message}`); return []; }),
    fetchBitableAll(tenantToken, HER_APP_TOKEN, HER_TABLE_ID).catch((e) => { console.error(`WARN her map: ${e.message}`); return []; }),
    fetchBitableAll(tenantToken, STATS_APP_TOKEN, STATS_TABLE_ID, {
      filter: { conjunction: 'and', conditions: [{ field_name: '人员', operator: 'isNotEmpty', value: [] }] },
    }).catch((e) => { console.error(`WARN stats map: ${e.message}`); return []; }),
  ]);

  for (const row of codingRows) {
    const f = row.fields || {};
    const u = userField(f['姓名']);
    if (!u.name) u.name = textField(f['姓名原文']) || textField(f.name);
    if (!u.openId) u.openId = textField(f.open_id);
    u.name = normalizeName(u.name);
    const prefix = textField(f['邮箱前缀']);
    if (prefix && (u.name || u.openId)) prefixToUser[prefix] = { ...u, source: 'coding-prefix' };
    const alias = textField(f.key_alias || f['key_alias'] || f['API Key']);
    if (alias && (u.name || u.openId)) addAliasUser(aliasToUser, nameToOpenId, alias, u, 'coding-alias');
    if (u.name && u.openId && !nameToOpenId[u.name]) nameToOpenId[u.name] = u.openId;
  }

  for (const row of herRows) {
    const f = row.fields || {};
    const alias = textField(f['API Key']);
    const u = userField(f['姓名']);
    if (!u.name) u.name = textField(f['姓名原文']) || textField(f['Bot 名']);
    u.name = normalizeName(u.name);
    if (alias && (u.name || u.openId)) addAliasUser(aliasToUser, nameToOpenId, alias, u, 'her');
  }

  for (const row of statsRows) {
    const f = row.fields || {};
    const alias = textField(f.key_alias || f['key_alias']);
    const u = userField(f['人员']);
    u.name = normalizeName(u.name);
    if (alias && (u.name || u.openId) && !aliasToUser[baseAliasOf(alias)]) addAliasUser(aliasToUser, nameToOpenId, alias, u, 'stats');
  }

  return { aliasToUser, prefixToUser, nameToOpenId, counts: { coding: codingRows.length, her: herRows.length, stats: statsRows.length } };
}

async function fetchAllKeys(config) {
  const chunks = await Promise.all(LITELLM_ENVS.map(async (env) => {
    const masterKey = config.masterKeyByLabel[env.label];
    if (!masterKey) throw new Error(`missing master key for ${env.label}`);
    const u = new URL(`${env.base}/spend/keys`);
    const keys = await requestJson({
      hostname: u.hostname,
      path: u.pathname + u.search,
      method: 'GET',
      headers: { Authorization: `Bearer ${masterKey}` },
    });
    if (!Array.isArray(keys)) throw new Error(`${env.label} /spend/keys returned non-array`);
    return keys.map((k) => ({ ...k, env: env.label, envName: env.name, masterKey, baseUrl: env.base }));
  }));
  return chunks.flat();
}

function buildUsageKeyToAliasMap(keys) {
  const map = new Map();
  for (const k of keys) {
    const alias = baseAliasOf(k.key_alias || '');
    if (!alias || !isTrackedAlias(alias)) continue;
    if (k.user_id) map.set(`${k.env}:${k.user_id}`, alias);
    map.set(`${k.env}:${alias}`, alias);
  }
  return map;
}

async function fetchDailyRows(config, reportDate, usageKeyToAlias = new Map()) {
  const startDate = HISTORY_START_DATE <= reportDate ? HISTORY_START_DATE : reportDate;
  const endDate = addDays(reportDate, 1);
  const dailyByEnvAlias = new Map();
  const cumulativeByEnvAlias = new Map();
  let fail = 0;
  const failedEnvs = [];

  await Promise.all(LITELLM_ENVS.map(async (env) => {
    const masterKey = config.masterKeyByLabel[env.label];
    if (!masterKey) {
      fail++;
      console.error(`  daily logs ${env.label} skipped: missing master key`);
      return;
    }
    try {
      // Large /spend/logs windows can return 500 on 198pro. Keep the
      // cumulative calculation, but fetch bounded chunks so one bad wide
      // query cannot silently turn the daily report into a partial report.
      const rows = [];
      for (let chunkStart = startDate; chunkStart < endDate;) {
        // 1-day chunks: since 2026-09 a 7-day window on 198pro takes >30s and
        // the server returns 500 (/spend/logs Error); single days stay ~3-11s.
        const chunkEnd = addDays(chunkStart, SPEND_LOGS_CHUNK_DAYS);
        const u = new URL(`${env.base}/spend/logs`);
        u.searchParams.set('start_date', chunkStart);
        u.searchParams.set('end_date', chunkEnd > endDate ? endDate : chunkEnd);
        const part = await retry(() => requestJson({
          hostname: u.hostname,
          path: u.pathname + u.search,
          method: 'GET',
          headers: { Authorization: `Bearer ${masterKey}` },
        }), 3, 800);
        if (!Array.isArray(part)) throw new Error(`${env.label} /spend/logs returned non-array`);
        rows.push(...part);
        chunkStart = chunkEnd > endDate ? endDate : chunkEnd;
      }

      let reportDateSpend = 0;
      let reportDateUserSpend = 0;
      for (const row of rows) {
        const date = String(row.date || row.startTime || '').slice(0, 10);
        if (!date || date < startDate || date > reportDate) continue;
        const users = row.users || {};
        if (date === reportDate) reportDateSpend += Number(row.spend || 0);
        for (const [alias, amount] of Object.entries(users)) {
          const base = usageKeyToAlias.get(`${env.label}:${alias}`) || baseAliasOf(alias);
          if (!isTrackedAlias(base)) continue;
          const spend = Number(amount || 0);
          if (!Number.isFinite(spend) || spend === 0) continue;
          const id = `${env.label}:${base}`;
          cumulativeByEnvAlias.set(id, (cumulativeByEnvAlias.get(id) || 0) + spend);
          if (date === reportDate) {
            reportDateUserSpend += spend;
            dailyByEnvAlias.set(id, (dailyByEnvAlias.get(id) || 0) + spend);
          }
        }
      }
      if (reportDateSpend > 0 && reportDateUserSpend === 0) {
        throw new Error(`${env.label} /spend/logs has spend for ${reportDate} but no tracked users; response schema may have changed`);
      }
      console.error(`  daily logs ${env.label}: ${rows.length} day rows, ${dailyByEnvAlias.size} active env-alias rows so far`);
    } catch (e) {
      fail++;
      failedEnvs.push(env.label);
      console.error(`  daily logs ${env.label} failed: ${e.message}`);
    }
  }));

  return { dailyByEnvAlias, cumulativeByEnvAlias, fail, failedEnvs, startDate, endDate };
}

function fmtMoney(n, digits = 2) {
  return `$${Number(n || 0).toFixed(digits)}`;
}

function fmtPct(spend, budget) {
  if (!budget) return 'n/a';
  return `${((Number(spend || 0) / Number(budget || 0)) * 100).toFixed(1)}%`;
}

function fmtBjDay(utcDate) {
  const start = utcDate;
  const end = addDays(utcDate, 1);
  return `${start} 08:00 -> ${end} 08:00`;
}

function padVisual(s, width) {
  const str = String(s);
  let w = 0;
  for (const c of str) w += c.charCodeAt(0) > 127 ? 2 : 1;
  return str + ' '.repeat(Math.max(0, width - w));
}

function resolveUser(baseAlias, key, mappings) {
  const byAlias = mappings.aliasToUser[baseAlias];
  const prefix = extractEmailPrefix(baseAlias);
  const byPrefix = prefix ? mappings.prefixToUser[prefix] : null;
  const metaName = normalizeName(key.metadata?.instance || key.metadata?.user || '');
  const raw = byAlias || byPrefix || {};
  let name = normalizeName(raw.name || metaName || key.user_id || baseAlias);
  let openId = raw.openId || '';
  if (!openId && name && mappings.nameToOpenId[name]) openId = mappings.nameToOpenId[name];
  return { name, openId, source: raw.source || (metaName ? 'metadata' : 'fallback') };
}

function aggregate(keys, spendRows, mappings, reportDate) {
  const people = new Map();
  const groups = new Map();

  for (const k of keys.filter((x) => x.token && isTrackedAlias(x.key_alias || ''))) {
    const base = baseAliasOf(k.key_alias);
    const id = `${k.env}:${base}`;
    if (!groups.has(id)) {
      groups.set(id, {
        id,
        env: k.env,
        alias: base,
        type: accountType(base),
        key: k,
        budget: 0,
        tokenCount: 0,
        blockedTokenCount: 0,
      });
    }
    const g = groups.get(id);
    g.budget += Number(k.max_budget || 0);
    g.tokenCount += 1;
    if (k.blocked === true) g.blockedTokenCount += 1;
  }

  for (const [id, daily] of spendRows.dailyByEnvAlias.entries()) {
    if (!groups.has(id)) {
      const [env, alias] = id.split(':');
      groups.set(id, {
        id,
        env,
        alias,
        type: accountType(alias),
        key: { env, key_alias: alias, metadata: {}, user_id: '' },
        budget: 0,
        tokenCount: 0,
        blockedTokenCount: 0,
      });
    }
    groups.get(id).daily = Number(daily || 0);
  }

  for (const g of groups.values()) {
    g.daily = Number(g.daily || 0);
    g.cumulative = Number(spendRows.cumulativeByEnvAlias.get(g.id) || 0);
    const user = resolveUser(g.alias, g.key, mappings);
    const personKey = user.openId ? `open:${user.openId}` : `name:${user.name}`;

    if (!people.has(personKey)) {
      people.set(personKey, {
        key: personKey,
        name: user.name,
        openId: user.openId,
        daily: 0,
        cumulative: 0,
        budget: 0,
        accounts: [],
        aliases: new Set(),
        types: new Set(),
        activeAccounts: 0,
        blockedActiveAccounts: 0,
      });
    }
    const p = people.get(personKey);
    p.daily += g.daily;
    p.cumulative += g.cumulative;
    p.budget += g.budget;
    p.aliases.add(g.id);
    p.types.add(g.type);
    if (g.daily > 0) p.activeAccounts += 1;
    if (g.daily > 0 && g.blockedTokenCount > 0) p.blockedActiveAccounts += 1;
    p.accounts.push({
      env: g.env,
      alias: g.alias,
      type: g.type,
      daily: g.daily,
      cumulative: g.cumulative,
      budget: g.budget,
      blocked: g.blockedTokenCount > 0,
      tokenCount: g.tokenCount,
    });
  }

  const peopleArr = [...people.values()].map((p) => ({
    ...p,
    aliases: [...p.aliases].sort(),
    types: [...p.types].sort(),
    accounts: p.accounts.sort((a, b) => b.daily - a.daily || `${a.env}:${a.alias}`.localeCompare(`${b.env}:${b.alias}`)),
  })).sort((a, b) => b.daily - a.daily || a.name.localeCompare(b.name, 'zh-Hans-CN'));

  const activePeople = peopleArr.filter((p) => p.daily > 0);
  const byType = {};
  for (const p of peopleArr) {
    for (const a of p.accounts) {
      if (!byType[a.type]) byType[a.type] = { daily: 0, cumulative: 0, budget: 0, active: 0, accounts: 0, blockedActive: 0 };
      byType[a.type].daily += a.daily;
      byType[a.type].cumulative += a.cumulative;
      byType[a.type].budget += a.budget;
      byType[a.type].accounts += 1;
      if (a.daily > 0) byType[a.type].active += 1;
      if (a.daily > 0 && a.blocked) byType[a.type].blockedActive += 1;
    }
  }

  return {
    people: peopleArr,
    activePeople,
    byType,
    totals: {
      daily: peopleArr.reduce((s, p) => s + p.daily, 0),
      cumulative: peopleArr.reduce((s, p) => s + p.cumulative, 0),
      budget: peopleArr.reduce((s, p) => s + p.budget, 0),
      people: peopleArr.length,
      activePeople: activePeople.length,
      accounts: groups.size,
      activeAccounts: [...groups.values()].reduce((s, g) => s + (g.daily > 0 ? 1 : 0), 0),
      blockedActiveAccounts: peopleArr.reduce((s, p) => s + p.blockedActiveAccounts, 0),
    },
  };
}

function saveDashboardSnapshot(report) {
  const dailyByAlias = {};
  for (const person of report.summary.people) {
    for (const account of person.accounts) {
      if (!account.daily) continue;
      dailyByAlias[account.alias] = (dailyByAlias[account.alias] || 0) + account.daily;
    }
  }
  fs.mkdirSync(SNAPSHOT_DIR, { recursive: true });
  const file = path.join(SNAPSHOT_DIR, `logs-${report.reportDate}.json`);
  fs.writeFileSync(file, `${JSON.stringify({
    method: report.method,
    methodNote: report.methodNote,
    generatedAtUtc: report.checkedAtUtc,
    reportDate: report.reportDate,
    apiTotalSpend: report.summary.totals.daily,
    dailyByAlias,
  }, null, 2)}\n`);
  return file;
}

function generateMarkdown(report, topN) {
  const s = report.summary;
  const t = s.totals;
  let out = '';
  out += `📊 LiteLLM 全员每日实际消费报告 · 北京账期 ${fmtBjDay(report.reportDate)}\n`;
  if (report.failedEnvs && report.failedEnvs.length) {
    out += `⚠️ **数据源拉取失败：${report.failedEnvs.join(', ')} 未计入，以下金额严重偏低，请勿作为结论**\n`;
  }
  out += `刷新：${report.checkedAtUtc.replace('T', ' ').slice(0, 19)} UTC；统计日：UTC ${report.reportDate}\n`;
  out += '口径：双源 LiteLLM `/spend/logs` 日聚合 users map 精确取数并本地按 date 过滤；按人员合并 Claude Code / CarHer / Cursor；不做 GPT 剔除、Opus 半价或活跃账号 +$1。\n';
  out += `当日合计：**${fmtMoney(t.daily)}**；新口径累计（自 ${HISTORY_START_DATE}）：**${fmtMoney(t.cumulative)}**\n`;
  out += `人员：${t.activePeople}/${t.people} 活跃；账号：${t.activeAccounts}/${t.accounts} 活跃`;
  if (t.blockedActiveAccounts) out += `；blocked 但当日有 spend 的 key ${t.blockedActiveAccounts} 把已计入`;
  if (report.fetchFailures) out += `；拉取失败 ${report.fetchFailures} 把，可能低估`;
  out += '\n';

  out += '──── 📈 按账号类型 ────\n';
  for (const type of ['Claude Code', 'CarHer', 'Cursor']) {
    const row = s.byType[type];
    if (!row) continue;
    out += `${padVisual(accountTypeShort(type), 8)} ${padVisual(fmtMoney(row.daily), 11)} · ${row.active}/${row.accounts} 活跃 · 累计 ${fmtMoney(row.cumulative)}\n`;
  }

  const top = s.activePeople.slice(0, topN);
  out += `──── 🏆 人员 Top ${top.length}（已合并多账号） ────\n`;
  top.forEach((p, i) => {
    const rank = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `${String(i + 1).padStart(2, ' ')}.`;
    const typeText = p.types.map(accountTypeShort).join('+');
    out += `${rank} ${padVisual(p.name, 12)} ${padVisual(fmtMoney(p.daily), 10)} 累计 ${padVisual(fmtMoney(p.cumulative), 10)} ${p.activeAccounts}/${p.accounts.length} 账号 ${typeText}\n`;
  });

  const unmapped = s.activePeople.filter((p) => p.key.startsWith('name:') && /^(carher-|claude-code-|cursor-)/.test(p.name)).slice(0, 10);
  if (unmapped.length) {
    out += '──── ⚠️ 有消费但未映射到人员 ────\n';
    for (const p of unmapped) out += `- ${p.name}: ${fmtMoney(p.daily)} (${p.aliases.join(', ')})\n`;
  }
  return out.trimEnd();
}

async function sendToChat(tenantToken, chatId, text) {
  const card = {
    config: { wide_screen_mode: true },
    header: { title: { tag: 'plain_text', content: '📊 LiteLLM 全员每日实际消费' }, template: 'blue' },
    elements: [{ tag: 'markdown', content: text }],
  };
  const body = JSON.stringify({ receive_id: chatId, msg_type: 'interactive', content: JSON.stringify(card) });
  const res = await requestJson({
    hostname: 'open.feishu.cn',
    path: '/open-apis/im/v1/messages?receive_id_type=chat_id',
    method: 'POST',
    headers: { Authorization: `Bearer ${tenantToken}`, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
  }, body);
  if (res.code === 0) return res;

  const textBody = JSON.stringify({ receive_id: chatId, msg_type: 'text', content: JSON.stringify({ text }) });
  return requestJson({
    hostname: 'open.feishu.cn',
    path: '/open-apis/im/v1/messages?receive_id_type=chat_id',
    method: 'POST',
    headers: { Authorization: `Bearer ${tenantToken}`, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(textBody) },
  }, textBody);
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const config = loadConfig();
  const tenantToken = await getTenantToken(config.appId, config.appSecret);
  const [keys, mappings] = await Promise.all([fetchAllKeys(config), loadUserMappings(tenantToken)]);
  const spendRows = await fetchDailyRows(config, opts.date, buildUsageKeyToAliasMap(keys));
  const summary = aggregate(keys, spendRows, mappings, opts.date);
  const report = {
    method: 'actual_daily_spend_by_person_v3',
    methodNote: 'raw backend /spend/logs daily users map with client-side date filtering; users keys are normalized via /spend/keys user_id->key_alias before merging aliases by owner; no internal discounts or active-account fee',
    reportDate: opts.date,
    historyStartDate: spendRows.startDate,
    checkedAtUtc: new Date().toISOString(),
    mappingCounts: mappings.counts,
    fetchFailures: spendRows.fail,
    failedEnvs: spendRows.failedEnvs,
    summary,
  };
  const snapshotFile = saveDashboardSnapshot(report);
  console.error(`snapshot=${snapshotFile}`);

  if (opts.json) {
    console.log(JSON.stringify(report, null, 2));
    return;
  }
  const markdown = generateMarkdown(report, opts.top);
  if (opts.dryRun) {
    console.log(markdown);
    return;
  }
  const sendResult = await sendToChat(tenantToken, opts.chat, markdown);
  console.log(JSON.stringify({ status: sendResult.code === 0 ? 'sent' : 'send_failed', code: sendResult.code, msg: sendResult.msg }));
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  console.error(err.stack);
  process.exit(1);
});
