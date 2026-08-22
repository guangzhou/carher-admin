#!/usr/bin/env python3
# Round-15: full-file review fixes (critical triage of 3 confirmed issues)
#  F1 finishWebTools reconcile assumes streamed deltas are a prefix of the
#     delivered text and blindly tail-slices by wtSentLen. act-retry /
#     chat-fallback deliver a SECOND-pass replacement text — slicing it by the
#     first pass's streamed length beheads/garbles it. Prefix-verify against
#     wt.text; on mismatch close the streamed shell as its own item and emit
#     the replacement as a NEW message item (fresh id — reusing msgId after
#     output_item.done would collide in clients that merge by item id).
#  F2 chat-fallback re-ask prompt still carries the hook-injected
#     [EXECUTION ENVIRONMENT] "reply must be only tool call" block — the very
#     rule proven (r5) to squash prose. Strip it for the plain-chat re-ask.
#  F3 retry/fallback turns advance the upstream conversation but their
#     conversation_id/parentId are never captured; saveConvSession already ran
#     at finish() top, so the next turn's delta send forks off a stale parent
#     and the model can't see what it said in the retry turn (same family as
#     r11 tool-result blindness). collectWebTextR now returns convId/parentId
#     and the two live consumers re-save the session.
src = open('/tmp/responses.r14.js').read()
orig = src

# ── F3a: id capture vars in collectWebTextR ───────────────────────────
a_old = """      let acc = ''
      let done = false
      let vis = true, code = false, buf = '', rec = null, hv = null"""
assert src.count(a_old) == 1, 'anchor A=%d' % src.count(a_old)
a_new = """      let acc = ''
      let done = false
      let vis = true, code = false, buf = '', rec = null, hv = null
      let cv = null, pm = null"""
src = src.replace(a_old, a_new)

# ── F3b: capture conversation ids (same shapes as the main path) ──────
b_old = """        onData: (d) => {
          if (!d || done) return
          const mm = (d.v && d.v.message) || d.message"""
assert src.count(b_old) == 1, 'anchor B=%d' % src.count(b_old)
b_new = """        onData: (d) => {
          if (!d || done) return
          if (d.conversation_id) cv = d.conversation_id
          else if (d.v && d.v.conversation_id) cv = d.v.conversation_id
          if (d.o === 'add' && d.v && d.v.message && d.v.message.id) pm = d.v.message.id
          else if (d.message && d.message.id) pm = d.message.id
          const mm = (d.v && d.v.message) || d.message"""
src = src.replace(b_old, b_new)

# ── F3c: return ids ────────────────────────────────────────────────────
c_old = "      return { text: _out, harvest: hv, harvestRec: rec }"
assert src.count(c_old) == 1, 'anchor C=%d' % src.count(c_old)
src = src.replace(c_old,
    "      return { text: _out, harvest: hv, harvestRec: rec, convId: cv, parentId: pm }")

# ── F2 + F3: chat-fallback consumer ────────────────────────────────────
d_old = """          const _fbPrompt = convSess ? flattenInput(dietCursorInput(codexInput), instructions) : basePrompt
          collectWebTextR(_fbPrompt, true)
            .then((r2) => {
              if (_hb2) clearInterval(_hb2)
              const plain = (r2 && r2.text) || ''"""
assert src.count(d_old) == 1, 'anchor D=%d' % src.count(d_old)
d_new = """          let _fbPrompt = convSess ? flattenInput(dietCursorInput(codexInput), instructions) : basePrompt
          // r15：纯聊天重问要把 hook 注入的 [EXECUTION ENVIRONMENT]（"回复必须
          // 只有 tool call"硬规则）一并剥掉 —— 它正是把首轮压扁的规则之一
          //（chatOnly 路 r5 已实证这段压死 prose），带着它重问等于半只脚还在坑里。
          _fbPrompt = _fbPrompt.replace(/\\[EXECUTION ENVIRONMENT\\][\\s\\S]*?(?:\\n\\n|$)/, '')
          collectWebTextR(_fbPrompt, true)
            .then((r2) => {
              if (_hb2) clearInterval(_hb2)
              // r15：fallback 开的新会话才是"含本轮回答"的那个 —— 不重存的话
              // 下一轮 delta 续发回到旧会话，模型看不见自己刚说的话。
              try { if (r2 && r2.convId) saveConvSession(input, instructions, r2.convId, r2.parentId) } catch (_) {}
              const plain = (r2 && r2.text) || ''"""
src = src.replace(d_old, d_new)

# ── F3: act-retry consumer re-save (kick exchange must thread forward) ─
e_old = """            .then((r2) => {
              if (_hb3) clearInterval(_hb3)
              if (r2 && r2.harvest) {"""
assert src.count(e_old) == 1, 'anchor E=%d' % src.count(e_old)
e_new = """            .then((r2) => {
              if (_hb3) clearInterval(_hb3)
              // r15：kick 交换推进了会话，parentId 必须跟上 —— 否则下一轮 delta
              // 续发 fork 回 kick 之前的分支，模型看不到自己在重问轮里做的事。
              try { if (r2 && r2.convId) saveConvSession(input, instructions, r2.convId, r2.parentId) } catch (_) {}
              if (r2 && r2.harvest) {"""
src = src.replace(e_old, e_new)

# ── F1: prefix-verified reconcile in finishWebTools ────────────────────
f_old = """    if (stream && wtWasOpen) {
      const first = output[0]
      if (first && first.type === 'message') {
        const whole = first.content[0].text || ''"""
assert src.count(f_old) == 1, 'anchor F=%d' % src.count(f_old)
f_new = """    if (stream && wtWasOpen) {
      const first = output[0]
      // r15：切尾续发只在交付文本确实以已流出文本为前缀时合法（同一流的
      // 过滤前缀）。act-retry/chat-fallback 交付的是第二轮替换文本 —— 盲按
      // 首轮已发长度切尾会把替换文本砍头/错位。前缀不符走下面的"收口+另发"。
      const _isPrefix = !!(first && first.type === 'message'
        && (((first.content[0] && first.content[0].text) || '').startsWith((wt && wt.text) || '')))
      if (first && first.type === 'message' && _isPrefix) {
        const whole = first.content[0].text || ''"""
src = src.replace(f_old, f_new)

# ── F1: re-id the replacement message emitted after the dangling close ─
g_old = """        res.write(`event: response.output_item.done\\ndata: ${JSON.stringify({
          type: 'response.output_item.done', output_index: 0,
          item: { type: 'message', id: msgId, role: 'assistant', status: 'completed',
                  content: [{ type: 'output_text', text: _t }] },
        })}\\n\\n`)
      }
    }"""
assert src.count(g_old) == 1, 'anchor G=%d' % src.count(g_old)
g_new = """        res.write(`event: response.output_item.done\\ndata: ${JSON.stringify({
          type: 'response.output_item.done', output_index: 0,
          item: { type: 'message', id: msgId, role: 'assistant', status: 'completed',
                  content: [{ type: 'output_text', text: _t }] },
        })}\\n\\n`)
        // r15：替换 message 接下来会在下面循环整发一遍 —— msgId 刚被收口，
        // 复用会让按 item id 归并的客户端撞车，换新 id。
        if (first && first.type === 'message') first.id = 'msg_' + crypto.randomBytes(12).toString('hex')
      }
    }"""
src = src.replace(g_old, g_new)

assert src != orig
open('/tmp/responses.r15.js', 'w').write(src)
print('round15 OK: %d chars' % len(src))
