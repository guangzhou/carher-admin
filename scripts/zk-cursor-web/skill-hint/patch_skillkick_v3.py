#!/usr/bin/env python3
# skill-kick v3:kick 过的会话,下一轮的拒绝词也冻住(依据 09-03 01:01 用户实测:grep 结果回来后
# 那 326 字「已找到技能…但当前环境没连你本机 shell」漏给了用户;那轮有工具结果,v2 的冻结条件
# 只盖"无工具结果"的轮)。做法:模块级 Map convId→ts,kick 时登记;后续轮 convSess 命中该表 →
# 流式层见 _REFUSE_RE 即冻住,判定层本就会走 prose 重问(actRetried 每请求独立),重问结果替换交付。
# 用法:patch_skillkick_v3.py <src.js> <out.js>
import sys
src_path, out_path = sys.argv[1], sys.argv[2]
src = open(src_path, encoding='utf-8').read(); n0 = len(src)
def rep(a, b, tag):
    global src
    c = src.count(a); assert c == 1, '%s 命中 %d' % (tag, c); src = src.replace(a, b, 1)

rep("let _skillKickN = 0\n",
    "let _skillKickN = 0\n"
    "const _skillKickedConv = new Map()   // [skill-kick] convId -> ts;kick 过的会话后续轮拒绝词也冻住\n", 'A0')

# 流式层:armed 时不要求 !_skFresh
rep("        if (process.env.ZK_SKILL_HINT === '1' && shellTool && !_skFresh() && _REFUSE_RE.test(full)) {\n"
    "          console.log(`[skill-kick] refusal spotted in stream (${full.length} chars) -> hold`)\n",
    "        const _skArmed = !!(convSess && _skillKickedConv.has(String(convSess.convId)))\n"
    "        if (process.env.ZK_SKILL_HINT === '1' && shellTool && (_skArmed || !_skFresh()) && _REFUSE_RE.test(full)) {\n"
    "          console.log(`[skill-kick] refusal spotted in stream (${full.length} chars${_skArmed ? ', armed conv' : ''}) -> hold`)\n", 'A1')

# 判定层:kick 时登记 convId
rep("                _skillKickN++\n",
    "                _skillKickN++\n"
    "                { const _kc = _cvSeen || (convSess && convSess.convId); if (_kc) { _skillKickedConv.set(String(_kc), Date.now()); if (_skillKickedConv.size > 200) _skillKickedConv.delete(_skillKickedConv.keys().next().value) } }\n", 'A2')

open(out_path, 'w', encoding='utf-8').write(src)
print('源 %d -> 产物 %d (+%d)' % (n0, len(src), len(src) - n0))
