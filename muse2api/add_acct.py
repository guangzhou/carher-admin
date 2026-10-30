#!/usr/bin/env python3
"""Add one muse.ai account to the 198 muse2api pool, end to end.

    .venv/bin/python add_acct.py <label> < card.txt

card.txt holds one 成品号 line, either format:
  A) "email----password----client_id----refresh_token"   (Outlook OAuth; code read over IMAP on 188)
  B) "email <space> code_link_url"                         (gg.00yx.cn style; GET returns JSON, latest mail only)
     Format B also accepts the raw seller text "微软邮箱X密码Y接码链接URL".

Steps: open muse.ai in agent-browser via the SOCKS tunnel -> submit email -> fetch the
6-digit code -> log in -> (if Meta shows "Confirm you're human", reopen headed and wait for a
person to clear it) -> open /thread/new so hatch_gw/hatch_vml get set -> read cookies over CDP
-> POST /admin/accounts -> close the browser -> /test the new account -> raise
MUSE2API_CONCURRENCY to the account count.

Needs: a SOCKS tunnel on MUSE_ADD_SOCKS (default 127.0.0.1:18990 -> Singapore exit), agent-browser, ssh to 10.68.13.198.
Never prints the password, refresh token, code or cookie values.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import import_cookies  # noqa: E402

JUMP = "cltx@10.68.13.188"
K3S = "cltx@10.68.13.198"
# Browser egress. Default = the pool's own exit (Aliyun Singapore, 2026-10-16):
#   ssh -i <museegress key> -fN -D 18990 museegress@47.84.85.100   (key: Secret muse2api/muse2api-egress-ssh)
# Logging in from the same exit the pool uses keeps the session on one IP.
SOCKS = os.environ.get("MUSE_ADD_SOCKS", "socks5://127.0.0.1:18990")
POOL = "http://10.68.13.198:30610"


def sh(cmd, inp=None, check=True, timeout=120):
    r = subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        sys.exit(f"command failed ({r.returncode}): {' '.join(cmd[:3])}…\n{r.stderr[-400:]}")
    return r.stdout


def ab(session, *args, timeout=90):
    return sh(["agent-browser", "--session", session, *args], timeout=timeout, check=False)


def ref_of(session, pattern):
    m = re.search(pattern + r'[^\[]*\[ref=(e\d+)\]', ab(session, "snapshot", "-i"))
    return m.group(1) if m else None


def parse_card(text):
    text = text.strip()
    if "----" in text:
        p = text.split("----")
        if len(p) < 4:
            sys.exit("format A needs email----password----client_id----refresh_token")
        return {"kind": "oauth", "email": p[0].strip(), "line": text}
    m = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)", text)
    u = re.search(r"(https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+)", text)
    if not (m and u):
        sys.exit("could not find email + code link in card text")
    if "generator.email" in u.group(1):
        # 一次性邮箱：没有 JSON 接口，收件箱服务端渲染 ⇒ genemail_code.py（10-29）
        return {"kind": "genemail", "email": m.group(1), "url": u.group(1)}
    return {"kind": "link", "email": m.group(1), "url": u.group(1)}


def fetch_code(card, since):
    """Poll until a code that arrived after `since` shows up (both sources can return an old mail)."""
    if card["kind"] == "oauth":
        sh(["scp", "-q", os.path.join(HERE, "outlook_code.py"), f"{JUMP}:/tmp/muse-outlook-code.py"])
    elif card["kind"] == "genemail":
        sh(["scp", "-q", os.path.join(HERE, "genemail_code.py"), f"{JUMP}:/tmp/muse-genemail-code.py"])
    try:
        for _ in range(12):
            if card["kind"] == "oauth":
                out = sh(["ssh", JUMP, f"python3 /tmp/muse-outlook-code.py --since-epoch {since - 60}"],
                         inp=card["line"], check=False)
                m = re.search(r"^CODE=(\d{6})$", out, re.M)
                if m:
                    return m.group(1)
            elif card["kind"] == "genemail":
                out = sh(["ssh", JUMP, f"python3 /tmp/muse-genemail-code.py {card['email']} "
                                       f"--wait 20 --stale {card.get('stale_code') or ''}"], check=False)
                m = re.search(r"^CODE=(\d{6})", out, re.M)
                if m:
                    return m.group(1)
            else:
                raw = sh(["ssh", JUMP, f"curl -fsSL --max-time 25 -A Mozilla/5.0 '{card['url']}'"], check=False)
                try:
                    d = json.loads(raw)
                except ValueError:
                    d = {}
                # The link only returns the latest mail and its received_at is in the seller's clock,
                # so a code counts as fresh only if it differs from what the link showed before we asked.
                code = str(d.get("verification_code") or "")
                if re.fullmatch(r"\d{6}", code) and code != card.get("stale_code"):
                    return code
            time.sleep(10)
    finally:
        if card["kind"] == "oauth":
            sh(["ssh", JUMP, "rm -f /tmp/muse-outlook-code.py"], check=False)
    sys.exit("no fresh login code after 2 minutes")


def login(session, card, headed):
    flags = ["--headed"] if headed else []
    ab(session, *flags, "--proxy", SOCKS, "open", "https://muse.ai/")
    time.sleep(6)
    if card["kind"] in ("link", "genemail"):
        # Remember what the link shows *before* we ask for a code, so an old code is never reused.
        raw = sh(["ssh", JUMP, f"curl -fsSL --max-time 25 -A Mozilla/5.0 '{card['url']}'"], check=False)
        try:
            card["stale_code"] = str(json.loads(raw).get("verification_code") or "")
        except ValueError:
            card["stale_code"] = ""
    elif card["kind"] == "genemail":
        out = sh(["ssh", JUMP, f"python3 /tmp/muse-genemail-code.py {card['email']} --wait 3"], check=False)
        m0 = re.search(r"^CODE=(\d{6})", out, re.M)
        card["stale_code"] = m0.group(1) if m0 else ""
    box, go = ref_of(session, 'textbox "手机号或邮箱"'), ref_of(session, 'button "继续"')
    if not (box and go):
        if "muse.ai" in ab(session, "get", "url") and ref_of(session, 'button "设置"'):
            return "already"
        sys.exit("login form not found (UI changed or not on muse.ai)")
    since = time.time()
    ab(session, "fill", f"@{box}", card["email"])
    ab(session, "click", f"@{go}")
    time.sleep(12)
    code = fetch_code(card, since)
    box, nxt = ref_of(session, 'textbox "6 位数安全码"'), ref_of(session, 'button "下一步"')
    if not box:
        sys.exit("code box not found")
    ab(session, "fill", f"@{box}", code)
    if nxt:
        ab(session, "click", f"@{nxt}")
    time.sleep(12)
    return "ok"


def wait_checkpoint(session, card):
    """Meta sometimes demands reCAPTCHA (auth.meta.com/checkpoints). We never solve it: a person does."""
    if "/checkpoints/" not in ab(session, "get", "url"):
        return
    print("Meta checkpoint (reCAPTCHA). Reopening a visible browser; clear it there and press Continue.")
    sh(["agent-browser", "close", "--all"], check=False)
    time.sleep(2)
    login(session, card, headed=True)
    deadline = time.time() + 900
    while time.time() < deadline:
        url = ab(session, "get", "url").strip()
        if url.startswith("https://muse.ai/") and "/checkpoints/" not in url:
            print("checkpoint cleared")
            return
        time.sleep(5)
    sys.exit("checkpoint not cleared within 15 minutes; browser left open")


def pool_key():
    out = sh(["ssh", K3S, "sudo kubectl -n muse2api get secret muse2api-env "
              "-o jsonpath='{.data.MUSE2API_KEY}' | base64 -d"])
    if not out.startswith("m2a_"):
        sys.exit("could not read MUSE2API_KEY from 198")
    return out.strip()


def pool(key, method, path, body=None, timeout=200):
    req = urllib.request.Request(POOL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("label", help="pool label, e.g. muse-horwich-04")
    ap.add_argument("--no-concurrency", action="store_true", help="do not touch MUSE2API_CONCURRENCY")
    ap.add_argument("--headed", action="store_true",
                    help="visible browser from the start (needed when Meta shows a checkpoint)")
    ap.add_argument("--relogin", action="store_true",
                    help="label already in pool: log in again and replace its cookies (keeps id/usage)")
    a = ap.parse_args()
    card = parse_card(sys.stdin.read())
    key = pool_key()
    existing = [x for x in pool(key, "GET", "/admin/accounts")["accounts"] if x["label"] == a.label]
    if existing and not a.relogin:
        sys.exit(f"label {a.label} already in pool (use --relogin to refresh its cookies)")
    if a.relogin and not existing:
        sys.exit(f"--relogin: label {a.label} not in pool")

    session = "museadd-" + re.sub(r"\W", "", a.label)[-12:]
    sh(["agent-browser", "close", "--all"], check=False)
    try:
        login(session, card, headed=a.headed)
        wait_checkpoint(session, card)
        ab(session, "open", "https://muse.ai/thread/new")
        time.sleep(25)  # hatch_gw / hatch_vml only appear after the cloud VM connects
        # 2026-10-16: Meta's checkpoint shows up only once /thread/new triggers the hatch OIDC
        # hop (login itself lands on muse.ai), so look again here.
        if "/checkpoints/" in ab(session, "get", "url"):
            if not a.headed:
                sys.exit("Meta checkpoint after /thread/new; rerun with --headed so a person can clear it")
            print("Meta checkpoint (reCAPTCHA) in the open browser window: tick it and press Continue.", flush=True)
            deadline = time.time() + 900
            while "/checkpoints/" in ab(session, "get", "url") and time.time() < deadline:
                time.sleep(5)
            if "/checkpoints/" in ab(session, "get", "url"):
                sys.exit("checkpoint not cleared within 15 minutes")
            print("checkpoint left; now at " + ab(session, "get", "url").strip().splitlines()[-1][:120], flush=True)
            time.sleep(15)  # let the OIDC hop land back on muse.ai before navigating
            ab(session, "open", "https://muse.ai/thread/new")
            time.sleep(25)
        cdp = ab(session, "get", "cdp-url").strip().splitlines()[-1]
        jar, expires, missing = import_cookies.collect(cdp)
        if missing:
            # hatch_gw / hatch_vml 由云端 VM 连上后才写入；固定 sleep 不可靠（10-29 人过完
            # checkpoint 后 25s 仍没齐）。轮询等到真出现，期间重开一次 /thread/new 催一下。
            deadline = time.time() + 180
            nudged = False
            while missing and time.time() < deadline:
                time.sleep(10)
                if not nudged and time.time() > deadline - 120:
                    ab(session, "open", "https://muse.ai/thread/new")
                    nudged = True
                jar, expires, missing = import_cookies.collect(cdp)
            if not missing:
                print("essential cookies arrived after extra wait", flush=True)
        if missing:
            # 这两个 cookie 由云端 VM 连上后才写入 ⇒ 缺了就直接问 /api/session 要原因，
            # 否则只看到 "missing" 猜不出是没分配 VM、还是号没过 onboarding。
            try:
                import engine_probe_session as _eps  # noqa: F401
            except Exception:
                pass
            try:
                req = urllib.request.Request("https://muse.ai/api/session", headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                    "Origin": "https://muse.ai", "Referer": "https://muse.ai/thread/new",
                    "Cookie": "; ".join("%s=%s" % kv for kv in jar.items() if kv[1])})
                import socket as _s
                _oc = _s.socket
                # 走和浏览器同一个出口（SOCKS），否则问出来的是本机 IP 的结果
                import socks  # type: ignore
                host, port = SOCKS.split("//")[-1].split(":")
                socks.set_default_proxy(socks.SOCKS5, host, int(port), rdns=True)
                _s.socket = socks.socksocket
                try:
                    sj = json.loads(urllib.request.urlopen(req, timeout=20).read())
                finally:
                    _s.socket = _oc
                print("  /api/session: status=%s vm_state=%s vm_id=%s" % (
                    sj.get("status"), sj.get("vm_state"), "有" if sj.get("vm_id") else "无"), flush=True)
            except Exception as exc:
                print("  /api/session 问不到：%s %s" % (type(exc).__name__, str(exc)[:120]), flush=True)
            url = ab(session, "get", "url").strip().splitlines()[-1][:120]
            heads = re.findall(r'(?:heading|button|textbox) "([^"]{1,50})"', ab(session, "snapshot", "-i"))[:10]
            sys.exit(f"essential cookies missing: {missing}; url={url}; page={heads}; have={sorted(jar)}")
        if existing:
            acc_id = existing[0]["id"]
            pool(key, "POST", f"/admin/accounts/{acc_id}/cookies", {"cookies": jar, "expires": expires})
            pool(key, "PATCH", f"/admin/accounts/{acc_id}", {"enabled": True})
            print(f"relogin {a.label} id={acc_id} cookies={len(jar)} (re-enabled)")
        else:
            res = pool(key, "POST", "/admin/accounts", {"label": a.label, "cookies": jar, "expires": expires})
            acc_id = res["added"][0]["id"]
            print(f"imported {a.label} id={acc_id} cookies={len(jar)}")
    finally:
        sh(["agent-browser", "close", "--all"], check=False)  # same cookies must not stay live in two places

    t = pool(key, "POST", f"/admin/accounts/{acc_id}/test")
    print(f"test: ok={t.get('ok')} {t.get('message') or t.get('detail') or ''}")
    q = pool(key, "POST", f"/admin/accounts/{acc_id}/quota")
    print(f"quota: {q.get('plan')} weekly_used={q.get('weekly_used_pct')}% reset={q.get('weekly_reset')} extra={q.get('extra_left')}")

    if not a.no_concurrency:
        n = sum(1 for x in pool(key, "GET", "/admin/accounts")["accounts"] if x.get("enabled", True))
        sh(["ssh", K3S, f"sudo kubectl -n muse2api set env deploy/muse2api MUSE2API_CONCURRENCY={n} "
            "&& sudo kubectl -n muse2api rollout status deploy/muse2api --timeout=180s"], timeout=240)
        for _ in range(30):
            try:
                if urllib.request.urlopen(POOL + "/healthz", timeout=5).status == 200:
                    break
            except OSError:
                time.sleep(2)
        print(f"concurrency set to {n}; update MUSE2API_CONCURRENCY in muse2api/k8s.yaml to match")
    sys.exit(0 if t.get("ok") else 1)


if __name__ == "__main__":
    main()