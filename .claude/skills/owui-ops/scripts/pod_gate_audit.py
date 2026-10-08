# 在 open-webui pod 里跑(由 owui-model-gate-audit.sh 经 pypod 注入 ARGS)。只读,不写库。
#
# 三层各自取证,任何一层不符就 FAIL:
#   L1 OWUI 自己的 /api/models   —— 用户真正看到的列表(按真实用户签 token,不用 admin 代测)
#   L2 反代 /v1/models + 发送     —— 白名单两处都要拦(历史会话会绕过列表)
#   L3 OWUI 侧的旁路             —— direct connections / 指向非白名单的 workspace 模型
import sqlite3, urllib.request

from open_webui.utils.auth import create_token

allowed = [m.lower() for m in ARGS["allowed"]]
blocked = ARGS.get("blocked", [])
OWUI = "http://127.0.0.1:8080"
PROXY = "http://key-swap-proxy.open-webui.svc.cluster.local:8081"
db = sqlite3.connect("/app/backend/data/webui.db")
fails = []


def http(url, headers, body=None, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", **headers})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode()
    except Exception as e:  # HTTPError 也带 body
        return getattr(e, "code", 0), getattr(e, "read", lambda: b"")().decode()[:200]


def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg)
    if not ok:
        fails.append(msg)


users = db.execute("select id,email,role from user where role='user' order by last_active_at desc limit ?",
                   (ARGS.get("users", 3),)).fetchall()
users += db.execute("select id,email,role from user where role='admin' limit 1").fetchall()

# L1
for uid, email, role in users:
    s, b = http(OWUI + "/api/models", {"Authorization": "Bearer " + create_token({"id": uid})})
    ids = sorted(m["id"] for m in json.loads(b)["data"]) if s == 200 else b
    check(s == 200 and [i.lower() for i in ids] == sorted(allowed), f"L1 {role} /api/models = {ids}")

uid, email, _ = users[0]
tok = {"Authorization": "Bearer " + create_token({"id": uid})}
for m in ARGS["allowed"]:
    s, b = http(OWUI + "/api/chat/completions", tok,
                {"model": m, "stream": False, "messages": [{"role": "user", "content": "reply exactly: nonce-3317"}]})
    out = json.loads(b)["choices"][0]["message"]["content"] if s == 200 else b
    check(s == 200 and "nonce-3317" in out, f"L1 chat {m} -> {s} {out[:80]!r}")

# L2 —— 直接打反代,模拟历史会话里存着的旧模型名
hdr = {"X-OpenWebUI-User-Email": email}
s, b = http(PROXY + "/v1/models", hdr)
ids = sorted(m["id"] for m in json.loads(b)["data"]) if s == 200 else b
check(s == 200 and [i.lower() for i in ids] == sorted(allowed), f"L2 proxy /v1/models = {ids}")
for m in blocked:
    s, b = http(PROXY + "/v1/chat/completions", hdr,
                {"model": m, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
    check(s == 403, f"L2 proxy send {m} -> {s} (want 403)")

# L3
cfg = dict(db.execute("select key,value from config where key in ('direct.enable','ui.default_models')"))
check(json.loads(cfg.get("direct.enable", "false")) is False, f"L3 direct.enable = {cfg.get('direct.enable')}")
print(f"NOTE ui.default_models = {cfg.get('ui.default_models')}")
for mid, base in db.execute("select id,base_model_id from model where is_active=1 and base_model_id is not null"):
    check(base.lower() in allowed, f"L3 workspace model {mid} (base={base}) active")

print("RESULT", "PASS" if not fails else f"FAIL x{len(fails)}")
