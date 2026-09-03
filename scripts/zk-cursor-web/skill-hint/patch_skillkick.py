#!/usr/bin/env python3
# skill-kick —— 首轮拒绝时,不再用一句话重问,而是把 skill 搜索当**真 Shell 调用**交给 Cursor 跑。
#
# 依据(09-03 00:50 真 Cursor,会话 6a9853e1,135):
#   · 1279 字的 skill-hint 确实发到了模型,它仍两次拒绝(「当前环境没有连接到你的本机终端」);
#     一句话重问(act-retry)也拒。
#   · 它拒绝词里写了个字面 ⟦cmd¦run=...⟧,被网关当真命令跑了 `...`(exit 127)。**这一条真实的
#     工具结果一回来它立刻信了**,随后 grep → cat SKILL.md → lark-cli 创建 → 给出真链接,4 轮全对。
#   ⇒ 说服它的不是文案,是一条真实工具结果。那就直接给它一条:把 grep 编成真 Shell 调用。
#
# 两刀,都门在 ZK_SKILL_HINT=1(默认关=零行为差):
#   ① 流式层 wtPump:本轮没有新鲜工具结果、正文开头就匹配 _REFUSE_RE → 冻住不吐(复用 envelope
#      的 wtFrozen),那段「我无法…」不再漏给用户。
#   ② 判定层 announce-without-action 分支:同条件下不发 prose 重问,用 execToToolCall 编一条
#      grep(关键词按 <user_query> 推:飞书/文档/表格→lark;k8s/kubectl/pod→'kubectl|k8s';
#      litellm→litellm;其余→列三处 skill 目录名),finishWebTools 交付真调用。
#   计数日志 `[skill-kick] … kw=… (n)`——判据 n>0,不是"代码在那儿"。
#
# 用法:patch_skillkick.py <src.js> <out.js>
import sys
src_path, out_path = sys.argv[1], sys.argv[2]
src = open(src_path, encoding='utf-8').read()
n0 = len(src)

def rep(anchor, new, tag):
    global src
    c = src.count(anchor)
    assert c == 1, '%s 锚点命中 %d 次(要 1)' % (tag, c)
    src = src.replace(anchor, new, 1)

# ── 0. 关键词 → 搜索命令(模块级,挨着 _PLAN_RE) ───────────────────────────────
A0 = "const _PLAN_RE = /"
rep(A0,
    "// [skill-kick] 首轮拒绝 → 真 Shell 搜索调用(见 patch_skillkick.py)。门控 ZK_SKILL_HINT=1。\n"
    "let _skillKickN = 0\n"
    "function _skillKickCmd(uq) {\n"
    "  const q = String(uq || '')\n"
    "  const dirs = '~/.claude/skills/*/SKILL.md ~/.codex/skills/*/SKILL.md .cursor/skills/*/SKILL.md'\n"
    "  let kw = null\n"
    "  if (/飞书|feishu|lark|文档|表格|多维|画板|群消息|知识库/i.test(q)) kw = 'lark'\n"
    "  else if (/k8s|kubectl|kubernetes|\\bpod\\b|deployment|configmap/i.test(q)) kw = 'kubectl\\\\|k8s'\n"
    "  else if (/litellm/i.test(q)) kw = 'litellm'\n"
    "  else if (/carher|her\\b|实例/i.test(q)) kw = 'carher'\n"
    "  const cmd = kw\n"
    "    ? `grep -ril '${kw}' ${dirs} 2>/dev/null | head -20`\n"
    "    : `ls ~/.claude/skills ~/.codex/skills .cursor/skills 2>/dev/null | head -80`\n"
    "  return { kw: kw || '(list)', cmd }\n"
    "}\n"
    + A0, 'A0')

# ── ① 流式层:拒绝词不外漏 ───────────────────────────────────────────────────
A1 = ("        if (looksLikeEnvelope(full)) { wtFrozen = true; return }\n"
      "        wtDecided = true\n")
rep(A1,
    "        if (looksLikeEnvelope(full)) { wtFrozen = true; return }\n"
    "        // [skill-kick] 首轮(无新鲜工具结果)开头即拒绝 → 冻住不吐,finish() 会换成真搜索调用\n"
    "        if (process.env.ZK_SKILL_HINT === '1' && shellTool && !_skFresh() && _REFUSE_RE.test(full)) {\n"
    "          console.log(`[skill-kick] refusal spotted in stream (${full.length} chars) -> hold`)\n"
    "          wtFrozen = true; return\n"
    "        }\n"
    "        wtDecided = true\n", 'A1')

# _skFresh:本轮有没有新鲜工具结果。定义放 wtPump 之前(与 DECIDE_AT 同级)。
A1b = "    const DECIDE_AT = 80\n"
rep(A1b,
    A1b +
    "    // [skill-kick] 与 _v2FreshResult 同判据:增量轮看 _convDelta,首轮看最后 3 条 input\n"
    "    const _skFresh = () => ((convSess && _convDelta) ? _convDelta : (Array.isArray(input) ? input.slice(-3) : []))\n"
    "      .some((it) => it && (it.type === 'function_call_output' || it.type === 'custom_tool_call_output'))\n", 'A1b')

# ── ② 判定层:换 prose 重问为真调用 ─────────────────────────────────────────
A2 = ("            actRetried = true\n"
      "            console.log(`[turn-verdict-v2] announce-without-action (${_v2clean.length} chars) -> forced action retry`)\n")
rep(A2,
    "            actRetried = true\n"
    "            if (process.env.ZK_SKILL_HINT === '1' && !_v2FreshResult && _REFUSE_RE.test(_v2clean)) {\n"
    "              const _sk = _skillKickCmd(extractUserQuery(basePrompt))\n"
    "              const _skTc = execToToolCall(shellTool, _sk.cmd)\n"
    "              if (_skTc) {\n"
    "                _skillKickN++\n"
    "                console.log(`[skill-kick] refusal without tool result (${_v2clean.length} chars) -> real search call kw=${_sk.kw} (n=${_skillKickN})`)\n"
    "                const _skParsed = { calls: [{ name: _skTc.name, arguments: _skTc.arguments }], leadingText: '' }\n"
    "                return finishWebTools(res, { stream, respId, msgId, created, mdl, usage: mkUsage(JSON.stringify(_skTc.arguments)), full: '', parsed: _skParsed, wt: { sent: wtSentF, opened: wtOpened, text: wtStreamText } })\n"
    "              }\n"
    "            }\n"
    "            console.log(`[turn-verdict-v2] announce-without-action (${_v2clean.length} chars) -> forced action retry`)\n", 'A2')

open(out_path, 'w', encoding='utf-8').write(src)
print('源 %d -> 产物 %d 字符(+%d),4 处锚点各命中 1 次' % (n0, len(src), len(src) - n0))
