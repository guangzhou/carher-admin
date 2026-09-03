#!/usr/bin/env python3
"""seed_token_survey.py —— 只读：对比 lane web seed 里的 bearer 与 acct WS 线那份 OAuth token 的新鲜度。

脱敏纪律：只打 **长度 / JWT exp / iss / 是否过期**，绝不打 token 本体。
不下结论：exp 在未来 ≠ 上游还认它（zero-82 踩过），唯一判据是 live-probe。
这一步只回答「WS 那份比 seed 这份新吗，值不值得灌」。
"""
import base64
import json
import subprocess
import sys
import time

NS = "litellm-product"


def sh(*a):
    r = subprocess.run(a, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def pod(label):
    rc, out, _ = sh("sudo", "-n", "kubectl", "-n", NS, "get", "pod", "-l", label,
                    "--field-selector=status.phase=Running",
                    "-o", "jsonpath={.items[0].metadata.name}")
    return out.strip()


def jwt_info(tok):
    """只解 JWT 的 payload 取 exp/iss，不回显任何 token 字节。"""
    if not tok:
        return {"len": 0, "note": "空"}
    body = tok.split(" ")[-1]
    info = {"len": len(body)}
    parts = body.split(".")
    if len(parts) != 3:
        info["note"] = "非 JWT 形状"
        return info
    try:
        p = parts[1] + "=" * (-len(parts[1]) % 4)
        d = json.loads(base64.urlsafe_b64decode(p).decode())
    except Exception as e:
        info["note"] = "payload 解不开: %s" % type(e).__name__
        return info
    exp = d.get("exp")
    now = int(time.time())
    info["iss"] = d.get("iss", "")
    info["exp"] = exp
    if exp:
        info["exp_utc"] = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(exp))
        info["剩余小时"] = round((exp - now) / 3600, 1)
        info["已过期"] = exp < now
    return info


def find_auth(obj, path=""):
    """在 seed json 里找所有 authorization 字段，返回 (路径, 值)。"""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = path + "/" + str(k)
            if str(k).lower() == "authorization" and isinstance(v, str):
                out.append((p, v))
            else:
                out += find_auth(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += find_auth(v, path + "/%d" % i)
    return out


for n in sys.argv[1:]:
    print("=" * 60, "lane", n)
    lp = pod("app=zero-cursor-bpi-%s" % n)
    ap = pod("app=chatgpt-acct-%s" % n)
    print("lane pod=%s  acct pod=%s" % (lp or "(无)", ap or "(无)"))

    if lp:
        rc, out, err = sh("sudo", "-n", "kubectl", "-n", NS, "exec", lp, "--",
                          "cat", "/seed/users.json")
        if rc != 0:
            print("  seed 读不到:", err.strip()[:200])
        else:
            try:
                seed = json.loads(out)
                hits = find_auth(seed)
                print("  seed authorization 字段数 =", len(hits))
                for p, v in hits:
                    print("   ", p, jwt_info(v))
            except Exception as e:
                print("  seed 解析失败:", e)

    if ap:
        rc, out, err = sh("sudo", "-n", "kubectl", "-n", NS, "exec", ap, "--",
                          "cat", "/chatgpt-auth/auth.json")
        if rc != 0:
            print("  acct auth.json 读不到:", err.strip()[:200])
        else:
            try:
                a = json.loads(out)
                tok = (a.get("tokens") or {}).get("access_token") or a.get("access_token")
                print("  WS access_token", jwt_info(tok))
                print("  有 refresh_token =", bool((a.get("tokens") or {}).get("refresh_token")
                                                   or a.get("refresh_token")))
                print("  last_refresh =", a.get("last_refresh", "(无)"))
            except Exception as e:
                print("  acct auth.json 解析失败:", e)
