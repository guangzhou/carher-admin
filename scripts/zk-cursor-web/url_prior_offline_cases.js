#!/usr/bin/env node
/* url_prior_offline_cases.js — [url-safe-prior] 修法离线单测(ZK_URL_PRIOR)
 *
 * 2026-08-30 用户实锤:模型作答"链接是..."时上游发 content_reference 占位
 * (matched_text="url打开飞书文档https://t83dfrspj4",29 字符前缀截断),
 * 真链**在上一轮**工具输出里已下发(feishu docx URL),本轮上游不再重发
 * url_moderation 帧 → _urlModUrls=[] → 回填器判据 `refs && mods` 为假 →
 * 用户看到残链 "url打开飞书文档https://t83dfrspj"。
 *
 * 修法:门控 ZK_URL_PRIOR=1 默认关。从 basePrompt(含历史 TOOL RESULT)扫出所有
 * URL 建候选池;回填时若 _urlModUrls 空,按 matched_text 里 "https://X" 前缀
 * 唯一匹配捞真链回填(0 或 多 匹配都不回填,避免张冠李戴)。
 *
 * 用法: node scripts/zk-cursor-web/url_prior_offline_cases.js /tmp/resp_now.js
 * 控制组: 指向未打补丁字节(402cc207) 必须 FAIL S0。
 */
'use strict'
const fs = require('fs')
const SRC = process.argv[2] || '/tmp/resp_now.js'
const code = fs.readFileSync(SRC, 'utf8')

let pass = 0, fail = 0
const fails = []
function check(name, ok, why) {
  if (ok) { pass++; console.log('  PASS ' + name) }
  else { fail++; fails.push({name, why}); console.log('  FAIL ' + name + (why ? ' — ' + why : '')) }
}

// ── S0 门控 anchor(控制组在此 FAIL) ────────────────────────
check('S0 ZK_URL_PRIOR 门控注入', code.includes("process.env.ZK_URL_PRIOR === '1'"))
check('S0b _priorUrls 声明', /const _priorUrls = \[\]/.test(code))
check('S0c fallback 分支存在', /else if \(_urlRefs\.length && _priorUrls\.length && process\.env\.ZK_URL_PRIOR === '1'\)/.test(code))
if (fail) { console.log(`\n${pass} pass / ${fail} fail`); process.exit(1) }

// ── S1 扫描器纯函数复现(与上线字节逐字同构) ─────────────
function scanPrior(basePrompt) {
  const _priorUrls = []
  const _re = /https?:\/\/[^\s"'<>)\]}]+/g
  let _mm
  const _seen = new Set()
  while ((_mm = _re.exec(basePrompt)) && _priorUrls.length < 64) {
    const _u = _mm[0].replace(/[.,;:!?)\]]+$/, '')
    if (!_seen.has(_u)) { _seen.add(_u); _priorUrls.push(_u) }
  }
  return _priorUrls
}

// 真实 TOOL RESULT 形状(来自 2026-08-30 用户实锤 REQ)
const REAL_BP = `USER: [TOOL RESULT call_web_mtf3s8m5_232_Shell]
Exit code: 0
Command output:
\`\`\`
{
  "ok": true,
  "data": {
    "document": {
      "content": "<title>测试文档</title><p>正文</p>",
      "document_id": "JggndDwNyoKA5oxtYCicp3mHnDh",
      "url": "https://t83dfrspj4.feishu.cn/docx/JggndDwNyoKA5oxtYCicp3mHnDh",
      "revision_id": 3
    }
  }
}
\`\`\`
`

{
  const urls = scanPrior(REAL_BP)
  check('S1a 真实 REQ 扫到目标 feishu URL',
    urls.some(u => u === 'https://t83dfrspj4.feishu.cn/docx/JggndDwNyoKA5oxtYCicp3mHnDh'),
    'got: ' + JSON.stringify(urls))
  check('S1b 无重复', urls.length === new Set(urls).size)
  check('S1c URL 尾部无标点', urls.every(u => !/[.,;:!?)\]]$/.test(u)))
}

// ── S2 空 basePrompt 不炸 ──────────────────────────
check('S2 空 basePrompt 扫不出', scanPrior('').length === 0)
check('S2b 无 URL basePrompt', scanPrior('这里没有链接').length === 0)

// ── S3 上限 64 截断 ─────────────────────────────
{
  let big = ''
  for (let i = 0; i < 80; i++) big += ` https://x${i}.com`
  check('S3 64 上限截断', scanPrior(big).length === 64)
}

// ── S4 匹配逻辑纯函数(修法核心) ─────────────────
function refillPrior(full, urlRefs, priorUrls) {
  let subbed = 0
  for (const _r of urlRefs) {
    if (!_r.matched || _r.matched.indexOf('http') < 0) continue
    const _hi = _r.matched.indexOf('http')
    const _prefix = _r.matched.slice(_hi).trim()
    if (_prefix.length < 12) continue
    const _cands = priorUrls.filter((u) => u.startsWith(_prefix))
    if (_cands.length !== 1) continue
    const _url = _cands[0]
    const _at = full.indexOf(_r.matched)
    if (_at < 0) continue
    let _anchor = ''
    if (_r.matched.slice(0, 3).toLowerCase() === 'url') {
      _anchor = _r.matched.slice(3, _hi).trim()
    }
    const _rep = _anchor ? `[${_anchor}](${_url})` : _url
    full = full.slice(0, _at) + _rep + full.slice(_at + _r.matched.length)
    subbed++
  }
  return { full, subbed }
}

// ── S5 用户实锤形状:回填成功 markdown 链 ─────────
{
  const full = "已创建。标题:测试文档。url打开飞书文档https://t83dfrspj"
  const refs = [{ matched: 'url打开飞书文档https://t83dfrspj', start: 15 }]
  const pool = ['https://t83dfrspj4.feishu.cn/docx/JggndDwNyoKA5oxtYCicp3mHnDh']
  const r = refillPrior(full, refs, pool)
  check('S5a 用户实锤形状回填成功', r.subbed === 1)
  check('S5b 结果含 markdown [锚](真链)',
    r.full.includes('[打开飞书文档](https://t83dfrspj4.feishu.cn/docx/JggndDwNyoKA5oxtYCicp3mHnDh)'),
    'got: ' + r.full)
  check('S5c 残链已消失', !r.full.includes('t83dfrspj"') && !r.full.endsWith('t83dfrspj'))
}

// ── S6 前缀歧义(池中 2 个都以前缀开头 → 不回填) ─
{
  const full = "参考 url看这里https://t83dfrspj4.feishu.cn/docx/abcdefghijk 后文。"
  const refs = [{ matched: 'url看这里https://t83dfrspj4.feishu.cn/docx/abcdefghijk', start: 3 }]
  const pool = [
    'https://t83dfrspj4.feishu.cn/docx/abcdefghijk-suffix1',
    'https://t83dfrspj4.feishu.cn/docx/abcdefghijk-suffix2',
  ]
  const r = refillPrior(full, refs, pool)
  check('S6 歧义前缀不回填(避免张冠李戴)', r.subbed === 0)
}

// ── S7 前缀太短(<12 字符)不回填 ─────────────
{
  const refs = [{ matched: 'urlxhttps://', start: 0 }]
  const pool = ['https://a.com', 'https://b.com']
  check('S7 短前缀 skip', refillPrior('xxxurlxhttps://xxx', refs, pool).subbed === 0)
}

// ── S8 池无匹配不回填 ─────────────────────────
{
  const refs = [{ matched: 'urlopen linkhttps://nowhere.com/xyz', start: 0 }]
  const pool = ['https://other.com/abc']
  check('S8 池无候选 skip', refillPrior('xxxurlopen linkhttps://nowhere.com/xyzxxx', refs, pool).subbed === 0)
}

// ── S9 matched 里没 http 前缀不处理 ─────────
{
  const refs = [{ matched: 'plain text no url', start: 0 }]
  const pool = ['https://x.com/y']
  check('S9 无 http 前缀 skip', refillPrior('xxx plain text no url xxx', refs, pool).subbed === 0)
}

// ── S10 门控关时 fallback 分支不进(结构断言) ─
{
  const priorRe = /else if \(_urlRefs\.length && _priorUrls\.length && process\.env\.ZK_URL_PRIOR === '1'\)/
  check('S10 fallback 严格 gated', priorRe.test(code))
}

// ── S11 修法**不**影响原路径(_urlModUrls 有值时优先原路径) ─
{
  const orig = /if \(_urlRefs\.length && _urlModUrls\.length\)\s*\{/
  check('S11 原路径不变', orig.test(code))
}

// ── S12 与用户真实截图形状完全一致(端到端) ─
{
  const bp = REAL_BP  // 上一轮 REQ 里已有真链
  const priorUrls = scanPrior(bp)
  const full = "已创建并验证成功。标题:测试文档。正文:这是一个测试文档。链接:url打开飞书文档https://t83dfrspj"
  const refs = [{ matched: 'url打开飞书文档https://t83dfrspj', start: 25 }]
  const r = refillPrior(full, refs, priorUrls)
  check('S12 端到端:用户看到的真链完整',
    r.subbed === 1 && r.full.includes('https://t83dfrspj4.feishu.cn/docx/JggndDwNyoKA5oxtYCicp3mHnDh'),
    'got: ' + r.full)
}

console.log(`\n${pass} pass / ${fail} fail`)
if (fail) { console.log(JSON.stringify(fails, null, 2)); process.exit(1) }
