#!/usr/bin/env python3
"""patch_conv_prefix.py — 会话复用改「最长严格前缀匹配」。

修的问题:key = sha1(input[0]) + ':' + sha1(instructions),而 instructions 由 Cursor
客户端产生、同一个 chat 内隔几轮就变 → key 变 → _convCache.get(key) 直接 undefined →
连逐项前缀 digest 校验都走不到 → 判成首轮 → 全量重发 → GPT 网页新开会话。

改法与 zk-delta/common/framing.js 的 findLongestPrefix 同语义:不做单键查表,
在所有候选里找「是当前 items 的严格前缀且最长」那条。存储槽键改用 convId。
详见 docs/conv-reuse-prefix-match-20260831.md。

用法: python3 scripts/zk-cursor-web/patch_conv_prefix.py <in.js> <out.js>
所有锚点用 assert count==1 钉死,锚点漂了就报错退出,不做模糊匹配。
"""
import sys

src_path, out_path = sys.argv[1], sys.argv[2]
src = open(src_path, encoding='utf-8').read()

# ── A: findConvSession —— 单键查表 → 最长严格前缀扫描 ────────────────────
a_old = """function findConvSession(input, instructions) {
  if (process.env.ZK_CONV_REUSE === '0') return null
  if (!Array.isArray(input) || input.length < 1) return null
  const key = _convKeyOf(input, instructions)
  const s = _convCache.get(key)
  if (!s) return null
  if (Date.now() - s.ts > _CONV_TTL_MS) { _convCache.delete(key); return null }
  if (input.length <= s.count) return null
  for (let i = 0; i < s.count; i++) {
    if (_itemDigest(input[i]) !== s.digests[i]) return null
  }
  return { key, convId: s.convId, parentId: s.parentId, count: s.count }
}"""
assert src.count(a_old) == 1, 'anchor A=%d' % src.count(a_old)

a_new = """function findConvSession(input, instructions) {
  if (process.env.ZK_CONV_REUSE === '0') return null
  if (!Array.isArray(input) || input.length < 1) return null
  // 2026-08-31:不再按 (input[0], instructions) 单键查表。instructions 来自 Cursor
  // 客户端、同一个 chat 内隔几轮就变,key 一变 get() 直接 undefined,连下面这段逐项
  // 前缀校验都走不到 → 判成首轮 → 全量重发 → 网页新开会话(实测 19h / 63 条会话里
  // 12 条是这么来的)。改成「在候选里找最长严格前缀」,与 zk-delta 的 findLongestPrefix
  // 同语义:免疫 instructions 漂移,也免疫不同会话撞车(靠整段前缀区分而非第 0 条)。
  // 正确性只增不减:旧逻辑命中 = 键相等 ∧ 前缀全等,新逻辑 = 前缀全等,是其超集。
  const digests = input.map(_itemDigest)
  const instrDigest = _itemDigest(instructions || '')
  const now = Date.now()
  let best = null, bestKey = null
  let nStale = 0, nNotPrefix = 0, nMismatch = 0
  const expired = []
  for (const [k, s] of _convCache.entries()) {
    if (!s || !Array.isArray(s.digests)) continue
    if (now - s.ts > _CONV_TTL_MS) { expired.push(k); nStale++; continue }
    if (s.count < 1) continue
    if (s.count >= digests.length) { nNotPrefix++; continue }   // 必须是严格前缀
    let ok = true
    for (let i = 0; i < s.count; i++) {
      if (s.digests[i] !== digests[i]) { ok = false; break }
    }
    if (!ok) { nMismatch++; continue }
    // 平局(等长严格前缀)取最近用过的那条。2026-08-31 实测:两次探针文本完全相同,
    // 第 6 轮同时匹配上「上一次那条会话」和「本次这条」,长度都是 10,首个胜出 →
    // 新一轮被接到了旧会话上,两条对话在网页侧混了。真实场景同样会撞(同一句开场白
    // 问两次就够了),所以按 ts 取最新,活跃的那条才是用户正在说话的那条。
    if (!best || s.count > best.count || (s.count === best.count && s.ts > best.ts)) { best = s; bestKey = k }
  }
  for (const k of expired) _convCache.delete(k)
  if (!best) {
    // miss 原因日志:旧版四条 miss 路径打印完全一样,定位只能去翻盘上缓存文件。
    console.log(`[conv] miss items=${digests.length} cands=${_convCache.size} stale=${nStale} notprefix=${nNotPrefix} mismatch=${nMismatch}`)
    return null
  }
  // instrDigest 是 2026-08-31 新增字段;老持久化文件里没有 → undefined → 不判变化,
  // 退化成"当它没变"(等价于旧行为里 instructions 不参与复用路径内容那一面)。
  const instrChanged = best.instrDigest !== undefined && best.instrDigest !== instrDigest
  return { key: bestKey, convId: best.convId, parentId: best.parentId, count: best.count, instrChanged }
}"""
src = src.replace(a_old, a_new)

# ── B: saveConvSession —— 槽键改 convId + 存 instrDigest ────────────────
b_old = """  const key = _convKeyOf(input, instructions)
  // Map.set 更新已有 key 不刷新插入序 —— 活跃会话会被当"最旧"LRU 逐出
  //（逐出只是多付一次全量，但没必要）。先删再插把它挪到队尾。
  _convCache.delete(key)
  _convCache.set(key, {
    convId, parentId: parentId || 'client-created-root',
    count: input.length, digests: input.map(_itemDigest), ts: Date.now(),
  })"""
assert src.count(b_old) == 1, 'anchor B=%d' % src.count(b_old)

b_new = """  // 2026-08-31:槽键改用 convId。旧键含 instructions,提示词一漂同一条会话会占多个槽
  // (实测缓存里同一条对话占了 3 个槽);按 convId 存 = 一条网页会话一个槽,原地更新。
  const key = String(convId)
  // Map.set 更新已有 key 不刷新插入序 —— 活跃会话会被当"最旧"LRU 逐出
  //（逐出只是多付一次全量，但没必要）。先删再插把它挪到队尾。
  _convCache.delete(key)
  _convCache.set(key, {
    convId, parentId: parentId || 'client-created-root',
    count: input.length, digests: input.map(_itemDigest),
    instrDigest: _itemDigest(instructions || ''), ts: Date.now(),
  })"""
src = src.replace(b_old, b_new)

# ── C: 持久化落盘带上 instrDigest ───────────────────────────────────────
c_old = "      arr.push({ key, convId: s.convId, parentId: s.parentId, count: s.count, digests: s.digests, ts: s.ts })"
assert src.count(c_old) == 1, 'anchor C=%d' % src.count(c_old)
c_new = "      arr.push({ key, convId: s.convId, parentId: s.parentId, count: s.count, digests: s.digests, instrDigest: s.instrDigest, ts: s.ts })"
src = src.replace(c_old, c_new)

# ── D: 加载时按 convId 重建槽键(老文件自动迁移) ────────────────────────
d_old = "      _convCache.set(e.key, { convId: e.convId, parentId: e.parentId || 'client-created-root', count: e.count, digests: e.digests, ts: e.ts })"
assert src.count(d_old) == 1, 'anchor D=%d' % src.count(d_old)
# 老文件 key 是 "sha1:sha1",直接照搬会让新逻辑存两份槽;按 convId 重建即自动迁移。
d_new = "      _convCache.set(String(e.convId), { convId: e.convId, parentId: e.parentId || 'client-created-root', count: e.count, digests: e.digests, instrDigest: e.instrDigest, ts: e.ts })"
src = src.replace(d_old, d_new)

# ── E: 命中但 instructions 变了 → 随增量补发;超长则退回全量 ─────────────
e_old = """    let convSess = useWebTools ? findConvSession(input, instructions) : null
    let _convDelta = null
    if (convSess) {
      _convDelta = _stripAssistantItems(input.slice(convSess.count))
      if (!_convDelta.length) convSess = null
    }
    const basePrompt = convSess
      ? flattenInput(prepareCodexInput(_stripIdeHint(_convDelta)), null)
      : flattenInput(useWebTools ? dietCursorInput(dietOldToolOutputs(codexInput)) : codexInput, instructions)"""
assert src.count(e_old) == 1, 'anchor E=%d' % src.count(e_old)

e_new = """    let convSess = useWebTools ? findConvSession(input, instructions) : null
    let _convDelta = null
    if (convSess) {
      _convDelta = _stripAssistantItems(input.slice(convSess.count))
      if (!_convDelta.length) convSess = null
    }
    // 2026-08-31:复用路径原本 flattenInput(..., null) —— instructions 压根不发上游。
    // 旧版靠"提示词一变就全量重发"顺带把新指令送到;既然不再因此断链,就得显式补发,
    // 否则新指令永远到不了模型。超长则退回全量重发(= 与旧版等价),不冒门①的险。
    const _instrMax = parseInt(process.env.ZK_CONV_INSTR_MAX || '8192', 10)
    let _convInstr = null
    if (convSess && convSess.instrChanged && instructions) {
      const _il = String(instructions).length
      if (_il <= _instrMax) {
        _convInstr = instructions
        console.log(`[conv] instructions changed (${_il} chars) -> carried in delta`)
      } else {
        console.log(`[conv] instructions changed but too long (${_il} > ${_instrMax}) -> full resend`)
        convSess = null
        _convDelta = null
      }
    }
    const basePrompt = convSess
      ? flattenInput(prepareCodexInput(_stripIdeHint(_convDelta)), _convInstr)
      : flattenInput(useWebTools ? dietCursorInput(dietOldToolOutputs(codexInput)) : codexInput, instructions)"""
src = src.replace(e_old, e_new)

open(out_path, 'w', encoding='utf-8').write(src)
print('patched: %s -> %s (%d bytes)' % (src_path, out_path, len(src)))
