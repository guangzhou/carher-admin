#!/usr/bin/env python3
"""sub2api admin API 客户端（在 198 上跑）。

用法（198 本机）:
    sudo kubectl -n litellm-dev get secret sub2api-secrets \
      -o jsonpath='{.data.ADMIN_PASSWORD}' | base64 -d > /run/.s2apw && chmod 600 /run/.s2apw
    S2A_PW_FILE=/run/.s2apw python3 sub2api_admin.py GET /api/v1/admin/accounts
    S2A_PW_FILE=/run/.s2apw python3 sub2api_admin.py POST /api/v1/admin/users/1/balance \
        '{"balance":100000,"operation":"set"}'

当模块用（拿 call/TOK）:
    exec(open("sub2api_admin.py").read().split("if __name__")[0])
    st, j = call("GET", "/api/v1/admin/accounts", token=TOK)

注意:
  * 充值只认 POST /api/v1/admin/users/{id}/balance。
    PUT /api/v1/admin/users/{id} 里塞 balance 会返 200 但静默丢弃 —— 写完必 GET 回读。
  * keys 在 /api/v1/keys（不是 /admin/api-keys）；accounts/groups 在 /api/v1/admin/*。
"""
import json, os, sys, urllib.error, urllib.request

BASE = os.environ.get("S2A", "http://127.0.0.1:31880")
PW_FILE = os.environ.get("S2A_PW_FILE", "/run/.s2apw")
ADMIN_EMAIL = os.environ.get("S2A_EMAIL", "admin@sub2api.local")


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=40)
        return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}


with open(PW_FILE) as f:
    _pw = f.read().strip()
_st, _j = call("POST", "/api/v1/auth/login", {"email": ADMIN_EMAIL, "password": _pw})
assert _st == 200, (_st, _j)
TOK = _j["data"]["access_token"]

if __name__ == "__main__":
    m, p = sys.argv[1], sys.argv[2]
    b = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
    st, j = call(m, p, b, TOK)
    print(st)
    print(json.dumps(j, ensure_ascii=False, indent=1)[:4000])
