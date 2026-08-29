#!/usr/bin/env node
/* write_dialect_offline_cases.js — ⟦write⟧/⟦replace⟧ 方言翻译层离线单测
 *
 * 背景(2026-08-29 canary-82 E2E 实锤):
 *   资p2 方言宽容层原白名单只 4 个只读方言(ls/glob/read/grep),模型 emit
 *   ⟦write¦path=…¦content=…⟧ / ⟦replace¦path=…¦old=…¦new=…⟧ 时不匹配 →
 *   _v2clean 剥完 ⟦⟧ 后空 → 判 violation → 教学重发 → 换 ⟦cmd¦run⟧ 自愈,+40-70s。
 *
 * 修法(门控 ZK_WRITE_DIALECT 默认关):
 *   Line 1827 三元:开时正则扩到 ls|glob|read|grep|write|replace,关时保原状零行为差。
 *   两条新翻译分支(用 base64 消灭所有 shell quote/heredoc 陷阱):
 *   - ⟦write¦path=P¦content=C⟧  → echo '<b64(C)>' | base64 -d > P
 *   - ⟦replace¦path=P¦old=A¦new=B⟧
 *       → echo '<b64(JSON[p,o,n])>' | base64 -d |
 *         python3 -c "…s=open(p).read();open(p,'w').write(s.replace(o,n))"
 *
 * ⚠️ 施工陷阱(离线单测 S14 抓到):
 *   python 原写法 open(p,'w').write(open(p).read().replace(...)) —— open('w')
 *   先执行截断,.write(...) 参数才求值 → open(p).read() 读到空 → 文件被清空!
 *   必须先 s=open(p).read() 再 open('w'). 教训:离线单测必须包括真跑一遍
 *   (S14/S15 用 child_process.execSync 真起 shell),纯正则断言查不出运行时语义 bug。
 *
 * 用法: node scripts/zk-cursor-web/write_dialect_offline_cases.js [/path/to/responses.js]
 * 全过退出 0,任一 FAIL 退 1。控制组用法:指向未打补丁字节(pre-writedialect.bak) 必须 FAIL S0。
 */
'use strict'
const fs = require('fs')
const SRC = process.argv[2] || '/tmp/resp_edit.js'
const code = fs.readFileSync(SRC, 'utf8')

let pass = 0, fail = 0
const fails = []
function check(name, ok, why) {
  if (ok) { pass++; console.log('  PASS ' + name) }
  else { fail++; fails.push({name, why}); console.log('  FAIL ' + name + (why ? ' — ' + why : '')) }
}

// ── S0 anchor 存在(控制组在此 FAIL) ────────────────────────
const anchor = code.includes("_dlAll = process.env.ZK_WRITE_DIALECT === '1'")
check('S0 门控 _dlAll 已注入', anchor, 'expected new gate variable')
if (!anchor) { console.log(`\n${pass} pass / ${fail} fail`); process.exit(1) }

// ── S1 门控关时正则不含 write/replace ─────────────────────
const gatedOff = /_dlAll\s*\n\s*\?\s*\/⟦\(ls\|glob\|read\|grep\|write\|replace\)\(\(\?:¦\[\^⟧\]\*\)\?\)⟧\/g\s*\n\s*:\s*\/⟦\(ls\|glob\|read\|grep\)\(\(\?:¦\[\^⟧\]\*\)\?\)⟧\/g/
check('S1 三元正则双臂正确', gatedOff.test(code), 'gate on/off regex not both present')

// ── S2 write/replace 分支存在,基于 base64 ─────────────────
check('S2a write 分支存在', /_kind === 'write' && _kv\.path && _kv\.content !== undefined/.test(code))
check('S2b write 用 Buffer.from ... base64', /Buffer\.from\(String\(_kv\.content\)\)\.toString\('base64'\)/.test(code))
check('S2c write 生成 echo|base64 -d > path', /`echo '\$\{_b64w\}' \| base64 -d > \$\{_q\(_kv\.path\)\}`/.test(code))
check('S2d replace 分支存在', /_kind === 'replace' && _kv\.path && _kv\.old !== undefined && _kv\.new !== undefined/.test(code))
check('S2e replace 用 JSON+base64 argv', /JSON\.stringify\(_rpArgv\)\)\.toString\('base64'\)/.test(code))
check('S2f replace 生成 python3 -c 先读后写', /base64 -d \| python3 -c "import json,sys;p,o,n=json\.loads\(sys\.stdin\.read\(\)\);s=open\(p\)\.read\(\);open\(p,'w'\)\.write\(s\.replace\(o,n\)\)"/.test(code))

// ── S3 缺参数分支跳过 (else { continue }) ─────────────────
check('S3 未识别 kind 或缺参数走 continue', /\} else \{ continue \}/.test(code))

// ── S4 抠出方言循环,eval,行为等价性测试 ──────────────────
const bloc = code.match(/const _dlAll = process\.env\.ZK_WRITE_DIALECT[\s\S]*?_dlKinds\.push\(_kind\)/)
check('S4 方言块可抠', !!bloc)
if (!bloc) { console.log(`\n${pass} pass / ${fail} fail`); process.exit(1) }

// 手写 eval 环境(模拟外层 while 循环) —— 与线上分支逐字同构
function runDialect(full, gateOn) {
  const _dlAll = gateOn
  const _dlRe = _dlAll
    ? /⟦(ls|glob|read|grep|write|replace)((?:¦[^⟧]*)?)⟧/g
    : /⟦(ls|glob|read|grep)((?:¦[^⟧]*)?)⟧/g
  const _dlCmds = []
  const _dlKinds = []
  let _dlM
  while ((_dlM = _dlRe.exec(full || '')) && _dlCmds.length < 8) {
    const _kind = _dlM[1]
    const _kv = {}
    for (const _seg of (_dlM[2] || '').split('¦')) {
      if (!_seg) continue
      const _eq = _seg.indexOf('=')
      if (_eq > 0) _kv[_seg.slice(0, _eq)] = _seg.slice(_eq + 1)
    }
    const _q = (s) => `'${String(s || '').replace(/'/g, `'\\''`)}'`
    const _max = (d) => { const n = parseInt(_kv.max, 10); return n > 0 ? n : d }
    if (_kind === 'ls' && _kv.path) {
      _dlCmds.push(`echo "── ls ${_kv.path} ──"; ls -la -- ${_q(_kv.path)}`)
    } else if (_kind === 'read' && _kv.path) {
      _dlCmds.push(`echo "── read ${_kv.path} ──"; sed -n '1,250p' -- ${_q(_kv.path)}`)
    } else if (_kind === 'grep' && _kv.pattern) {
      _dlCmds.push(`echo "── grep ${_kv.pattern} ──"; grep -rn -e ${_q(_kv.pattern)} -- ${_q(_kv.path || '.')} 2>/dev/null | head -n ${_max(100)}`)
    } else if (_kind === 'glob' && _kv.pattern) {
      const _pat = String(_kv.pattern)
      const _meta = _pat.search(/[*?{]/)
      const _base = _meta > 0 ? (_pat.slice(0, _meta).replace(/\/[^/]*$/, '') || '.') : _pat
      const _last = _pat.slice(_pat.lastIndexOf('/') + 1)
      const _nameF = /^\*\.[A-Za-z0-9_]+$/.test(_last) ? ` -name ${_q(_last)}` : ''
      _dlCmds.push(`echo "── glob ${_pat} ──"; find ${_base.includes('{') ? _base : _q(_base)} -type f${_nameF} 2>/dev/null | head -n ${_max(200)}`)
    } else if (_kind === 'write' && _kv.path && _kv.content !== undefined) {
      const _b64w = Buffer.from(String(_kv.content)).toString('base64')
      _dlCmds.push(`echo '${_b64w}' | base64 -d > ${_q(_kv.path)}`)
    } else if (_kind === 'replace' && _kv.path && _kv.old !== undefined && _kv.new !== undefined) {
      const _rpArgv = [String(_kv.path), String(_kv.old), String(_kv.new)]
      const _b64r = Buffer.from(JSON.stringify(_rpArgv)).toString('base64')
      _dlCmds.push(`echo '${_b64r}' | base64 -d | python3 -c "import json,sys;p,o,n=json.loads(sys.stdin.read());s=open(p).read();open(p,'w').write(s.replace(o,n))"`)
    } else { continue }
    _dlKinds.push(_kind)
  }
  return { _dlCmds, _dlKinds }
}

// ── S5 门控关 → write 被 dlRe 不匹配 → 空翻译 ────────────
{
  const r = runDialect("⟦write¦path=/tmp/a.txt¦content=hi⟧", false)
  check('S5 门控关时 ⟦write⟧ 零匹配', r._dlCmds.length === 0 && r._dlKinds.length === 0)
}

// ── S6 门控开 → write 正确翻译 base64 + path ──────────────
{
  const r = runDialect("⟦write¦path=/tmp/a.txt¦content=hello world⟧", true)
  check('S6a 门控开 write 翻译', r._dlKinds.length === 1 && r._dlKinds[0] === 'write')
  const cmd = r._dlCmds[0]
  check('S6b write cmd 含 base64 -d', cmd.includes("| base64 -d > '/tmp/a.txt'"))
  const b64 = cmd.match(/echo '([^']+)'/)[1]
  check('S6c base64 解码 == content', Buffer.from(b64, 'base64').toString() === 'hello world')
}

// ── S7 write content 含单引号/换行/特殊 → base64 消化 ────
{
  const evil = "line1\nline2 with 'single' quotes\n\"double\"\n$var\n``backtick``"
  const r = runDialect(`⟦write¦path=/tmp/evil.txt¦content=${evil}⟧`, true)
  const b64 = r._dlCmds[0].match(/echo '([^']+)'/)[1]
  check('S7 特殊字符 base64 精确复原', Buffer.from(b64, 'base64').toString() === evil)
}

// ── S8 replace 三参齐 → python3 argv base64 ───────────────
{
  const r = runDialect("⟦replace¦path=/tmp/b.py¦old=range(1, n)¦new=range(1, n+1)⟧", true)
  check('S8a replace 翻译', r._dlKinds[0] === 'replace')
  const cmd = r._dlCmds[0]
  check('S8b replace cmd 先读后写', cmd.includes("s=open(p).read();open(p,'w').write(s.replace(o,n))"))
  const b64 = cmd.match(/echo '([^']+)'/)[1]
  const argv = JSON.parse(Buffer.from(b64, 'base64').toString())
  check('S8c argv 三元逐字保', argv[0] === '/tmp/b.py' && argv[1] === 'range(1, n)' && argv[2] === 'range(1, n+1)')
}

// ── S9 缺参数分支跳过 ─────────────────────────────────────
{
  check('S9a write 缺 content 跳过', runDialect("⟦write¦path=/tmp/a⟧", true)._dlCmds.length === 0)
  check('S9b write 缺 path 跳过', runDialect("⟦write¦content=hi⟧", true)._dlCmds.length === 0)
  check('S9c replace 缺 new 跳过', runDialect("⟦replace¦path=/tmp/a¦old=x⟧", true)._dlCmds.length === 0)
}

// ── S10 与既有 ls/glob/read/grep 共存(混用一起翻译) ──────
{
  const mixed = "⟦ls¦path=/tmp⟧prose⟦write¦path=/tmp/out.txt¦content=x⟧⟦read¦path=/tmp/out.txt⟧"
  const r = runDialect(mixed, true)
  check('S10a 混用三 kind 全翻译', r._dlKinds.length === 3 && r._dlKinds.join(',') === 'ls,write,read')
}

// ── S11 门控关时既有 ls/glob/read/grep 行为不变 ────────────
{
  const r = runDialect("⟦ls¦path=/tmp⟧", false)
  check('S11 门控关既有只读族仍翻译', r._dlKinds.length === 1 && r._dlKinds[0] === 'ls')
}

// ── S12 空 content 也允许(!== undefined 判据),生成空文件 ──
{
  const r = runDialect("⟦write¦path=/tmp/empty.txt¦content=⟧", true)
  check('S12a 空 content 允许', r._dlKinds[0] === 'write')
  const b64m = r._dlCmds[0].match(/echo '([^']*)'/)
  const b64 = b64m ? b64m[1] : ''
  check('S12b 空 content base64 解为空', Buffer.from(b64, 'base64').toString() === '')
}

// ── S13 8 块上限 ─────────────────────────────────────────
{
  const many = Array(12).fill("⟦write¦path=/tmp/x¦content=y⟧").join('')
  const r = runDialect(many, true)
  check('S13 8 块上限截断', r._dlCmds.length === 8)
}

// ── S14 replace shell round-trip 真跑(捕 open('w')/read 顺序 bug) ─────
{
  const { execSync } = require('child_process')
  const os = require('os'), path = require('path'), fs = require('fs')
  const tmp = path.join(os.tmpdir(), 'wdt_s14_' + Date.now() + '.txt')
  fs.writeFileSync(tmp, 'foo bar baz\n')
  const r = runDialect(`⟦replace¦path=${tmp}¦old=bar¦new=QUUX⟧`, true)
  try { execSync(r._dlCmds[0], { stdio: 'pipe' }) } catch (e) {}
  const after = fs.readFileSync(tmp, 'utf8')
  check('S14 replace 真跑后内容正确(读→改→写顺序)', after === 'foo QUUX baz\n', 'got ' + JSON.stringify(after))
  fs.unlinkSync(tmp)
}

// ── S15 write shell round-trip 真跑(特殊字符) ───────────────────────
{
  const { execSync } = require('child_process')
  const os = require('os'), path = require('path'), fs = require('fs')
  const tmp = path.join(os.tmpdir(), 'wdt_s15_' + Date.now() + '.txt')
  const evil = "line1\nline2 with 'quotes' \"double\"\n$var\ndone"
  const r = runDialect(`⟦write¦path=${tmp}¦content=${evil}⟧`, true)
  try { execSync(r._dlCmds[0], { stdio: 'pipe' }) } catch (e) {}
  const after = fs.readFileSync(tmp, 'utf8')
  check('S15 write 真跑特殊字符 round-trip', after === evil, 'got ' + JSON.stringify(after))
  fs.unlinkSync(tmp)
}

console.log(`\n${pass} pass / ${fail} fail`)
if (fail) { console.log(JSON.stringify(fails, null, 2)); process.exit(1) }
