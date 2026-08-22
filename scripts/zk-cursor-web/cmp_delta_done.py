import json, sys, os, urllib.request
KEY = os.environ['MK']
name = sys.argv[1]
body = open('/tmp/cw/replay_%s.json' % name, 'rb').read()
req = urllib.request.Request('http://localhost:4000/v1/responses', data=body,
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + KEY})
with urllib.request.urlopen(req, timeout=180) as r:
    text = r.read().decode('utf-8', 'replace')
deltas, done_text = [], None
for line in text.split('\n'):
    if not line.startswith('data: ') or line[6:] == '[DONE]': continue
    try: ev = json.loads(line[6:])
    except Exception: continue
    if ev.get('type') == 'response.output_text.delta': deltas.append(ev.get('delta') or '')
    if ev.get('type') == 'response.output_text.done': done_text = ev.get('text')
joined = ''.join(deltas)
print('deltas:', len(deltas), 'joined_chars:', len(joined), 'done_chars:', None if done_text is None else len(done_text))
print('MATCH' if joined == done_text else 'MISMATCH')
if joined != done_text and done_text is not None:
    import itertools
    for i,(a,b) in enumerate(itertools.zip_longest(joined, done_text)):
        if a != b: print('first diff at', i, repr(a), repr(b)); break
pua = [ch for ch in (done_text or '') if 0xe000 <= ord(ch) <= 0xf8ff]
print('PUA residue:', len(pua), '| cite residue:', 'citeturn' in (done_text or ''))
print('text:', repr((done_text or '')[:200]))
