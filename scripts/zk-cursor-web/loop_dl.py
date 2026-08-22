import json, os, re, urllib.request
KEY = os.environ['MK']
def call(payload):
    req = urllib.request.Request('http://localhost:4000/v1/responses',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + KEY})
    with urllib.request.urlopen(req, timeout=180) as r:
        text = r.read().decode('utf-8', 'replace')
    out = []
    for line in text.split('\n'):
        if not line.startswith('data: ') or line[6:] == '[DONE]': continue
        try: ev = json.loads(line[6:])
        except Exception: continue
        if ev.get('type') == 'response.completed':
            out = (ev.get('response') or {}).get('output') or []
    return out

d = json.load(open('/tmp/cw/replay_ls.json'))
def swap(txt, q):
    i = txt.rfind('<user_query>'); j = txt.find('</user_query>', i)
    return txt[:i+12] + q + txt[j:]
last = d['input'][-1]
Q = '把 https://github.com/octocat/Hello-World 这个仓库下载到本地 /tmp/hw 目录，并确认里面有哪些文件'
if isinstance(last.get('content'), str): last['content'] = swap(last['content'], Q)
else:
    for p in last['content']:
        if 'text' in p and '<user_query>' in p['text']: p['text'] = swap(p['text'], Q)

REFUSE = re.compile(r"无法|不能直接|做不到|请你|你可以自己|手动|I can't|cannot|unable|yourself|your terminal", re.I)
fake_outputs = ["Cloning into '/tmp/hw'...\nremote: Enumerating objects: 13, done.\nReceiving objects: 100% (13/13), done.",
                "README\n", "total 8\n-rw-r--r-- 1 u u 13 Aug 21 17:00 README"]
calls_made = 0
for turn in range(1, 5):
    out = call(d)
    fc = next((it for it in out if it.get('type') == 'function_call'), None)
    if fc:
        calls_made += 1
        print('turn%d CALL: %s %s' % (turn, fc.get('name'), (fc.get('arguments') or '')[:110]))
        d['input'].append({'type': 'function_call', 'id': fc.get('id'), 'call_id': fc.get('call_id'),
                           'name': fc.get('name'), 'arguments': fc.get('arguments')})
        d['input'].append({'type': 'function_call_output', 'call_id': fc.get('call_id'),
                           'output': fake_outputs[min(calls_made-1, len(fake_outputs)-1)]})
        continue
    msg = next((it for it in out if it.get('type') == 'message'), None)
    t = ((msg or {}).get('content') or [{}])[0].get('text') or ''
    print('turn%d TEXT: %r' % (turn, t[:180]))
    print('VERDICT:', 'FAIL-refused' if REFUSE.search(t) and calls_made == 0 else
          ('PASS calls=%d final-answer' % calls_made if calls_made else 'FAIL-no-action'))
    break
else:
    print('VERDICT: PASS calls=%d (still working at turn cap)' % calls_made)
