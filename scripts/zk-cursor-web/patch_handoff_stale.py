#!/usr/bin/env python3
"""patch_handoff_stale.py —— handoff 轮询「抢跑取到上一轮消息」硬修。

## 症状（2026-09-02 用户真实会话 conv=6a97750b / 6a976d44 实证）
1. 同一条命令被执行 2~3 次：连着几轮 `[handoff] done polls=1 chars=49`，
   49 字逐轮相同 —— 取回的是同一条含命令块的旧消息，于是同一条命令被解析执行多次。
2. 问「快速排序」返回上一轮 ls 的结果：
   `01:00:25 ... chars=438` → `prompt: '快速排序'` → `01:00:32 ... chars=438`，438=438。

## 根因
`_handoffPoll` 取「整个会话里最新一条 assistant/recipient=all/text 消息」，
**没有任何一处检查这条消息属不属于本轮**。本轮新消息还没落库时挑到上一轮那条，
而它的 status 早就是 finished_successfully → 当场 finish() 交付。

## 修法（最小面、可秒关、绝不会比现状更差）
按 convId 记住**上一轮交付过的正文**；本轮快照与它逐字相同且本轮还没流出任何字符时，
判为「新消息还没落库」，继续轮询而不是收工。等超过 ZK_HANDOFF_STALE_MS 仍只有旧的，
则**照旧交付**（落回今天的行为），所以最坏情况 == 现状，不会引入新的空回显。

开关：ZK_HANDOFF_STALE_GUARD=0 秒关；ZK_HANDOFF_STALE_MS 调等待预算（默认 30s，
远小于 ZK_HANDOFF_MAX_MS 的 240s）。

用法: python3 patch_handoff_stale.py <in.js> <out.js>
"""
import sys

src = open(sys.argv[1], encoding="utf-8").read()

# ── 锚点 1：缓存声明处，挂一张同族的小表 ────────────────────────────
A1 = "const _convCache = new Map()"
assert src.count(A1) == 1, "A1 anchor count=%d" % src.count(A1)
src = src.replace(A1, A1 + """
// [handoff-stale] convId -> 上一轮 handoff 轮询交付过的正文。只服务一个判别:
// 本轮快照与上一轮交付逐字相同 = 本轮新消息还没落库(抢跑),不是"模型又说了一遍"。
// 与 _convCache 分开:那张表要落盘,不动它的 schema。
const _handoffLast = new Map()""", 1)

# ── 锚点 2：轮询局部变量,加 stale 判别所需状态 ──────────────────────
A2 = "      let polls = 0, errs = 0, mism = 0\n"
assert src.count(A2) == 1, "A2 anchor count=%d" % src.count(A2)
src = src.replace(A2, A2 + """      // [handoff-stale] 上一轮交付过的正文(同一 convId),用于判"抢跑取到旧消息"
      const _staleGuard = process.env.ZK_HANDOFF_STALE_GUARD !== '0'
      const _staleMs = parseInt(process.env.ZK_HANDOFF_STALE_MS || '30000', 10)
      const _prevTxt = _staleGuard ? _handoffLast.get(convId) : null
      let _staleLogged = false
      let _staleHits = 0
""", 1)

# ── 锚点 3：拿到 best 之后、消费之前插闸 ────────────────────────────
A3 = """        if (best) {
          if (best.txt.length > full.length && best.txt.startsWith(full)) {"""
assert src.count(A3) == 1, "A3 anchor count=%d" % src.count(A3)
src = src.replace(A3, """        if (best) {
          // [handoff-stale] 本轮一个字都还没流出,而快照与上一轮交付的正文逐字相同
          // ⇒ 本轮新消息还没落库,取到的是上一轮那条(它的 status 早已 finished)。
          // 继续轮询;超预算仍只有旧的就落回原行为照旧交付(最坏==现状)。
          if (_prevTxt && !full && best.txt === _prevTxt && Date.now() - t0 < _staleMs) {
            _staleHits++
            if (!_staleLogged) {
              _staleLogged = true
              console.log(`[handoff-stale] snapshot == prev turn (${best.txt.length}c)`
                + ` -> 本轮新消息未落库,继续轮询(预算 ${_staleMs}ms)`)
            }
            continue
          }
          if (_staleHits) {
            console.log(`[handoff-stale] cleared after ${_staleHits} stale poll(s)`
              + ` in ${Math.round((Date.now() - t0) / 1000)}s -> 拿到本轮新消息 ${best.txt.length}c`)
            _staleHits = 0
          }
          if (best.txt.length > full.length && best.txt.startsWith(full)) {""", 1)

# ── 锚点 4a：done 分支,记下本轮交付的正文 ───────────────────────────
A4 = """            console.log(`[handoff] done in ${Math.round((Date.now() - t0) / 1000)}s`
              + ` polls=${polls} chars=${full.length}`)
            finish()"""
assert src.count(A4) == 1, "A4 anchor count=%d" % src.count(A4)
src = src.replace(A4, """            console.log(`[handoff] done in ${Math.round((Date.now() - t0) / 1000)}s`
              + ` polls=${polls} chars=${full.length}`
              + (_staleHits ? ` stale=${_staleHits}` : ''))
            _rememberHandoff(convId, full)
            finish()""", 1)

# ── 锚点 4b：give-up 分支,同样记账(否则下一轮拿它当"上一轮"会漏判) ──
A5 = """        console.log(`[handoff] give up after ${Math.round((Date.now() - t0) / 1000)}s`
          + ` polls=${polls} chars=${full.length} -> 交付手头已有的`)
        finish()"""
assert src.count(A5) == 1, "A5 anchor count=%d" % src.count(A5)
src = src.replace(A5, """        console.log(`[handoff] give up after ${Math.round((Date.now() - t0) / 1000)}s`
          + ` polls=${polls} chars=${full.length} -> 交付手头已有的`)
        _rememberHandoff(convId, full)
        finish()""", 1)

# ── 锚点 5：_handoffPoll 之前放记账函数 ─────────────────────────────
A6 = "    async function _handoffPoll(convId) {"
assert src.count(A6) == 1, "A6 anchor count=%d" % src.count(A6)
src = src.replace(A6, """    // [handoff-stale] 记下某会话本轮交付的正文,下一轮用它判抢跑。上限同 _convCache。
    function _rememberHandoff(convId, txt) {
      if (!convId || !txt) return
      _handoffLast.delete(convId)
      _handoffLast.set(convId, txt)
      if (_handoffLast.size > 200) _handoffLast.delete(_handoffLast.keys().next().value)
    }

    async function _handoffPoll(convId) {""", 1)

open(sys.argv[2], "w", encoding="utf-8").write(src)
print("ok -> %s  (%d bytes)" % (sys.argv[2], len(src)))
