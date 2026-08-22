import json, sys, os, urllib.request, time
KEY = os.environ['MK']
name = sys.argv[1]
body = open('/tmp/replay_%s.json' % name, 'rb').read()
req = urllib.request.Request('http://localhost:4000/v1/responses', data=body,
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + KEY})
t0 = time.time()
msgs, calls, usage, raw_pua = [], [], None, False
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        buf = b''
        for chunk in r:
            buf += chunk
        text = buf.decode('utf-8', 'replace')
except Exception as e:
    print('%s ERROR %s' % (name, e)); sys.exit(0)
dt = time.time() - t0
for line in text.split('\n'):
    if not line.startswith('data: '): continue
    d = line[6:]
    if d == '[DONE]': continue
    try: ev = json.loads(d)
    except Exception: continue
    if ev.get('type') == 'response.output_item.done':
        it = ev.get('item') or {}
        if it.get('type') == 'message':
            t = ''.join(c.get('text','') for c in (it.get('content') or []) if isinstance(c, dict))
            msgs.append(t)
        elif it.get('type') in ('function_call','custom_tool_call'):
            calls.append({'name': it.get('name'), 'args': (it.get('arguments') or '')[:80]})
    if ev.get('type') == 'response.completed':
        usage = ((ev.get('response') or {}).get('usage'))
alltext = ' '.join(msgs)
for ch in alltext:
    if 0xe000 <= ord(ch) <= 0xf8ff: raw_pua = True
import re
cite_residue = bool(re.search(r'cite\w*turn\d|turn\d+(news|new|file|search)', alltext))
print('%s: %.1fs msgs=%d calls=%s' % (name, dt, len(msgs), calls))
print('%s: text=%s' % (name, json.dumps(alltext[:160], ensure_ascii=False)))
print('%s: output_tokens=%s pua=%s cite_residue=%s' % (name, (usage or {}).get('output_tokens'), raw_pua, cite_residue))
