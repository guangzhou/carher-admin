#!/usr/bin/env python3
# Round-16: live-caught in r15 regression run — weather reply showed visible
# "citeturn " residue (PUA residue 0). Mechanism: held only ever starts at the
#  opener; upstream truncated the marker mid-stream, and the next
# non-body char (emoji) hit the conservative flush branch which emits held
# verbatim. Apply the SAME policy as flush() at EOF: a single-token held body
# that looks like marker debris (_CITE_TAIL_RE) is dropped; anything with a
# space stays emitted (rather leak a marker than eat prose).
src = open('/tmp/responses.r15.js').read()
orig = src

a_old = "          if (!_CITE_BODY_CH.test(ch)) { out += held.replace(_PUA_RE, '') + ch; held = ''; continue }"
assert src.count(a_old) == 1, 'anchor A=%d' % src.count(a_old)
a_new = """          if (!_CITE_BODY_CH.test(ch)) {
            // held 只可能由 \\ue200 开符启动 —— 单 token 且形如标记尾缀的残段
            //（上游断流截断，实测漏出 "citeturn "）按 EOF flush 同一政策丢弃；
            // 含空格的多词内容仍放行，宁可漏删不误吞正文。
            const _b = held.replace(_PUA_RE, '')
            const _bt = _b.trim()
            if (_bt && !(_bt.indexOf(' ') < 0 && _CITE_TAIL_RE.test(_bt))) out += _b
            out += ch
            held = ''
            continue
          }"""
src = src.replace(a_old, a_new)

assert src != orig
open('/tmp/responses.r16.js', 'w').write(src)
print('round16 OK: %d chars' % len(src))
