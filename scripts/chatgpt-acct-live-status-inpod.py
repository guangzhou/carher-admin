#!/usr/bin/env python3
# acct pod 内读「这个号此刻的真实状态」,只读、不写任何东西。
#   kubectl exec -i <acct-pod> -- python3 - < chatgpt-acct-live-status-inpod.py
# 输出一行机读:
#   LIVE plan=<pro|free|..|-> used=<0-100|-> allowed=<..> sub_active=<..> expires=<..> will_renew=<..> err=<..>
#
# 为什么要两个端点:
#   wham/usage       —— codex API 路径,CF 不挡;给 live plan + 周额度。是判 pro/free 的主尺子
#   accounts/check   —— web 路径,CF 常 403(重试能过);给订阅是否 active / 到期 / 是否自动续费
# 盘上 id_token/AT 的 plan 是签发快照,刚开通的号会一直写 free,别拿它判。
# 401 时 err 写上游 code(token_revoked / token_expired …),给调用方决定要不要 refresh。
import json, time, urllib.request, urllib.error

d = json.load(open("/chatgpt-auth/auth.json"))
acct = d.get("account_id") or ""
H = {"Authorization": "Bearer " + d.get("access_token", ""), "ChatGPT-Account-ID": acct,
     "originator": "codex_cli_rs", "User-Agent": "codex_cli_rs/0.41.0"}
r = dict(plan="-", used="-", allowed="-", sub_active="-", expires="-", will_renew="-", err="-")


def get(url, tries):
    for _ in range(tries):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=H), timeout=40).read())
        except urllib.error.HTTPError as e:
            body = e.read()[:400].decode(errors="replace")
            if e.code == 401:
                for tok in ("token_revoked", "token_expired", "token_invalidated", "invalid"):
                    if tok in body:
                        r["err"] = tok
                        break
                else:
                    r["err"] = "401"
                return None
            time.sleep(6)
        except Exception:
            time.sleep(6)
    return None


u = get("https://chatgpt.com/backend-api/wham/usage", 4)
if u:
    rl = u.get("rate_limit") or {}
    r.update(plan=u.get("plan_type"), allowed=rl.get("allowed"),
             used=(rl.get("primary_window") or {}).get("used_percent"))
if r["err"] == "-":
    j = get("https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27", 6)
    if j:
        a = (j.get("accounts") or {}).get(acct, {})
        ent = a.get("entitlement") or {}
        r.update(sub_active=ent.get("has_active_subscription"),
                 expires=(ent.get("expires_at") or "-")[:10],
                 will_renew=(a.get("last_active_subscription") or {}).get("will_renew"))
print("LIVE " + " ".join("%s=%s" % (k, r[k]) for k in
                         ("plan", "used", "allowed", "sub_active", "expires", "will_renew", "err")))
