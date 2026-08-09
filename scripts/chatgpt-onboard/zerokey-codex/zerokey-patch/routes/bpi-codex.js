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
    case 'cmd': {
      if (!p.run) return null
      // 剥对称的外层引号：模型常发 ⟦cmd¦run='git status --short'⟧（带引号）。
      // 2026-08-09 用户现场：整串带引号进了 shell -> zsh 把 "git status --short"
      // 当成**一个**命令名 -> command not found。BPI 参数本身就是字面值，
      // 外层引号永远是多余的。
      let run = String(p.run).trim()
      const q = run[0]
      if ((q === "'" || q === '"' || q === '`') && run.endsWith(q) && run.length > 1) {
        run = run.slice(1, -1)
      }
      return `await tools.exec_command({cmd: ${jsStr(run)}})`
    }
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

// ── agents 层：⟦spawn⟧ -> Codex 原生 spawn_agent（协议对齐，不自造编排）─────
//
// 架构对称：我们已经把 ⟦write⟧ 编译成 custom_tool_call(exec)，同理把
// ⟦spawn¦task_name=…¦message=…⟧ 编译成 function_call(spawn_agent)。
// spawn_agent **本来就在客户端发来的 additional_tools.collaboration 命名空间里**，
// 客户端**自己会编排子 agent**（fork 上下文、跑子线程、把结果回灌）—— 我们这边
// 零编排逻辑，只做"文本块 -> 原生调用"的翻译，跟 exec 一模一样。
//
// schema（读真实载荷 spawn_agent.parameters 原文，非推测）：
//   required: task_name(小写字母/数字/下划线), message(纯文本任务)
//   optional: fork_turns("none"|"all"|正整数字符串, 默认 all), model, reasoning_effort
//
// ⚠️ 为什么保守：实测网页模型**不主动**发委派块（给了也不用，它偏好自己 batch），
// 所以这条路平时是**休眠**的 —— 只有用户明确要求子 agent、且账号契约教了 ⟦spawn⟧
// 时才会走到。它的价值是：真需要时**走原生编排**（结果由客户端保证回灌），
// 而不是让模型用 exec 假装并行。客户端侧编排是 Codex 自己的代码，我们不重造。
function spawnToFunctionCall(blk) {
  const p = blk.params
  const taskName = (p.task_name || p.name || '').trim()
  const message = (p.message || p.task || '').trim()
  if (!taskName || !message) return null
  const args = { task_name: taskName.toLowerCase().replace(/[^a-z0-9_]/g, '_'), message }
  if (p.fork_turns) args.fork_turns = String(p.fork_turns)
  if (p.model) args.model = String(p.model)
  if (p.reasoning_effort) args.reasoning_effort = String(p.reasoning_effort)
  return { name: 'spawn_agent', arguments: args }
}

/** 抽出所有 ⟦spawn⟧ 块，编译成 spawn_agent 调用列表（可并行多个）。 */
function extractSpawns(text) {
  return extractBlocks(text)
    .filter((b) => b.name === 'spawn')
    .map(spawnToFunctionCall)
    .filter(Boolean)
}

module.exports = { compileToExec, extractBlocks, blockToJs, addFilePatch, updateFilePatch,
                   askToFunctionCall, firstAsk, spawnToFunctionCall, extractSpawns,
                   OPEN, CLOSE, SEP }


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

// ── 环境：从协议块读，不要去猜 ────────────────────────────────────────
//
// Codex 在 input 里发了一个**结构化**的环境块（真实抓包）：
//
//   <environment_context>
//     <cwd>/Users/x/codes/repo</cwd>
//     <shell>zsh</shell>
//     <current_date>2026-08-08</current_date>
//     <timezone>Asia/Shanghai</timezone>
//     <filesystem><workspace_roots><root>/Users/x/codes/repo</root></workspace_roots>...
//   </environment_context>
//
// 2026-08-09 用户 /init 现场：模型反问"需要当前仓库的绝对路径"。根因是这里
// **原来只去正则刮 "# AGENTS.md instructions for /path" 那行**，而那行只有
// 仓库**已经有** AGENTS.md 时才存在 —— 而 `/init` 恰恰是"还没有 AGENTS.md"
// 时才跑的命令。鸡生蛋：cwd 恒为 null -> 交手块不带路径 -> 模型只能反问。
//
// 改成读协议块：结构化、任何仓库都有、不依赖任何文件是否存在。
// AGENTS.md 那行退化成 fallback（老客户端/被裁剪的载荷）。
function parseEnvironment(items) {
  let blob = ''
  for (const it of items || []) {
    if (!it || it.type === 'additional_tools') continue
    const t = textOf(it)
    if (t.includes('<environment_context>')) { blob = t; break }
  }
  const pick = (tag) => {
    const m = blob.match(new RegExp('<' + tag + '>([^<]*)</' + tag + '>'))
    return m ? m[1].trim() : null
  }
  const roots = []
  const rootsBlock = blob.match(/<workspace_roots>([\s\S]*?)<\/workspace_roots>/)
  if (rootsBlock) {
    for (const m of rootsBlock[1].matchAll(/<root>([^<]*)<\/root>/g)) roots.push(m[1].trim())
  }
  let cwd = pick('cwd') || roots[0] || null
  if (!cwd) {
    // fallback：老式抬头（仅当仓库已有 AGENTS.md 时存在）
    for (const it of items || []) {
      if (!it || it.type === 'additional_tools') continue
      const m = textOf(it).match(/AGENTS\.md instructions for (\/[^\s\n]+)/)
      if (m) { cwd = m[1]; break }
    }
  }
  return { cwd, shell: pick('shell'), roots }
}

/** 向后兼容：只要 cwd。 */
function extractCwd(items) {
  return parseEnvironment(items).cwd
}

// 两种情形必须分开说。
// 2026-08-07 用户实测：第一版只写了"要动手时只输出块本身，不要解释"，模型把这条
// 也套到了**拿到结果之后**——`ll` 那轮工具跑了、目录列出来了，模型却回一句
// 「请下达具体任务」，闷着不肯把结果讲给用户。而换成更明确的自然语言提问
// （"罗列下本地的文件夹"）就正常。所以要显式区分"还没动手"和"已有结果"。
// env 可以是字符串(旧调用，只有 cwd) 或 parseEnvironment() 的结果
function handsBlock(env) {
  const e = (typeof env === 'string' || env == null) ? { cwd: env } : env
  const cwd = e.cwd
  return [
    HANDS_HEAD,
    '你现在连着用户那台机器。下面这些块由一个外部执行器真实执行 —— 不是示例，',
    '也不是你自己的沙箱；不要用内建的代码解释器去试，那里没有用户的文件。',
    '⟦ls¦path={abs}⟧ ⟦read¦path={abs}⟧ ⟦write¦path={abs}¦content={str}⟧ ⟦cmd¦run={shell}⟧',
    // agents 层：委派独立子任务给子 agent（编排在 Codex 客户端）。只在任务确实
    // 可拆、且自己做完更慢时用 —— 自己能顺手做的活不要转包。
    '需要并行独立子任务时可用 ⟦spawn¦task_name={名}¦message={子任务描述}⟧ 委派子 agent。',
    // cwd 来自协议块 <environment_context>，任何仓库都有 —— 别再让模型反问路径。
    cwd ? `当前工作目录（绝对路径）：${cwd}` : '',
    cwd ? `相对路径一律相对它解析；**不要反问用户工作目录**，上面就是。` : '',
    e.shell ? `shell 是 ${e.shell}。` : '',
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
  const env = parseEnvironment(items)
  const cwd = env.cwd
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
  // 结构压缩：照 Codex 本地 auto-compact（compact.rs:348-368, collect_user_messages:520）
  // —— 上下文快满时，丢工具调用/结果，只留最近 N 条 user 原文 + 最后 assistant。
  // 纯本地操作，不调模型，0 额外调用。详见 .claude/plans/zerokey-structural-compaction.md
  const compacted = compactInput(kept)
  // 回程：工具调用/结果 -> 文本，否则 flattenInput 会把它们拼成空的 "USER: "
  // 压缩后剩下的工具项（最近一轮的）照常 replay；更早的被丢了，不 replay。
  const replayed = compacted.map((i) => {
    const r = replayItemToText(i)
    if (!r) return i
    return { type: 'message', role: r.role, content: [{ type: 'input_text', text: r.text }] }
  })
  replayed.push({ type: 'message', role: 'user', content: [{ type: 'input_text', text: handsBlock(env) }] })
  return replayed
}

// ── 结构压缩（对齐 Codex auto-compact 的**完整**协议）───────────────────
//
// Codex 压缩后的历史是**两样东西**，缺一不可（compact.rs:345-348）：
//   new_history = user_messages + **summary_text**
// summary 的前缀原文（prompts/templates/compact/summary_prefix.md）明说：
// "Another language model started to solve this problem… Use this to build on
//  the work that has already been done and **avoid duplicating work**."
//
// 2026-08-09 用户 /init 现场（"Explored→List codex"重复 20+ 次）就是只抄了一半
// 的代价：第一版只留 user 消息、把工具调用/结果**全部丢掉且不留任何摘要**。
// 本地复现（见 test_bpi.js）：98151 字符压到 187，"AGENTS.md 已写入"那条结果
// 一起被抹掉 → 模型对自己做过的一切失忆 → 从头再探 → 每轮都超阈值 → 死循环。
//
// Codex 的 summary 靠再调一次模型生成；网关的位置优势是**亲眼看到每个工具调用
// 和结果**，可以 0 额外调用地生成确定性"工作台账"（toolLedger），并把**最近一次
// 工具交换原文保留**（模型必须看到刚刚那条命令的真实返回，否则永远不知道上一步
// 成功没有）。
//
// 基线实测（每轮工具结果 3 万字符）：改前第 2 轮就撞 10 万附件阈值 → filecite；
// 改后稳态 6 万+ 字符、不随轮次增长。从线性变常数。
//
// 硬约束（已查实，踩了就出事）：
//  - AGENTS.md 那条 user 必留（extractCwd 从它取工作目录，丢了模型瞎猜路径）
//  - developer/system 系统指令必留（模型人格，剥掉=自断一臂，8/8 vs 7/8 已证伪）
//  - 只在总长超阈值才压，短会话原样返回（不无谓丢历史）
//  - 台账必留 + 最近一次工具交换必留（丢了=失忆循环，/init 现场已实测）
const COMPACT_TRIGGER_CHARS = 70000   // 附件阈值的 70%，对齐 Codex "90% 窗口才压"
const KEEP_RECENT_USERS = 8           // 保留最近 N 条 user 原文（Codex 2万token≈放宽）
const LEDGER_HEAD = '[已完成的工作台账]'
const LEDGER_MAX_CHARS = 6000         // 台账封顶；超了丢最老的行（最近的动作价值最高）

/**
 * 确定性台账：把被压缩丢弃的工具调用/结果对，压成"命令 → 结果要点"清单。
 * 对齐 Codex summary 的语义（"这些工作已经做过了，别重复"），但不调模型。
 */
function toolLedger(pairs) {
  const lines = []
  for (const { call, output } of pairs) {
    const r = replayItemToText(call)
    const did = r ? r.text.replace(/^\(已执行\) /, '') : '(工具调用)'
    // 结果要点：蒸馏后的第一行足够判断成败（"✓ 文件已写入" / "total 512 …" / 报错头）
    const gist = output
      ? (distillExecOutput(toolOutputText(output)).split('\n')[0] || '').slice(0, 120)
      : '(无返回)'
    lines.push(`- ${did.slice(0, 160)} → ${gist}`)
  }
  // 封顶：保最近的行
  let body = lines.join('\n')
  while (body.length > LEDGER_MAX_CHARS && lines.length > 1) {
    lines.shift()
    body = '- …（更早的动作已省略）\n' + lines.join('\n')
  }
  return [
    LEDGER_HEAD,
    '（历史被压缩。以下动作**已经真实执行过**，结果如后 —— 不要重复执行；',
    '基于这些结果继续下一步，或直接给最终答复。）',
    body,
  ].join('\n')
}

/** 把 items 里的工具项按 call_id 配成 {call, output} 对，保持出现顺序。 */
function collectToolPairs(items) {
  const pairs = []
  const byId = new Map()
  for (const it of items || []) {
    if (!it) continue
    if (it.type === 'custom_tool_call' || it.type === 'function_call') {
      const p = { call: it, output: null }
      pairs.push(p)
      if (it.call_id) byId.set(it.call_id, p)
    } else if (it.type === 'custom_tool_call_output' || it.type === 'function_call_output') {
      const p = it.call_id && byId.get(it.call_id)
      if (p) p.output = it
      else pairs.push({ call: null, output: it })   // 孤儿输出也别丢
    }
  }
  return pairs
}

function isAgentsMdItem(it) {
  return /AGENTS\.md instructions for \//.test(textOf(it).slice(0, 200))
}

function compactInput(items) {
  // 估算总长：textOf 只看 content，但工具结果的文本在 output、工具调用的在 input。
  // 第一版只用 textOf，漏算了几万字符的工具结果，导致没触发压缩。用真实文本长度。
  const itemLen = (it) => {
    if (it.type === 'custom_tool_call' || it.type === 'function_call')
      return String(it.input || '').length
    if (it.type === 'custom_tool_call_output' || it.type === 'function_call_output')
      return toolOutputText(it).length
    return textOf(it).length
  }
  const total = items.reduce((s, it) => s + itemLen(it), 0)
  if (total <= COMPACT_TRIGGER_CHARS) return items   // 短会话不压

  // 1) 系统指令（developer/system）全留 —— 模型人格不能丢
  // 2) AGENTS.md 那条 user 必留 —— cwd 来源
  // 3) 最近 N 条 user 消息（不含 AGENTS.md，它单列）
  // 4) 最后一条 assistant（若有）
  // 5) **最近一次工具交换原文必留** —— 模型必须看到刚刚那条命令的真实返回
  // 6) 更早的工具交换 → 确定性台账（对齐 Codex summary 的"别重复已做的工作"语义）
  const systemMsgs = items.filter((i) => i.role === 'developer' || i.role === 'system')
  const agentsMsg = items.find(isAgentsMdItem)
  const userMsgs = items.filter((i) => i.role === 'user' && !isAgentsMdItem(i))
  const recentUsers = userMsgs.slice(-KEEP_RECENT_USERS)
  const lastAssistant = [...items].reverse().find((i) => i.role === 'assistant')

  const pairs = collectToolPairs(items)
  const lastPair = pairs.length ? pairs[pairs.length - 1] : null
  const olderPairs = pairs.slice(0, -1)
  const keepToolItems = lastPair
    ? [lastPair.call, lastPair.output].filter(Boolean)
    : []

  // 保持原顺序：系统指令 → AGENTS → (最近user + 最后assistant + 最近工具交换 按原相对顺序)
  const tail = [agentsMsg, ...recentUsers, lastAssistant, ...keepToolItems]
    .filter(Boolean)
  // 按它们在原 items 里的出现顺序排
  const order = new Map(items.map((it, i) => [it, i]))
  tail.sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0))

  const result = [...systemMsgs.filter((s) => !tail.includes(s)), ...tail]
  // 去重（systemMsgs 与 tail 不会有交集，但保险）
  const seen = new Set()
  const dedup = result.filter((it) => {
    if (seen.has(it)) return false; seen.add(it); return true
  })

  // 台账插在最近工具交换（或对话尾部）**之前** —— 语序上先"你已经做过这些"，
  // 再"这是刚刚那条的返回"，模型顺着读就是 Codex summary + 现场的关系。
  if (olderPairs.length) {
    const ledgerItem = {
      type: 'message', role: 'user',
      content: [{ type: 'input_text', text: toolLedger(olderPairs) }],
    }
    const firstKept = keepToolItems.length
      ? dedup.indexOf(keepToolItems[0])
      : -1
    if (firstKept >= 0) dedup.splice(firstKept, 0, ledgerItem)
    else dedup.push(ledgerItem)
  }

  // 只打 type/role 计数，不打内容
  const before = countShapes(items), after = countShapes(dedup)
  const afterSize = dedup.reduce((s, it) => s + itemLen(it), 0)
  console.log(`[compact] ${total} -> ${afterSize} chars  ${before} => ${after}`
    + `  ledger=${olderPairs.length} kept_last_tool=${keepToolItems.length > 0}`)
  return dedup
}

function countShapes(items) {
  const n = {}
  for (const it of items || []) {
    if (!it) continue
    const k = `${it.type || 'msg'}/${it.role || '-'}`
    n[k] = (n[k] || 0) + 1
  }
  return Object.entries(n).map(([k, v]) => `${k}:${v}`).join(',')
}

module.exports.prepareCodexInput = prepareCodexInput
module.exports.compactInput = compactInput
module.exports.toolLedger = toolLedger
module.exports.collectToolPairs = collectToolPairs
module.exports.LEDGER_HEAD = LEDGER_HEAD
module.exports.isCodexLite = isCodexLite
module.exports.extractCwd = extractCwd
module.exports.parseEnvironment = parseEnvironment
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
  + "|(do" + AP + "?n?" + AP + "?t|do not|cannot|can" + AP + "?t|could" + AP + "?n?" + AP + "?t|could not"
  + "|did" + AP + "?n?" + AP + "?t|failed to|unable to|no active|not connected)"
  + "\\s*(have|access|execute|run|write|edit|tool|file)"
  , "i")

// 追措辞是追不完的（2026-08-08 实测：修了 don’t 之后又冒出 couldn’t access、
// 以及"I checked for AGENTS.md at /Users/…, but…"这种**声称查过其实没调工具**的）。
// 所以加一条与措辞无关的判据：**没吐块、却在回答里提到了绝对路径**。
// 那种内容只有真执行过才可能知道，没执行就必然是编的或拒答 —— 一律重试。
// 纯问答（"快速排序复杂度"）不会出现绝对路径，不受影响。
const ABS_PATH_RE = /(?:\/(?:Users|home|opt|var|etc|tmp)\/[^\s'"`)]+)|(?:[A-Za-z]:\\\\)/

// ── 结构分类器（对齐 Codex 的隐式 turn 协议）─────────────────────────
// 读 Codex 源码确认（core/src/stream_events_utils.rs:298-329, turn.rs:465-514）：
// Codex 的 turn 结束是**纯隐式协议 —— 这一轮解析出 0 个 tool call 就是 turn 结束**，
// 没有 sentinel、没有"完成工具"（attempt_completion 那类），`end_turn` 字段也只是
// 服务端单向否决(Some(false)=强制续跑)、不是模型声明完成的通道。
// 所以**不发明 ⟦done⟧**（少一个模型会忘记发的 sentinel = 少一类 hang 死的 bug）。
//
// 一轮回复按**结构**归类：
//   'act'  含至少一个可执行块（read/write/cmd…）  -> 要动手，执行它
//   'ask'  含 ⟦ask¦…⟧                            -> 需要用户澄清
//   'text' 没有任何块（纯文本）                    -> turn 结束，这就是最终答复
// 对齐 Codex：'text' = "这一轮没 tool call" = 正常收尾。
//
// ⚠️ 与 Codex 唯一的差别、也是那几个兜底正则**无法被塌缩掉**的根本原因：
// Codex 的模型不会拒答/谎报，所以"没块=真最终答复"永远成立。网页模型会拒答/
// 谎报/走画布，于是 'text' 有两种含义：真·最终答复 vs 假·没动手。needsEscalation
// 用兜底判据区分这两者 —— 这不是可省的 if-else，是弥补"网页模型不如原生模型可信"
// 的必要代价。协议对齐能塌缩"正常路径"，但塌缩不掉"网页模型会撒谎"这个事实。
function classifyResponse(text) {
  const blocks = extractBlocks(text)
  if (blocks.some((b) => EXECUTABLE.has(b.name))) return 'act'
  if (blocks.some((b) => b.name === 'ask')) return 'ask'
  // spawn = 委派子 agent，也是"这轮要动手"的结构（编排在客户端）。不归入 act
  // 会被当纯文本 -> needsEscalation 的 ABS_PATH_RE 可能把它当编造打回。
  if (blocks.some((b) => b.name === 'spawn')) return 'spawn'
  return 'text'
}

// ChatGPT 网页版"文档/画布"标记（2026-08-09 用户 /init 现场）：
// 模型没吐 ⟦write⟧ 块，而是调了**网页产品自己的文档功能**，输出形如
//     :::writing{variant="document" id="58391"}
//     # Repository Guidelines ...
//     :::
// 后果：**文件根本没被创建**（模型自己也只说 "ready to be created"），
// 而且这段标记原样漏给用户看。这跟 filecite 同类 —— 网页产品功能漏进 API 路径。
// 复现不出来（zk-115 上 8 发全正常），推测是**账号级功能差异**：某些号开了画布。
// 但判据是硬的：**吐画布 = 没动手**，所以一律当"该动手却没动手"处理并重试。
const CANVAS_OPEN_RE = /^:::\s*\w+\s*\{[^}]*\}\s*$/m
const CANVAS_ANY_RE = /^:::\s*\w*\s*\{?[^}\n]*\}?\s*$/gm

/** 剥掉画布围栏但保留里面的正文（内容本身是有用的）。 */
function stripCanvas(text) {
  if (typeof text !== 'string' || text.indexOf(':::') < 0) return text
  return text.replace(CANVAS_ANY_RE, '').replace(/\n{3,}/g, '\n\n').trim()
}

// 假完成判据（2026-08-09 闭环尺子实测的最大残留）：模型第 0 轮**一个工具都没调**，
// 却直接说"已创建 add.py / 测试通过 / 已完成"。文件根本不存在，是编的。
// 纯 prompt 治不动（契约里写"别谎报"反而更糟，4/6），但网关有硬信号：
//   声称做了文件/命令动作  +  整段对话里没有任何工具结果  =  必然是编的。
// 判据保守：必须**动作词 + 文件/测试产物**同时出现，否则纯问答会被误伤
//（"AGENTS.md 已存在"没有动作词，不触发；"快速排序完成排序"没有文件产物，不触发）。
const CLAIM_DONE_RE = new RegExp(
  "(已|我已|我)?(创建|新建|生成|写入|保存|修改|更新|运行|执行)了?"
  + "[^。\\n]{0,40}"
  + "(\\.(py|js|ts|jsx|tsx|mjs|cjs|md|txt|json|ya?ml|toml|sh|go|rs|c|cpp|h|hpp|java|rb|php|html|css|sql)"
  + "|测试.{0,6}(通过|完成)|test.{0,10}(pass|ok))"
  + "|(created|wrote|added|saved|generated|ran|executed)\\s+[^.\\n]{0,40}"
  + "(file|test|\\.(py|js|ts|md|txt|json|sh|go|rs))",
  "i")

// ── 空承诺/拖延判据（2026-08-09 用户现场：创建飞书文档）────────────────
// 现场：用户"那你现在创建一个文档" -> 模型「可以继续」；"创建了吗？" ->
// 「我现在直接用本机已授权的 lark-cli 创建飞书文档…」—— 全程零块，
// 用户连问三轮才被戳穿。这在协议里是**结构非法**的：宣告"我现在就去做 X"
// 的回复必须携带做 X 的块；纯文本只能是最终答复或提问。判据是结构性的：
//   第一人称即时意图 + 动作动词 + 零块 = 拖延，重试。
// 动词表收紧："我可以帮你生成内容"（能力陈述，无"现在"类词头）不触发；
// "你可以运行 X"（第二人称）不触发；正常最终答复"已完成 X"走 CLAIM_DONE 通道。
const STALL_INTENT_RE = new RegExp(
  '(我现在|我将|我这就|接下来我?|让我|我马上|随即|现在直接|我先|正在)'
  + '[^。\\n]{0,30}'
  + '(创建|执行|运行|读取|检查|安装|升级|写入|调用|查询|下载|上传|打开|删除|列出)')
// 纯敷衍：整条回复就是一句应答词，零信息零动作（现场原句「可以继续」）。
const FILLER_RE = /^(可以|好的|收到|明白|嗯|OK|ok)[。！!，,\s]*(继续|了解|开始)?[。！!\s]*$/

/** 这一轮是不是"该动手却没动手"。hadToolResult=整段对话此前是否已有工具结果。 */
function needsEscalation(text, hadToolResult) {
  if (typeof text !== 'string' || !text.trim()) return false
  // ── 对齐 Codex 隐式协议：有块=继续，无块=turn 结束 ──
  // 有可执行块或 ask 块 -> 结构合法，放行（Codex 里 tool call 即 needs_follow_up）。
  const cls = classifyResponse(text)
  if (cls === 'act' || cls === 'ask' || cls === 'spawn') return false
  // cls === 'text'：没有块。在 Codex 里这就是"最终答复"、正常收尾。
  // 但网页模型的"没块"有两种：真·最终答复 vs 假·拒答/谎报/画布。用兜底判据区分。
  // 这几条不是可塌缩的 if-else，是弥补"网页模型会撒谎"的必要判据（见 classifyResponse 注释）。
  if (REFUSAL_RE.test(text)) return true          // 拒答："我没有权限/终端"（任何时候都不合法）
  if (CANVAS_OPEN_RE.test(text)) return true      // 画布 = 走了网页写文档功能，文件没创建
  // 拖延：承诺"我现在就去做 X"却零块，或整条就一句敷衍（"可以继续"）。
  // 不分阶段 —— 工具跑没跑过，空承诺都非法（现场就是有过工具结果后继续拖）。
  if (STALL_INTENT_RE.test(text)) return true     // 空承诺："我现在直接用 lark-cli 创建…"
  if (FILLER_RE.test(text.trim())) return true    // 纯敷衍："可以继续"
  // ── 谎报判据只在"全程 0 工具"时生效 ──
  // 2026-08-09 /init 死循环的直接扳机就在这里：模型真把 AGENTS.md 写完了、
  // 给出合法最终答复"已在 /Users/…/AGENTS.md 创建…"——最终答复**必然**提到
  // 绝对路径和"已创建 xx.md"。旧版 ABS_PATH_RE 无条件生效，把真答复判成编造
  // → 升级重试逼模型"只输出块" → 模型只好又发 ⟦ls⟧ → 客户端显示 Explored →
  // 下轮又想收尾又被打回 → 无限循环。
  // 判据的本意（"没调工具却说出绝对路径=编的"）只在 hadToolResult=false 时成立；
  // 工具真跑过之后，路径正是从工具结果里学来的，是最终答复的**应有内容**。
  if (!hadToolResult && ABS_PATH_RE.test(text)) return true   // 0 工具却报路径 = 编的
  if (!hadToolResult && CLAIM_DONE_RE.test(text)) return true // 0 工具却宣告完成 = 编的
  return false                                    // 其余"没块" = 真·最终答复/正常闲聊，收尾
}

const ESCALATE = [
  '上一次回复被丢弃了：你没有真正动手 —— 要么在说明/拒绝，要么声称做完了却一个块都没发。',
  '重要：文件在你发出块、且看到"[上一步执行结果]"之前**根本不存在**。',
  '你不需要自己执行任何东西，也没有"没有权限"这回事 —— 你只负责把要执行的块写出来，',
  '外部执行器会在用户机器上真实运行它，再把结果贴回来给你。',
  '也不要使用文档/画布功能（``:::writing`` 之类）—— 那只是在聊天里排版，**不会创建文件**。',
  '现在只输出块本身，不要任何前言、解释、或"已完成"之类的话。',
  '例：要看目录 -> ⟦ls¦path=/abs/dir⟧；要写文件 -> ⟦write¦path=/abs/f¦content=…⟧；',
  '要跑命令 -> ⟦cmd¦run=…⟧。可以一次发多个独立的块。',
].join('\n')

module.exports.needsEscalation = needsEscalation
module.exports.classifyResponse = classifyResponse
module.exports.ESCALATE = ESCALATE
module.exports.REFUSAL_RE = REFUSAL_RE
module.exports.ABS_PATH_RE = ABS_PATH_RE
module.exports.CLAIM_DONE_RE = CLAIM_DONE_RE
module.exports.STALL_INTENT_RE = STALL_INTENT_RE
module.exports.FILLER_RE = FILLER_RE
module.exports.stripCanvas = stripCanvas
module.exports.CANVAS_OPEN_RE = CANVAS_OPEN_RE

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

// BPI 操作 -> 成功时的人话。apply_patch 在 code-mode 里**成功返回空对象 `{}`**
// （codex-rs/core/src/tools/context.rs:275 `code_mode_result` -> 空 Map），
// 原样喂回去模型根本看不出成功。
// 2026-08-09 用户 /init 现场：模型连着 4~5 次"已完成"重复宣告，并把
// `BPI(write) 返回 {}` 这句内部噪声讲给用户听 —— 就是这里。
const OP_OK = {
  write: '✓ 文件已写入',
  replace: '✓ 内容已替换',
  mkdir: '✓ 目录已创建',
}

/** exec 的返回体是一坨 JSON，把真正的 stdout 摘出来，别把 chunk_id 之类喂给模型。 */
function distillExecOutput(text) {
  const out = []
  let lastOp = null
  for (const line of String(text).split('\n')) {
    const t = line.trim()
    if (!t) continue
    if (/^(Script completed|Wall time|Output:)/.test(t)) continue
    // `BPI(name):` 是我们编译时自己塞的标记，不是模型/系统的输出。
    // 记下是哪个操作（下一行的结果要用），但**不要喂给模型** —— 它会当成
    // 真实输出转述给用户（用户现场就看到了 "BPI(write) 返回 {}"）。
    const mk = t.match(/^BPI\(([a-z_]+)\):$/)
    if (mk) { lastOp = mk[1]; continue }
    // 空对象 = apply_patch 成功。翻译成人话，否则模型判断不了成功与否 -> 重做。
    if (t === '{}') {
      out.push(OP_OK[lastOp] || '✓ 执行成功')
      lastOp = null
      continue
    }
    if (t.startsWith('{') && t.includes('"output"')) {
      try {
        const j = JSON.parse(t)
        if (typeof j.output === 'string') {
          const body = j.output.trimEnd()
          // exec_command 成功但无输出（如 mkdir）也要给个明确信号
          out.push(body || (j.exit_code === 0 || j.exit_code === undefined
            ? (OP_OK[lastOp] || '✓ 命令执行成功（无输出）') : `(exit ${j.exit_code})`))
          lastOp = null
          continue
        }
      } catch (_) { /* 不是完整 JSON 就原样保留 */ }
    }
    out.push(line)
    lastOp = null
  }
  return out.join('\n').trim()
}

// ── 工具输出截断（对齐 Codex）─────────────────────────────────────────
//
// Codex 给模型看工具输出时按 1 万 token 中间截断，保头尾 + 提示
// (utils/output-truncation/src/lib.rs:12-28)。我们现在 distillExecOutput 不截断，
// 一次 ls/grep 几万字符全塞下一轮 → 9 轮就撞 10 万字符附件阈值 → filecite/
// "说一半就停"。这里对齐 Codex：超阈值就保头尾、省中间，并告诉模型"截了、原多大"。
//
// 阈值取 30000 字符（≈7500 token，按 4 字符/token 估，留余量到 Codex 的 1 万 token
// 线）。没装 tiktoken，字符近似够用——截断是"别撑爆"，不是精确计量。
//   Codex 截断提示: "Warning: truncated output (original token count: N)"
//   这里用中文，跟 RESULT_HEAD 一个风格。
const TOOL_OUTPUT_MAX_CHARS = 30000
const TOOL_OUTPUT_HEAD = 14000   // 保头 14k
const TOOL_OUTPUT_TAIL = 14000   // 保尾 14k

/** 中间截断保头尾，加提示。不超阈值原样返回。 */
function truncateToolOutput(text) {
  const s = String(text || '')
  if (s.length <= TOOL_OUTPUT_MAX_CHARS) return s
  const head = s.slice(0, TOOL_OUTPUT_HEAD)
  const tail = s.slice(s.length - TOOL_OUTPUT_TAIL)
  const lines = s.split('\n').length
  return head
    + `\n\n…（已截断，原文 ${s.length} 字符 / ${lines} 行；如需中间部分请分块或缩小范围重新获取）…\n\n`
    + tail
}

/**
 * 把 custom_tool_call / custom_tool_call_output 两类 item 换成模型看得懂的文本。
 * 其它 item 原样返回 null（调用方保留原项）。
 */
function replayItemToText(item) {
  if (!item || typeof item !== 'object') return null
  if (item.type === 'function_call') {
    // ask/spawn 编译出的原生调用被客户端回灌时长这样。不认它会掉进 flattenInput
    // 的默认分支拼成空 "USER: "（与 custom_tool_call_output 当年同一个坑）。
    const name = item.name || 'tool'
    let gist = ''
    try {
      const a = JSON.parse(item.arguments || '{}')
      gist = a.message || (a.questions && a.questions[0] && a.questions[0].question) || ''
    } catch (_) { /* 参数不是 JSON 就不摘要 */ }
    return { role: 'assistant', text: `(已调用 ${name}) ` + String(gist).slice(0, 160) }
  }
  if (item.type === 'custom_tool_call') {
    // 回放成 assistant "(已执行) <命令摘要>"，保留"这步是你自己干的"绑定。
    // 2026-08-09 实测：原来回放成模糊的 ⟦…⟧，模型认不出自己干了啥 -> 重发同一条。
    // 摘要出真实命令后，模型能识别"这步做过了"。
    const js = String(item.input || '')
    const cmds = []
    for (const m of js.matchAll(/exec_command\(\{cmd:\s*("(?:[^"\\]|\\.)*")\}\)/g)) {
      try { cmds.push(JSON.parse(m[1]).split('\n')[0].slice(0, 80)) } catch (_) { /* skip */ }
    }
    for (const m of js.matchAll(/Add File: ([^\\\n"]+)/g)) cmds.push('写文件 ' + m[1])
    const summary = cmds.length ? cmds.join(' ; ') : '(工具调用)'
    return { role: 'assistant', text: '(已执行) ' + summary }
  }
  if (item.type === 'custom_tool_call_output' || item.type === 'function_call_output') {
    // 截断在 distill 之后、喂回之前 —— 大输出不撑爆下一轮上下文。
    const body = truncateToolOutput(distillExecOutput(toolOutputText(item)))
    // 关键：明确框定"这是你上一条命令的真实返回 + 循环纪律"。
    // 2026-08-09 实测（回程绑定假设，n=5）：原来只写"[上一步执行结果]...请据此回答"，
    // 模型在看到 "all tests passed" 后仍重发同一条命令，4~12 轮才停（甚至卡死）。
    // 改成把"你那条命令已执行完毕 / 成功就别重发 / 完成就直接答复"讲清楚后，
    // 5/5 在 2~4 轮干净收尾，真产物全对 —— 从"卡顿重做"变"像 acct 一样利落"。
    if (!body) return { role: 'user', text: `${RESULT_HEAD} (无输出，命令已执行)` }
    return {
      role: 'user',
      text: `${RESULT_HEAD}\n${body}\n\n`
        + '（这是你上一条命令的真实返回。若结果表明成功，该步已完成——**不要重发同一条命令**；'
        + '若整个任务已完成，直接给最终答复结束本轮；否则只发下一步还没做的命令。）',
    }
  }
  return null
}

module.exports.replayItemToText = replayItemToText
module.exports.RESULT_HEAD = RESULT_HEAD
module.exports.handsBlock = handsBlock
module.exports.distillExecOutput = distillExecOutput
module.exports.truncateToolOutput = truncateToolOutput
module.exports.TOOL_OUTPUT_MAX_CHARS = TOOL_OUTPUT_MAX_CHARS
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
  const env = parseEnvironment(items)
  const cwd = env.cwd
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
// 引用标记体只含 ASCII（filecite/turn0file0/L3-L3）+ 私有区分隔符。
// 出现任何非 ASCII 非 PUA 字符（中文/全角标点）即证明扣住的不是标记。
const CITE_BODY_CH = /^[\x20-\x7e-]$/
// flush 时残段长这样才是"断在标记中间"（丢弃合法）；其它内容一律抢救。
const CITE_TAIL_RE = /^[a-z]*cite|^turn\d|^L\d+(-L\d+)?$|^[\s\x20-\x2f]*$/

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
          // ── 2026-08-09 用户现场（"需要指定目标飞书文档（"后整段没了）：
          // 上游发了个孤立的 （断流重接/半个标记），后面跟的是**正文**。
          // 旧逻辑扣满 CITE_MAX=200 才放行、流又在 60 字符处结束 -> flush 把
          // 正文当"未闭合标记"整段丢弃。复现：59 字符只剩 11。
          // 判据收紧：标记体只含 ASCII + PUA 分隔符（filecite/turn0file0/L3-L3），
          // 出现任何中文/全角字符即证明扣住的不是标记 —— 立即吐出，别等 200。
          if (ch === CITE_END) { held = ''; continue }   // 完整标记，整段丢弃
          if (ch === CITE_SEP || PUA_RE.test(ch)) { PUA_RE.lastIndex = 0; held += ch; continue }
          if (!CITE_BODY_CH.test(ch)) {
            out += held.replace(PUA_RE, '') + ch
            held = ''
            continue
          }
          held += ch
          if (held.length > CITE_MAX) {            // 不像标记，别扣着正文
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
    /**
     * 收尾。残段**只有真像标记时才丢**（filecite…/turn0…/L3-L3 或纯分隔符）——
     * 那正是"末尾只剩 filecite"的来源。其它内容是被误扣的正文，抢救回来。
     */
    flush() {
      const dangling = held
      held = ''
      const body = dangling.replace(PUA_RE, '')
      if (!dangling) return { text: '', truncated: false, dropped: 0 }
      if (CITE_TAIL_RE.test(body)) return { text: '', truncated: true, dropped: dangling.length }
      return { text: body, truncated: false, dropped: dangling.length - body.length }
    },
    get pending() { return held.length },
  }
}

/** 非流式的一把梭版本。 */
function stripCitations(text) {
  if (typeof text !== 'string' || !text) return text
  const f = makeCitationFilter()
  const out = f.push(text)
  const tail = f.flush()
  return out + (tail.text || '')
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

// ── 出站守恒守卫（系统性防御，2026-08-09）──────────────────────────────
//
// 复盘：本网关历史上 14 个 bug 里 8 个的本质都是**转换点静默吃正文**
// （引用剥离器扣留吞尾、画布剥离、flatten 拼空串、compact 全丢、buffer 不放…）。
// 逐个修补丁堵不完 —— 任何未来新增的过滤器都可能再引入同款。
//
// 架构解法：在**最终出口**设一条守恒不变量：
//     交付给客户端的文本量 ≈ 模型产出的文本量 - 可解释的删除量
// 各过滤器自己申报删了多少（accounting）；不变量被打破（丢失超过申报 + 容差）
// 时，**放弃过滤结果、回退到只做最小安全清洗的原文**（剥 PUA 字符 —— 这一步
// 永远无害），并响亮记录。宁可漏一个标记给用户，不可吞一段正文。
// 这把"静默吃正文"这一整类 bug 从「用户撞上+复现才知道」降级为
// 「自动回退+一行日志」。
const GUARD_TOLERANCE = 80        // 绝对容差（标记本体、空白折叠等零碎）
const GUARD_RATIO = 0.10          // 相对容差：未申报丢失超过原文 10% 才回退
const PUA_ALL_RE = /[-]/g

/**
 * @param raw        过滤前的模型原文
 * @param processed  过滤后的文本
 * @param accounted  各过滤器申报的合法删除量（字符数），如引用标记长度
 * @returns {text, fellBack, unexplained}
 */
function guardOutbound(raw, processed, accounted) {
  const r = String(raw || ''), p = String(processed || '')
  const lost = r.length - p.length
  const unexplained = lost - (accounted || 0)
  if (unexplained <= GUARD_TOLERANCE || unexplained <= r.length * GUARD_RATIO) {
    return { text: p, fellBack: false, unexplained: Math.max(0, unexplained) }
  }
  // 不变量被打破：过滤器吃了没申报的正文。回退到最小安全清洗。
  const safe = r.replace(PUA_ALL_RE, '')
  console.error(`[guard] 出站守恒被打破: raw=${r.length} processed=${p.length}`
    + ` accounted=${accounted || 0} unexplained=${unexplained} -> 回退最小清洗`)
  return { text: safe, fellBack: true, unexplained }
}

module.exports.guardOutbound = guardOutbound

// ── 入站形状封闭（系统性防御，2026-08-09）──────────────────────────────
//
// 复盘：4/14 个 bug 是"不认识的 item 形状被拼成空串"（custom_tool_call_output
// 无 role、function_call 回放、normalize 后 type 被删…）。每来一个新形状翻车
// 一次，翻车形态永远是**静默**的（模型收到空 "USER: "，答非所问）。
//
// 架构解法：把"默认分支"从「返回空」改成「可见占位 + 计数」。任何未被显式
// 处理的形状都会在日志里现形（unknownShapes），第一次出现就能定位，
// 而模型至少知道"这里有个东西没转译"而不是收到空行。
function describeUnknownItem(item) {
  if (!item || typeof item !== 'object') return null
  const t = item.type || (item.role ? null : 'unknown')
  if (!t || t === 'message') return null
  return `[未转译项 type=${t}${item.name ? ' name=' + item.name : ''}]`
}

module.exports.describeUnknownItem = describeUnknownItem
