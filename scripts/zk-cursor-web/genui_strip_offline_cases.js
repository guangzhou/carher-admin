#!/usr/bin/env node
/* genui_strip_offline_cases.js — genui 富渲染指令占位剥离离线单测(ZK_STRIP_GENUI)。
 *
 * 背景(2026-08-28 canary 82 实锤,ZK_URL_DEBUG 抓帧钉死):
 *   ChatGPT 网页版偶尔在消息尾部附一个 genui 渲染指令(learning_viz 等富组件),形如
 *     genui{"learning_viz":{"type_id":"BUBBLE_SORT","locale_override":"zh-CN"}}
 *   走 content_reference,帧属性 safe_urls:[]、invalid:true、type:"hidden"、带 start_idx/end_idx
 *   /matched_text(=genui 字面)。官方前端凭它渲染富组件,我们网关无渲染器 → 若不剥,它随
 *   finish() 的 full 裸漏成文本(实测"会话结尾偶发 genui{…}")。
 *
 *   关键坑:genui 占位与 url 占位**共用 harvest 签名**(start_idx 数值 + 空 safe_urls),都会进
 *   _urlRefs;且两者 type 都是 "hidden"、invalid 都是 true(见 urlsafe_offline_cases 头注 L6-8)。
 *   → 判别 genui 的唯一可靠依据是 **matched_text 前缀 "genui"**(凭 type:hidden 会误伤合法 url 占位)。
 *   修复:finish() 在 url-safe 回填**之前**,把 matched 前缀为 genui 的 ref 从 full 剥掉(仅在 matched
 *   字面确在 full 时剥,剥不掉就留着=不吞正文),并从 _urlRefs 过滤掉(免得 genui 占一个
 *   url_moderation 槽位错配真链)。ZK_STRIP_GENUI 门控默认关=零行为差。
 *
 * 用法: node scripts/zk-cursor-web/genui_strip_offline_cases.js
 * 全过退出码 0,任一失败退 1。
 */
'use strict'

// —— 照搬 responses.js 的捕获算法(逐字对齐 urlsafe_offline_cases)——
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

// 喂上游帧,产出 (_urlRefs,_urlModUrls);跑 genui-strip(门控 stripGenui)再跑 url 回填。
// 逐字镜像 responses.js finish() 里两个块的顺序与逻辑。返回 { full, gRemoved, subbed, urlRefsLeft }。
function run(fullIn, frames, stripGenui) {
  const _urlRefs = []
  const _urlModUrls = []
  for (const d of frames) {
    if (d.type === 'url_moderation' && d.url_moderation_result
        && typeof d.url_moderation_result.full_url === 'string'
        && d.url_moderation_result.is_safe && !d.url_moderation_result.is_blocked) {
      _urlModUrls.push(d.url_moderation_result.full_url)
    }
    if (typeof d.p === 'string' && d.p.includes('content_references')) harvestUrlRefs(d.v, _urlRefs)
    if (d.o === 'patch' && Array.isArray(d.v)) {
      for (const op of d.v) {
        if (op && typeof op.p === 'string' && op.p.includes('content_references')) harvestUrlRefs(op.v, _urlRefs)
      }
    }
    const _mdRefs = d.v && d.v.message && d.v.message.metadata && d.v.message.metadata.content_references
    if (Array.isArray(_mdRefs) && _mdRefs.length) harvestUrlRefs(_mdRefs, _urlRefs)
  }

  let full = fullIn
  let _gRemoved = 0

  // —— genui-strip 块(逐字镜像上线代码)——
  if (stripGenui && _urlRefs.length) {
    const _kept = []
    for (const _r of _urlRefs) {
      const _m = (typeof _r.matched === 'string') ? _r.matched : ''
      if (_m.slice(0, 5).toLowerCase() === 'genui' && full.indexOf(_m) >= 0) {
        const _at = full.indexOf(_m)
        full = full.slice(0, _at) + full.slice(_at + _m.length)
        _gRemoved++
      } else {
        _kept.push(_r)
      }
    }
    if (_gRemoved) {
      _urlRefs.length = 0
      for (const _r of _kept) _urlRefs.push(_r)
      full = full.replace(/[ \t]+$/, '').replace(/\n{3,}$/, '\n')
      if (full.endsWith('\n')) full = full.replace(/\s+$/, '')
    }
  }

  // —— url-safe 回填块(逐字镜像 urlsafe_offline_cases)——
  let _ui = 0, _subbed = 0
  if (_urlRefs.length && _urlModUrls.length) {
    for (const _r of _urlRefs) {
      if (_ui >= _urlModUrls.length) break
      const _url = _urlModUrls[_ui]
      let _hit = false
      if (_r.matched && full.indexOf(_r.matched) >= 0) {
        const _at = full.indexOf(_r.matched)
        let _rep = _url
        if (_r.matched.slice(0, 3).toLowerCase() === 'url') {
          let _anchor = _r.matched.slice(3)
          const _hi = _anchor.indexOf('http')
          if (_hi >= 0 && _url.startsWith(_anchor.slice(_hi))) _anchor = _anchor.slice(0, _hi)
          _anchor = _anchor.trim()
          _rep = _anchor ? `[${_anchor}](${_url})` : _url
        }
        full = full.slice(0, _at) + _rep + full.slice(_at + _r.matched.length)
        _hit = true
      } else {
        const _s = _r.start
        if (typeof _s === 'number' && (full || '').slice(_s, _s + 3).toLowerCase() === 'url') {
          full = full.slice(0, _s) + _url + full.slice(_s + 3); _hit = true
        }
      }
      if (_hit) { _ui++; _subbed++ }
    }
  }
  return { full, gRemoved: _gRemoved, subbed: _subbed, urlRefsLeft: _urlRefs.length }
}

// —— 病例库 ——
const GENUI = 'genui{"learning_viz":{"type_id":"BUBBLE_SORT","locale_override":"zh-CN"}}'
const REAL = 'https://t83dfrspj4.feishu.cn/docx/OEeadb5x0ohRiOxnKh6cxe5cnge'
const MOD = (u) => ({ type: 'url_moderation', url_moderation_result: { full_url: u, is_safe: true, is_blocked: false } })
const CREF = (refs) => ({ p: '/message/content/parts/0/content_references', v: refs })
// 实锤帧(canary 82 抓,start_idx 589/end_idx 664 = 尾附加物)
const GENUI_REF = (extra) => Object.assign(
  { matched_text: GENUI, start_idx: 589, end_idx: 664, safe_urls: [], invalid: true, type: 'hidden' }, extra || {})

const cases = [
  {
    name: 'real-genui-tail-stripped',           // 实锤:尾部 genui → 剥净,正文保留
    full: '冒泡排序是一种简单的排序算法……\n\n' + GENUI,
    frames: [ CREF([GENUI_REF()]) ], strip: true,
    wantGRemoved: 1, wantNoGenui: true, wantExact: '冒泡排序是一种简单的排序算法……',
  },
  {
    name: 'genui-midtext-joins',                // genui 在正文中段 → 剥掉后两侧接合
    full: '前半段。' + GENUI + '后半段。',
    frames: [ CREF([Object.assign({}, GENUI_REF(), { start_idx: 4 })]) ], strip: true,
    wantGRemoved: 1, wantNoGenui: true, wantExact: '前半段。后半段。',
  },
  {
    name: 'default-off-not-stripped',           // 门控关 → 不剥(零行为差),genui 留在 full
    full: '正文。\n\n' + GENUI,
    frames: [ CREF([GENUI_REF()]) ], strip: false,
    wantGRemoved: 0, wantNoGenui: false, wantEqualInput: true,
  },
  {
    name: 'genui-plus-url-no-slot-steal',       // genui + 真 url 占位同轮:genui 剥掉,url 照常回填,genui 不吃 mod 槽
    full: '文档链接：url查看\n\n' + GENUI,
    frames: [
      CREF([{ matched_text: 'url查看', start_idx: 5, safe_urls: [] }, GENUI_REF({ start_idx: 100 })]),
      MOD(REAL),
    ], strip: true,
    wantGRemoved: 1, wantNoGenui: true, wantSub: 1, wantHasReal: true,
    wantExact: '文档链接：[查看](' + REAL + ')',
  },
  {
    name: 'genui-matched-not-in-full-kept',     // matched 前缀 genui 但 full 里没有 → 保守不剥、不吞正文
    full: '完全不含 genui 占位的正常正文。',
    frames: [ CREF([{ matched_text: GENUI, start_idx: 5, safe_urls: [] }]) ], strip: true,
    wantGRemoved: 0, wantEqualInput: true,
  },
  {
    name: 'url-only-untouched-by-genui-strip',  // 纯 url 占位(非 genui):genui-strip 不碰,url 正常回填
    full: '文档链接：url查看',
    frames: [ CREF([{ matched_text: 'url查看', start_idx: 5, safe_urls: [] }]), MOD(REAL) ], strip: true,
    wantGRemoved: 0, wantSub: 1, wantHasReal: true,
    wantExact: '文档链接：[查看](' + REAL + ')',
  },
  {
    name: 'two-genui-both-stripped',            // 一轮两个 genui 占位 → 都剥
    full: 'A' + GENUI + 'B' + GENUI + 'C',
    frames: [ CREF([
      Object.assign({}, GENUI_REF(), { start_idx: 1 }),
      Object.assign({}, GENUI_REF(), { start_idx: 1 }),
    ]) ], strip: true,
    wantGRemoved: 2, wantNoGenui: true, wantExact: 'ABC',
  },
  {
    name: 'genui-uppercase-prefix-stripped',    // 前缀大小写不敏感(GENUI{…})
    full: '正文。GENUI{"x":1}',
    frames: [ CREF([{ matched_text: 'GENUI{"x":1}', start_idx: 3, safe_urls: [] }]) ], strip: true,
    wantGRemoved: 1, wantExact: '正文。',
  },
]

let pass = 0, fail = 0
const fails = []
for (const c of cases) {
  const r = run(c.full, c.frames, c.strip)
  let ok = true, why = `g=${r.gRemoved} sub=${r.subbed}`
  if (c.wantGRemoved !== undefined && r.gRemoved !== c.wantGRemoved) { ok = false; why += ` wantG=${c.wantGRemoved}` }
  if (ok && c.wantSub !== undefined && r.subbed !== c.wantSub) { ok = false; why += ` wantSub=${c.wantSub}` }
  if (ok && c.wantNoGenui) {
    if (r.full.indexOf('genui{') >= 0 || r.full.toLowerCase().indexOf('genui{') >= 0) { ok = false; why += ` GENUI-REMAINS full=${JSON.stringify(r.full)}` }
  }
  if (ok && c.wantNoGenui === false) {
    if (r.full.indexOf(GENUI) < 0) { ok = false; why += ` GENUI-WRONGLY-STRIPPED` }
  }
  if (ok && c.wantHasReal !== undefined) {
    const has = r.full.includes(REAL)
    if (has !== c.wantHasReal) { ok = false; why += ` hasReal=${has} want=${c.wantHasReal}` }
  }
  if (ok && c.wantEqualInput) {
    if (r.full !== c.full) { ok = false; why += ` MUTATED full=${JSON.stringify(r.full)}` }
  }
  if (ok && c.wantExact !== undefined) {
    if (r.full !== c.wantExact) { ok = false; why += ` EXACT-MISMATCH got=${JSON.stringify(r.full)}` }
  }
  if (ok) { pass++ } else { fail++; fails.push({ name: c.name, why }) }
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${c.name} — ${why}`)
}

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO (genui-strip 8 形状:尾部/中段/门控关/不吃url槽/失配保守/纯url不误伤/双genui/大小写)')
