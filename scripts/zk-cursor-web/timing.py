import json, time, urllib.request, sys, os
sc = sys.argv[1]
body = json.load(open(f"replay_{sc}.json"))
body["stream"] = True
if len(sys.argv) > 2: body["reasoning"] = {"effort": sys.argv[2]}
req = urllib.request.Request("http://localhost:4000/v1/responses",
    data=json.dumps(body).encode(),
    headers={"Content-Type":"application/json","Authorization":"Bearer "+os.environ["MK"]})
t0 = time.time()
first = None; last_ev = "?"; last_t = 0
with urllib.request.urlopen(req, timeout=300) as r:
    for line in r:
        line = line.decode(errors="replace").strip()
        if not line.startswith("data: ") or line == "data: [DONE]": continue
        try: d = json.loads(line[6:])
        except Exception: continue
        ev = d.get("type") or "?"
        t = time.time()-t0
        if first is None and ev in ("response.output_text.delta","response.function_call_arguments.delta"):
            first = t
        last_ev = ev; last_t = t
print(f"{sc}: first_content={first and round(first,1)}s total={round(last_t,1)}s last={last_ev}")
