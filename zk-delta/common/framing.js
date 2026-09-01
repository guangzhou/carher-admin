'use strict'
/*
 * zk-delta/common/framing.js
 *
 * 增量协议的全部纯函数。没有 IO、没有第三方依赖，可离线单测。
 *
 * 核心不变量（S3 验收①依赖的那条）：
 *   服务端重建出、发给 LiteLLM 的 body 字节，与 Cursor 原本会发的 body 字节完全相同。
 *
 * 这条不变量的证明链（每一环都在运行时被检查，不靠"跑几个用例没发现问题"）：
 *   1. 小代理本地自证：stringify(rebuild(template, itemsLocal)) === 原始 raw 字节。
 *      不相等就当场退回全量透传，绝不进增量。
 *   2. 前缀摘要相等 ⇒ 服务端存的每条历史 item 与小代理手上的那条 stringify 完全相同。
 *      （摘要就是对 stringify 结果取 sha256，相等即同串。）
 *   3. delta 里的新 item 与 template 都是原样 JSON 传输，两侧 stringify 一致。
 *   ⇒ 服务端 stringify(rebuild(templateSrv, itemsSrv)) === 原始 raw 字节。
 *
 * 为什么第 1 环是必须的：JSON.stringify 并不承诺逐字节复现输入文本。
 *   1.0 会变成 1、1e3 会变成 1000、é 会变成字面 é、原文里的换行缩进会被抹掉。
 *   这些都跟 Cursor 那一侧的序列化器实现有关，是经验事实而不是语言保证，
 *   所以只能实测，不能假设。
 */

const crypto = require('crypto')

const PROTO_VERSION = 1

function sha256hex (s) {
  return crypto.createHash('sha256').update(Buffer.isBuffer(s) ? s : Buffer.from(s, 'utf8')).digest('hex')
}

/** 单条消息的摘要。两侧都对"已解析对象"取 JSON.stringify，
 *  JS 保留字符串键的插入序，键序天然一致，不需要 canonical 化。 */
function itemDigest (item) {
  return sha256hex(JSON.stringify(item))
}

function itemDigests (arr) {
  return arr.map(itemDigest)
}

/** 前 n 条的滚动摘要。把条数也搅进去，避免 "空数组" 与 "长度 0 的别的东西" 撞值。 */
function prefixDigest (digests, n) {
  const h = crypto.createHash('sha256')
  h.update('zkd1:' + n)
  for (let i = 0; i < n; i++) { h.update('\x1f'); h.update(digests[i]) }
  return h.digest('hex')
}

/** 判断这个 body 走哪个数组键：chat 用 messages，responses 用 input。 */
function arrayKeyOf (body) {
  if (body && Array.isArray(body.messages)) return 'messages'
  if (body && Array.isArray(body.input)) return 'input'
  return null
}

/** 把 body 拆成「模板 + 数组」。模板保留原键序，数组键位置不变、值置空。 */
function splitBody (body, arrayKey) {
  const template = {}
  for (const k of Object.keys(body)) template[k] = (k === arrayKey) ? [] : body[k]
  return { template, items: body[arrayKey] }
}

/** 用模板 + 数组重建 body。键序完全跟随模板。 */
function rebuildBody (template, arrayKey, items) {
  const out = {}
  for (const k of Object.keys(template)) out[k] = (k === arrayKey) ? items : template[k]
  return out
}

function templateDigest (template) {
  return sha256hex(JSON.stringify(template))
}

/**
 * 第 1 环：本地自证往返。
 * @returns {{ok:boolean, reason:string, template:object, items:Array, arrayKey:string, rebuilt:string}}
 */
function proveRoundTrip (rawBuf) {
  const raw = Buffer.isBuffer(rawBuf) ? rawBuf.toString('utf8') : String(rawBuf)
  let body
  try { body = JSON.parse(raw) } catch (e) { return { ok: false, reason: 'not_json' } }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return { ok: false, reason: 'not_object' }
  const arrayKey = arrayKeyOf(body)
  if (!arrayKey) return { ok: false, reason: 'no_array_key' }
  if (body[arrayKey].length === 0) return { ok: false, reason: 'empty_array' }

  const { template, items } = splitBody(body, arrayKey)
  const rebuilt = JSON.stringify(rebuildBody(template, arrayKey, items))
  if (rebuilt !== raw) return { ok: false, reason: 'byte_mismatch', arrayKey }
  return { ok: true, reason: 'ok', template, items, arrayKey, rebuilt }
}

/** 在候选会话里找「是当前 items 的严格前缀」且最长的那一个。
 *  刻意不按 messages[0] 建索引 —— Cursor 每条会话的第 0 条框架消息都一样，
 *  按它建索引会让所有会话撞成同一个 key（这正是网关旧 _convKeyOf 的缺陷）。
 *
 *  等长平局按 ts 取最近（2026-09-01 补）：两条会话有等长严格前缀不是理论问题——
 *  同一句开场白问两次就够了。convList() 是 Map 插入序（旧的在前），平局取先遇到的
 *  那条 = 取到**旧会话**，新一轮会被接到旧分支上。网关侧同款写法当天被实测抓出真
 *  bug（见 memory feedback_prefix_match_tie_break_by_recency），这里是同一个缺陷。 */
function findLongestPrefix (candidates, digests) {
  let best = null
  for (const c of candidates) {
    if (c.count >= digests.length) continue      // 不是严格前缀
    if (c.count < 1) continue
    let ok = true
    for (let i = 0; i < c.count; i++) {
      if (c.digests[i] !== digests[i]) { ok = false; break }
    }
    if (!ok) continue
    if (!best) { best = c; continue }
    if (c.count > best.count) { best = c; continue }
    // 平局：ts 大的（更近的）赢；缺 ts 当 0，永远输给有 ts 的
    if (c.count === best.count && (c.ts || 0) > (best.ts || 0)) best = c
  }
  return best
}

module.exports = {
  PROTO_VERSION,
  sha256hex,
  itemDigest,
  itemDigests,
  prefixDigest,
  arrayKeyOf,
  splitBody,
  rebuildBody,
  templateDigest,
  proveRoundTrip,
  findLongestPrefix
}
