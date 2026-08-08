// bpi-codex.js — 把网页模型吐出的 BPI 块编译成 Codex 的 exec 工具调用。
//
// 为什么需要这个（2026-08-07 实测链条）
// ------------------------------------
// 1. Codex 的 responses-lite 线路把工具塞在 `input` 的 `additional_tools` 项里，
//    顶层 `tools` 不存在（codex-rs/core/src/client.rs:869）。zerokey 的
//    responses.js 只读顶层 -> 读到空 -> 一个字的工具都没注入。
// 2. 模型手里真的没有工具，于是如实回答「我没有本地文件写入能力」。
//    实测：CLI->zerokey 拒答 4/5，同一份载荷 Desktop->acct 工具调用 5/5。
// 3. 之前的补丁反着来：`Use your shell to accomplish the task below` 把模型指向
//    它**自己**的沙箱，等于坐实了"我碰不到你的机器"。实测拒答 5/5。
//
// 真正的解法不是在用户消息里"劝"模型，而是把 ChatGPT 的**消费级外壳**换掉：
// 通过 `PATCH /backend-api/user_system_messages` 把 agent 契约写进账号级自定义
// 指令。实测（同一 pod、同一句话、消息里零契约）：
//
//     账号指令为空  -> 拒答 5/5
//     写入 BPI 契约 -> BPI 块 5/5，零拒答
//     未改的对照号  -> 拒答 5/5
//
// 契约生效后模型吐的是 `⟦write¦path=…¦content=…⟧` 这类块，本模块负责把它编译成
// Codex 认得的 `custom_tool_call(exec)`。
//
// exec 的契约（读 additional_tools 里 exec 的 description 原文，非推测）
// --------------------------------------------------------------------
// * 接受**原始 JavaScript 源文本**（不是 JSON、不是代码围栏），语法是
//   `SOURCE: /[\s\S]+/`，放行一切。
// * 子工具挂在全局 `tools` 上，实测该请求暴露：
//     tools.exec_command({cmd: string})   // shell
//     tools.apply_patch(input: string)    // 文件增删改
//   （另有 update_plan / view_image / web__run 等，本模块暂不用。）
//
// 安全性：本模块**只在响应里出现 BPI 块时**才动手。没写账号级契约的号根本不会
// 吐这种块，那些请求原样透传 —— 所以可以先全量部署本模块，再逐号开契约。

'use strict'

const OPEN = '⟦'   // ⟦
const CLOSE = '⟧'  // ⟧
const SEP = '¦'    // ¦

// 只编译"会产生副作用/需要真机执行"的块。ask/todos 这类纯交互块留给文本层，
// 硬翻成 shell 反而会骗客户端执行奇怪的东西。
const EXECUTABLE = new Set(['read', 'write', 'replace', 'ls', 'mkdir', 'glob', 'grep', 'cmd'])

function shq(s) {
  // POSIX 单引号转义：把 ' 换成 '\''
  return "'" + String(s).replace(/'/g, "'\\''") + "'"
}

function jsStr(s) {
  return JSON.stringify(String(s))
}

/** 解析一个 BPI 块体（不含首尾括号）成 {name, params}。 */
function parseBlock(body) {
  const parts = body.split(SEP)
  const name = parts.shift().trim()
  const params = {}
  // 重复键：BPI 用 `¦option=A¦option=B` 表达多个选项，直接对象赋值会后者覆盖前者
  // （2026-08-07 被单测抓到：两个 option 只剩一个，request_user_input 的 schema
  // 要求 2-3 个，于是 options 整个被丢掉）。所以既留"最后一个"给普通参数用，
  // 也把全部值收进 _multi 供 ask 这类需要重复键的块使用。
  const multi = {}
  for (const p of parts) {
    const i = p.indexOf('=')
    if (i < 0) continue
    const k = p.slice(0, i).trim()
    const v = p.slice(i + 1)
    params[k] = v
    ;(multi[k] = multi[k] || []).push(v)
  }
  params._multi = multi
  return { name, params }
}

/** 从整段文本里抽出所有 BPI 块。返回 [{name, params, raw}]。 */
function extractBlocks(text) {
  const out = []
  if (typeof text !== 'string' || text.indexOf(OPEN) < 0) return out
  let i = 0
  while (true) {
    const s = text.indexOf(OPEN, i)
    if (s < 0) break
    // 反引号包裹的是示例，不是调用（上游 2bfc012d 同款约定）
    if (s > 0 && text[s - 1] === '`') { i = s + 1; continue }
    const e = text.indexOf(CLOSE, s + 1)
    if (e < 0) break
    const body = text.slice(s + 1, e)
    const blk = parseBlock(body)
    if (blk.name) { blk.raw = text.slice(s, e + 1); out.push(blk) }
    i = e + 1
  }
  return out
}

/** 生成 apply_patch 的补丁信封。`content` 的每一行前缀 '+'。 */
function addFilePatch(path, content) {
  const lines = String(content).split('\n').map((l) => '+' + l)
  return ['*** Begin Patch', '*** Add File: ' + path, ...lines, '*** End Patch'].join('\n')
}

function updateFilePatch(path, oldStr, newStr) {
  const del = String(oldStr).split('\n').map((l) => '-' + l)
  const add = String(newStr).split('\n').map((l) => '+' + l)
  return ['*** Begin Patch', '*** Update File: ' + path, '@@', ...del, ...add, '*** End Patch'].join('\n')
}

/** 单个 BPI 块 -> 一行 JS。不认识的返回 null。 */
function blockToJs(blk) {
  const p = blk.params
  switch (blk.name) {
    case 'cmd':
      if (!p.run) return null
      return `await tools.exec_command({cmd: ${jsStr(p.run)}})`
    case 'ls':
      if (!p.path) return null
      return `await tools.exec_command({cmd: ${jsStr('ls -la ' + shq(p.path))}})`
    case 'mkdir':
      if (!p.path) return null
      return `await tools.exec_command({cmd: ${jsStr('mkdir -p ' + shq(p.path))}})`
    case 'read': {
      if (!p.path) return null
      const from = parseInt(p.from, 10)
      const to = parseInt(p.to, 10)
      const cmd = Number.isFinite(from) && Number.isFinite(to)
        ? `sed -n ${from},${to}p ${shq(p.path)}`
        : `cat ${shq(p.path)}`
      return `await tools.exec_command({cmd: ${jsStr(cmd)}})`
    }
    case 'glob': {
      if (!p.pattern) return null
      const max = parseInt(p.max, 10)
      const n = Number.isFinite(max) ? max : 200
      return `await tools.exec_command({cmd: ${jsStr(`ls -1d ${p.pattern} 2>/dev/null | head -n ${n}`)}})`
    }
    case 'grep': {
      const q = p.queryR || p.query
      if (!q) return null
      const max = parseInt(p.max, 10)
      const n = Number.isFinite(max) ? max : 200
      const glob = p.glob ? ` --glob ${shq(p.glob)}` : ''
      const flag = p.queryR ? '' : ' -F'
      return `await tools.exec_command({cmd: ${jsStr(`rg -n${flag}${glob} -- ${shq(q)} | head -n ${n}`)}})`
    }
    case 'write':
      if (!p.path || p.content === undefined) return null
      return `await tools.apply_patch(${jsStr(addFilePatch(p.path, p.content))})`
    case 'replace':
      if (!p.path || p.old === undefined || p.new === undefined) return null
      return `await tools.apply_patch(${jsStr(updateFilePatch(p.path, p.old, p.new))})`
    default:
      return null
  }
}

/**
 * 把一段模型输出编译成 Codex 的 exec 调用。
 *
 * 返回 { js, blocks, leftover }：
 *   js       - 要塞进 custom_tool_call.input 的 JavaScript；无可执行块时为 null
 *   blocks   - 命中的块（供日志/计数）
 *   leftover - 去掉已编译块之后剩下的文本（通常为空；非空说明模型混了说明文字，
 *              按上游 output_contract 那是违约，但我们保留下来当普通文本回传）
 */
function compileToExec(text) {
  const blocks = extractBlocks(text).filter((b) => EXECUTABLE.has(b.name))
  if (!blocks.length) return { js: null, blocks: [], leftover: text }
  const lines = []
  for (const b of blocks) {
    const js = blockToJs(b)
    if (js) lines.push(`text(${jsStr('BPI(' + b.name + '):')});`, js.replace(/^await /, 'text(await ') + ');')
  }
  if (!lines.length) return { js: null, blocks: [], leftover: text }
  let leftover = text
  for (const b of blocks) leftover = leftover.split(b.raw).join('')
  return { js: lines.join('\n'), blocks, leftover: leftover.trim() }
}

/**
 * ⟦ask⟧ -> Codex 原生 request_user_input 的 function_call。
 *
 * 之前 ask 被排除在编译之外，结果整块 `⟦ask¦question=…⟧` 原样流给用户当文本看
 * （2026-08-07 用户实测第 18 行）。Codex 的 additional_tools 里本来就声明了
 * request_user_input，映射过去就是原生提问 UI。
 * schema（读声明原文）：questions[] 里每项 {id, header(<=12字), question, options[]}。
 */
function askToFunctionCall(blk) {
  const q = blk.params.question
  if (!q) return null
  const opts = ((blk.params._multi && blk.params._multi.option) || [])
    .map((v) => ({ label: String(v).slice(0, 60) }))
  const questions = [{
    id: 'bpi_ask',
    header: '需要确认',
    question: String(q),
    ...(opts.length >= 2 ? { options: opts.slice(0, 3) } : {}),
  }]
  return { name: 'request_user_input', arguments: { questions } }
}

/** 从整段文本里抽第一个 ask 块（若有）。 */
function firstAsk(text) {
  const b = extractBlocks(text).find((x) => x.name === 'ask')
  return b ? askToFunctionCall(b) : null
}

module.exports = { compileToExec, extractBlocks, blockToJs, addFilePatch, updateFilePatch,
                   askToFunctionCall, firstAsk, OPEN, CLOSE, SEP }


// ── 入站：把 Codex 形状的请求改造成网页模型能接住的样子 ──────────────
//
// 2026-08-07 用用户真实 82KB 载荷做的 A/B（每组 n>=3，判据=有没有出
// custom_tool_call）：
//
//     A 原样                                  1/3   拒答 2/3
//     B 只在末尾交手                          2/3   拒答 1/3
//     C 去掉 Codex 的 developer 指令 + 交手   5/6   拒答 0      ← 采用
//     D 只去指令、不交手                      1/3   拒答 2/3
//
// 两件事缺一不可：
//  1. **剥掉 developer 消息**。那 22KB 是 Codex 写给**真 codex 后端**的运行环境
//     说明（sandbox_mode / 审批策略 / 它自己的工具），喂给网页会话等于告诉模型
//     "你在另一个环境里"，它就会去伸自己的代码解释器，然后如实报告
//     「运行环境中该路径不可用，命令未执行成功」。
//     AGENTS.md 是 **user** 角色，不受影响，仍然保留。
//  2. **本轮明确交手**。账号级契约给的是通用词表，但没说"你现在连着哪台机器、
//     工作目录在哪"。补一句就够。

const HANDS_HEAD = '[本轮可用的手]'
const RESULT_HEAD = '[上一步执行结果]'

function extractCwd(items) {
  // AGENTS.md 那条 user 消息的抬头形如
  // "# AGENTS.md instructions for /Users/xxx/codes/repo"
  for (const it of items || []) {
    if (!it || it.type === 'additional_tools') continue
    const parts = it.content
    if (!Array.isArray(parts)) continue
    for (const p of parts) {
      const t = (p && p.text) || ''
      const m = t.match(/AGENTS\.md instructions for (\/[^\s\n]+)/)
      if (m) return m[1]
    }
  }
  return null
}

// 两种情形必须分开说。
// 2026-08-07 用户实测：第一版只写了"要动手时只输出块本身，不要解释"，模型把这条
// 也套到了**拿到结果之后**——`ll` 那轮工具跑了、目录列出来了，模型却回一句
// 「请下达具体任务」，闷着不肯把结果讲给用户。而换成更明确的自然语言提问
// （"罗列下本地的文件夹"）就正常。所以要显式区分"还没动手"和"已有结果"。
function handsBlock(cwd) {
  return [
    HANDS_HEAD,
    '你现在连着用户那台机器。下面这些块由一个外部执行器真实执行 —— 不是示例，',
    '也不是你自己的沙箱；不要用内建的代码解释器去试，那里没有用户的文件。',
    '⟦ls¦path={abs}⟧ ⟦read¦path={abs}⟧ ⟦write¦path={abs}¦content={str}⟧ ⟦cmd¦run={shell}⟧',
    cwd ? `当前工作目录 ${cwd}。` : '',
    '两种情形，选一种：',
    '1) 还需要动手 —— 只输出块本身，不要解释，不要声称自己没有权限。',
    '2) 上面已经出现 “' + RESULT_HEAD + '” —— 说明活已经干完了，'
      + '**直接用那些结果回答用户**（比如把目录列表整理出来）。',
    '   此时不要再输出块，也不要反问用户要干什么。',
    '像 `ll`、`ls`、`pwd` 这种简写就是让你执行它，不是让你解释它是什么命令。',
  ].filter(Boolean).join('\n')
}

/** 是不是 Codex 的 responses-lite 载荷（工具塞在 input 里）。 */
function isCodexLite(items) {
  return Array.isArray(items) && items.some((i) => i && i.type === 'additional_tools')
}

/**
 * 返回改造后的 input；非 Codex 载荷原样返回（其它客户端零影响）。
 */
// 只剥"描述运行环境"的那条 developer 消息，**保留把模型配置成 agent 的那条**。
//
// 2026-08-07 更正：第一版把 developer 消息一刀全剥了，理由是"它们描述的是另一个
// 环境"。这是拿 3 个样本做的决定，太草率 —— 那批消息里第一条 17.7KB 是
// "You are Codex, an agent based on GPT-5…"，正是把大脑配置成 agent 的系统提示
// 词，剥掉等于自断一臂。真正该剥的只有 22KB 那条 `<permissions instructions>`：
// 它讲的是 sandbox_mode / 审批升级 / `sandbox_permissions` 参数，全是真 Codex
// 运行时才存在的东西，喂给网页会话只会逼模型去想"我该在哪个沙箱里执行"，
// 然后如实报告「运行环境中该路径不可用」。
function isEnvironmentPrompt(text) {
  return /^\s*<permissions instructions>/.test(text) || /`sandbox_mode`\s*is/.test(text)
}

function textOf(item) {
  const parts = item && item.content
  // normalize 之后 content 会从数组摊平成字符串，两种都要认
  if (typeof parts === 'string') return parts
  if (!Array.isArray(parts)) return ''
  return parts.map((p) => (p && p.text) || '').join('')
}

function prepareCodexInput(items) {
  if (!isCodexLite(items)) return items
  const cwd = extractCwd(items)
  // 判据只看 role + 内容，**不要求 type==='message'**。
  // 2026-08-07 实测：经 LiteLLM 时 chatgpt_responses_normalize 会把
  // {type:'message', role:'developer'} 改写成 {role:'system'} 并**删掉 type**
  // （它的 _normalize_item 对 _TARGET_ROLES={system,developer} 走 system_message
  // 分支）。第一版按 type+developer 匹配，于是经 LiteLLM 时一条都匹配不上，
  // 22KB 环境说明原样喂进去 —— 直连 pod 5/5、经 LiteLLM 只有 2~3/6 的差距
  // 就是这么来的，不是模型随机。
  const kept = items.filter(
    (i) => i && i.type !== 'additional_tools'
      && !((i.role === 'developer' || i.role === 'system') && isEnvironmentPrompt(textOf(i))),
  )
  // 回程：工具调用/结果 -> 文本，否则 flattenInput 会把它们拼成空的 "USER: "
  const replayed = kept.map((i) => {
    const r = replayItemToText(i)
    if (!r) return i
    return { type: 'message', role: r.role, content: [{ type: 'input_text', text: r.text }] }
  })
  replayed.push({ type: 'message', role: 'user', content: [{ type: 'input_text', text: handsBlock(cwd) }] })
  return replayed
}

module.exports.prepareCodexInput = prepareCodexInput
module.exports.isCodexLite = isCodexLite
module.exports.extractCwd = extractCwd
module.exports.isEnvironmentPrompt = isEnvironmentPrompt

// ── 拒答检测与升级重试 ────────────────────────────────────────────
//
// 上线后实测：契约 + 剥指令 + 交手之后仍有约 1/3 的轮次既不吐块也不干活，
// 其中一半是"我没有权限/终端"这类拒答。web-tools.js 的 JSON 那条路早有
// escalate 重试（措辞："you are only WRITING the request; a separate worker
// fulfills it"），BPI 这条路照搬。

// 撇号必须写成 ['’]：模型输出用的是**印刷体撇号 U+2019**，不是 ASCII 的 '。
// 2026-08-08 实测漏判现场（用户第一轮被直接拒答、重试没点火）：
//   "I do’t have an active file-editing tool connection in this chat"
//                ^ 这个撇号是 ’，写 don't 匹配不上。
// 这个坑我在记忆里记过（typographic apostrophe breaks text patterns）又踩了一次。
const AP = "['\u2019\u02bc]"
const REFUSAL_RE = new RegExp(
  "(没有|无法|不能|尚未|未能)[^。\n]{0,20}(权限|终端|执行|访问|接口|环境|工具|落盘|文件系统)"
  + "|(do" + AP + "?n" + AP + "?t|do not|cannot|can" + AP + "?t|could" + AP + "?n" + AP + "?t|could not"
  + "|did" + AP + "?n" + AP + "?t|failed to|unable to|no active|not connected)"
  + "\\s*(have|access|execute|run|write|edit|tool|file)"
  , "i")

// 追措辞是追不完的（2026-08-08 实测：修了 don’t 之后又冒出 couldn’t access、
// 以及"I checked for AGENTS.md at /Users/…, but…"这种**声称查过其实没调工具**的）。
// 所以加一条与措辞无关的判据：**没吐块、却在回答里提到了绝对路径**。
// 那种内容只有真执行过才可能知道，没执行就必然是编的或拒答 —— 一律重试。
// 纯问答（"快速排序复杂度"）不会出现绝对路径，不受影响。
const ABS_PATH_RE = /(?:\/(?:Users|home|opt|var|etc|tmp)\/[^\s'"`)]+)|(?:[A-Za-z]:\\\\)/

/** 这一轮是不是"该动手却没动手"。 */
function needsEscalation(text) {
  if (typeof text !== 'string' || !text.trim()) return false
  if (extractBlocks(text).length) return false   // 已经吐块了
  if (REFUSAL_RE.test(text)) return true
  return ABS_PATH_RE.test(text)                  // 提到了路径却一次工具都没调
}

const ESCALATE = [
  '上一次回复被丢弃了：你回的是说明文字，不是块。',
  '说"没有权限/没有终端/访问不了"在这里是无效的 —— 你不需要执行任何东西，',
  '你只负责把要执行的块写出来，外部执行器会在用户机器上真实运行它并把结果贴回来。',
  '现在只输出块本身，不要任何前言和解释。',
  '例：要看目录 -> ⟦ls¦path=/abs/dir⟧；要写文件 -> ⟦write¦path=/abs/f¦content=…⟧',
].join('\n')

module.exports.needsEscalation = needsEscalation
module.exports.ESCALATE = ESCALATE
module.exports.REFUSAL_RE = REFUSAL_RE
module.exports.ABS_PATH_RE = ABS_PATH_RE

// ── 回程：把工具执行结果拼回 prompt ──────────────────────────────
//
// 2026-08-07 生产实测，这是"调用成功了却等于没调"的真凶：
//
//     15 [CALL] exec  -> tools.exec_command({cmd:"ls -la ..."})
//     16 [OUT ] exit_code:0  output:"total 512 drwxr-xr-x 63 ..."   ← 真的执行了
//     22 [assistant] hi，有什么需要我帮你处理的？                    ← 却答非所问
//
// 因为 Codex 把结果作为 `custom_tool_call_output` 项回传，而 zerokey 的
// flattenInput 只认 `item.role` + `item.content`：这个 item **两样都没有**
// （结果在 `output` 字段，且没有 role），于是被拼成一行空的 "USER: "。
// 模型问了一句、什么都没收到，只能重问或胡答。
//
// 上游对应实现是 compiler.js:55-60（role:'tool' -> `BPI(name): <output>`）。

function toolOutputText(item) {
  const o = item && item.output
  if (typeof o === 'string') return o
  if (!Array.isArray(o)) return ''
  return o.map((p) => (p && p.text) || '').join('\n')
}

/** exec 的返回体是一坨 JSON，把真正的 stdout 摘出来，别把 chunk_id 之类喂给模型。 */
function distillExecOutput(text) {
  const out = []
  for (const line of String(text).split('\n')) {
    const t = line.trim()
    if (!t) continue
    if (/^(Script completed|Wall time|Output:)/.test(t)) continue
    if (t.startsWith('{') && t.includes('"output"')) {
      try {
        const j = JSON.parse(t)
        if (typeof j.output === 'string') { out.push(j.output.trimEnd()) ; continue }
      } catch (_) { /* 不是完整 JSON 就原样保留 */ }
    }
    out.push(line)
  }
  return out.join('\n').trim()
}

/**
 * 把 custom_tool_call / custom_tool_call_output 两类 item 换成模型看得懂的文本。
 * 其它 item 原样返回 null（调用方保留原项）。
 */
function replayItemToText(item) {
  if (!item || typeof item !== 'object') return null
  if (item.type === 'custom_tool_call') {
    // 模型自己发起的那一步，回放成它当初写的块，保持"一问一答"的形状
    const marks = String(item.input || '').match(/BPI\(([a-z_]+)\):/g) || []
    const names = marks.map((m) => m.slice(4, -2))
    return { role: 'assistant', text: names.length ? names.map((n) => `⟦${n}…⟧`).join(' ') : '⟦…⟧' }
  }
  if (item.type === 'custom_tool_call_output' || item.type === 'function_call_output') {
    const body = distillExecOutput(toolOutputText(item))
    return {
      role: 'user',
      text: body
        ? `${RESULT_HEAD}\n${body}\n(以上是真实执行结果，请直接据此回答用户。)`
        : `${RESULT_HEAD} (无输出)`,
    }
  }
  return null
}

module.exports.replayItemToText = replayItemToText
module.exports.RESULT_HEAD = RESULT_HEAD
module.exports.handsBlock = handsBlock
module.exports.distillExecOutput = distillExecOutput
module.exports.toolOutputText = toolOutputText

/**
 * 重试用的**精简 prompt**。
 *
 * 2026-08-08 实测：近 60 分钟 18 台合计 12 次成功编译 / 5 次拒答重试 ——
 * 约四成"要动手"的轮次要跑两趟。而第一版重试是把 basePrompt（真实场景 107KB）
 * 整个再发一遍，代价与首轮相同，用户直接感知为卡顿。
 *
 * 重试其实不需要全部历史：模型第一轮已经"理解了任务但不肯动手"，缺的只是
 * 「你有手、照格式输出」这件事。所以只带**最后一条用户消息 + 交手块 + 升级指令**。
 * 顺带一个副作用是好的：107KB 里那些互相打架的上下文（Codex 自己的 agent 提示词
 * 等）被去掉了，合规概率反而更高。
 */
// 诉求短于这个长度时**额外**附上对话尾部（不是替换掉诉求）。
//
// ⚠️ 这里第一版写的是 `MIN_ASK = 40`、且短诉求就"当没提取到"丢掉，理由是
// "真实诉求几乎不会短于 40 字符" —— 拍脑袋，而且手里的数据当场就反驳它：
// 单测里那条真实诉求「把结构写到 /tmp/a.md」15 字符，用户本人用过的
// `ll`(2) / `罗列下本地的文件夹`(9) 更短。线上失败那两次 ask 长度其实是 **0**
// （568 = 交手块含 cwd 371 + 升级指令 190 + 空 `USER: ` + 换行）。
// 所以：短诉求照留，只是补上尾部；长诉求（>=200）路径**逐字节不变**，
// 保住已经实测 4/4 成功的那条路。
const ASK_ENOUGH = 200

/**
 * 对话尾部摘要 —— 提取不到用户诉求时的兜底。
 *
 * 2026-08-08 线上实测，上线首日 6 次重试 4 成 2 败，两次失败共享同一签名：
 *
 *     zero-116  escalate prompt 572 chars (was 164086)  -> 之后没有 compiled
 *     zero-84   escalate prompt 568 chars (was 161594)  -> 之后没有 compiled
 *     （成功的都是 2416 chars）
 *
 * 交手块 346 + 升级指令 190 + "USER: " ≈ 545，也就是说**诉求那段只剩 20 来个
 * 字符**：16 万字符的会话里一条用户消息都没提取到，等于让模型闭着眼重试。
 *
 * 为什么会提取不到，目前只有形状可推（长 agent 循环里本轮的 input 尾部是工具
 * 结果、不是新的用户提问，而更早那条原始提问可能已被客户端压缩掉了），**没有
 * 现场数据**，所以不去猜着改提取逻辑。这里只做一件有据可依的事：诉求为空时，
 * 用对话尾部的真实内容顶上，让模型至少知道刚刚发生了什么。
 */
function tailDigest(items, limit = 3, cap = 600) {
  const picked = []
  for (let i = (items || []).length - 1; i >= 0 && picked.length < limit; i--) {
    const it = items[i]
    if (!it || it.type === 'additional_tools') continue
    // developer/system 一律不算"对话" —— 那是客户端脚手架（17.7KB agent 提示词、
    // 22KB permissions 说明）。第一版忘了这条，兜底把刚剥掉的打架上下文原地拖了
    // 回来，被单测「剔除打架的上下文」抓到。
    if (it.role === 'developer' || it.role === 'system') continue
    let t = textOf(it)
    if (!t) {
      const r = replayItemToText(it)   // 工具调用/结果没有 content，得先还原成文本
      t = (r && r.text) || ''
    }
    t = t.trim()
    if (!t || t.startsWith(HANDS_HEAD)) continue   // 交手块是我们自己塞的，不算内容
    if (t.length > cap) t = t.slice(-cap)
    picked.push(`${it.role || it.type || '?'}: ${t}`)
  }
  return picked.reverse().join('\n')
}

/** 形状摘要（只有 type/role 计数，**不含任何会话内容**），用于事后定位。 */
function shapeOf(items) {
  const n = {}
  for (const it of items || []) {
    if (!it) continue
    const k = `${it.type || 'message'}/${it.role || '-'}`
    n[k] = (n[k] || 0) + 1
  }
  return Object.entries(n).map(([k, v]) => `${k}:${v}`).join(',')
}

function escalatePrompt(items, escalateText) {
  const cwd = extractCwd(items)
  let lastUser = ''
  for (const it of items || []) {
    if (!it || it.type === 'additional_tools') continue
    if (it.role !== 'user') continue
    const t = textOf(it)
    // 跳过我们自己塞进去的块，别把它当成用户诉求
    if (!t || t.startsWith(HANDS_HEAD) || t.startsWith(RESULT_HEAD)) continue
    lastUser = t
  }
  // AGENTS.md 那条抬头很长，只留头部足够定位仓库
  if (lastUser.length > 4000) lastUser = lastUser.slice(-4000)

  const body = []
  if (lastUser) body.push('USER: ' + lastUser)
  // 诉求够长就到此为止（这条路已实测 4/4，不去动它）；短或为空才补尾部
  const tail = lastUser.length >= ASK_ENOUGH ? '' : tailDigest(items)
  if (tail) body.push('[对话尾部]', tail)
  // 只打长度和形状，不打内容 —— 下次再遇到空诉求时能直接看出是什么形状导致的
  console.log(`[bpi] escalate ask=${lastUser.length} tail=${tail.length}`
    + ` shapes=${shapeOf(items)}`)
  return [handsBlock(cwd), '', ...body, '', escalateText].join('\n')
}

module.exports.escalatePrompt = escalatePrompt
module.exports.tailDigest = tailDigest
module.exports.ASK_ENOUGH = ASK_ENOUGH
module.exports.shapeOf = shapeOf


// ── 网页版引用标记：剥掉 + 从源头少产生 ──────────────────────────────
//
// 2026-08-08 用户现场：回答里出现 `filecite<PUA>turn0file0`，或者末尾只剩
// `filecite` 然后整个停住。抓到的原始码点：
//
//     \ue200 filecite \ue202 turn0file0 \ue202 L3-L3 \ue201
//      ↑起始           ↑分隔      ↑文件编号  ↑分隔  ↑行号    ↑结束
//
// 这是 ChatGPT **网页版**的引用标记，用私有区字符包住，网页前端负责渲染成引用
// 卡片。我们直接透传，Codex 客户端不认，就当普通文字显示了。
//
// 只在**走了附件上传**那条路时才出现（`responses.js` 里 prompt 超过
// ZK_INLINE_MAX 就把整段会话当 .txt 传上去）。实测 5 万字符直接发那档 0/4 发生，
// 一超过阈值就 16/16 发生。
//
// 为什么它同时造成"然后就停止"（实测 n=16，A/B 见 CITE_FREE_HINT 注释）：
// 上游 8/8 都报 `status=completed`、`incomplete_details=None`，但 9/16 的回复里
// 标记**只有开始没有结束** —— 流走到引用标记那里就不往下发了。不是超时、不是
// token 上限、也不是我们截的（累加逻辑只有 append）。
//
// 所以两头都要治：这里剥字符（治脏），CITE_FREE_HINT 从源头少产生（治截断）。

const CITE_OPEN = '\ue200'   // 引用标记起始
const CITE_END = '\ue201'    // 引用标记结束
const CITE_SEP = '\ue202'    // 标记内分隔
// 私有区兜底：上面三个是实测到的，同族还有别的（\ue203…）用于其它卡片类型。
// 与其逐个追（追措辞追不完，这教训吃过），不如整段私有区一律不放行。
const PUA_RE = /[\ue000-\uf8ff]/g
// 一个引用标记实测 30~60 字符。给足余量；超过就认定不是标记，原样放行，
// 避免把正文永久扣在缓冲区里。
const CITE_MAX = 200

/**
 * 流式安全的引用剥离器。
 *
 * 标记会**跨分片**到达（`\ue200file` 一片、`cite\ue202turn0` 又一片），所以不能
 * 逐片正则替换 —— 那样会把标记切成两半，各自漏出一截。这里遇到起始符就把后续
 * 内容扣住，等到结束符再整段丢弃。
 *
 * 用法::
 *
 *     const f = makeCitationFilter()
 *     res.write(f.push(chunk))   // 每片
 *     res.write(f.flush())       // 收尾（未闭合的残留在此丢弃）
 */
function makeCitationFilter() {
  let held = ''      // 已经看到起始符、正在等结束符的内容
  return {
    push(chunk) {
      if (!chunk) return ''
      let out = ''
      for (const ch of String(chunk)) {
        if (held) {
          held += ch
          if (ch === CITE_END) { held = '' }            // 完整标记，整段丢弃
          else if (held.length > CITE_MAX) {            // 不像标记，别扣着正文
            out += held.replace(PUA_RE, ''); held = ''
          }
          continue
        }
        if (ch === CITE_OPEN) { held = ch; continue }
        // 落单的分隔符/其它私有区字符（标记被上游截断时会剩下）一律不放行
        if (ch === CITE_SEP || PUA_RE.test(ch)) { PUA_RE.lastIndex = 0; continue }
        out += ch
      }
      return out
    },
    /** 收尾。未闭合的那截**丢掉** —— 那正是"末尾只剩 filecite"的来源。 */
    flush() {
      const dangling = held
      held = ''
      return { text: '', truncated: Boolean(dangling), dropped: dangling.length }
    },
    get pending() { return held.length },
  }
}

/** 非流式的一把梭版本。 */
function stripCitations(text) {
  if (typeof text !== 'string' || !text) return text
  const f = makeCitationFilter()
  const out = f.push(text)
  f.flush()
  return out
}

// 加在附件提示后面，从源头少产生引用。
//
// A/B 实测（同一份材料、交替发控漂移，只改这一个变量，每组 n=16）：
//
//                     引用漏出   断在标记中间   三标记全中
//     A 原样           16/16      9/16          1/16
//     B 要求不引用     13/16      2/16         16/16
//
// 截断 56% -> 12.5%，答对率 6% -> 100%。但**引用仍会漏出 13/16** —— 模型嘴上
// 答应不引用照样带标记，所以出站剥离那一层省不掉，两个都要。
const CITE_FREE_HINT =
  '直接给出纯文本答案，不要引用来源、不要标注文件名或行号、不要添加任何引用角标。'

module.exports.makeCitationFilter = makeCitationFilter
module.exports.stripCitations = stripCitations
module.exports.CITE_FREE_HINT = CITE_FREE_HINT
module.exports.CITE_OPEN = CITE_OPEN
module.exports.CITE_END = CITE_END
