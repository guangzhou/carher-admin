#!/usr/bin/env python3
# Round-17: latency levers (architecture review follow-up)
#  L1 default thinking effort for web-tools turns = 'standard' (ZK_TE_DEFAULT
#     override; =0 disables and returns to account default). Tool-loop turns
#     only need "the next bash command" — deep thinking just adds seconds.
#     Client-sent reasoning.effort (mapped upstream of this) still wins.
#     Plus a [te] log line so the knob is observable end-to-end.
#  L2 prompt nudge: chain related steps with && — fewer web turns per task.
src = open('/tmp/responses.r16.js').read()
orig = src

# ── L1a: const -> let ─────────────────────────────────────────────────
a_old = """    const thinkingEffort = _carher_rawEffort
      ? (_CARHER_TE_MAP[String(_carher_rawEffort).toLowerCase()] || null)
      : null"""
assert src.count(a_old) == 1, 'anchor A=%d' % src.count(a_old)
a_new = """    let thinkingEffort = _carher_rawEffort
      ? (_CARHER_TE_MAP[String(_carher_rawEffort).toLowerCase()] || null)
      : null"""
src = src.replace(a_old, a_new)

# ── L1b: default + observability after useWebTools is known ───────────
b_old = "    const useWebTools = _webToolDefs.length > 0 && !hasTokens()"
assert src.count(b_old) == 1, 'anchor B=%d' % src.count(b_old)
b_new = """    const useWebTools = _webToolDefs.length > 0 && !hasTokens()
    // r17：web-tools 轮默认 thinking 档钉 standard（ZK_TE_DEFAULT 覆盖，=0 关
    // 回账号默认）。工具轮要的是"下一条 bash 命令"，深思考只添秒数；客户端
    // 显式传 reasoning.effort 时优先（上面映射后的值不被覆盖）。
    if (useWebTools && !thinkingEffort && process.env.ZK_TE_DEFAULT !== '0') {
      thinkingEffort = process.env.ZK_TE_DEFAULT || 'standard'
    }
    if (thinkingEffort) console.log(`[te] thinking_effort=${thinkingEffort} (${_carher_rawEffort ? 'client' : 'default'})`)"""
src = src.replace(b_old, b_new)

# ── L2: batch nudge (main prefix + conv-fail resend prefix) ───────────
c_old = "Prefer a single bash shell command. Do NOT use your python/analysis tool"
n = src.count(c_old)
assert n == 2, 'anchor C=%d' % n
src = src.replace(c_old,
    "Prefer a single bash shell command; chain related steps with && when reasonable. Do NOT use your python/analysis tool")

assert src != orig
open('/tmp/responses.r17.js', 'w').write(src)
print('round17 OK: %d chars' % len(src))
