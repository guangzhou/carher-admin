import hashlib
src = open("/tmp/tooldiet/resp_tooldiet.js").read()
assert hashlib.md5(src.encode()).hexdigest().startswith("9902cbe0"), "base drift"

MOD = r'''
// ── CarHer patch (2026-08-29): B线 统一工具注册表 + ⟦call⟧ 动词(ZK_TOOL_REGISTRY 默认关)──
// codex 骨架搬运:内置与 MCP 工具同一条声明→路由→配对路(registry.rs 一张表);模型用与
// ⟦cmd¦run⟧ 同族的 ⟦call¦<Tool>¦{json}⟧ 调任意客户端工具(含 CallMcpTool→Cursor 自执行
// MCP,零本地件);MCP 工具名便捷形查目录折成 CallMcpTool。失败=教学重发一次→诚实交付。
const V2_CALL_RE = /⟦call¦([A-Za-z][\w.\-]*)¦([\s\S]*?)⟧/
const V2_CALL_ADDON = '\n\nADDITIONAL ALLOWED BLOCK — ⟦call¦<ToolName>¦{json args}⟧: invoke any client tool by exact name (Read, Grep, TodoWrite, GetMcpTools, CallMcpTool…). For an MCP tool from <mcp_server_catalog>: ⟦call¦CallMcpTool¦{"server":"<srv>","toolName":"<tool>","arguments":{…}}⟧, or simply ⟦call¦<mcp tool name>¦{args}⟧. ONE block per reply TOTAL (⟦cmd¦run⟧ OR ⟦call⟧) — the only-block rule includes ⟦call⟧ now. Its result returns next turn and is REAL.'
function buildToolRegistry(defs) {
  const reg = new Map()
  if (Array.isArray(defs)) {
    for (const t of defs) {
      const n = t && (t.name || (t.function && t.function.name))
      if (n) reg.set(String(n).toLowerCase(), String(n))
    }
  }
  return reg
}
function mcpServerOfTool(input, toolName) {
  try {
    const items = Array.isArray(input) ? input.slice(0, 3) : []
    for (const it of items) {
      if (!it || it.type !== 'message') continue
      const t = textOfContent(it.content) || ''
      const re = /<mcp_meta_tool_server name="([^"]+)" tools="([^"]*)"/g
      let m
      while ((m = re.exec(t))) {
        if (m[2].split(',').map((x) => x.trim()).includes(toolName)) return m[1]
      }
    }
  } catch (_) {}
  return null
}
'''

BRANCH = r'''        // ── B线 ⟦call⟧ 动词(ZK_TOOL_REGISTRY):统一注册表分发。客户端全部工具
        // (含 CallMcpTool/GetMcpTools)查表直发 function_call;MCP 工具名便捷形
        // (⟦call¦browser_navigate¦{…}⟧)查目录折成 CallMcpTool。未知名/烂 JSON →
        // 同会话教学重发一次(预算 1),再败诚实交付 —— 不裸漏、不假绿。
        if (process.env.ZK_TOOL_REGISTRY === '1') {
          const _cvM = V2_CALL_RE.exec(full || '')
          if (_cvM) {
            const _cvProse = (full || '').replace(/⟦[\s\S]*?⟧/g, '').trim()
            const _cvName = _cvM[1]
            let _cvArgs = {}
            let _cvErr = null
            const _cvRaw = (_cvM[2] || '').trim()
            if (_cvRaw) { try { _cvArgs = JSON.parse(_cvRaw) } catch (e) { _cvErr = 'arguments are not valid JSON (' + String(e && e.message).slice(0, 60) + ')' } }
            const _cvReg = buildToolRegistry(_webToolDefs)
            let _cvHitName = !_cvErr ? _cvReg.get(_cvName.toLowerCase()) : null
            if (!_cvErr && !_cvHitName) {
              const _srv = mcpServerOfTool(input, _cvName)
              const _cmName = _cvReg.get('callmcptool')
              if (_srv && _cmName) { _cvArgs = { server: _srv, toolName: _cvName, arguments: _cvArgs }; _cvHitName = _cmName }
            }
            if (_cvHitName) {
              const parsed = { calls: [{ name: _cvHitName, arguments: JSON.stringify(_cvArgs) }], leadingText: _cvProse }
              console.log(`[registry] complete-call (${_cvHitName}${_cvHitName !== _cvName ? ' via ' + _cvName : ''}, args ${JSON.stringify(_cvArgs).length} chars, prose ${_cvProse.length} chars)`)
              return finishWebTools(res, { stream, respId, msgId, created, mdl, usage: mkUsage(JSON.stringify(_cvArgs)), full: '', parsed, wt: { sent: wtSentF, opened: wtOpened, text: wtStreamText } })
            }
            if (!callBlockRetried) {
              callBlockRetried = true
              const _cvWhy = _cvErr ? _cvErr : ('no tool named "' + _cvName + '"')
              const _cvIdx = Array.from(_cvReg.values()).join(', ')
              console.log(`[registry] call-block invalid (${_cvWhy}) -> same-conv teach resend`)
              let _hbc = null
              if (stream && !res.writableEnded) { _hbc = setInterval(() => { try { res.write(': registry-resend\n\n') } catch (_) {} }, 2000) }
              collectWebTextR('Your previous ⟦call¦…⟧ block failed: ' + _cvWhy + '. Available tool names (exact): ' + _cvIdx + '. Resend now: prose plus EXACTLY ONE corrected block — ⟦call¦<ToolName>¦{valid json}⟧ or ⟦cmd¦run=<bash>⟧.')
                .then((r2) => { if (_hbc) clearInterval(_hbc); const t2 = r2 && r2.text; if (t2 && t2.trim()) full = t2; finished = false; finish() })
                .catch(() => { if (_hbc) clearInterval(_hbc); finished = false; finish() })
              return
            }
            console.log('[registry] call-block invalid (retry exhausted) -> deliver prose honestly')
            const _cvHon = (_cvProse ? _cvProse + '\n\n' : '') + '(本轮的工具调用块无效,未执行。请换一种说法重试。)'
            return finishWebTools(res, { stream, respId, msgId, created, mdl, usage, full: _cvHon, parsed: null, wt: { sent: wtSentF, opened: wtOpened, text: wtStreamText } })
          }
        }
'''

# E1: module functions after dietMcpText closing
marker = "'[tool-diet] meta ' + mMeta[0].length"
i = src.index(marker)
j = src.index("\n  return out\n}\n", i) + len("\n  return out\n}\n")
src = src[:j] + MOD + src[j:]

# E2: sibling retry flag
a = "    let bpiBlockRetried = false"
assert src.count(a) == 1
src = src.replace(a, a + "\n    let callBlockRetried = false // [registry] ⟦call⟧ 教学重发一次性闸")

# E3: verdict branch before unterminated salvage
a = "        // ── 未闭合 run 块打捞(2026-08-27"
assert src.count(a) == 1
src = src.replace(a, BRANCH + a)

# E4: _cAdd const
a = "      const _V2H = _bOn ? V2B_HANDSHAKE_IMPLICIT : V2_HANDSHAKE_IMPLICIT\n"
assert src.count(a) == 1
src = src.replace(a, a + "      const _cAdd = process.env.ZK_TOOL_REGISTRY === '1' ? V2_CALL_ADDON : '' // [registry] 契约增补\n")

# E5: contract chain
assert src.count("prompt = _p2base + _V2C\n") == 2
src = src.replace("prompt = _p2base + _V2C\n", "prompt = _p2base + _V2C + _cAdd\n")
a = "prompt = _p2base + '\\n\\n' + _V2M"
assert src.count(a) == 1
src = src.replace(a, a + " + _cAdd")
a = "prompt = _p2base + '\\n\\n' + _V2H"
assert src.count(a) == 1
src = src.replace(a, a + " + _cAdd")

# E6: empty-retry rebuild path
a = "      return _rp + (mcpBridge.isEnabled() ? V2B_CONTRACT : V2_CONTRACT)"
assert src.count(a) == 1
src = src.replace(a, a + " + (process.env.ZK_TOOL_REGISTRY === '1' ? V2_CALL_ADDON : '')")

# E7: conv-invalid rebuild path
a = "        const _fp = proto2 ? (_stripExecEnv(_fp0) + (mcpBridge.isEnabled() ? V2B_CONTRACT : V2_CONTRACT))"
assert src.count(a) == 1
src = src.replace(a, "        const _fp = proto2 ? (_stripExecEnv(_fp0) + (mcpBridge.isEnabled() ? V2B_CONTRACT : V2_CONTRACT) + (process.env.ZK_TOOL_REGISTRY === '1' ? V2_CALL_ADDON : ''))")

open("/tmp/tooldiet/resp_bline.js", "w").write(src)
print("written md5:", hashlib.md5(src.encode()).hexdigest())
