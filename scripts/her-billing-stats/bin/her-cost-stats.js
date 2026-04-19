#!/usr/bin/env node
/**
 * Her Cost Stats v3 - 跨实例 OpenClaw Token & 成本统计
 *
 * 全功能：
 *   - 自动定位 OpenClaw 数据根目录
 *   - 7 类来源全统计
 *   - 去除双重计算
 *   - 按 session 类型拆分（私聊/群聊/cron/subagent/dreaming）
 *   - 输出表格 + JSON（方便跨 her 汇总）
 *
 * 用法：
 *   node her-cost-stats.js                  # 表格输出，全部历史
 *   node her-cost-stats.js 30               # 表格输出，最近 30 天
 *   node her-cost-stats.js --json           # JSON 输出（机器可读）
 *   node her-cost-stats.js --json --days 7  # JSON + 7 天
 *   node her-cost-stats.js --root /path     # 指定数据根目录
 *
 * 跨 her 汇总场景：
 *   ssh her-N "node her-cost-stats.js --json" > /tmp/her-N.json
 *   然后本地汇总
 */

const fs = require('fs');
const path = require('path');
const os = require('os');

// ============== 参数解析 ==============
const args = process.argv.slice(2);
const flags = {};
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === '--json') flags.json = true;
  else if (a === '--days') flags.days = parseInt(args[++i], 10);
  else if (a === '--root') flags.root = args[++i];
  else if (/^\d+$/.test(a)) flags.days = parseInt(a, 10);
}

const days = flags.days || 999;
const cutoff = days >= 999 ? 0 : Date.now() - days * 86400000;

// ============== 自动定位数据根 ==============
function findRoot() {
  if (flags.root) return flags.root;
  const candidates = [
    '/data/.openclaw',
    path.join(os.homedir(), '.openclaw'),
    process.env.OPENCLAW_HOME,
  ].filter(Boolean);
  for (const c of candidates) {
    if (fs.existsSync(path.join(c, 'agents'))) return c;
  }
  throw new Error('找不到 OpenClaw 数据根，用 --root 指定');
}

const ROOT = findRoot();
const SESSIONS_DIR = path.join(ROOT, 'agents/main/sessions');
const SESSIONS_INDEX = path.join(SESSIONS_DIR, 'sessions.json');
const CRON_RUNS_DIR = path.join(ROOT, 'cron/runs');
const CRON_JOBS = path.join(ROOT, 'cron/jobs.json');
const A2A_AUDIT = path.join(ROOT, 'a2a-audit.jsonl');
const A2A_TASKS_DIR = path.join(ROOT, 'a2a-tasks');
const COMPACTION_DIR = path.join(ROOT, 'compaction-reports');
const IDENTITY = path.join(ROOT, 'identity/device.json');

// ============== 价格表（USD per 1M tokens, 2026.4）==============
const PRICING = {
  'anthropic/claude-opus-4.7':     { i: 15,   o: 75,  cr: 1.50,  cw: 18.75 },
  'anthropic/claude-opus-4.6':     { i: 15,   o: 75,  cr: 1.50,  cw: 18.75 },
  'anthropic/claude-sonnet-4.6':   { i: 3,    o: 15,  cr: 0.30,  cw: 3.75  },
  'anthropic/claude-haiku-4-5':    { i: 1,    o: 5,   cr: 0.10,  cw: 1.25  },
  'openai/gpt-5.4':                { i: 1.25, o: 10,  cr: 0.125, cw: 1.25  },
  'openai/gpt-5.3-codex':          { i: 1.25, o: 10,  cr: 0.125, cw: 1.25  },
  'google/gemini-3.1-pro-preview': { i: 1.25, o: 10,  cr: 0.125, cw: 1.25  },
  'z-ai/glm-5':                    { i: 0.5,  o: 2,   cr: 0.05,  cw: 0.5   },
  'minimax/minimax-m2.7':          { i: 0.5,  o: 2,   cr: 0.05,  cw: 0.5   },
  'minimax/minimax-m2.5':          { i: 0.5,  o: 2,   cr: 0.05,  cw: 0.5   },
  default:                         { i: 5,    o: 25,  cr: 0.5,   cw: 6     },
};

const FX_RMB = 6.87;

function priceFor(model) {
  return PRICING[model] || PRICING.default;
}

function calcCost(usage, model) {
  const p = priceFor(model);
  const i  = usage.input  || usage.input_tokens  || 0;
  const o  = usage.output || usage.output_tokens || 0;
  const cr = usage.cacheRead  || usage.cache_read_input_tokens || 0;
  const cw = usage.cacheWrite || usage.cache_creation_input_tokens || 0;
  return (i * p.i + o * p.o + cr * p.cr + cw * p.cw) / 1e6;
}

// ============== Session 索引：sessionId → 类型 ==============
function loadSessionIndex() {
  const map = {};
  if (!fs.existsSync(SESSIONS_INDEX)) return map;
  try {
    const d = JSON.parse(fs.readFileSync(SESSIONS_INDEX, 'utf-8'));
    for (const [key, v] of Object.entries(d)) {
      const sid = v.sessionId;
      if (!sid) continue;
      let kind = 'main';
      if (key.includes('cron')) kind = 'cron';
      else if (key.includes('subagent')) kind = 'subagent';
      else if (key.includes('dreaming')) kind = 'dreaming';
      else if (key.includes('feishu:group')) kind = 'group';
      else if (key.includes('feishu:user') || key.includes('feishu:peer')) kind = 'dm';
      else if (key.includes('realtime_capsule')) kind = 'realtime';
      else if (key === 'agent:main:main') kind = 'main_chat';
      map[sid] = { kind, key };
    }
  } catch (e) {}
  return map;
}

// ============== Source 1: Main Sessions（按 session 类型拆分）==============
function statMainSessions(sessionIndex, cronSidsInRuns) {
  // 按 kind 分桶
  const buckets = {
    main_chat: emptyBucket(),
    dm: emptyBucket(),
    group: emptyBucket(),
    subagent: emptyBucket(),
    dreaming: emptyBucket(),
    realtime: emptyBucket(),
    cron_dup: emptyBucket(),  // 重复算的 cron session
    orphan: emptyBucket(),     // sessions.json 里找不到的孤儿文件
  };

  if (!fs.existsSync(SESSIONS_DIR)) return buckets;

  const files = fs.readdirSync(SESSIONS_DIR).filter(f =>
    f.endsWith('.jsonl') || /\.jsonl\.reset\./.test(f)
  );

  for (const f of files) {
    // 提取 sessionId（去掉 .jsonl 或 .jsonl.reset.xxx）
    const sid = f.replace(/\.jsonl(\.reset\..*)?$/, '');

    let kind;
    if (cronSidsInRuns.has(sid)) {
      // 这个文件本质上是 cron 任务的 session（cron/runs 里也有）
      kind = 'cron_dup';
    } else if (sessionIndex[sid]) {
      kind = sessionIndex[sid].kind;
      if (!buckets[kind]) kind = 'main_chat';
    } else {
      kind = 'orphan';
    }

    const fp = path.join(SESSIONS_DIR, f);
    let txt;
    try { txt = fs.readFileSync(fp, 'utf-8'); } catch { continue; }

    for (const line of txt.split('\n')) {
      if (!line.includes('"usage"')) continue;
      let o;
      try { o = JSON.parse(line); } catch { continue; }
      if (o.type !== 'message') continue;
      const ts = new Date(o.timestamp).getTime();
      if (ts < cutoff) continue;
      const u = o.message?.usage;
      if (!u) continue;
      const m = o.message?.model || 'unknown';

      let c = 0;
      if (typeof u.cost === 'number') c = u.cost;
      else if (u.cost?.total !== undefined) c = u.cost.total;
      else c = calcCost(u, m);

      const b = buckets[kind];
      b.calls++;
      b.input  += u.input  || 0;
      b.output += u.output || 0;
      b.cacheRead  += u.cacheRead  || 0;
      b.cacheWrite += u.cacheWrite || 0;
      b.cost += c;
      b.models[m] = (b.models[m] || 0) + 1;
    }
  }

  return buckets;
}

// ============== Source 2: Cron Runs（独立 session，不在 main/sessions/）==============
function statCronRuns() {
  const stats = emptyBucket();
  stats.byJob = {};
  const sidsSeen = new Set();

  if (!fs.existsSync(CRON_RUNS_DIR)) return { stats, sidsSeen };

  // 读 jobs 名字映射
  const jobNames = {};
  try {
    const jd = JSON.parse(fs.readFileSync(CRON_JOBS, 'utf-8'));
    const jobs = Array.isArray(jd) ? jd : (jd.jobs || []);
    for (const j of jobs) {
      jobNames[j.id] = j.name || (j.id || '').slice(0, 8);
    }
  } catch {}

  const files = fs.readdirSync(CRON_RUNS_DIR).filter(f => f.endsWith('.jsonl'));
  for (const f of files) {
    const fp = path.join(CRON_RUNS_DIR, f);
    let txt;
    try { txt = fs.readFileSync(fp, 'utf-8'); } catch { continue; }
    for (const line of txt.split('\n')) {
      if (!line) continue;
      let o;
      try { o = JSON.parse(line); } catch { continue; }
      if (o.action !== 'finished' || !o.usage) continue;
      const ts = o.ts || 0;
      if (ts < cutoff) continue;
      const u = o.usage;
      const m = o.model || 'unknown';
      const c = calcCost(u, m);

      const sid = o.sessionId;
      if (sid) sidsSeen.add(sid);

      stats.calls++;
      stats.input  += u.input_tokens  || 0;
      stats.output += u.output_tokens || 0;
      stats.cost += c;
      stats.models[m] = (stats.models[m] || 0) + 1;

      const jid = o.jobId;
      const jname = jobNames[jid] || (jid || '').slice(0, 8) || 'unknown';
      if (!stats.byJob[jname]) stats.byJob[jname] = { calls: 0, cost: 0, tokens: 0 };
      stats.byJob[jname].calls++;
      stats.byJob[jname].cost += c;
      stats.byJob[jname].tokens += (u.total_tokens || (u.input_tokens || 0) + (u.output_tokens || 0));
    }
  }
  return { stats, sidsSeen };
}

// ============== Source 3: A2A inbound（粗略统计）==============
function statA2A() {
  const stats = { tasks: 0, completedHistory: 0, totalDurMs: 0 };
  if (fs.existsSync(A2A_AUDIT)) {
    try {
      const lines = fs.readFileSync(A2A_AUDIT, 'utf-8').split('\n');
      for (const l of lines) {
        if (!l) continue;
        try {
          const o = JSON.parse(l);
          const ts = new Date(o.ts).getTime();
          if (ts < cutoff) continue;
          if (o.direction === 'inbound') {
            stats.tasks++;
            stats.totalDurMs += o.durationMs || 0;
          }
        } catch {}
      }
    } catch {}
  }
  if (fs.existsSync(A2A_TASKS_DIR)) {
    stats.completedHistory = fs.readdirSync(A2A_TASKS_DIR).filter(f => f.endsWith('.json')).length;
  }
  return stats;
}

// ============== Source 4: Compaction events ==============
function statCompaction() {
  const stats = { count: 0, totalTokensCompacted: 0 };
  if (!fs.existsSync(COMPACTION_DIR)) return stats;
  for (const f of fs.readdirSync(COMPACTION_DIR)) {
    if (!f.endsWith('.md')) continue;
    const fp = path.join(COMPACTION_DIR, f);
    const st = fs.statSync(fp);
    if (st.mtime.getTime() < cutoff) continue;
    stats.count++;
    try {
      const content = fs.readFileSync(fp, 'utf-8');
      const m = content.match(/tokensBefore=([\d,]+)/);
      if (m) stats.totalTokensCompacted += parseInt(m[1].replace(/,/g, ''), 10);
    } catch {}
  }
  return stats;
}

// ============== Helpers ==============
function emptyBucket() {
  return { calls: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, models: {} };
}

function fmtNum(n, w) {
  return String(Math.round(n)).padStart(w);
}

function fmtKB(n, w) {
  return ((n / 1000).toFixed(0) + 'k').padStart(w);
}

function getHerIdentity() {
  try {
    const d = JSON.parse(fs.readFileSync(IDENTITY, 'utf-8'));
    return d.deviceName || d.name || d.deviceId || 'unknown';
  } catch {
    return os.hostname() || 'unknown';
  }
}

// ============== 主流程 ==============
function main() {
  const sessionIndex = loadSessionIndex();
  const { stats: cronStats, sidsSeen: cronSids } = statCronRuns();
  const mainBuckets = statMainSessions(sessionIndex, cronSids);
  const a2a = statA2A();
  const compaction = statCompaction();

  const identity = getHerIdentity();
  const dayDesc = days >= 999 ? 'all-time' : `last ${days} days`;

  // 算总数（去掉 cron_dup 避免重复）
  let totalCost = cronStats.cost;
  let totalCalls = cronStats.calls;
  for (const [k, b] of Object.entries(mainBuckets)) {
    if (k === 'cron_dup') continue; // 重复，已在 cronStats 算
    totalCost += b.cost;
    totalCalls += b.calls;
  }

  const totalRMB = totalCost * FX_RMB;

  if (flags.json) {
    // JSON 输出（跨 her 汇总用）
    const out = {
      her: identity,
      root: ROOT,
      timeRange: dayDesc,
      generatedAt: new Date().toISOString(),
      total: {
        calls: totalCalls,
        cost_usd: +totalCost.toFixed(4),
        cost_rmb: +totalRMB.toFixed(2),
      },
      sources: {
        main_chat: bucketSummary(mainBuckets.main_chat),
        dm: bucketSummary(mainBuckets.dm),
        group_chat: bucketSummary(mainBuckets.group),
        subagent: bucketSummary(mainBuckets.subagent),
        dreaming: bucketSummary(mainBuckets.dreaming),
        realtime: bucketSummary(mainBuckets.realtime),
        orphan_sessions: bucketSummary(mainBuckets.orphan),
        cron: { ...bucketSummary(cronStats), byJob: cronStats.byJob },
        cron_double_count_excluded: bucketSummary(mainBuckets.cron_dup),
      },
      a2a_inbound: {
        tasks: a2a.tasks,
        durationSec: Math.round(a2a.totalDurMs / 1000),
        completedTasksOnDisk: a2a.completedHistory,
        note: 'token usage 未单独追踪',
      },
      compaction: {
        events: compaction.count,
        tokensCompacted: compaction.totalTokensCompacted,
        note: '花费已包含在 main_chat / cron 中',
      },
    };
    console.log(JSON.stringify(out, null, 2));
    return;
  }

  // 表格输出
  const W = 70;
  console.log(`\n${'='.repeat(W)}`);
  console.log(`📊 OpenClaw Her 成本统计 v3`);
  console.log(`   Her:    ${identity}`);
  console.log(`   Root:   ${ROOT}`);
  console.log(`   Range:  ${dayDesc}`);
  console.log(`${'='.repeat(W)}\n`);

  console.log('| 来源                | 调用    | Tokens     | USD        | RMB         |');
  console.log('|---------------------|---------|------------|------------|-------------|');
  printRow('1. 私聊主对话', mainBuckets.main_chat);
  printRow('2. 飞书 DM 私聊', mainBuckets.dm);
  printRow('3. 飞书群聊', mainBuckets.group);
  printRow('4. Subagent 子代理', mainBuckets.subagent);
  printRow('5. Dreaming 梦境', mainBuckets.dreaming);
  printRow('6. Realtime 实时', mainBuckets.realtime);
  printRow('7. 孤儿 session', mainBuckets.orphan);
  printRow('8. Cron 定时任务', cronStats);
  console.log('|---------------------|---------|------------|------------|-------------|');
  console.log(`| **合计**            | ${String(totalCalls).padStart(7)} |    -       | $${totalCost.toFixed(2).padStart(9)} | ¥${totalRMB.toFixed(0).padStart(10)} |`);

  console.log(`\n⚠️  已扣除重复（cron 跑出的 session 也在 main/sessions/）: $${mainBuckets.cron_dup.cost.toFixed(2)} (${mainBuckets.cron_dup.calls} calls)`);

  if (a2a.tasks > 0) {
    console.log(`\n🔗 A2A 入站任务: ${a2a.tasks} 个，累计 LLM 跑了 ${(a2a.totalDurMs / 60000).toFixed(1)} 分钟`);
    console.log(`   （token 未单独记录，但实际花费已并入 main_chat）`);
  }

  if (compaction.count > 0) {
    console.log(`\n🗜  Compaction 事件: ${compaction.count} 次，累计被压缩 ${(compaction.totalTokensCompacted/1000).toFixed(0)}k tokens`);
    console.log(`   （花费已并入 main_chat / cron）`);
  }

  // Cron 任务 TOP 10
  if (Object.keys(cronStats.byJob).length > 0) {
    console.log('\n📋 Cron 任务 TOP 10（按成本）');
    console.log('| Job 名                                 | 调用 | Tokens   | USD       | RMB     |');
    console.log('|----------------------------------------|------|----------|-----------|---------|');
    const sorted = Object.entries(cronStats.byJob).sort((a, b) => b[1].cost - a[1].cost).slice(0, 10);
    for (const [name, s] of sorted) {
      console.log(`| ${name.padEnd(38)} | ${String(s.calls).padStart(4)} | ${fmtKB(s.tokens, 7)} | $${s.cost.toFixed(2).padStart(8)} | ¥${(s.cost*FX_RMB).toFixed(0).padStart(6)} |`);
    }
  }

  // 模型分布（合并所有来源）
  const allModels = {};
  for (const b of [...Object.values(mainBuckets), cronStats]) {
    for (const [m, c] of Object.entries(b.models)) {
      allModels[m] = (allModels[m] || 0) + c;
    }
  }
  console.log('\n🤖 模型调用分布（按调用次数）');
  const totalModelCalls = Object.values(allModels).reduce((a, b) => a + b, 0);
  for (const [m, c] of Object.entries(allModels).sort((a, b) => b[1] - a[1]).slice(0, 8)) {
    const pct = (c / totalModelCalls * 100).toFixed(1);
    console.log(`  ${m.padEnd(45)} ${String(c).padStart(6)} (${pct}%)`);
  }

  console.log(`\n${'='.repeat(W)}`);
  console.log(`💰 累计花费: $${totalCost.toFixed(2)} USD = ¥${totalRMB.toFixed(2)} RMB (汇率 ${FX_RMB})`);
  console.log(`${'='.repeat(W)}\n`);

  console.log('💡 提示:');
  console.log('  - 用 --json 输出机器可读格式，方便跨 her 汇总');
  console.log('  - Compaction 花费已含在主对话里（压缩动作触发的 message 也有 usage）');
  console.log('  - Dreaming 拆分到独立桶');
  console.log('  - cron_dup 已被剔除，避免双重计算\n');
}

function bucketSummary(b) {
  return {
    calls: b.calls,
    tokens: {
      input: b.input,
      output: b.output,
      cacheRead: b.cacheRead || 0,
      cacheWrite: b.cacheWrite || 0,
    },
    cost_usd: +b.cost.toFixed(4),
    cost_rmb: +(b.cost * FX_RMB).toFixed(2),
  };
}

function printRow(label, b) {
  const rmb = b.cost * FX_RMB;
  const tokens = b.input + b.output + (b.cacheRead || 0) + (b.cacheWrite || 0);
  console.log(`| ${label.padEnd(19)} | ${String(b.calls).padStart(7)} | ${fmtKB(tokens, 9)}  | $${b.cost.toFixed(2).padStart(9)} | ¥${rmb.toFixed(0).padStart(10)} |`);
}

main();
