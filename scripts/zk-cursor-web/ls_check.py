import json, sys, os, urllib.request
KEY = os.environ['MK']
body = open('/tmp/cw/replay_ls.json', 'rb').read()
req = urllib.request.Request('http://localhost:4000/v1/responses', data=body,
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + KEY})
with urllib.request.urlopen(req, timeout=180) as r:
    text = r.read().decode('utf-8', 'replace')
out, deltasum = None, 0
for line in text.split('\n'):
    if not line.startswith('data: ') or line[6:] == '[DONE]': continue
    try: ev = json.loads(line[6:])
    except Exception: continue
    if ev.get('type') == 'response.output_text.delta': deltasum += len(ev.get('delta') or '')
    if ev.get('type') == 'response.completed':
        out = (ev.get('response') or {}).get('output') or []
kinds = []
for it in out or []:
    if it.get('type') == 'function_call':
        kinds.append('function_call:%s args=%s' % (it.get('name'), (it.get('arguments') or '')[:90]))
    else:
        t = ((it.get('content') or [{}])[0]).get('text') or ''
        kinds.append('message:%r' % t[:90])
print('deltas:', deltasum, '|', ' ; '.join(kinds))
