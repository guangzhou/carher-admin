#!/usr/bin/env python3
# Round-18: latency/robustness review follow-ups (all mechanism-confirmed)
#  O1 main path has NO keepalive between response.created and the first content
#     byte — upstream thinking can take 20s+; retry/fallback paths already ship
#     2s comment frames (live-proven harmless through the LiteLLM front door)
#     but the primary path was left silent. 5s comment heartbeat, ZK_HB=0 off.
#  O2 full-resend flatten (cache miss / conv invalid) carries every historical
#     tool output verbatim -> easily >100K -> attachment-upload path (slow,
#     measured zero-output risk). Truncate all but the LAST tool exchange
#     (truncating the latest one causes the amnesia loop we hit on codex).
#  O3 conv TTL 30min wastes a full resend after any pause; correctness is
#     guarded by prefix digests + the delta-send failure catch, so TTL is just
#     hygiene. Default 240min, ZK_CONV_TTL_MIN override.
#  O4 Map.set on an existing key keeps its old insertion position -> an ACTIVE
#     conversation can be LRU-evicted as "oldest". delete-before-set refreshes.
src = open('/tmp/responses.r17.js').read()
orig = src

# ── O3: TTL env ───────────────────────────────────────────────────────
a_old = "const _CONV_TTL_MS = 30 * 60 * 1000"
assert src.count(a_old) == 1, 'anchor A=%d' % src.count(a_old)
src = src.replace(a_old,
    "const _CONV_TTL_MS = parseInt(process.env.ZK_CONV_TTL_MIN || '240', 10) * 60 * 1000")

# ── O4: LRU refresh ───────────────────────────────────────────────────
b_old = """  const key = _convKeyOf(input, instructions)
  _convCache.set(key, {"""
assert src.count(b_old) == 1, 'anchor B=%d' % src.count(b_old)
b_new = """  const key = _convKeyOf(input, instructions)
  // Map.set 更新已有 key 不刷新插入序 —— 活跃会话会被当"最旧"LRU 逐出
  //（逐出只是多付一次全量，但没必要）。先删再插把它挪到队尾。
  _convCache.delete(key)
  _convCache.set(key, {"""
src = src.replace(b_old, b_new)

# ── O2: history tool-output diet helper (after dietCursorInput) ───────
c_old = """function extractUserQuery(s) {"""
assert src.count(c_old) == 1, 'anchor C=%d' % src.count(c_old)
c_new = """// r18：全量重发（缓存 miss/会话失效）时历史工具输出是体积大头，长会话轻易
// 破 INLINE_MAX 走附件上传（慢且实测有零输出风险）。只保留**最近一次**工具
// 交换的完整输出，更早的截头 1200+尾 200 —— 最近一次必须完整（截了会失忆
// 死循环，codex 压缩侧踩过同款）。只影响发给网页的 flatten，客户端 items 与
// 会话指纹不动。ZK_HISTDIET=0 关。
const _HIST_KEEP = 1200
function dietOldToolOutputs(input) {
  if (process.env.ZK_HISTDIET === '0' || !Array.isArray(input)) return input
  let last = -1
  for (let i = input.length - 1; i >= 0; i--) {
    const t = input[i] && input[i].type
    if (t === 'function_call_output' || t === 'custom_tool_call_output') { last = i; break }
  }
  return input.map((it, i) => {
    if (i === last || !it || typeof it !== 'object') return it
    if (it.type !== 'function_call_output' && it.type !== 'custom_tool_call_output') return it
    if (typeof it.output !== 'string' || it.output.length <= _HIST_KEEP + 600) return it
    const cut = it.output.slice(0, _HIST_KEEP)
      + `\\n\\u2026[${it.output.length - _HIST_KEEP - 200} chars truncated]\\u2026\\n`
      + it.output.slice(-200)
    return { ...it, output: cut }
  })
}

function extractUserQuery(s) {"""
src = src.replace(c_old, c_new)

# ── O2: wire into the three full-flatten sites ────────────────────────
d_old = "dietCursorInput(codexInput)"
n = src.count(d_old)
assert n == 3, 'anchor D=%d' % n
src = src.replace(d_old, "dietCursorInput(dietOldToolOutputs(codexInput))")

# ── O1: main-path heartbeat (after started/finished declared — TDZ safe) ─
e_old = """    let full = ''
    let started = false
    let finished = false"""
assert src.count(e_old) == 1, 'anchor E=%d' % src.count(e_old)
e_new = """    let full = ''
    let started = false
    let finished = false
    // r18：首包到第一个内容字节之间隔着上游 thinking（可 20s+），这段主路径
    // 完全静默 —— retry/fallback 的 2s 注释帧已在前门 live 验证无害，主路径
    // 补同款（5s 一帧，规范要求客户端忽略 ":" 行）。自清理：出内容或收尾即停。
    // ZK_HB=0 关。
    let _hbMain = null
    if (stream && process.env.ZK_HB !== '0') {
      _hbMain = setInterval(() => {
        if (started || finished) { clearInterval(_hbMain); _hbMain = null; return }
        try { res.write(': hb\\n\\n') } catch (_) {}
      }, 5000)
    }"""
src = src.replace(e_old, e_new)

assert src != orig
open('/tmp/responses.r18.js', 'w').write(src)
print('round18 OK: %d chars' % len(src))
