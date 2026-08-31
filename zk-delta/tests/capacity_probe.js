'use strict'
/*
 * zk-delta/tests/capacity_probe.js —— 会话缓存的字节上限该设多少，用实测定，不拍脑袋
 *
 * 为什么要有这个：
 *   服务端 ZKD_MAX_BYTES 默认 800MB，而 pod 内存 limit 是 2Gi。store 里存的不是字节流
 *   而是 JS 对象/字符串，实际 RSS 是 store_bytes 的若干倍——倍率是多少，看代码看不出来。
 *   一旦 OOMKill：单副本 + 会话状态在进程内存里 = 所有人的会话全丢，
 *   **不报错**，只是每条会话下一发退化成全量。正好是最难发现的那种坏法。
 *   全员发之前必须把这条算清：1 个人用和 10 个人用，store 是 10 倍。
 *
 * 做法：本地起真服务端 + 假上游，灌到 store_bytes 过阈值，读它自己报的 rss。
 *      纯本地，一发都不打真上游（不碰 acct-82 的 7d 窗口）。
 *
 * 用法：node zk-delta/tests/capacity_probe.js [目标MB，默认 200]
 */

const path = require('path')
const H = require('./harness')
const F = require('../common/framing')

const P_UP = 18991
const P_SRV = 18993
const TARGET_MB = parseInt(process.argv[2] || '200', 10)
// 每人同时挂几条活跃会话。这是**假设**不是实测——我只有我自己一台机器的行为，
// 没有多人数据。取 8 是因为 Cursor 一个窗口一条 composer 会话，一天开七八个不夸张。
// 换个数结论会变，所以下面按人数那张表必须标明它是假设。
const AVG_CONV_PER_USER = parseInt(process.env.ZKD_CONV_PER_USER || '8', 10)

// 一条真实 Cursor 会话大概 100KB～1.5MB。这里造 ~500KB 一条的会话，
// 用 makeCursorBody 同一套形状，免得测出来的倍率是「我这个玩具形状」的倍率。
function bigBody (seed, turns) {
  const b = H.makeCursorBody(turns)
  b.messages = b.messages.map((m, i) => Object.assign({}, m, {
    content: typeof m.content === 'string' ? m.content + ' #' + seed + '-' + i : m.content
  }))
  return b
}

async function main () {
  const up = await H.startRecorder(P_UP, 'up')
  const logs = []
  const srv = H.spawnNode(path.join(__dirname, '../server/server.js'), {
    ZKD_PORT: String(P_SRV),
    ZKD_UPSTREAM: 'http://127.0.0.1:' + P_UP,
    ZKD_MAX_CONV: '100000',            // 故意放开条数上限，让字节上限成为唯一约束
    ZKD_MAX_BYTES: String(64 * 1024 * 1024 * 1024)
  }, '[srv]', logs)
  const cleanup = () => { try { srv.kill() } catch (e) {} ; up.srv.close() }
  process.on('exit', cleanup)

  try {
    await H.waitHealthy(P_SRV)
    console.log(`灌到 store_bytes > ${TARGET_MB}MB，然后看进程 RSS`)

    let n = 0
    let m = null
    const samples = []
    const t0 = Date.now()
    while (true) {
      n++
      const body = bigBody(n, 14)
      const raw = JSON.stringify(body)
      const proof = F.proveRoundTrip(raw)
      if (!proof.ok) throw new Error('造的 body 自己都不可复现: ' + proof.reason)
      const env = {
        v: F.PROTO_VERSION, path: '/v1/chat/completions', array_key: proof.arrayKey,
        handle: null, base_count: 0, base_digest: F.prefixDigest([], 0),
        template_digest: F.templateDigest(proof.template), template: proof.template,
        delta: proof.items, expect_bytes: raw.length
      }
      const r = await H.post(P_SRV, '/zkd/v1/delta', JSON.stringify(env))
      if (r.status !== 200) throw new Error('灌到第 ' + n + ' 条时 status=' + r.status)

      if (n % 20 === 0 || n === 1) {
        m = JSON.parse((await H.get(P_SRV, '/metrics.json')).body.toString('utf8'))
        const mb = m.store_bytes / 1048576
        const rss = (m.rss_bytes || 0) / 1048576
        if (m.rss_bytes) samples.push({ n, store: m.store_bytes, rss: m.rss_bytes })
        console.log(`  ${String(n).padStart(4)} 条  store ${mb.toFixed(1)}MB  rss ${rss ? rss.toFixed(1) + 'MB' : '(服务端没报 rss)'}` +
          (rss ? `  总倍率 ${(rss / mb).toFixed(2)}x` : ''))
        if (mb > TARGET_MB) break
      }
      if (Date.now() - t0 > 240000) { console.log('  超时先停'); break }
    }

    m = JSON.parse((await H.get(P_SRV, '/metrics.json')).body.toString('utf8'))
    console.log('\n最终：', JSON.stringify({
      conv: m.conv_live, store_MB: +(m.store_bytes / 1048576).toFixed(1),
      rss_MB: m.rss_bytes ? +(m.rss_bytes / 1048576).toFixed(1) : null
    }))
    if (!m.rss_bytes) {
      console.log('\n服务端 /metrics.json 里没有 rss_bytes —— 先给它加上，否则这条只能靠外部 ps 猜。')
      process.exit(3)
    }

    // 判据修正（两处）：
    //  1) 第一版用 rss/store 这个**总倍率**反推上限，错的——总倍率被固定基座污染，
    //     store 小时会飙到 20x（1 条会话时实测 20.17x），照它算的上限小得荒唐。
    //     该用**边际倍率**：多存 1MB 会话 RSS 多涨多少。拟合 rss = base + k·store。
    //  2) 第二版拿「本探针自己的 ZKD_MAX_BYTES」当被判对象——而那个值是我为了做实验
    //     故意放大到 64GB 的，等于在量自己的旋钮。**判据必须锚在生产实配上**，
    //     所以生产的两个上限从参数进来，并把出处打出来。
    if (samples.length < 3) { console.log('样本不足（<3），没法拟合'); process.exit(3) }
    // 最小二乘拟合全部样本（跳过第 1 个基座主导点）；两点法在 GC 噪声下不稳，
    // 同一份代码连跑两次能给出 1.01x 和 1.25x 两个斜率。
    const fit = samples.slice(1)
    const nS = fit.length
    const sx = fit.reduce((a, s) => a + s.store, 0)
    const sy = fit.reduce((a, s) => a + s.rss, 0)
    const sxx = fit.reduce((a, s) => a + s.store * s.store, 0)
    const sxy = fit.reduce((a, s) => a + s.store * s.rss, 0)
    const k = (nS * sxy - sx * sy) / (nS * sxx - sx * sx)
    const base = (sy - k * sx) / nS
    // 保守：取拟合斜率与相邻两点最大边际斜率里更大的那个。宁可把上限判紧，
    // 不可以因为一次跑得漂亮就放过一个会 OOM 的配置。
    let kMax = k
    for (let i = 1; i < fit.length; i++) {
      const dk = (fit[i].rss - fit[i - 1].rss) / (fit[i].store - fit[i - 1].store)
      if (dk > kMax) kMax = dk
    }
    console.log(`\n拟合（最小二乘 ${nS} 点）：rss ≈ ${(base / 1048576).toFixed(0)}MB + ${k.toFixed(2)} × store`)
    console.log(`  相邻点最大边际斜率 ${kMax.toFixed(2)}x —— 下面按这个保守值判`)
    console.log(`  （总倍率此刻是 ${(s => s.rss / s.store)(fit[fit.length - 1]).toFixed(2)}x，那个数不能用来定上限）`)

    // 生产实配：默认写死成 server.js 的默认值 + k8s yaml 里的值。
    // 探针自己那两个上限被故意放大过，绝不能拿来当判据。
    const LIMIT_MB = parseInt(process.env.ZKD_POD_LIMIT_MB || '2048', 10)
    const REQ_HEADROOM_MB = parseInt(process.env.ZKD_REQ_HEADROOM_MB || '400', 10)
    const prodMaxMB = parseInt(process.env.ZKD_PROD_MAX_BYTES_MB || '800', 10)
    const prodConvMax = parseInt(process.env.ZKD_PROD_MAX_CONV || '400', 10)
    console.log(`\n被判对象 = 生产实配（不是本探针的旋钮）：` +
      `ZKD_MAX_BYTES=${prodMaxMB}MB，ZKD_MAX_CONV=${prodConvMax}，pod limit ${LIMIT_MB}MB`)
    console.log(`  本探针自己跑的是 ${Math.round((m.store_max_bytes || 0) / 1048576)}MB / ${m.conv_max} —— 故意放开的，只为把曲线灌出来`)

    const baseMB = base / 1048576
    const rssAtCap = baseMB + kMax * prodMaxMB
    let bad = false
    console.log(`\n[字节上限] 撑满 ${prodMaxMB}MB → RSS ≈ ${rssAtCap.toFixed(0)}MB，` +
      `另给并发请求的临时全量 buffer 留 ${REQ_HEADROOM_MB}MB`)
    if (rssAtCap + REQ_HEADROOM_MB > LIMIT_MB) {
      const want = Math.floor((LIMIT_MB - REQ_HEADROOM_MB - baseMB) / kMax)
      console.log(`  ✗ 会超 limit（${rssAtCap.toFixed(0)}+${REQ_HEADROOM_MB} > ${LIMIT_MB}）→ 必须把 ZKD_MAX_BYTES 调到 ${want}MB 以下`)
      bad = true
    } else {
      console.log(`  ✓ 撑满也在 limit 内（${rssAtCap.toFixed(0)}+${REQ_HEADROOM_MB} ≤ ${LIMIT_MB}），不用改`)
    }

    // 另一条腿：光有条数上限够不够？长会话下它会不会先于字节上限把内存顶爆
    const avgMB = m.conv_avg_bytes / 1048576
    const convOnlyMB = avgMB * prodConvMax
    const rssConvOnly = baseMB + kMax * convOnlyMB
    console.log(`\n[条数上限] 本次样本平均 ${avgMB.toFixed(2)}MB/会话 × ${prodConvMax} 条 = ${convOnlyMB.toFixed(0)}MB store → RSS ≈ ${rssConvOnly.toFixed(0)}MB`)
    if (convOnlyMB > prodMaxMB) {
      console.log(`  → 会话一长，${prodConvMax} 条就能到 ${convOnlyMB.toFixed(0)}MB，越过字节上限 ${prodMaxMB}MB。`)
      console.log(`     **真正在拦的是字节上限；条数上限单独拦不住** —— 两条都得留着。`)
      if (rssConvOnly + REQ_HEADROOM_MB > LIMIT_MB) {
        console.log(`     （也就是说：假如哪天有人把 ZKD_MAX_BYTES 拿掉只留条数上限，RSS 会到 ${rssConvOnly.toFixed(0)}MB，直接 OOM）`)
      }
    } else {
      console.log(`  → 本次样本尺寸下条数上限先撞，字节上限是兜底。`)
    }
    console.log(`\n[全员发容量] 按平均 ${avgMB.toFixed(2)}MB/会话、每人同时挂 ${AVG_CONV_PER_USER} 条活跃会话算` +
      `（**每人几条会话是假设，不是实测** —— 我只有自己一台机器的行为数据）：`)
    for (const users of [5, 10, 20, 40]) {
      const need = avgMB * AVG_CONV_PER_USER * users
      const rss = baseMB + kMax * Math.min(need, prodMaxMB)
      const evict = need > prodMaxMB
      console.log(`  ${String(users).padStart(2)} 人 → 需 ${need.toFixed(0)}MB store，` +
        (evict ? `超过 ${prodMaxMB}MB 上限 → 会开始淘汰最久没用的会话（被淘汰的那条下一发退化成全量，不出错）` :
          `在上限内，RSS ≈ ${rss.toFixed(0)}MB`))
    }
    process.exit(bad ? 1 : 0)
  } finally {
    cleanup()
  }
  process.exit(0)
}

main().catch((e) => { console.error(e); process.exit(2) })
