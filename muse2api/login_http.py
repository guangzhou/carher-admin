#!/usr/bin/env python3
"""纯 HTTP 登录一个 muse.ai 号（不开浏览器），拿齐 cookie 后导入 198 号池。

    python3 login_http.py <label> <email> [--proxy socks5://127.0.0.1:18990] [--relogin]

为什么不用浏览器（10-30 调研 + 实测）：muse 的登录是 Meta 的 native-auth 流程，
**整条链路没有任何验证码**（36 个 JS chunk 全文搜 recaptcha/hcaptcha/turnstile = 0 命中），
五步纯 JSON 接口就能登完；人机验证不是页面跳转，而是接口回
`403 {"type":"checkpointRequired","checkpoint_url":...}` ⇒ 脚本能自己判出「这号要人过」。

    ① GET  /                            跟跳转链拿 datr
    ② POST /api/auth/native/restart     {"resume":false} -> csrf_token + 三个 native_auth cookie
    ③ POST /api/auth/native/send-otp    {"contact_point":<邮箱>,"region":"US"}
    ④ POST /api/auth/native/confirm-otp {"otp_code":<6位>,"remember_device":true}
    ⑤ POST /api/auth/save-account  +  GET /api/auth/check
    然后 GET /thread/new 等云端 VM 写入 hatch_gw / hatch_vml

🔴 ②~④ 必须带 `X-Hatch-CSRF-Token`（少了 403 csrfFailed）、`X-Hatch-Waterfall-Id`、
   `X-Hatch-Caa-Reg-Entry-Point: login_home`，以及同源的 Origin/Referer/Sec-Fetch-*。
🔴 TLS 指纹要像浏览器：用 curl_cffi 的 impersonate（别的项目用 cycletls 被挡回去，换 curl_cffi 通）。
🔴 登录出口要和这个号将来服务时的出口一致（一号一辈子一个 IP，见 harden #7）。
验证码从 generator.email 一次性邮箱读（genemail_code.py 在 188 上跑）。
不打印验证码 / cookie 值。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
JUMP = "cltx@10.68.13.188"
K3S = "cltx@10.68.13.198"
POOL = "http://10.68.13.198:30610"
ESSENTIAL = ("hatch_sess", "hatch_vml", "hatch_native_auth_device")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")


def sh(cmd, check=True, timeout=240):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        sys.exit("命令失败 (%s): %s" % (r.returncode, r.stderr[-300:]))
    return r.stdout


def code_for(email, stale=""):
    sh(["scp", "-q", os.path.join(HERE, "genemail_code.py"), JUMP + ":/tmp/muse-genemail-code.py"])
    cmd = "python3 /tmp/muse-genemail-code.py %s --wait %d" % (email, 150 if stale else 5)
    if stale:
        cmd += " --stale " + stale
    out = sh(["ssh", JUMP, cmd], check=False)
    m = re.search(r"^CODE=(\d{6})", out, re.M)
    return m.group(1) if m else None


def pool_key():
    return sh(["ssh", "-o", "ClearAllForwardings=yes", K3S,
               "sudo k3s kubectl -n muse2api get secret muse2api-env "
               "-o jsonpath={.data.MUSE2API_KEY} | base64 -d"]).strip()


def pool_api(key, method, path, body=None, timeout=200):
    import urllib.request
    req = urllib.request.Request(POOL + path, method=method,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("email")
    ap.add_argument("--proxy", default=os.environ.get("MUSE_LOGIN_PROXY", ""),
                    help="HTTP 代理（集群内 mihomo/squid）。🔴 curl_cffi 和 ssh 的 SOCKS5 不兼容，用 http://")
    ap.add_argument("--relogin", action="store_true")
    ap.add_argument("--region", default="US")
    ap.add_argument("--wait-vm", type=int, default=240)
    ap.add_argument("--no-import", action="store_true", help="只登录并打印结果，不写号池")
    ap.add_argument("--code", default="", help="直接给验证码（pod 内没有 ssh 到 188 时用）")
    ap.add_argument("--stale", default="", help="已知的旧码，配合 --code 外部取码时用")
    ap.add_argument("--print-cookies", action="store_true", help="把 cookie JSON 打到 stdout（调用方导入号池用）")
    a = ap.parse_args()

    from curl_cffi import requests as cr

    s = cr.Session(impersonate="chrome", timeout=60)
    if a.proxy:
        s.proxies = {"http": a.proxy, "https": a.proxy}

    def hdr(csrf=None):
        h = {"User-Agent": UA, "Origin": "https://muse.ai", "Referer": "https://muse.ai/",
             "Content-Type": "application/json", "Accept": "*/*",
             "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
             "X-Hatch-Waterfall-Id": str(uuid.uuid4()),
             "X-Hatch-Caa-Reg-Entry-Point": "login_home"}
        if csrf:
            h["X-Hatch-CSRF-Token"] = csrf
        return h

    def ck():
        return {c.name: c.value for c in s.cookies.jar if "muse.ai" in (c.domain or "")}

    if a.code or a.stale or a.print_cookies:
        stale = a.stale          # 外部取码模式：pod 里没有 ssh/scp，绝不调 code_for
    else:
        stale = code_for(a.email, "") or ""
    print("收件箱已有码:", "有" if stale else "无", flush=True)

    r = s.get("https://muse.ai/", headers={"User-Agent": UA}, allow_redirects=True)
    print("① 首页:", r.status_code, "| 出口:", s.get("https://api.ipify.org", timeout=30).text.strip(), flush=True)

    r = s.post("https://muse.ai/api/auth/native/restart", headers=hdr(), json={"resume": False})
    if r.status_code not in (200, 201):
        print("STOP ② restart", r.status_code, r.text[:200], flush=True)
        return 2
    csrf = (r.json() or {}).get("csrf_token")
    print("② restart:", r.status_code, "csrf:", "有" if csrf else "无", flush=True)
    if not csrf:
        return 2

    r = s.post("https://muse.ai/api/auth/native/send-otp", headers=hdr(csrf),
               json={"contact_point": a.email, "region": a.region})
    body = r.text[:200]
    print("③ send-otp:", r.status_code, body if r.status_code >= 400 else "", flush=True)
    if r.status_code >= 400:
        if "checkpointRequired" in body:
            print("STOP 这个号要人过验证（checkpointRequired）", flush=True)
            return 9
        return 3

    if a.code:
        code = a.code
    elif a.print_cookies:
        # 外部取码：等外面把新码写到这个文件里（pod 内没有出网取码的途径）
        wait_f = "/tmp/muse-otp-%s" % re.sub(r"[^A-Za-z0-9]", "", a.email)
        print("WAITCODE=%s" % wait_f, flush=True)
        code = None
        for _ in range(30):
            if os.path.exists(wait_f):
                code = open(wait_f).read().strip()
                if re.fullmatch(r"\d{6}", code):
                    break
                code = None
            time.sleep(5)
    else:
        code = code_for(a.email, stale)
    if not code:
        print("STOP 没拿到验证码", flush=True)
        return 4
    print("已取到验证码", flush=True)

    r = s.post("https://muse.ai/api/auth/native/confirm-otp", headers=hdr(csrf),
               json={"otp_code": code, "remember_device": True})
    body = r.text[:250]
    print("④ confirm-otp:", r.status_code, body if r.status_code >= 400 else "", flush=True)
    if r.status_code >= 400:
        if "checkpointRequired" in body:
            print("STOP 这个号要人过验证（checkpointRequired）", flush=True)
            return 9
        return 5

    for path, meth in (("/api/auth/save-account", "POST"), ("/api/auth/check", "GET")):
        rr = s.request(meth, "https://muse.ai" + path, headers=hdr(csrf),
                       json={} if meth == "POST" else None)
        print("⑤ %s:" % path, rr.status_code, rr.text[:120], flush=True)

    # hatch_gw / hatch_vml 只在云端 VM 连上后才签发 ⇒ 轮询 /thread/new 和 /api/session
    deadline = time.time() + a.wait_vm
    while time.time() < deadline:
        s.get("https://muse.ai/thread/new", headers={"User-Agent": UA}, allow_redirects=True)
        try:
            sj = s.get("https://muse.ai/api/session", headers=hdr(), allow_redirects=False)
            st = (sj.json() or {}) if sj.status_code == 200 else {}
            print("  /api/session:", sj.status_code, st.get("status"), st.get("vm_state"), flush=True)
        except Exception:
            pass
        if all(n in ck() for n in ESSENTIAL):
            break
        time.sleep(10)

    jar = ck()
    missing = [n for n in ESSENTIAL if n not in jar]
    print("cookie:", ",".join(sorted(jar)), flush=True)
    if missing:
        print("STOP 缺关键 cookie:", missing, flush=True)
        return 6
    if a.print_cookies:
        exp = {c.name: int(c.expires) for c in s.cookies.jar
               if "muse.ai" in (c.domain or "") and getattr(c, "expires", None)}
        print("COOKIEJSON=" + json.dumps({"cookies": dict(jar), "expires": exp}), flush=True)
    if a.no_import:
        print("OK 登录成功（--no-import，没写号池）", flush=True)
        return 0

    key = pool_key()
    cookies = dict(jar)
    expires = {c.name: int(c.expires) for c in s.cookies.jar
               if "muse.ai" in (c.domain or "") and getattr(c, "expires", None)}
    accts = pool_api(key, "GET", "/admin/accounts")
    accts = accts.get("accounts", accts)
    existing = [x for x in accts if x.get("label") == a.label]
    if existing:
        if not a.relogin:
            print("STOP 号已在池里，加 --relogin 才替换", flush=True)
            return 7
        aid = existing[0]["id"]
        pool_api(key, "POST", "/admin/accounts/%s/cookies" % aid, {"cookies": cookies, "expires": expires})
        pool_api(key, "PATCH", "/admin/accounts/%s" % aid, {"enabled": True})
        print("OK relogin %s id=%s cookie=%d" % (a.label, aid, len(cookies)), flush=True)
    else:
        res = pool_api(key, "POST", "/admin/accounts", {"label": a.label, "cookies": cookies, "expires": expires})
        aid = res["added"][0]["id"]
        print("OK imported %s id=%s cookie=%d" % (a.label, aid, len(cookies)), flush=True)
    t = pool_api(key, "POST", "/admin/accounts/%s/test" % aid)
    print("真登录测试:", t.get("ok"), (t.get("message") or t.get("detail") or "")[:100], flush=True)
    return 0 if t.get("ok") else 8


if __name__ == "__main__":
    sys.exit(main())
