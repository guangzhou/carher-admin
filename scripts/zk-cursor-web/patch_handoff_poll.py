#!/usr/bin/env python3
"""patch_handoff_poll.py — 让 `cursor-g-5.6-pro` 出字:stream_handoff 之后转轮询。

修的问题:`gpt-5-6-pro`(以及 `-wm` 那族)**不在主 SSE 里出正文**。主流走到
`stream_handoff` 事件就收口,生成在另一条通道(`resume_sse_endpoint` /
`subscribe_ws_topic`)上继续。我们的重放不跟那条通道 → 主流零正文 →
HTTP 200 + output_tokens=0 → 用户面菜单里这一档"点了没反应"。
根因三段式(含对照:`gpt-5-6` 21 事件收在 message_stream_complete、pro 12 事件
无该事件;轮询会话 t=28s 拿到 `PROBEOK`)见
docs/cursor-g-pro-stream-handoff-20260901.md。

为什么不跟 resume 端点:7 种 URL/参数组合全被拒(400 Invalid conversation resume /
404 / 405),参数形状得先反编译网页客户端。轮询 `/backend-api/conversation/<id>`
是**实测可行**那条,api.js 里 `getConversation()` 早就有。

隔离性是构造性的:只有流里真出现 `stream_handoff` 才可能进这条路,而对照实测
sol / luna / instant / 5.5 **从不产生**该事件 —— 门①(干净载荷)与门②(功能不回退)
因此不可能被这次改动碰到。开关 `ZK_HANDOFF_POLL=0` 秒关。

三个锚点,全 assert count==1,漂了就退出,不做模糊匹配。
用法: python3 patch_handoff_poll.py <in.js> <out.js>
"""
import sys

src_path, out_path = sys.argv[1], sys.argv[2]
src = open(src_path, encoding='utf-8').read()

# ── A: 声明 _handoffSeen + 轮询函数(放在 onText/_cvSeen 声明处附近)────────
a_old = """    let _cvSeen = null, _pmSeen = null
    let _lastChunkAt = Date.now()
"""
assert src.count(a_old) == 1, 'anchor A=%d' % src.count(a_old)

a_new = """    let _cvSeen = null, _pmSeen = null
    let _lastChunkAt = Date.now()

    // ── stream_handoff:延迟出流兜底(2026-09-01)──────────────────────────
    // pro 档的主 SSE 在 `stream_handoff` 处就收口,正文在别的通道上继续生成。
    // resume 端点参数形状未知(7 种组合全 400/404/405),所以走实测可行那条:
    // 轮询 /backend-api/conversation/<id>,把**新长出来的尾巴**喂给 onText
    // (逐段喂 = 客户端有字可看、连接不空转),assistant 收 finished_successfully 即收尾。
    // 只有流里真出现 stream_handoff 才进得来;sol/luna/instant/5.5 从不产生该事件。
    // ZK_HANDOFF_POLL=0 关;ZK_HANDOFF_MAX_MS / ZK_HANDOFF_POLL_MS 调预算。
    let _handoffSeen = null
    async function _handoffPoll(convId) {
      const maxMs = parseInt(process.env.ZK_HANDOFF_MAX_MS || '240000', 10)
      const everyMs = parseInt(process.env.ZK_HANDOFF_POLL_MS || '2500', 10)
      const t0 = Date.now()
      let polls = 0, errs = 0, mism = 0
      while (!finished && Date.now() - t0 < maxMs) {
        await new Promise((r) => setTimeout(r, everyMs))
        if (finished) break
        let conv = null
        try {
          conv = await chatgptApi.getConversation(convId)
        } catch (e) {
          if (++errs >= 5) {
            console.log(`[handoff] getConversation 连错 ${errs} 次,放弃轮询: ${e.message}`)
            break
          }
          continue
        }
        polls++
        // 取「最新一条 role=assistant / recipient=all / content_type=text」的消息。
        // recipient!=all 是内部通道(python/container.exec),不是交付物,别当正文。
        const nodes = (conv && conv.mapping) ? Object.values(conv.mapping) : []
        let best = null
        for (const n of nodes) {
          const m = n && n.message
          if (!m || !m.author || m.author.role !== 'assistant') continue
          const rc = m.recipient || m.author.recipient || 'all'
          if (rc !== 'all') continue
          if (!m.content || m.content.content_type !== 'text') continue
          const txt = (m.content.parts || []).filter((p) => typeof p === 'string').join('')
          if (!txt) continue
          if (!best || (m.create_time || 0) >= best.t) {
            best = { t: m.create_time || 0, txt, st: m.status }
          }
        }
        if (best) {
          if (best.txt.length > full.length && best.txt.startsWith(full)) {
            onText(best.txt.slice(full.length))
          } else if (!full) {
            onText(best.txt)
          } else if (best.txt !== full && ++mism === 1) {
            // 已流出去的不是快照的前缀:不重发(避免重字),只记一行以便事后对账。
            console.log(`[handoff] snapshot not a superset of streamed ${full.length} chars, skip append`)
          }
          if (best.st === 'finished_successfully') {
            console.log(`[handoff] done in ${Math.round((Date.now() - t0) / 1000)}s`
              + ` polls=${polls} chars=${full.length}`)
            finish()
            return
          }
        }
      }
      if (!finished) {
        console.log(`[handoff] give up after ${Math.round((Date.now() - t0) / 1000)}s`
          + ` polls=${polls} chars=${full.length} -> 交付手头已有的`)
        finish()
      }
    }
"""
src = src.replace(a_old, a_new, 1)

# ── B: 认 stream_handoff 事件 + 流末别急着 finish(否则轮询永远进不去)──────
b_old = """        if (d.type === 'message_stream_complete') finish()
      },
      onDone: finish,
"""
assert src.count(b_old) == 1, 'anchor B=%d' % src.count(b_old)

b_new = """        if (d.type === 'stream_handoff') {
          _handoffSeen = d.conversation_id || _cvSeen || _handoffSeen
          const _opts = ((d.options || []).map((o) => o && o.type).filter(Boolean).join(',')) || '-'
          console.log(`[handoff] stream_handoff conv=${_handoffSeen || '?'} opts=${_opts}`
            + ` chars=${full.length} -> 主流收口后转轮询`)
        }
        if (d.type === 'message_stream_complete') finish()
      },
      // 见过 handoff 就不能在流末 finish():finish() 一进去 finished=true,
      // 下面那段轮询兜底永远进不去,pro 照旧空回。
      onDone: () => {
        if (!finished && _handoffSeen && process.env.ZK_HANDOFF_POLL !== '0') return
        finish()
      },
"""
src = src.replace(b_old, b_new, 1)

# ── C: 主流 await 完之后跑轮询兜底 ──────────────────────────────────────
c_old = """      isDone: () => finished,
    })
  })
"""
assert src.count(c_old) == 1, 'anchor C=%d' % src.count(c_old)

c_new = """      isDone: () => finished,
    })

    // 主流收口了、但正文在别的通道上(stream_handoff)→ 转轮询把它取回来再收尾。
    if (!finished && _handoffSeen && process.env.ZK_HANDOFF_POLL !== '0') {
      await _handoffPoll(_handoffSeen)
    }
  })
"""
src = src.replace(c_old, c_new, 1)

open(out_path, 'w', encoding='utf-8').write(src)
print('OK -> %s (%d chars)' % (out_path, len(src)))
