#!/usr/bin/env python3
"""conv_prefix_live_probe.py — 会话复用「instructions 漂移」端到端探针(82 lane)。

复现并验证 2026-08-31 的修复:同一条对话连发多轮,**每轮故意换一次 instructions**
(这正是 Cursor 客户端的真实行为)。修复前 key 含 instructions,一换就 miss → 全量重发
→ GPT 网页新开一条会话;修复后靠「最长严格前缀」匹配,应当全程一条会话。

判据(全部要过):
  · 全程只出现 1 个 convId
  · 第 2 轮起每轮都有 [conv] delta send,且 [handshake] implicit 只出现 1 次
  · 断链 miss(items>2)为 0 —— 第 1 轮 items=2 的 miss 是**正确行为**(真·新会话),不算
  · instructions 变化时打印 "carried in delta"(新指令没被丢掉)
  · 门① 观察位:实发字符要看 [execenv-strip] 之后的数,delta send 打的是 strip 前的值

注意:这是**合成探针**,按既有纪律它不构成上线依据,只证明机制通了;
真验收在真 Cursor GUI 的多轮对话(见 docs/conv-reuse-prefix-match-20260831.md R4-R6)。

用法: python3 scripts/zk-cursor-web/conv_prefix_live_probe.py [turns]
"""
import json
import re
import subprocess
import sys
import time
import urllib.request

TURNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
MODEL = 'cursor-web-fc-82-terra'
BASE = 'https://cc.auto-link.com.cn/pro/v1/responses'
SSH = ['sshpass', '-p', 'Hn8#mKLp3QxZ', 'ssh', '-o', 'StrictHostKeyChecking=no', 'cltx@10.68.13.198']
SUDO = "echo 'Hn8#mKLp3QxZ' | sudo -S k3s kubectl -n litellm-product "


def sh(cmd, timeout=180):
    r = subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout)
    return r.stdout


def pod_name():
    out = sh(SUDO + "get pods -l app=zero-cursor-bpi-82 -o name 2>/dev/null")
    return out.strip().splitlines()[-1].strip()


def log_lines(pod):
    return sh(SUDO + "logs %s --tail=-1 2>/dev/null" % pod, timeout=300).splitlines()


def mint_key():
    py = ("import urllib.request, json, os;"
          "data=json.dumps({'models':['%s'],'key_alias':'convprefix-%d','duration':'20m'}).encode();"
          "req=urllib.request.Request('http://localhost:4000/key/generate',data=data,"
          "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],'Content-Type':'application/json'});"
          "print(json.load(urllib.request.urlopen(req,timeout=15))['key'])" % (MODEL, int(time.time())))
    out = sh(SUDO + 'exec deploy/litellm-proxy -- python3 -c "%s" 2>&1' % py)
    for ln in out.splitlines():
        if ln.strip().startswith('sk-'):
            return ln.strip()
    raise SystemExit('mint key failed:\n' + out)


def del_key(key):
    """探针 key 用完即删,不留活口(duration 20m 只是兜底)。"""
    py = ("import urllib.request, json, os;"
          "data=json.dumps({'keys':['%s']}).encode();"
          "req=urllib.request.Request('http://localhost:4000/key/delete',data=data,"
          "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],'Content-Type':'application/json'});"
          "print(urllib.request.urlopen(req,timeout=15).status)" % key)
    return sh(SUDO + 'exec deploy/litellm-proxy -- python3 -c "%s" 2>&1' % py).strip()


FRAME_TEXT = ('<framework>You are operating inside an editor harness. '
              'Follow the tool contract. This block is identical across all chats.</framework>')
TOOLS = [{
    'type': 'function', 'name': 'shell',
    'description': 'Run a shell command and return its output.',
    'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command']},
}]


def item(role, text):
    return {'type': 'message', 'role': role,
            'content': [{'type': ('input_text' if role == 'user' else 'output_text'), 'text': text}]}


def post(key, items, instructions):
    body = json.dumps({'model': MODEL, 'stream': False, 'input': items,
                       'instructions': instructions, 'tools': TOOLS}).encode()
    req = urllib.request.Request(BASE, data=body, headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def main():
    pod = pod_name()
    print('pod =', pod)
    before = len(log_lines(pod))
    print('log offset =', before)

    key = mint_key()
    print('key  = %s...%s' % (key[:12], key[-4:]))

    items = [item('user', FRAME_TEXT)]
    # 每轮换一次 instructions —— 这就是线上那个 bug 的触发条件
    instr_variants = [
        'You are a concise assistant. Session context revision 1.',
        'You are a concise assistant. Session context revision 2 (files changed).',
        'You are a concise assistant. Session context revision 3 (different open files).',
    ]

    ok = True
    for t in range(1, TURNS + 1):
        items.append(item('user', 'Turn %d: reply with exactly the word OK%d and nothing else.' % (t, t)))
        instr = instr_variants[(t - 1) % len(instr_variants)]
        try:
            resp = post(key, items, instr)
        except Exception as e:
            print('  turn %d FAILED: %s' % (t, e))
            ok = False
            break
        status = resp.get('status')
        text = ''
        for o in resp.get('output', []):
            for c in (o.get('content') or []):
                if c.get('type') in ('output_text', 'text'):
                    text += c.get('text', '')
        print('  turn %d: status=%s instr_rev=%d(%dc) text=%r'
              % (t, status, (t - 1) % len(instr_variants) + 1, len(instr), text[:60]))
        if status != 'completed':
            ok = False
        # 模拟 Cursor 把上一轮回答回灌进历史
        items.append(item('assistant', text or ('OK%d' % t)))

    del_key(key)
    time.sleep(3)
    after = log_lines(pod)
    new = after[before:]
    convs = re.findall(r'\[conv\] saved items=\d+ conv=(\w+)', '\n'.join(new))
    deltas = re.findall(r'\[conv\] delta send (\d+) new items, (\d+) chars', '\n'.join(new))
    handshakes = len(re.findall(r'\[handshake\] implicit', '\n'.join(new)))
    misses = re.findall(r'\[conv\] miss items=(\d+)[^\n]*', '\n'.join(new))
    carried = len(re.findall(r'instructions changed .*carried in delta', '\n'.join(new)))
    # 门① 的真实数字在 execenv-strip 之后 —— delta send 打的是 strip 前的值
    stripped = re.findall(r'\[execenv-strip\] \d+ -> (\d+) chars', '\n'.join(new))
    dietzero = len(re.findall(r'\[proto2\] v2 contract DIET-ZERO \(delta turn', '\n'.join(new)))
    # 第 1 轮必然 miss(items=2,真·新会话),那是对的;断链 miss 才是 bug
    break_miss = [m for m in misses if int(m) > 2]

    print('\n== 现场日志判据 ==')
    print('  不同 convId 数量 :', len(set(convs)), sorted(set(convs)))
    print('  delta send 轮数  :', len(deltas), 'strip 前字符:', [d[1] for d in deltas])
    print('  strip 后实发字符 :', stripped)
    print('  契约 DIET-ZERO   :', dietzero, '轮(增量轮不发 3019 字符契约)')
    print('  handshake 次数   :', handshakes)
    print('  instructions 补发:', carried)
    print('  miss(items>2)   :', break_miss or '(无)', ' 全部 miss:', misses)

    verdict = True
    def chk(name, cond, detail=''):
        nonlocal verdict
        print('  %-28s %s %s' % (name, 'ok' if cond else 'FAIL', detail))
        if not cond:
            verdict = False

    chk('全程只有一条会话', len(set(convs)) == 1, '实测 %d 条' % len(set(convs)))
    chk('第2轮起都走增量', len(deltas) >= TURNS - 1, '实测 %d 轮' % len(deltas))
    chk('只握手一次', handshakes <= 1, '实测 %d 次' % handshakes)
    chk('无断链 miss', len(break_miss) == 0, '实测 %d 条(items>2)' % len(break_miss))
    chk('新指令有补发', carried >= 1, '实测 %d 次' % carried)
    chk('增量轮不发契约', dietzero >= TURNS - 1, '实测 %d 轮' % dietzero)
    chk('门①实发是小量级', all(int(s) < 400 for s in stripped[1:]),
        'strip 后 %s(首轮含契约,不计)' % stripped[1:])
    chk('所有轮次上游 200', ok)

    print('\n== VERDICT: %s ==' % ('PASS' if verdict else 'FAIL'))
    sys.exit(0 if verdict else 1)


if __name__ == '__main__':
    main()
