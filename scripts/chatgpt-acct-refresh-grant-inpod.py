#!/usr/bin/env python3
# acct pod 内跑的 refresh grant(无 docstring/反引号/$,可 base64 直送 kubectl exec)。
#   B64=$(base64 < this | tr -d '\n')
#   kubectl exec <pod> -- python3 -c "import base64;exec(compile(base64.b64decode('$B64'),'p','exec'))"
# 200 => 同轮写回:备份 .bak-rg-<ts> -> .tmp -> os.replace -> 回读断言 rt/at 都换了。
#        (RT 只发一次;200 后不写回 = 把凭据烧掉)
# 4xx => 什么都不写,旧 RT 原样。
# 输出机读行: RG code=<..> plan=<..> until=<..> acct=<..> writeback=<OK|MISMATCH|NONE> err=<..>
import json, os, sys, time, base64, urllib.request, urllib.error

CID = "app_EMoamEEZ73f0CkXaXp7hrann"
URL = "https://auth.openai.com/oauth/token"
CLAIM = "https://api.openai.com/auth"
path = sys.argv[1] if len(sys.argv) > 1 else "/chatgpt-auth/auth.json"


def claims(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get(CLAIM, {})
    except Exception:
        return {}


cur = json.load(open(path))
body = json.dumps({"client_id": CID, "grant_type": "refresh_token",
                   "refresh_token": cur["refresh_token"],
                   "scope": "openid profile email"}).encode()
req = urllib.request.Request(URL, data=body, method="POST", headers={
    "Content-Type": "application/json",
    "User-Agent": "codex_cli_rs/0.30.0 (Linux; x86_64)"})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        code, raw = r.getcode(), r.read().decode()
except urllib.error.HTTPError as e:
    txt = e.read().decode()
    try:
        err = json.loads(txt).get("error", {})
        err = err.get("code") if isinstance(err, dict) else err
    except Exception:
        err = txt[:80].replace(" ", "_")
    print("RG code=%d plan=- until=- acct=- writeback=NONE err=%s" % (e.code, err))
    raise SystemExit(0)

new = json.loads(raw)
merged = dict(cur)
for k in ("access_token", "refresh_token", "id_token"):
    if new.get(k):
        merged[k] = new[k]
if new.get("expires_in"):
    merged["expires_at"] = int(time.time()) + int(new["expires_in"])
with open("%s.bak-rg-%d" % (path, int(time.time())), "w") as fh:
    json.dump(cur, fh, indent=2)
with open(path + ".tmp", "w") as fh:
    json.dump(merged, fh, indent=2)
os.replace(path + ".tmp", path)
back = json.load(open(path))
ok = (back["refresh_token"] == merged["refresh_token"]
      and back["access_token"] != cur.get("access_token"))
a = claims(back.get("id_token", ""))
print("RG code=%d plan=%s until=%s acct=%s writeback=%s err=-" % (
    code, a.get("chatgpt_plan_type"), a.get("chatgpt_subscription_active_until"),
    a.get("chatgpt_account_id"), "OK" if ok else "MISMATCH"))
