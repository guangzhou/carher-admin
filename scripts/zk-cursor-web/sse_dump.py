import json, sys, os, urllib.request, time
KEY = os.environ['MK']
name = sys.argv[1]
body = open('/tmp/cw/replay_%s.json' % name, 'rb').read()
req = urllib.request.Request('http://localhost:4000/v1/responses', data=body,
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + KEY})
with urllib.request.urlopen(req, timeout=180) as r:
    text = r.read().decode('utf-8', 'replace')
types, delta_chars = [], 0
completed_output = None
for line in text.split('\n'):
    if not line.startswith('data: '): continue
    d = line[6:]
    if d == '[DONE]': continue
    try: ev = json.loads(d)
    except Exception: continue
    t = ev.get('type')
    types.append(t)
    if t == 'response.output_text.delta': delta_chars += len(ev.get('delta') or '')
    if t == 'response.completed':
        completed_output = [(it.get('type'), len(json.dumps(it, ensure_ascii=False))) for it in (ev.get('response') or {}).get('output') or []]
seq = []
for t in types:
    if seq and seq[-1][0] == t: seq[-1][1] += 1
    else: seq.append([t, 1])
print(name, 'EVENT SEQ:', ' -> '.join('%s x%d' % (t, c) if c > 1 else t for t, c in seq))
print(name, 'delta_chars:', delta_chars, 'completed.output:', completed_output)
