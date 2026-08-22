import json, sys, os, urllib.request
KEY = os.environ['MK']
body = open('/tmp/cw/replay_hi.json', 'rb').read()
req = urllib.request.Request('http://localhost:4000/v1/responses', data=body,
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + KEY})
with urllib.request.urlopen(req, timeout=180) as r:
    text = r.read().decode('utf-8', 'replace')
for line in text.split('\n'):
    if not line.startswith('data: ') or line[6:] == '[DONE]': continue
    try: ev = json.loads(line[6:])
    except Exception: continue
    if ev.get('type') == 'response.completed':
        print('usage:', json.dumps((ev.get('response') or {}).get('usage')))
