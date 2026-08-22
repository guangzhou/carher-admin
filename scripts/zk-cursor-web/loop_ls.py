import json, os, urllib.request
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
# turn 1
out1 = call(d)
fc = next((it for it in out1 if it.get('type') == 'function_call'), None)
print('TURN1:', [(it.get('type')) for it in out1])
if not fc:
    print('turn1 no function_call ->', [( (it.get('content') or [{}])[0].get('text') or '')[:80] for it in out1]); raise SystemExit(1)
print('TURN1 call:', fc.get('name'), (fc.get('arguments') or '')[:80])
# turn 2: feed the tool result back, Cursor-style
d2 = json.loads(json.dumps(d))
d2['input'].append({'type': 'function_call', 'id': fc.get('id'), 'call_id': fc.get('call_id'),
                    'name': fc.get('name'), 'arguments': fc.get('arguments')})
d2['input'].append({'type': 'function_call_output', 'call_id': fc.get('call_id'),
                    'output': 'README.md\nbackend\nfrontend\noperator-go\nscripts\nk8s'})
out2 = call(d2)
print('TURN2:', [(it.get('type')) for it in out2])
for it in out2:
    if it.get('type') == 'message':
        t = (it.get('content') or [{}])[0].get('text') or ''
        print('TURN2 text:', repr(t[:200]))
        bad = 'No command' in t or 'provide the shell task' in t
        seen = any(k in t for k in ('README', 'backend', 'frontend', 'operator', 'scripts', 'k8s'))
        print('VERDICT:', 'FAIL-blind' if bad else ('PASS-digested' if seen else 'PASS-no-blindness (files not echoed)'))
    if it.get('type') == 'function_call':
        print('TURN2 call:', it.get('name'), (it.get('arguments') or '')[:100])
