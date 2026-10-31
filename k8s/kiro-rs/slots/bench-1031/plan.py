"""Build the 31-exit plan from the Feishu doc (doc-before.md) + current slot config + cluster proxies.

Output plan.json: one row per exit IP with bench numbers, and what to do with it:
  existing  — already a slot (by exit IP)        ⇒ only add bench
  new       — kiro-px-<n> exists, not a slot yet ⇒ add a slot, enabled=false (no Kiro account assigned)
  noproxy   — no kiro proxy for that IP          ⇒ needs a proxy first (reported, not auto-created here)
Dedup rule: an exit IP appears at most once among slots (checked against both slot name suffix and proxy label).
"""
import json, re, sys

doc = json.load(open(sys.argv[1]))["data"]["document"]["content"]
slots = json.load(open(sys.argv[2]))          # GET /slots/config
proxies = json.load(open(sys.argv[3]))        # {deploy name: exit ip} from kiro-px-* labels + us/uk

# first table = 综合排名: 排名 | 节点 | 出口 IP | 综合分 | 备注 | 连 Kiro | 下载
t = doc.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
rows = []
for tr in re.findall(r"<tr>(.*?)</tr>", t, re.S):
    cells = [re.sub(r"<[^>]+>", "", c).replace("&amp;", "&").strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
    rank, node, ip, score, note, kiro, down = cells
    rows.append({"rank": int(rank), "node": node, "ip": ip, "score": float(score), "note": note,
                 "kiroSecs": float(kiro.replace("秒", "").strip()), "downMbps": float(down.replace("Mbps", "").strip())})
assert len(rows) == 31, len(rows)
ips = [r["ip"] for r in rows]
assert len(set(ips)) == 31, "doc has duplicate IPs"

slot_by_ip = {}
for i, s in enumerate(slots):
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)$", s["name"])
    if m:
        assert m.group(1) not in slot_by_ip, f"duplicate IP already in slots: {m.group(1)}"
        slot_by_ip[m.group(1)] = (i + 1, s["name"], s["proxyUrl"], s["enabled"])
proxy_by_ip = {}
for name, ip in proxies.items():
    if ip:
        proxy_by_ip.setdefault(ip, []).append(name)

out = []
for r in rows:
    r["of"] = 31
    if r["ip"] in slot_by_ip:
        no, name, url, en = slot_by_ip[r["ip"]]
        r.update(action="existing", slotNo=no, slotName=name, proxyUrl=url, enabled=en)
    else:
        kp = [p for p in proxy_by_ip.get(r["ip"], []) if p.startswith("kiro-px-")]
        if kp:
            assert len(kp) == 1, (r["ip"], kp)
            r.update(action="new", proxy=kp[0], proxyUrl=f"http://{kp[0]}.kiro-rs.svc:7890")
        else:
            r.update(action="noproxy", others=proxy_by_ip.get(r["ip"], []))
    out.append(r)

# every slot's IP must map to the doc row it claims
for ip, (no, name, url, en) in slot_by_ip.items():
    if ip not in ips:
        print(f"note: slot {no} {name} is not in the 31-row doc (no bench)")
json.dump(out, open("plan.json", "w"), ensure_ascii=False, indent=1)
from collections import Counter
print(Counter(r["action"] for r in out))
for r in out:
    print(f'{r["rank"]:>2} {r["ip"]:<16} {r["kiroSecs"]:.2f}s {r["downMbps"]:>4.0f}Mbps {r["score"]:>5.1f}  {r["action"]:<8} '
          + (f'槽{r["slotNo"]} {r["slotName"]}' if r["action"] == "existing" else r.get("proxy") or f'others={r.get("others")}'))
