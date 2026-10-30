#!/usr/bin/env python3
"""Gemini 网页池「每个号到底登上了没有」+「密码还对不对」的量具。在 198 上以 root 跑。

为什么需要它（10-29 查清的坑）：
  gemini_webapi 2.1.1 对**没 cookie 的匿名 guest** 和**cookie 过期的号**给出的读数**完全一样**
  （account_status=UNAUTHENTICATED、模型表只有 gemini-flash-lite、纯聊天照样能回话）。
  ⇒ 只看 account_status / 只看「聊天通不通」都判不出失效。唯一能分开的判据是 **SNlM0e access_token**：
  真登上的会话有，guest 没有。本脚本就以此为判据。

  python3 login_health.py check [id ...]     # pod 内逐号 init，报 authed / 模型表 / 档位（只读，不花额度）
  python3 login_health.py quota [id ...]     # 打 p5 的 /carher/quota 读额度（只读 RPC）
  python3 login_health.py report             # 合并一张表：authed / pro / cookie 天数 / 密码状态（读缓存）
  python3 login_health.py cards-export [id ...]   # 导卡密给本机的密码体检用（见下）
  python3 login_health.py pw-ingest < 判定.json   # 收本机跑出来的密码判定

密码体检（浏览器）只能从**本机 Mac** 发起 —— 198 连不上 188（没 publickey，10-29 实测）：
  bash scripts/gemini-web/pwcensus.sh            # 本机编排：198 导卡密 → 188 四车道浏览器 → 结果回 198
判据和产物都落 /opt/chatgpt-acct-console/data/gweb_login.json（0600），管理台 8796 直接读这份。
不打印 cookie / 密码 / TOTP。
"""
import json
import os
import subprocess
import sys
import time

NS = "litellm-dev"
K = ["k3s", "kubectl", "-n", NS]
CFG = "/root/gemini-web-build/config.yaml"
ACCTS = "/root/gemini-web-build/accounts"
STATE = "/opt/chatgpt-acct-console/data/gweb_login.json"
H188 = "cltx@10.68.13.188"
D188 = "/tmp/gweb-health"
IMG = "mcr.microsoft.com/playwright/python:v1.60.0-noble"
HERE = os.path.dirname(os.path.abspath(__file__))


def sh(argv, inp=None, timeout=600):
    r = subprocess.run(argv, input=inp, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError("%s: %s" % (" ".join(argv[:5]), (r.stderr or r.stdout).strip()[:300]))
    return r.stdout


def cfg():
    return json.load(open(CFG))


def pod():
    return sh(K + ["get", "pod", "-l", "app=gemini-web", "--field-selector=status.phase=Running",
                   "-o", "jsonpath={.items[0].metadata.name}"]).strip()


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(d):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


# ---------------------------------------------------------------- pod 内逐号 init
CHECK_PY = r'''
import asyncio, json, sys
sys.path.insert(0, "/app")
from gemini_webapi import GeminiClient

want = set(json.loads(sys.argv[1])) if len(sys.argv) > 1 and sys.argv[1] else set()
cs = json.load(open("/app/config/config.yaml"))["gemini"]["clients"]

async def one(c):
    r = {"id": c["id"], "at": __import__("time").time()}
    cl = GeminiClient(secure_1psid=c["secure_1psid"], secure_1psidts=c.get("secure_1psidts"),
                      proxy=c.get("proxy"))
    try:
        await cl.init(timeout=60, auto_refresh=False, verbose=False)
        # SNlM0e 才是判据：guest 会话没有它，而 account_status 对 guest 和过期号同形
        r["authed"] = bool(cl.access_token)
        r["status"] = cl.account_status.name
        r["models"] = sorted(m.model_name for m in cl._model_registry.values())
        r["pro"] = any(m != "gemini-flash-lite" for m in r["models"])
        r["cookie_source"] = getattr(cl, "_cookie_source", None)
    except Exception as e:
        r["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    try:
        await cl.close()
    except Exception:
        pass
    return r

async def main():
    out = []
    for c in cs:
        if want and c["id"] not in want:
            continue
        out.append(await one(c))
    print("@@JSON@@" + json.dumps(out))

asyncio.run(main())
'''


def check(ids=None):
    """pod 内逐号 init，判 authed。只读，不发生成请求（init 只打 GetUserStatus 等几个 RPC）。"""
    argv = K + ["exec", "-i", pod(), "--", "/app/.venv/bin/python", "-", json.dumps(list(ids or []))]
    out = sh(argv, inp=CHECK_PY, timeout=max(300, 12 * len(ids or cfg()["gemini"]["clients"])))
    line = [l for l in out.splitlines() if l.startswith("@@JSON@@")]
    if not line:
        raise RuntimeError("探针没回 JSON：%s" % out[-300:])
    rows = json.loads(line[-1][8:])
    st = load_state()
    st.setdefault("check", {}).update({r["id"]: r for r in rows})
    st["checkAt"] = time.time()
    save_state(st)
    return rows


def quota(ids=None):
    """p5 的 /carher/quota（只读 RPC，不花额度）。旧镜像（没 p5）返回 None。"""
    import urllib.error
    import urllib.request
    ip = sh(K + ["get", "svc", "gemini-web", "-o", "jsonpath={.spec.clusterIP}"]).strip()
    key = (cfg().get("server") or {}).get("api_key") or ""
    body = json.dumps({"ids": list(ids)} if ids else {}).encode()
    req = urllib.request.Request("http://%s:8000/carher/quota" % ip, data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    try:
        rows = json.loads(urllib.request.urlopen(req, timeout=180).read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    st = load_state()
    st.setdefault("quota", {}).update(rows)
    st["quotaAt"] = time.time()
    save_state(st)
    return rows


# ---------------------------------------------------------------- 188 上只验密码
# 🔴 198 **连不上** 188（没有 publickey，10-29 实测 Permission denied）⇒ 浏览器那一步只能
#    从本机 Mac 发起（它同时能 ssh 198 和 188）。198 上只负责导卡密、收结果，见 pwcheck_ingest()。
def cards_export(ids=None):
    """导出要验的卡密（JSON 行，供本机的 pwcheck 编排用）。只在 root 手里，不落盘。"""
    ids = sorted(ids or [c["id"] for c in cfg()["gemini"]["clients"]])
    out = {}
    for i in ids:
        p = "%s/%s.json" % (ACCTS, i)
        if os.path.exists(p):
            out[i] = json.load(open(p))
    return out


def pwcheck_ingest(res):
    """把本机跑出来的密码判定收进 gweb_login.json。res = {id: "ok"|"pw-changed:8d"|...}。"""
    st = load_state()
    st.setdefault("pw", {}).update({k: {"v": v, "at": time.time()} for k, v in res.items()})
    st["pwAt"] = time.time()
    save_state(st)
    return len(res)


def report():
    """合并表：每号 authed / pro / cookie 存了几天 / 密码状态。纯读缓存，不打上游。"""
    st = load_state()
    ck, pw, qa = st.get("check") or {}, st.get("pw") or {}, st.get("quota") or {}
    ts = {}
    for c in cfg()["gemini"]["clients"]:
        f = "%s/%s.cookies.json" % (ACCTS, c["id"])
        try:
            ts[c["id"]] = json.load(open(f)).get("ts")
        except Exception:
            ts[c["id"]] = None
    rows = []
    now = time.time()
    for c in cfg()["gemini"]["clients"]:
        i = c["id"]
        r = ck.get(i) or {}
        q = qa.get(i) or {}
        rows.append({"id": i, "authed": r.get("authed"), "pro": r.get("pro"),
                     "models": r.get("models"), "status": r.get("status"),
                     "cookie_age_d": round((now - ts[i]) / 86400, 1) if ts.get(i) else None,
                     "pw": (pw.get(i) or {}).get("v"), "pw_at": (pw.get(i) or {}).get("at"),
                     "quota": q.get("usage"), "quota_error": q.get("error"),
                     "checked_at": r.get("at")})
    return {"clients": rows, "checkAt": st.get("checkAt"), "pwAt": st.get("pwAt"),
            "quotaAt": st.get("quotaAt"), "at": now}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    arg = sys.argv[2:] or None
    if cmd == "check":
        rows = check(arg)
        bad = [r for r in rows if not r.get("authed")]
        for r in rows:
            print("%-24s authed=%-5s pro=%-5s %s %s" % (r["id"], r.get("authed"), r.get("pro"),
                                                        r.get("status") or "", ",".join(r.get("models") or [])[:60]))
        print("== 共 %d 号，没登上的 %d 个" % (len(rows), len(bad)))
    elif cmd == "quota":
        rows = quota(arg)
        if rows is None:
            sys.exit("现役镜像没有 p5 /carher/quota，先上 p5")
        for i, r in sorted(rows.items()):
            u = r.get("usage") or {}
            print("%-24s authed=%-5s %s %s" % (i, r.get("authed"), r.get("error") or "",
                                               json.dumps(u, ensure_ascii=False)[:140]))
    elif cmd == "cards-export":
        print(json.dumps(cards_export(arg)))
    elif cmd == "pw-ingest":
        n = pwcheck_ingest(json.load(sys.stdin))
        print("收了 %d 条密码判定" % n)
    elif cmd == "report":
        print(json.dumps(report(), ensure_ascii=False, indent=1)[:8000])
    else:
        sys.exit(__doc__)
