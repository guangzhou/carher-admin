"""Rebuild /root/gemini-web-build/config.yaml: keep existing clients, add/refresh one client per accounts/*.cookies.json.
Client id = file stem. Prints only ids, never cookie values."""
import glob, json, os

BASE = "/root/gemini-web-build"
cfg = json.load(open(f"{BASE}/config.yaml"))  # file is JSON (valid YAML)
clients = {c["id"]: c for c in cfg["gemini"]["clients"]}
for f in sorted(glob.glob(f"{BASE}/accounts/*.cookies.json")):
    cid = os.path.basename(f).split(".")[0]
    d = json.load(open(f))
    # keep the account's pinned exit (chatgpt-acct-console/gemini_web_pool.py, 2026-10-29); new accounts start on the pod default
    proxy = (clients.get(cid) or {}).get("proxy")
    clients[cid] = {"id": cid, "secure_1psid": d["secure_1psid"], "secure_1psidts": d["secure_1psidts"], "proxy": proxy}
cfg["gemini"]["clients"] = list(clients.values())
tmp = f"{BASE}/config.yaml.new"
with open(tmp, "w") as fh:
    json.dump(cfg, fh, indent=1)
os.chmod(tmp, 0o600)
os.replace(tmp, f"{BASE}/config.yaml")
print("clients:", [c["id"] for c in cfg["gemini"]["clients"]])
