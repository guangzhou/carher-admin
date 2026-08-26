#!/usr/bin/env node
/* v2_offline_cases.js — ZK_PROTO_V2 三态状态机离线单测(病例库)。
 *
 * 不重实现:从 responses.js 里逐字抠出 V2_RUN_RE / V2_REFY_RE 常量 eval 出来,
 * 再把状态机的判定逻辑(有块/无块/空;printf 自答拆解;正文剥块)照搬一份 classify(),
 * 喂四形状病例,断言分类正确 + wrapup 类零指涉词。纯离线、确定性、零上游。
 *
 * 用法: node scripts/zk-cursor-web/v2_offline_cases.js [/path/to/responses.js]
 * 默认读 /tmp/responses_live_20260826.js。全过退出码 0,任一失败退 1。
 */
'use strict'
const fs = require('fs')

const SRC = process.argv[2] || '/tmp/responses_live_20260826.js'
const code = fs.readFileSync(SRC, 'utf8')

// —— 从源文件抠出契约正则(逐字,保证测的是上线那份)——
function grab(name) {
  const m = new RegExp('const\\s+' + name + '\\s*=\\s*(/[\\s\\S]*?/[a-z]*)\\n').exec(code)
  if (!m) throw new Error('cannot find ' + name + ' in ' + SRC)
  // eslint-disable-next-line no-eval
  return eval(m[1])
}
const V2_RUN_RE = grab('V2_RUN_RE')
const V2_REFY_RE = grab('V2_REFY_RE')

// —— 状态机判定(照搬 responses.js proto2 分支的逻辑)——
function translateDialect(full) {
  // 方言宽容层(2026-08-27):⟦ls/glob/read/grep⟧ 确定性转译成只读 shell(照搬上线逻辑)
  const re = /⟦(ls|glob|read|grep)((?:¦[^⟧]*)?)⟧/g
  const cmds = []; const kinds = []
  let m
  while ((m = re.exec(full || '')) && cmds.length < 8) {
    const kind = m[1]; const kv = {}
    for (const seg of (m[2] || '').split('¦')) {
      if (!seg) continue
      const eq = seg.indexOf('=')
      if (eq > 0) kv[seg.slice(0, eq)] = seg.slice(eq + 1)
    }
    const q = (s) => `'${String(s || '').replace(/'/g, `'\\''`)}'`
    const max = (d) => { const n = parseInt(kv.max, 10); return n > 0 ? n : d }
    if (kind === 'ls' && kv.path) cmds.push(`echo "── ls ${kv.path} ──"; ls -la -- ${q(kv.path)}`)
    else if (kind === 'read' && kv.path) cmds.push(`echo "── read ${kv.path} ──"; sed -n '1,250p' -- ${q(kv.path)}`)
    else if (kind === 'grep' && kv.pattern) cmds.push(`echo "── grep ${kv.pattern} ──"; grep -rn -e ${q(kv.pattern)} -- ${q(kv.path || '.')} 2>/dev/null | head -n ${max(100)}`)
    else if (kind === 'glob' && kv.pattern) {
      const pat = String(kv.pattern)
      const meta = pat.search(/[*?{]/)
      const base = meta > 0 ? (pat.slice(0, meta).replace(/\/[^/]*$/, '') || '.') : pat
      const last = pat.slice(pat.lastIndexOf('/') + 1)
      const nameF = /^\*\.[A-Za-z0-9_]+$/.test(last) ? ` -name ${q(last)}` : ''
      cmds.push(`echo "── glob ${pat} ──"; find ${base.includes('{') ? base : q(base)} -type f${nameF} 2>/dev/null | head -n ${max(200)}`)
    } else continue
    kinds.push(kind)
  }
  return cmds.length ? { cmd: cmds.join('; '), kinds } : null
}

function classify(full) {
  const m = V2_RUN_RE.exec(full || '')
  // prose = 剥 cmd 块 + 防御性剥掉任何残留 ⟦…⟧ 方言块(与上线逐字一致)
  const prose = (full || '').replace(V2_RUN_RE, '').replace(/⟦[\s\S]*?⟧/g, '').trim()
  if (m && m[1] && m[1].trim()) {
    const cmd = m[1].trim()
    let self = ''
    // 收紧(2026-08-27):引号外有重定向/管道/串联 → 真命令,不拆自答(照搬 responses.js proto2)
    const bare = cmd.replace(/'[^']*'|"[^"]*"/g, '')
    const redir = /[;|]|>>?|<<?|&&/.test(bare)
    if (!redir && /^(?:printf|echo)\b/.test(cmd)) {
      const qre = /'([^']*)'|"([^"]*)"/g; let qm
      while ((qm = qre.exec(cmd))) { const s = (qm[1] !== undefined ? qm[1] : qm[2]) || ''; if (s.length > self.length) self = s }
      self = self.replace(/%s|\\n/g, ' ').trim()
    }
    if (self.length >= 8) return { verdict: 'complete-prose', prose: self, cmd: null }
    return { verdict: 'complete-run', prose, cmd }
  }
  // 方言宽容层(照搬上线顺序:run 块之后、prose 之前)
  const dl = translateDialect(full)
  if (dl) return { verdict: 'complete-run', prose, cmd: dl.cmd, dialect: dl.kinds }
  if (prose) return { verdict: 'complete-prose', prose, cmd: null }
  // ask 转译(照搬上线:prose 空时问题文本即正文)
  const askM = /⟦ask¦question=([^⟧]+)⟧/.exec(full || '')
  if (askM && askM[1].trim()) return { verdict: 'complete-prose', prose: askM[1].trim(), cmd: null }
  return { verdict: 'violation', prose: '', cmd: null }
}

// —— 病例库(四形状)——
const RUN = (c) => '\u27E6cmd\u00A6run=' + c + '\u27E7'
const cases = [
  // greet: 纯正文,无块 → complete-prose
  { name: 'greet-hi', full: '你好!有什么可以帮你的吗?', want: 'complete-prose', wrapup: false },
  { name: 'greet-zh', full: '早上好,今天想做点什么?', want: 'complete-prose', wrapup: false },
  // knowledge: 长正文讲解,无块 → complete-prose
  { name: 'know-qsort', full: '快速排序是一种分治排序算法。选一个基准元素,把数组分成比它小和比它大的两部分,再递归排序两部分。平均时间复杂度 O(n log n),最坏 O(n^2)。', want: 'complete-prose', wrapup: false },
  // task: 简述 + run 块 → complete-run(命令含 ls)
  { name: 'task-ls', full: '我看一下当前目录。' + RUN('ls -la'), want: 'complete-run', wrapup: false, cmdHas: 'ls' },
  { name: 'task-ls2', full: '列出文件:' + RUN('ls'), want: 'complete-run', wrapup: false, cmdHas: 'ls' },
  { name: 'task-find', full: '查找一下。' + RUN('find . -name "*.py"'), want: 'complete-run', wrapup: false, cmdHas: 'find' },
  // wrapup: 大结果回灌后收尾,自包含正文,无块,零指涉词 → complete-prose
  { name: 'wrapup-selfcontained', full: '当前目录有 3 个文件:src/main.py(2048 字节)、docs/readme.md(512 字节)、scripts/run.sh(128 字节),共 2688 字节。', want: 'complete-prose', wrapup: true },
  // wrapup 反例:指涉词(必须被 refy 检出,不是分类失败而是质量红线)
  { name: 'wrapup-refy', full: '文件列表如上所示,请查看。', want: 'complete-prose', wrapup: true, expectRefy: true },
  // printf 自答型:模型用终端"说"答案 → 拆正文,不执行 → complete-prose
  { name: 'selfans-printf', full: '我算一下。' + RUN("printf '快速排序平均复杂度是 O(n log n)'"), want: 'complete-prose', wrapup: false },
  // 收紧实锤(08-26 lark-doc 僵死根因):printf/echo 带重定向=真·文件写入 → 必须执行 → complete-run
  { name: 'selfans-printf-redirect', full: '写草稿。' + RUN("printf '<title>测试</title><p>正文内容占位够长</p>' > draft_c9f20c70_folder/draft.xml"), want: 'complete-run', wrapup: false, cmdHas: 'draft.xml' },
  { name: 'selfans-echo-append', full: '追加。' + RUN("echo '一行内容够长了吧' >> notes.txt"), want: 'complete-run', wrapup: false, cmdHas: 'notes.txt' },
  { name: 'selfans-echo-pipe', full: '管道。' + RUN("echo '内容够长可管道处理' | tee out.txt"), want: 'complete-run', wrapup: false, cmdHas: 'tee' },
  // 边界:重定向符在引号内 → 不是真重定向 → 仍按自答拆正文(验证 bare 去引号判定)
  { name: 'selfans-quoted-gt', full: '说明。' + RUN("echo '若 a > b 则交换两者位置'"), want: 'complete-prose', wrapup: false },
  // violation: 空 → violation
  { name: 'violation-empty', full: '', want: 'violation', wrapup: false },
  { name: 'violation-ws', full: '   \n  ', want: 'violation', wrapup: false },
  // ⟦ask⟧ 泄漏防线(2026-08-26 canary 实锤):账号级方言不可译,绝不能当正文漏出去。
  //   混合(prose + ⟦ask⟧) → 剥块后仍有真正文 → complete-prose,且正文里无 ⟦
  { name: 'ask-mixed', full: '我需要目录路径。' + RUN('').replace('cmd¦run=', 'ask¦question=请给出路径'), want: 'complete-prose', wrapup: false, noLeak: true },
  //   纯 ⟦ask⟧(无真正文) → ask 转译:问题文本即正文交付(2026-08-27 起,原为 violation 重发)
  { name: 'ask-only', full: '⟦ask¦question=请提供当前目录的绝对路径⟧', want: 'complete-prose', wrapup: false, noLeak: true, proseHas: '绝对路径' },
  // 方言宽容层(2026-08-27 复杂任务僵死实锤:⟦ls/glob/read⟧ 被剥成空→violation→死屏,现转译执行)
  { name: 'dialect-ls-glob', full: '⟦ls¦path=/Users/x/codes/app⟧\n⟦glob¦pattern=/Users/x/codes/app/backend/**/*¦max=120⟧', want: 'complete-run', wrapup: false, cmdHas: 'find', cmdHas2: 'ls -la', noLeak: true },
  { name: 'dialect-read', full: '⟦read¦path=/Users/x/codes/app/README.md⟧', want: 'complete-run', wrapup: false, cmdHas: 'sed -n', noLeak: true },
  { name: 'dialect-with-prose', full: '我先看下工程结构。⟦ls¦path=/Users/x/codes/app⟧', want: 'complete-run', wrapup: false, cmdHas: 'ls -la', proseHas: '工程结构', noLeak: true },
  { name: 'dialect-glob-braces', full: '⟦glob¦pattern=/Users/x/{backend,frontend}/**/*¦max=50⟧', want: 'complete-run', wrapup: false, cmdHas: '{backend,frontend}', noLeak: true },
  { name: 'dialect-glob-ext', full: '⟦glob¦pattern=/Users/x/app/**/*.py¦max=100⟧', want: 'complete-run', wrapup: false, cmdHas: "-name '*.py'", noLeak: true },
  { name: 'dialect-grep', full: '⟦grep¦pattern=configHash¦path=/Users/x/app¦max=40⟧', want: 'complete-run', wrapup: false, cmdHas: 'grep -rn', noLeak: true },
]

let pass = 0, fail = 0
const fails = []
for (const c of cases) {
  const r = classify(c.full)
  let ok = r.verdict === c.want
  let why = `verdict=${r.verdict} want=${c.want}`
  if (ok && c.cmdHas) { ok = (r.cmd || '').includes(c.cmdHas); why += ` cmd=${JSON.stringify((r.cmd || '').slice(0, 80))}` }
  if (ok && c.cmdHas2) { ok = (r.cmd || '').includes(c.cmdHas2); if (!ok) why += ` MISSING:${c.cmdHas2}` }
  if (ok && c.proseHas) { ok = (r.prose || '').includes(c.proseHas); if (!ok) why += ` prose=${JSON.stringify(r.prose)}` }
  // 泄漏防线:交付正文里绝不能残留 ⟦ 方言块字符
  if (ok && c.noLeak) {
    const leaked = (r.prose || '').includes('⟦') || (r.prose || '').includes('⟧')
    if (leaked) { ok = false; why += ` LEAKED-bracket` } else why += ` noLeak=ok`
  }
  // wrapup 类:断言正文里的指涉词能被 V2_REFY_RE 正确检出/不误检
  if (ok && c.wrapup) {
    const refy = V2_REFY_RE.test(r.prose)
    const wantRefy = !!c.expectRefy
    if (refy !== wantRefy) { ok = false; why += ` refy=${refy} wantRefy=${wantRefy}` }
    else why += ` refy=${refy}`
  }
  if (ok) { pass++ } else { fail++; fails.push({ name: c.name, why }) }
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${c.name} — ${why}`)
}

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO (四形状分类正确 + wrapup 指涉词检出)')
