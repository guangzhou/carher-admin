import re, sys, hashlib
src = open("/tmp/tooldiet/resp_live.js").read()
assert hashlib.md5(src.encode()).hexdigest().startswith("bcd3d4b4"), "base bytes drift"

FUNC = '''
// ── CarHer patch (2026-08-29): A线 tool-diet(ZK_TOOL_DIET,默认关=零行为差)──
// 客户端 MCP 目录 <mcp_server_catalog> 带超长 serverUseInstructions(实测 browser
// 服务器 4861c),叠上 <mcp_meta_tools> 教学 2895c,压重任务首轮(带目录首轮空率
// 3/8 vs 不带 4/45)。压法:目录保留服务器名+工具名清单(即索引),说明截 140c;
// meta 块换紧凑版。按需取参数走 Cursor 原生 GetMcpTools,不发明新机制。幂等。
const MCP_META_COMPACT = '<mcp_meta_tools>\\n'
  + 'You have GetMcpTools / CallMcpTool / FetchMcpResource for the MCP servers listed in '
  + '<mcp_server_catalog>. MANDATORY: call GetMcpTools to fetch a tool\\'s schema before its '
  + 'first CallMcpTool. If a server needs auth, call mcp_auth once for that server, then retry. '
  + 'Prefer completing the work with available tools; note any MCP gaps in your summary.\\n'
  + '</mcp_meta_tools>'
function dietMcpText(s) {
  if (process.env.ZK_TOOL_DIET !== '1' || typeof s !== 'string') return s
  let out = s
  const mCat = /<mcp_server_catalog>[\\s\\S]*?<\\/mcp_server_catalog>/.exec(out)
  if (mCat) {
    const blk = mCat[0]
    const slim = blk.replace(/ serverUseInstructions="([\\s\\S]*?)"(\\s*\\/>)/g, (all, v, tail) => {
      if (v.length <= 220) return all
      return ' serverUseInstructions="' + v.slice(0, 140).replace(/\\s+\\S*$/, '') + '\\u2026"' + tail
    })
    if (slim.length !== blk.length) {
      out = out.slice(0, mCat.index) + slim + out.slice(mCat.index + blk.length)
      try { console.log('[tool-diet] catalog ' + blk.length + ' -> ' + slim.length + ' chars') } catch (_) {}
    }
  }
  const mMeta = /<mcp_meta_tools>[\\s\\S]*?<\\/mcp_meta_tools>/.exec(out)
  if (mMeta && mMeta[0].length > MCP_META_COMPACT.length + 50) {
    out = out.slice(0, mMeta.index) + MCP_META_COMPACT + out.slice(mMeta.index + mMeta[0].length)
    try { console.log('[tool-diet] meta ' + mMeta[0].length + ' -> ' + MCP_META_COMPACT.length + ' chars') } catch (_) {}
  }
  return out
}
'''

# anchor 1: insert FUNC after dietOldToolOutputs closing
a1 = "    return { ...it, output: cut }\n  })\n}\n"
assert src.count(a1) == 1, f"anchor1 count={src.count(a1)}"
src = src.replace(a1, a1 + FUNC)

# anchor 2: hook inside _stripExecEnv
a2 = "    const _stripExecEnv = (p) => {\n      if (process.env.ZK_STRIP_EXECENV !== '1') return p"
assert src.count(a2) == 1, f"anchor2 count={src.count(a2)}"
src = src.replace(a2, "    const _stripExecEnv = (p) => {\n      p = (typeof dietMcpText === 'function') ? dietMcpText(p) : p // [tool-diet] 自带门控;防御形:函数缺失=原样\n      if (process.env.ZK_STRIP_EXECENV !== '1') return p")

# anchor 3: chat-fallback prompt
a3 = "          _fbPrompt = _fbPrompt.replace(/\\[EXECUTION ENVIRONMENT\\][\\s\\S]*?(?:\\n\\n|$)/, '')"
assert src.count(a3) == 1, f"anchor3 count={src.count(a3)}"
src = src.replace(a3, a3 + "\n          _fbPrompt = (typeof dietMcpText === 'function') ? dietMcpText(_fbPrompt) : _fbPrompt // [tool-diet]")

open("/tmp/tooldiet/resp_tooldiet.js", "w").write(src)
print("written, md5:", hashlib.md5(src.encode()).hexdigest())
