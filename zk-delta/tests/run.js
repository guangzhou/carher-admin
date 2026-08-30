'use strict'
/*
 * zk-delta/tests/run.js  —— S3 硬验收
 *
 *  用法：node zk-delta/tests/run.js
 *
 *  验收项（对应计划 §3 S3）：
 *    ①  逐字节金样：9 轮真实量级会话，上游每一轮实际收到的字节 === Cursor 原始字节
 *    ②  上行字节：第 9 轮出网字节 / 第 9 轮原始字节 < 1%
 *    ③  失效硬报错：篡改 handle / base_digest → 409，且上游一发都没收到
 *    ④  前缀被改：自动回落全量、计数 +1、上游收到的仍逐字节相同
 *
 *  另外补了几条我认为同样致命的：
 *    ⑤  不能逐字节复现的 body 一律原样透传（门① 的兜底）
 *    ⑥  会话中途换工具集（template 变）仍逐字节相同
 *    ⑦  上游 5xx 时服务端不提交状态，下一轮不会错位
 *    ⑧  换一把 key 不能拿到别人的 handle
 *    ⑩  编码对抗性性质测试（见 fuzz_encoding.js）：对任意输入字节，要么证明
 *        逐字节可复现并真的复现对，要么干净地拒掉走透传——绝不出现"说 ok 却重建错"
 */

const path = require('path')
const assert = require('assert')
const H = require('./harness')
const F = require('../common/framing')

const P_LITELLM = 18891   // 假 LiteLLM（服务端的上游）
const P_LEGACY = 18892    // 假"今天的地址"（小代理回落时走这里）
const P_SERVER = 18893
const P_SIDECAR = 18894

const logs = []
let pass = 0; let fail = 0
const failures = []

function ok (name, cond, detail) {
  if (cond) { pass++; console.log('  \u2713 ' + name) } else {
    fail++; failures.push(name + (detail ? ' :: ' + detail : ''))
    console.log('  \u2717 ' + name + (detail ? ' :: ' + detail : ''))
  }
}

function bodyBytes (obj) { return Buffer.from(JSON.stringify(obj), 'utf8') }

async function metrics (port) {
  const r = await H.get(port, '/metrics.json')
  return JSON.parse(r.body.toString('utf8'))
}

async function main () {
  console.log('zk-delta S3 离线验收\n' + '='.repeat(60))

  // ---------- 0. 纯函数单测 ----------
  console.log('\n[0] framing 纯函数')
  {
    const raw = JSON.stringify({ b: 1, messages: [{ x: 1 }], a: 'z' })
    const p = F.proveRoundTrip(raw)
    ok('紧凑 JSON 能逐字节往返', p.ok && p.rebuilt === raw, p.reason)
    ok('键序被保留', Object.keys(p.template).join(',') === 'b,messages,a')

    ok('美化过的 JSON 判为不可复现',
      F.proveRoundTrip(JSON.stringify({ messages: [{ x: 1 }] }, null, 2)).reason === 'byte_mismatch')
    ok('1.0 这类数字字面量判为不可复现',
      F.proveRoundTrip('{"messages":[{"x":1}],"temperature":1.0}').reason === 'byte_mismatch')
    ok('\\u 转义判为不可复现',
      F.proveRoundTrip('{"messages":[{"x":"\\u00e9"}]}').reason === 'byte_mismatch')
    ok('非 JSON 判掉', F.proveRoundTrip('not json').reason === 'not_json')
    ok('没有 messages/input 判掉', F.proveRoundTrip('{"a":1}').reason === 'no_array_key')

    const ds = F.itemDigests([{ a: 1 }, { b: 2 }, { c: 3 }])
    ok('前缀摘要对长度敏感', F.prefixDigest(ds, 2) !== F.prefixDigest(ds, 3))
    const best = F.findLongestPrefix(
      [{ count: 1, digests: ds.slice(0, 1) }, { count: 2, digests: ds.slice(0, 2) }], ds)
    ok('取最长前缀', best && best.count === 2)
    ok('等长不算严格前缀', F.findLongestPrefix([{ count: 3, digests: ds }], ds) === null)
  }

  // ---------- 起链路 ----------
  console.log('\n[1] 起链路：假上游 ← 真服务端 ← 真小代理')
  const litellm = await H.startRecorder(P_LITELLM, 'litellm')
  const legacy = await H.startRecorder(P_LEGACY, 'legacy')

  const srv = H.spawnNode(path.join(__dirname, '../server/server.js'), {
    ZKD_PORT: String(P_SERVER),
    ZKD_UPSTREAM: 'http://127.0.0.1:' + P_LITELLM
  }, '[srv]', logs)

  const side = H.spawnNode(path.join(__dirname, '../sidecar/sidecar.js'), {
    ZKD_PORT: String(P_SIDECAR),
    ZKD_DELTA_URL: 'http://127.0.0.1:' + P_SERVER + '/zkd/v1/delta',
    ZKD_UPSTREAM: 'http://127.0.0.1:' + P_LEGACY
  }, '[side]', logs)

  const cleanup = () => { try { srv.kill() } catch (e) {} ; try { side.kill() } catch (e) {} ; litellm.srv.close(); legacy.srv.close() }
  process.on('exit', cleanup)

  try {
    await H.waitHealthy(P_SERVER)
    await H.waitHealthy(P_SIDECAR)
    ok('服务端与小代理都健康', true)

    // ---------- 2. 逐字节金样 + 带宽 ----------
    console.log('\n[2] 9 轮真实量级会话：逐字节金样 + 带宽')
    const N = 9
    const originals = []
    const uplinks = []
    let allIdentical = true
    let firstDiff = ''

    for (let t = 1; t <= N; t++) {
      const body = H.makeCursorBody(t)
      const raw = bodyBytes(body)
      originals.push(raw)

      const m0 = await metrics(P_SIDECAR)
      const r = await H.post(P_SIDECAR, '/v1/chat/completions', raw)
      const m1 = await metrics(P_SIDECAR)
      uplinks.push(m1.bytes_uplink - m0.bytes_uplink)

      if (r.status !== 200) { allIdentical = false; firstDiff = `turn${t} status=${r.status}` ; break }

      const recv = litellm.got[litellm.got.length - 1]
      if (!recv) { allIdentical = false; firstDiff = `turn${t} 上游没收到` ; break }
      if (Buffer.compare(recv.body, raw) !== 0) {
        allIdentical = false
        firstDiff = `turn${t} 上游收到 ${recv.body.length}B，原始 ${raw.length}B`
        break
      }
    }

    ok('①  9 轮每一轮上游收到的字节都与原始逐字节相同', allIdentical, firstDiff)
    ok('   上游一共收到 9 发', litellm.got.length === N, 'got=' + litellm.got.length)
    ok('   回落通道一发都没走', legacy.got.length === 0, 'legacy=' + legacy.got.length)

    const ratio9 = uplinks[N - 1] / originals[N - 1].length
    console.log(`     第1轮 原始 ${originals[0].length}B / 出网 ${uplinks[0]}B`)
    console.log(`     第9轮 原始 ${originals[N - 1].length}B / 出网 ${uplinks[N - 1]}B  = ${(ratio9 * 100).toFixed(3)}%`)

    // 判据修正说明：
    //   计划 §3 S3 里我原先写的是"第 9 轮出网 < 原始的 1%"。那个数是拍脑袋定的，
    //   对着数据一算就站不住：增量的下限就是这一轮真正新增的内容本身。
    //   事故实测里 Cursor 每轮恒定新增约 505,700 字节，第 14 轮总量 7MB，
    //   增量占比天然就是 7%，永远到不了 1%。
    //   所以真正该验的是这两条：
    //     (a) 出网不随轮次增长 —— 这才是 G2 的原话
    //     (b) 出网只比"真新增内容"多出可忽略的协议开销
    const bNew = Buffer.byteLength(JSON.stringify(
      JSON.parse(originals[N - 1].toString('utf8')).messages.slice(
        JSON.parse(originals[N - 2].toString('utf8')).messages.length)), 'utf8')
    const overhead = uplinks[N - 1] / bNew
    console.log(`     第9轮真正新增内容 ${bNew}B，出网 ${uplinks[N - 1]}B，协议开销 ${((overhead - 1) * 100).toFixed(2)}%`)
    ok('②a 出网只比真新增内容多出 <5% 的协议开销', overhead < 1.05, overhead.toFixed(4) + 'x')

    const growth = uplinks[N - 1] / uplinks[0]
    console.log(`     出网增长倍数 第9轮/第1轮 = ${growth.toFixed(3)}x（G2 要求 < 2）`)
    ok('②b G2：出网不随轮次线性增长', growth < 2, growth.toFixed(3) + 'x')

    const totOrig = originals.reduce((a, b) => a + b.length, 0)
    const totUp = uplinks.reduce((a, b) => a + b, 0)
    console.log(`     9 轮累计：本来要发 ${(totOrig / 1048576).toFixed(1)}MB，实际出网 ${(totUp / 1048576).toFixed(1)}MB，省 ${(100 - totUp * 100 / totOrig).toFixed(1)}%`)
    ok('②c 9 轮累计省下 >50% 上行', totUp / totOrig < 0.5, (totUp * 100 / totOrig).toFixed(1) + '%')

    // ---------- 3. 篡改必须硬报错 ----------
    console.log('\n[3] 失效硬报错（直接打服务端，绕开小代理）')
    const before = litellm.got.length
    const bodyT = H.makeCursorBody(3)
    const proof = F.proveRoundTrip(JSON.stringify(bodyT))
    const digests = F.itemDigests(proof.items)

    const mkEnv = (over) => Object.assign({
      v: 1, path: '/v1/chat/completions', array_key: 'messages',
      handle: null, base_count: 0, base_digest: F.prefixDigest([], 0),
      template_digest: F.templateDigest(proof.template), template: proof.template,
      delta: proof.items
    }, over)

    let r = await H.post(P_SERVER, '/zkd/v1/delta', JSON.stringify(mkEnv({ handle: 'zd_deadbeef' })))
    ok('③a 认不出的 handle → 409', r.status === 409, 'status=' + r.status)
    ok('   409 里带 delta_base_not_found', /delta_base_not_found/.test(r.body.toString()))

    // 建一条真会话再篡改它的 base_digest
    r = await H.post(P_SERVER, '/zkd/v1/delta', JSON.stringify(mkEnv({})))
    const h = r.headers['x-zk-handle']
    ok('   建会话拿到 handle', !!h)
    r = await H.post(P_SERVER, '/zkd/v1/delta', JSON.stringify({
      v: 1, path: '/v1/chat/completions', array_key: 'messages',
      handle: h, base_count: proof.items.length, base_digest: 'f'.repeat(64),
      template_digest: F.templateDigest(proof.template), delta: [{ role: 'user', content: 'x' }]
    }))
    ok('③b 篡改 base_digest → 409', r.status === 409, 'status=' + r.status)
    r = await H.post(P_SERVER, '/zkd/v1/delta', JSON.stringify({
      v: 1, path: '/v1/chat/completions', array_key: 'messages',
      handle: h, base_count: 999, base_digest: F.prefixDigest(digests, proof.items.length),
      template_digest: F.templateDigest(proof.template), delta: [{ role: 'user', content: 'x' }]
    }))
    ok('③c base_count 对不上 → 409', r.status === 409, 'status=' + r.status)
    r = await H.post(P_SERVER, '/zkd/v1/delta', JSON.stringify({
      v: 1, path: '/v1/chat/completions', array_key: 'messages',
      handle: h, base_count: proof.items.length,
      base_digest: F.prefixDigest(digests, proof.items.length),
      template_digest: 'a'.repeat(64), delta: [{ role: 'user', content: 'x' }]
    }))
    ok('③d template 变了却没带上来 → 409', r.status === 409, 'status=' + r.status)
    r = await H.post(P_SERVER, '/zkd/v1/delta', JSON.stringify({
      v: 1, path: '/v1/chat/completions', array_key: 'messages',
      handle: h, base_count: proof.items.length,
      base_digest: F.prefixDigest(digests, proof.items.length),
      template_digest: F.templateDigest(proof.template), delta: [{ role: 'user', content: 'x' }]
    }), { authorization: 'Bearer sk-somebody-else' })
    ok('⑧  换一把 key 拿不到别人的 handle → 409', r.status === 409, 'status=' + r.status)

    // 只有那一发合法的建会话请求应该到上游；4 发篡改一发都不许到
    ok('③  4 发篡改里没有任何一发被当全量转发',
      litellm.got.length === before + 1, `上游多收了 ${litellm.got.length - before} 发，应为 1`)

    // ---------- 4. 前缀被改 → 自动回落全量 ----------
    console.log('\n[4] 用户编辑了历史消息（前缀变了）')
    {
      const m0 = await metrics(P_SIDECAR)
      const edited = H.makeCursorBody(9)
      edited.messages[3].content = '被用户改过的历史消息'
      const raw = bodyBytes(edited)
      const rr = await H.post(P_SIDECAR, '/v1/chat/completions', raw)
      const m1 = await metrics(P_SIDECAR)
      const recv = litellm.got[litellm.got.length - 1]
      ok('④  前缀被改后仍返回 200', rr.status === 200, 'status=' + rr.status)
      ok('   上游收到的仍与原始逐字节相同', recv && Buffer.compare(recv.body, raw) === 0)
      ok('   小代理确实走了全量而不是增量', m1.full_sent > m0.full_sent,
        `full ${m0.full_sent}->${m1.full_sent}`)
      ok('   没有静默：回落有计数', (m1.full_sent - m0.full_sent) === 1)
    }

    // ---------- 5. 不可逐字节复现 → 原样透传 ----------
    console.log('\n[5] 不能逐字节复现的 body 必须原样透传（门① 兜底）')
    {
      const weird = Buffer.from('{\n  "model": "x",\n  "temperature": 1.0,\n  "messages": [{"role":"user","content":"\\u00e9"}]\n}', 'utf8')
      const g0 = legacy.got.length
      const rr = await H.post(P_SIDECAR, '/v1/chat/completions', weird)
      ok('⑤  返回 200', rr.status === 200, 'status=' + rr.status)
      ok('   走了回落通道', legacy.got.length === g0 + 1, 'legacy +' + (legacy.got.length - g0))
      const recv = legacy.got[legacy.got.length - 1]
      ok('   回落通道收到的是原始字节，一个字节都没改',
        recv && Buffer.compare(recv.body, weird) === 0,
        recv ? `${recv.body.length} vs ${weird.length}` : 'none')
      const ms = await metrics(P_SIDECAR)
      ok('   降级被计数了', (ms.fallback_by_reason['roundtrip_byte_mismatch'] || 0) >= 1,
        JSON.stringify(ms.fallback_by_reason))
    }

    // ---------- 6. 中途换工具集 ----------
    console.log('\n[6] 会话中途换工具集（template 变）')
    {
      const b1 = H.makeCursorBody(2)
      const r1raw = bodyBytes(b1)
      await H.post(P_SIDECAR, '/v1/chat/completions', r1raw)

      const b2 = H.makeCursorBody(3)
      b2.tools = b2.tools.slice(0, 5)          // 工具集变了
      b2.reasoning_effort = 'high'             // 参数也变了
      const r2raw = bodyBytes(b2)
      const rr = await H.post(P_SIDECAR, '/v1/chat/completions', r2raw)
      const recv = litellm.got[litellm.got.length - 1]
      ok('⑥  换工具集后仍 200', rr.status === 200, 'status=' + rr.status)
      ok('   上游收到的仍逐字节相同', recv && Buffer.compare(recv.body, r2raw) === 0,
        recv ? `${recv.body.length} vs ${r2raw.length}` : 'none')
    }

    // ---------- 7. responses 端点 ----------
    console.log('\n[7] /v1/responses 端点（input 数组）')
    {
      const b = { model: 'm', input: [{ role: 'user', content: 'hi' }], stream: true }
      const raw = bodyBytes(b)
      const rr = await H.post(P_SIDECAR, '/v1/responses', raw)
      const recv = litellm.got[litellm.got.length - 1]
      ok('⑦  responses 走通且逐字节相同',
        rr.status === 200 && recv && Buffer.compare(recv.body, raw) === 0 && recv.path === '/v1/responses',
        recv ? recv.path + ' ' + recv.body.length : 'none')
    }

    // ---------- 8. 真实抓包金样（有就跑） ----------
    console.log('\n[8] 真实 Cursor 抓包金样')
    {
      const fs = require('fs')
      const dir = path.join(__dirname, 'fixtures')
      let files = []
      try { files = fs.readdirSync(dir).filter((f) => f.endsWith('.json')).sort() } catch (e) {}
      if (files.length === 0) {
        console.log('     （fixtures/ 为空 —— 真实抓包在 S4 用 ZKD_CAPTURE 采集后回灌，此项此刻不算通过）')
        ok('⑨  真实抓包金样', false, '尚未采集，S4 采集后必须回来重跑')
      } else {
        let allOk = true; let bad = ''
        for (const f of files) {
          const raw = fs.readFileSync(path.join(dir, f))
          const before2 = litellm.got.length + legacy.got.length
          const rr = await H.post(P_SIDECAR, '/v1/chat/completions', raw)
          const recv = litellm.got[litellm.got.length - 1] || legacy.got[legacy.got.length - 1]
          const grew = (litellm.got.length + legacy.got.length) === before2 + 1
          if (!(rr.status === 200 && grew && recv && Buffer.compare(recv.body, raw) === 0)) {
            allOk = false; bad = f; break
          }
        }
        ok('⑨  真实抓包每一发上游收到的都逐字节相同（n=' + files.length + '）', allOk, bad)
      }
    }

    // ---------- ⑩ 编码对抗性性质测试 ----------
    // ⑨ 想抓的风险本质是"对方的序列化器写出来的字节我能不能原样复现"——那是编码问题
    // 不是内容问题。这一组用对抗性编码把这条风险面系统性地打一遍，不依赖任何真实抓包。
    // 它**不替代** ⑨：真实抓包还要采。它只是让"没采到之前"不等于"没验过"。
    console.log('\n[10] 编码对抗性性质测试')
    {
      const fz = require('./fuzz_encoding')
      const st = fz.run(ok)
      console.log('     样本 ' + st.checked + ' 条：进增量 ' + st.proved + '，退全量 ' + st.refused)
    }

    // ---------- 汇总 ----------
    const ms = await metrics(P_SIDECAR)
    const mv = JSON.parse((await H.get(P_SERVER, '/metrics.json')).body.toString())
    console.log('\n' + '='.repeat(60))
    console.log('小代理计数：', JSON.stringify({
      req: ms.req_total, delta: ms.delta_sent, full: ms.full_sent, passthru: ms.passthru,
      conflict409: ms.conflict_409, fallback: ms.fallback_by_reason
    }))
    console.log('服务端计数：', JSON.stringify({
      req: mv.req_total, delta: mv.req_delta, full: mv.req_full,
      rebuild_ok: mv.rebuild_ok, reject409: mv.reject_409, reasons: mv.reject_by_reason,
      bytes_in: mv.bytes_in, bytes_out: mv.bytes_out
    }))
    console.log(`服务端侧放大比：收 ${mv.bytes_in}B → 发 ${mv.bytes_out}B = ${(mv.bytes_out / mv.bytes_in).toFixed(1)}x`)
    console.log('='.repeat(60))
    console.log(`PASS ${pass}   FAIL ${fail}`)
    if (fail) {
      console.log('\n失败项：')
      for (const f of failures) console.log('  - ' + f)
      console.log('\n服务日志尾部：')
      for (const l of logs.slice(-40)) console.log('  ' + l)
    }
  } finally {
    cleanup()
  }
  process.exit(fail ? 1 : 0)
}

main().catch((e) => { console.error(e); process.exit(2) })
