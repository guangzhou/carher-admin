#!/usr/bin/env node
/* urlsafe_offline_cases.js — 工具来源 URL 回填(url_moderation 占位修复)离线单测。
 *
 * 背景(2026-08-26 canary 82 实锤,数据钉死):
 *   ChatGPT 网页版对「来自工具结果(function_call_output,不可信外部内容)的 URL」做 url_moderation:
 *   parts/0 里 URL 就地被换成字面占位 "url",并发一个 content_reference(safe_urls:[]、invalid:true、
 *   type:"hidden",带 start_idx/end_idx/matched_text);真链单独走一个 url_moderation 帧的
 *   url_moderation_result.full_url(is_safe:true)。官方前端凭 moderation 把占位还原成真链,我们网关
 *   过去不还原 → 用户只看到字面 "url"。修复:流式期捕获 full_url + 占位 content_reference(带 matched_text),
 *   finish() 在 citeFilter/proto2 读 full 之前,优先用 matched_text 字面定位把整段换成真链,
 *   start_idx+slice==='url' 作兜底;配不上=原样(退化=现状,零风险)。
 *
 * 注:来自「用户 prompt」的 URL(逐字回显/markdown/inline)不会被换占位——只有工具来源 URL 才会。
 *
 * 用法: node scripts/zk-cursor-web/urlsafe_offline_cases.js
 * 全过退出码 0,任一失败退 1。
 */
'use strict'

// —— 照搬 responses.js 的捕获 + 回填算法(逐字对齐上线逻辑)——
function harvestUrlRefs(arr, out) {
  if (!Array.isArray(arr)) return false
  let found = false
  for (const e of arr) {
    if (e && typeof e.start_idx === 'number' && (!e.safe_urls || e.safe_urls.length === 0)) {
      out.push({ start: e.start_idx, matched: (typeof e.matched_text === 'string' ? e.matched_text : '') })
      found = true
    }
  }
  return found
}

// 喂一串上游帧(content_references / url_moderation),产出 (_urlRefs,_urlModUrls);
// 再对给定 full 跑回填。返回 { full, subbed }。
function backfill(fullIn, frames) {
  const _urlRefs = []
  const _urlModUrls = []
  for (const d of frames) {
    if (d.type === 'url_moderation' && d.url_moderation_result
        && typeof d.url_moderation_result.full_url === 'string'
        && d.url_moderation_result.is_safe && !d.url_moderation_result.is_blocked) {
      _urlModUrls.push(d.url_moderation_result.full_url)
    }
    // 独立 content_references 帧
    if (typeof d.p === 'string' && d.p.includes('content_references')) harvestUrlRefs(d.v, _urlRefs)
    // patch 帧内的 content_references op
    if (d.o === 'patch' && Array.isArray(d.v)) {
      for (const op of d.v) {
        if (op && typeof op.p === 'string' && op.p.includes('content_references')) harvestUrlRefs(op.v, _urlRefs)
      }
    }
  }
  let full = fullIn
  let _ui = 0, _subbed = 0
  if (_urlRefs.length && _urlModUrls.length) {
    for (const _r of _urlRefs) {
      if (_ui >= _urlModUrls.length) break
      const _url = _urlModUrls[_ui]
      let _hit = false
      if (_r.matched && full.indexOf(_r.matched) >= 0) {
        full = full.replace(_r.matched, _url); _hit = true
      } else {
        const _s = _r.start
        if (typeof _s === 'number' && (full || '').slice(_s, _s + 3).toLowerCase() === 'url') {
          full = full.slice(0, _s) + _url + full.slice(_s + 3); _hit = true
        }
      }
      if (_hit) { _ui++; _subbed++ }
    }
  }
  return { full, subbed: _subbed }
}

// —— 病例库 ——
const REAL = 'https://t83dfrspj4.feishu.cn/docx/OEeadb5x0ohRiOxnKh6cxe5cnge'
const MOD = (u) => ({ type: 'url_moderation', url_moderation_result: { full_url: u, is_safe: true, is_blocked: false } })
const CREF_PATCH = (refs) => ({ o: 'patch', v: [{ o: 'append', p: '/message/content/parts/0/content_references', v: refs }] })
const CREF_STANDALONE = (refs) => ({ p: '/message/content/parts/0/content_references', v: refs })

const cases = [
  {
    // 实锤形状(canary 82 抓帧):matched_text 含占位"url"+锚文本+截断真链片段,full 里字面存在 → 整段换真链。
    name: 'real-matched-replace',
    full: '创建成功。文档链接：url打开飞书测试文档https://t83dfrspj4',
    frames: [
      CREF_PATCH([{ matched_text: 'url打开飞书测试文档https://t83dfrspj4', start_idx: 52, end_idx: 84, safe_urls: [], invalid: true, type: 'hidden' }]),
      MOD(REAL),
    ],
    wantSub: 1, wantHasReal: true, wantNoLiteralUrl: true,
  },
  {
    // 独立 content_references 帧(非 patch 包裹)同样捕获。
    name: 'standalone-cref-matched',
    full: '文档链接：url查看',
    frames: [ CREF_STANDALONE([{ matched_text: 'url查看', start_idx: 5, safe_urls: [] }]), MOD(REAL) ],
    wantSub: 1, wantHasReal: true, wantNoLiteralUrl: false, // "查看"保留,"url"被换
  },
  {
    // matched_text 缺失 → 退回 start_idx + slice==='url' 兜底。
    name: 'fallback-start-idx',
    full: '链接：url',      // "链接：" = 3 字符,url 起于 idx 3
    frames: [ CREF_STANDALONE([{ start_idx: 3, safe_urls: [] }]), MOD(REAL) ],
    wantSub: 1, wantHasReal: true, wantNoLiteralUrl: true,
  },
  {
    // safe_urls 非空(moderation 已放行)→ 不是占位签名 → 不捕获 → full 原样。
    name: 'safe-urls-present-noop',
    full: '链接：https://example.com/ok',
    frames: [ CREF_STANDALONE([{ start_idx: 3, safe_urls: ['https://example.com/ok'] }]), MOD('https://example.com/ok') ],
    wantSub: 0, wantHasReal: false, wantEqualInput: true,
  },
  {
    // 用户来源 URL:无 content_reference 帧 → 无捕获 → 原样(逐字回显不受影响)。
    name: 'user-sourced-verbatim-noop',
    full: '你给的链接是 https://t83dfrspj4.feishu.cn/docx/OEeadb5x0ohRiOxnKh6cxe5cnge',
    frames: [ MOD(REAL) ],   // 只有 moderation 无 cref → refs 空 → 不动
    wantSub: 0, wantEqualInput: true,
  },
  {
    // 守卫:matched 不在 full 且 start 处非 "url" → 退化不动(零风险)。
    name: 'mismatch-degrade-noop',
    full: '完全不含占位的正常正文。',
    frames: [ CREF_STANDALONE([{ matched_text: 'urlXXX不存在', start_idx: 2, safe_urls: [] }]), MOD(REAL) ],
    wantSub: 0, wantEqualInput: true,
  },
  {
    // 有占位 content_reference 但没等到 url_moderation(真链帧缺)→ 不动(不能拿假链填)。
    name: 'ref-without-mod-noop',
    full: '文档链接：url',
    frames: [ CREF_STANDALONE([{ matched_text: 'url', start_idx: 5, safe_urls: [] }]) ],
    wantSub: 0, wantEqualInput: true,
  },
]

let pass = 0, fail = 0
const fails = []
for (const c of cases) {
  const r = backfill(c.full, c.frames)
  let ok = true, why = `sub=${r.subbed}`
  if (r.subbed !== c.wantSub) { ok = false; why += ` wantSub=${c.wantSub}` }
  if (ok && c.wantHasReal !== undefined) {
    const has = r.full.includes(REAL)
    if (has !== c.wantHasReal) { ok = false; why += ` hasReal=${has} want=${c.wantHasReal}` }
  }
  if (ok && c.wantNoLiteralUrl) {
    // 交付里不应残留裸 "url" 占位词(用真链的 http 前缀之外的独立 "url")
    const stripped = r.full.split(REAL).join('')
    if (/(^|[^a-zA-Z])url([^a-zA-Z]|$)/.test(stripped)) { ok = false; why += ` LITERAL-url-remains full=${JSON.stringify(r.full)}` }
  }
  if (ok && c.wantEqualInput) {
    if (r.full !== c.full) { ok = false; why += ` MUTATED full=${JSON.stringify(r.full)}` }
  }
  if (ok) { pass++ } else { fail++; fails.push({ name: c.name, why }) }
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${c.name} — ${why}`)
}

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO (工具来源 URL 回填 7 形状:matched替换/独立帧/start兜底/safe放行/用户源/失配退化/缺真链)')
