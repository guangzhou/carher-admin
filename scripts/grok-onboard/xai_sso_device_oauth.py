#!/usr/bin/env python3
"""xai_sso_device_oauth.py — 手里有 grok.com `sso` cookie 时，纯 HTTP 换 xAI OAuth bundle。

**为什么存在**：2026-09-25 起 sub2api 自带的 `POST /admin/grok/oauth/sso-token` 和
`sso-to-oauth` 对**任何** token 都返 403 `GROK_SSO_UPSTREAM_FAILED`。2026-09-28 定因：
`accounts.x.ai` 对机房出口一律回 **Cloudflare 403 质询页**（HTML，不是 JSON），
而 sub2api pod 的出口正是机房 IP。判据（同一个 sso、同一分钟）：

    sub2api pod (172.235.204.67) → accounts.x.ai/api/auth/session → 403 + CF HTML
    225 WARP    (104.28.238.115) → 同一个 URL                     → 200 authenticated

⇒ **403 不是凭据坏，是出口被挡**。而 `auth.x.ai/oauth2/device/code` 两边都 200，
所以被挡的只有 `accounts.x.ai` 那几跳。

**与 `xai_device_capture.py` 的分工**：那个跑 Playwright 做「邮箱+密码」登录（只有密码时用）。
本脚本在**已有 sso cookie**时用，不开浏览器、不碰 Turnstile ——
sso 就是登录态本身，四跳表单全是纯 HTTP。

链路（2026-09-28 实测，全部经 WARP socks5）：

    1. POST auth.x.ai/oauth2/device/code            → device_code + user_code
    2. POST auth.x.ai/oauth2/device/verify          → 303 → .../device/consent
       表单只要 `user_code`；sso cookie 提供登录态
    3. GET  accounts.x.ai/oauth2/device/consent     → 取 `consent_token`（一次性）
    4. POST auth.x.ai/oauth2/device/approve         → {user_code, principal_type,
                                                       principal_id, consent_token,
                                                       action=allow}
    5. POST auth.x.ai/oauth2/token (device_code)    → access_token + refresh_token

⚠️ 第 3 步的 `consent_token` 必须从**这一轮**的 consent 页取，它绑 user_code。
⚠️ 第 4 步的 `action=allow` 来自 Allow 按钮的 `value="allow"`（Deny 是 `deny`）。

用法（在 225 上，WARP socks5 已监听 127.0.0.1:40000）：

    python3 xai_sso_device_oauth.py --creds .creds.txt --out-dir /tmp/bundles
    python3 xai_sso_device_oauth.py --sso-file /tmp/.sso1 --email a@b.com --out /tmp/x.json

产物即 `create-grok-oauth-account.py` 吃的 payload 形状，0600。
⛔ 全程不打印 token，只打印长度和前缀。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PROXY = os.environ.get("XAI_PROXY", "socks5://127.0.0.1:40000")
GROUP_ID = 7


def log(*a):
    print(*a, flush=True)


def _curl(url, sso=None, post=None, follow=False, timeout=40, jar=None,
          referer="https://accounts.x.ai/", origin=None):
    """One HTTP call through the WARP socks5, returning (status, headers, body).

    curl rather than urllib on purpose: the only egress that x.ai's Cloudflare
    does not 403 is the WARP socks5 on 225, and `--socks5-hostname` also sends
    DNS through it.  A hostname resolved locally would leak the datacenter's
    resolver and is one more thing that can differ between the probe and the
    real request.

    `jar` is a cookie-jar path shared across one account's four hops, and it is
    not optional decoration: /oauth2/device/approve validates the request
    against cookies that /verify and /consent set (__cf_bm among them).
    Sending only `sso` on that hop returns 403 "Request could not be verified"
    -- an HTML page, and NOT the Cloudflare egress block, so the two 403s have
    to be told apart by their title, not their status.
    """
    import subprocess
    import tempfile
    bf = tempfile.mkstemp(prefix=".xai-b-")[1]
    hf = tempfile.mkstemp(prefix=".xai-h-")[1]
    cmd = ["curl", "-s", "--max-time", str(timeout),
           "--socks5-hostname", PROXY.split("//")[-1],
           "-o", bf, "-D", hf, "-w", "%{http_code}", "-A", UA]
    if follow:
        cmd.append("-L")
    if jar:
        cmd += ["-b", jar, "-c", jar]
    if sso:
        cmd += ["-H", "Cookie: sso=%s" % sso]
    if origin:
        cmd += ["-H", "Origin: " + origin]
    if post is not None:
        cmd += ["-X", "POST", "-H",
                "Content-Type: application/x-www-form-urlencoded",
                "-H", "Referer: " + referer, "--data-binary", post]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True)
    st = r.stdout.strip()
    body = open(bf, encoding="utf-8", errors="replace").read()
    hdrs = open(hf, encoding="utf-8", errors="replace").read()
    os.unlink(bf)
    os.unlink(hf)
    if not st.isdigit():
        raise SystemExit("curl failed (%s): %s" % (r.returncode, r.stderr[:200]))
    return int(st), hdrs, body


def _form(d):
    return urllib.parse.urlencode(d)


def _title(body):
    m = re.search(r"<title>([^<]*)</title>", body, re.I)
    return m.group(1).strip() if m else ""


def html_403_kind(status, body):
    """Which of the TWO HTML 403s is this?  They are different faults.

    Both are a 403 with an HTML body, so "403 + <!DOCTYPE html" cannot tell
    them apart -- only the <title> can, and getting it wrong costs a full round
    of work in the wrong direction (2026-09-25 .. 09-28):

      "Request could not be verified"  -> cookies/CSRF.  The jar did not carry
          what this hop validates against (__cf_bm et al).  Same egress works
          once the jar is threaded; moving hosts fixes nothing.
      Cloudflare block/challenge page  -> wrong egress.  accounts.x.ai 403s
          datacenter IPs outright.  Only re-running through the 225 WARP socks5
          fixes it; the cookie was never the problem.

    Returns "csrf" | "egress" | "" (not an HTML 403 at all).
    """
    if status != 403:
        return ""
    head = body[:400].lstrip()
    if not (head.startswith("<!DOCTYPE") or head.startswith("<html")
            or "<title>" in body[:2000].lower()):
        return ""
    t = _title(body).lower()
    if "could not be verified" in t or "csrf" in t:
        return "csrf"
    if ("cloudflare" in body[:4000].lower() or "attention required" in t
            or "access denied" in t or "just a moment" in t or "blocked" in t):
        return "egress"
    # Unknown title: say so instead of guessing one of the two.  A silent
    # mis-bucket here is what sent 09-25 chasing sub2api's version.
    return "unknown"


def _die_on_html_403(step, status, body):
    """Fail loudly AND in the right direction, or return (no HTML 403 here)."""
    kind = html_403_kind(status, body)
    if not kind:
        return
    t = _title(body) or "(no title)"
    if kind == "csrf":
        raise SystemExit(
            "%s http=403 <title>%s</title> -- this is the COOKIE/CSRF 403, not "
            "the egress block.\n  The jar did not carry this hop's cookies; "
            "moving to another host will NOT fix it." % (step, t))
    if kind == "egress":
        raise SystemExit(
            "%s http=403 <title>%s</title> -- this is the Cloudflare EGRESS "
            "block.\n  accounts.x.ai refuses datacenter IPs; run this on 225 "
            "through the WARP socks5 (XAI_PROXY=%s). The cookie is fine."
            % (step, t, PROXY))
    raise SystemExit(
        "%s http=403 <title>%s</title> -- an HTML 403 matching NEITHER known "
        "shape.\n  Do not assume egress or cookies: read the page before "
        "attributing this one." % (step, t))


def check_sso(sso):
    """Prove the cookie is alive BEFORE spending a device code on it.

    grok.com/rest/auth/get-user answers from any egress, so a failure here is
    genuinely the cookie; accounts.x.ai would confound cookie with egress.
    Returns the dict, so the caller can assert the email matches the line it
    came from -- a swapped sso would otherwise onboard the wrong xAI account
    under the right name.
    """
    st, _, body = _curl("https://grok.com/rest/auth/get-user", sso=sso)
    if st != 200:
        return None, "get-user http=%d %s" % (st, body[:120].replace("\n", " "))
    try:
        return json.loads(body), None
    except Exception:
        return None, "get-user body not json: %s" % body[:120]


def subscription_of(sso):
    """The tier as xAI itself reports it, from grok.com/rest/subscriptions.

    Used instead of the JWT `tier` claim: device-code tokens carry `tier: 5`
    while SSO-exchanged ones carry no such claim at all, so there is no mapping
    table to read it with -- filling one in from the number is inventing data.
    `productId` (grok.ultra / grok.premium...) is xAI's own word for it.
    """
    st, _, body = _curl("https://grok.com/rest/subscriptions", sso=sso)
    if st != 200:
        return None
    try:
        subs = json.loads(body).get("subscriptions") or []
    except Exception:
        return None
    for s in subs:
        for k in ("google", "apple", "stripe", "web"):
            if isinstance(s.get(k), dict) and s[k].get("productId"):
                return {"productId": s[k]["productId"],
                        "basePlanId": s[k].get("basePlanId"),
                        "expiryTime": s[k].get("expiryTime")}
    return None


def device_code(jar=None):
    st, _, body = _curl("https://auth.x.ai/oauth2/device/code", jar=jar,
                        post=_form({"client_id": CLIENT_ID, "scope": SCOPE}))
    if st != 200:
        raise SystemExit("device/code http=%d: %s" % (st, body[:200]))
    return json.loads(body)


def landing(sso, user_code, jar):
    """Step 1b: load the device page the way a browser does, to seed the jar.

    Skipping it leaves the jar without the cookies /approve checks against, and
    the failure lands three hops later as a 403 on approve -- far from its cause.
    """
    url = "https://accounts.x.ai/oauth2/device?user_code=%s" % user_code
    st, _, body = _curl(url, sso=sso, jar=jar)
    _die_on_html_403("device page", st, body)
    return st


def verify_code(sso, user_code, jar):
    """Step 2: hand the user_code to the logged-in session.

    A 303 to .../device/consent is the success shape.  200 here means the page
    came back instead of a redirect, i.e. it did not accept the session.
    """
    st, hdrs, body = _curl("https://auth.x.ai/oauth2/device/verify", sso=sso,
                           jar=jar, origin="https://accounts.x.ai",
                           referer="https://accounts.x.ai/oauth2/device?user_code=%s"
                                   % user_code,
                           post=_form({"user_code": user_code}))
    _die_on_html_403("device/verify", st, body)
    loc = ""
    for line in hdrs.splitlines():
        if line.lower().startswith("location:"):
            loc = line.split(":", 1)[1].strip()
    if st != 303 or "consent" not in loc:
        raise SystemExit("device/verify http=%d location=%r body=%s"
                         % (st, loc, body[:200]))
    return loc


def consent_token(sso, consent_url, jar):
    """Step 3: read this round's one-shot consent_token out of the HTML form.

    It is bound to the user_code, so it cannot be cached between accounts --
    reusing one silently approves nothing and the token poll then hangs on
    authorization_pending, which looks like the approve step never ran.
    """
    st, _, body = _curl(consent_url, sso=sso, jar=jar)
    _die_on_html_403("consent page", st, body)
    if st != 200:
        raise SystemExit("consent page http=%d: %s" % (st, body[:200]))
    out = {}
    for m in re.finditer(r'<input[^>]*type="hidden"[^>]*>', body):
        tag = m.group(0)
        n = re.search(r'name="([^"]*)"', tag)
        v = re.search(r'value="([^"]*)"', tag)
        if n:
            out[n.group(1)] = v.group(1) if v else ""
    if "consent_token" not in out:
        raise SystemExit("no consent_token in the consent page (%d bytes) -- "
                         "the form shape changed" % len(body))
    if 'value="allow"' not in body:
        raise SystemExit("no Allow button on the consent page -- refusing to "
                         "guess the action value")
    return out


def approve(sso, fields, consent_url, jar):
    d = dict(fields)
    d["action"] = "allow"
    st, hdrs, body = _curl("https://auth.x.ai/oauth2/device/approve", sso=sso,
                           jar=jar, origin="https://accounts.x.ai",
                           referer=consent_url, post=_form(d))
    # This is the hop that actually broke on 09-28, and it broke as the CSRF
    # 403, not the egress one.  Route it through the same discriminator so the
    # message points at the jar instead of sending anyone to a different host.
    _die_on_html_403("device/approve", st, body)
    if st not in (200, 302, 303):
        raise SystemExit("device/approve http=%d %s: %s"
                         % (st, "<title>%s</title>" % _title(body) if _title(body)
                            else "", body[:200]))
    return st


def poll_token(device_code_val, interval, budget_s=120):
    """Step 5.  Never treat a timeout here as "approve worked but xAI is slow":
    authorization_pending all the way to the deadline means the approve did not
    register, and the loud exit says which of the two it was."""
    deadline = time.time() + budget_s
    last = ""
    while time.time() < deadline:
        st, _, body = _curl("https://auth.x.ai/oauth2/token", post=_form({
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code_val, "client_id": CLIENT_ID}))
        try:
            j = json.loads(body)
        except Exception:
            j = {"error": "unparseable", "raw": body[:200]}
        if st == 200 and "access_token" in j:
            return j
        last = j.get("error", "")
        if last not in ("authorization_pending", "slow_down"):
            raise SystemExit("token poll http=%d error=%s %s"
                             % (st, last, body[:200]))
        time.sleep(interval + (5 if last == "slow_down" else 0))
    raise SystemExit("token poll timed out still %r -- the approve did not "
                     "register" % last)


def _xai_models_once(access_token):
    """One shot at api.x.ai/v1/models.  Returns (verdict_string, is_200)."""
    import subprocess
    r = subprocess.run(
        ["curl", "-s", "--max-time", "30", "-o", "/dev/stdout", "-w",
         "\n%{http_code}", "https://api.x.ai/v1/models",
         "-H", "Authorization: Bearer " + access_token],
        capture_output=True, text=True)
    parts = r.stdout.rsplit("\n", 1)
    body, st = (parts[0], parts[-1]) if len(parts) == 2 else ("", "?")
    if st == "200":
        try:
            return "200 usable (%d models)" % len(json.loads(body)["data"]), True
        except Exception:
            return "200 usable", True
    try:
        return "%s %s" % (st, json.loads(body).get("code") or body[:80]), False
    except Exception:
        return "%s %s" % (st, body[:80]), False


def xai_models_verdict(access_token, attempts=3, pause=20, log=None):
    """The one ruler that separates "token dead" from "xAI credit dead".

    A brand-new token that still gets 403 personal-team-blocked:spending-limit
    means the subscription has no credit: creating the account would add a leg
    that cannot serve, and no amount of re-authenticating changes it.  Run
    before writing anything into sub2api, so a dead account is never onboarded
    as if it were capacity.

    🔴 This is a GATE on onboarding, so a single sample must never decide it.
    2026-10-01: `ekobqjdskw@outlook.com` got one `403 permission-denied` here
    and was dropped as BAD -- three probes with the SAME token from the SAME
    host minutes later were 200/200/200 with 13 models, and the account was
    onboarded by hand as acct 71.  A transient 403 and a credit-dead 403 look
    identical in one shot, so a non-200 is retried up to `attempts` times:
    only a verdict that is non-200 EVERY time gets to reject an account.
    The returned string carries the attempt trail, so a flap stays visible in
    the bundle's `_probe` instead of being smoothed into a clean green.
    """
    trail = []
    for i in range(max(1, attempts)):
        verdict, ok = _xai_models_once(access_token)
        trail.append(verdict)
        if ok:
            return verdict if i == 0 else "%s [flapped: %s]" % (
                verdict, " -> ".join(trail[:-1]))
        if i + 1 < attempts:
            if log:
                log("  x.ai verdict: %s -- retrying in %ds (one shot is not a "
                    "ruler)" % (verdict, pause))
            time.sleep(pause)
    return "%s [%d/%d attempts: %s]" % (trail[-1], len(trail), attempts,
                                        " -> ".join(trail))


def parse_creds(path):
    """Same `-{4,}` split as sub2api-grok-onboard.py, same reasons.

    The real 卡密导出.txt uses FIVE dashes and carries a UTF-8 BOM, and a line
    with an `@` in it is never skipped quietly -- a mangled separator has to be
    loud here, or the account silently goes missing several steps later.
    """
    out = []
    try:
        lines = open(path, encoding="utf-8-sig").read().splitlines()
    except UnicodeDecodeError:
        lines = open(path, encoding="gbk").read().splitlines()
    for lineno, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        f = re.split(r"-{4,}", raw)
        if len(f) < 2:
            if "@" in raw:
                sys.exit("line %d: has an email but no `----` separator" % lineno)
            continue
        sso = f[7].strip() if len(f) >= 8 else f[-1].strip()
        if not sso.startswith("eyJ"):
            sys.exit("line %d: sso field is not a JWT: %r" % (lineno, sso[:40]))
        out.append({"name": f[0].strip(), "sso": sso,
                    "userid": f[6].strip() if len(f) >= 8 else None})
    if not out:
        sys.exit("no credential lines in %s" % path)
    return out


def one(email, sso, out_path, expect_userid=None):
    log("=== %s ===" % email)
    u, err = check_sso(sso)
    if err:
        log("  SSO DEAD: %s" % err)
        return False
    if (u.get("email") or "").lower() != email.lower():
        log("  REFUSING: sso belongs to %s, not %s" % (u.get("email"), email))
        return False
    uid = u.get("userId")
    if expect_userid and uid != expect_userid:
        log("  REFUSING: userId %s != field 7 %s" % (uid, expect_userid))
        return False
    sub = subscription_of(sso)
    log("  sso ok  userId=%s  subscription=%s" % (uid, sub))

    # One jar per account, shredded after: it holds session cookies, and
    # sharing it between accounts would carry one login into the next.
    import tempfile
    jar = tempfile.mkstemp(prefix=".xai-jar-")[1]
    os.chmod(jar, 0o600)
    try:
        d = device_code(jar)
        log("  user_code=%s" % d["user_code"])
        landing(sso, d["user_code"], jar)
        consent_url = verify_code(sso, d["user_code"], jar)
        fields = consent_token(sso, consent_url, jar)
        log("  consent_token len=%d principal=%s/%s"
            % (len(fields["consent_token"]), fields.get("principal_type"),
               fields.get("principal_id") or "-"))
        st = approve(sso, fields, consent_url, jar)
        log("  approve http=%d" % st)
    finally:
        try:
            os.unlink(jar)
        except OSError:
            pass
    tok = poll_token(d["device_code"], int(d.get("interval", 5)))
    at, rt = tok.get("access_token", ""), tok.get("refresh_token", "")
    log("  token ok  access=%s…(%d)  refresh=%s…(%d)  expires_in=%s"
        % (at[:12], len(at), rt[:8], len(rt), tok.get("expires_in")))

    verdict = xai_models_verdict(at, log=log)
    log("  x.ai verdict: %s" % verdict)

    tier = (sub or {}).get("productId") or ""
    bundle = {
        "name": email,
        "platform": "grok",
        "type": "oauth",
        "status": "active",
        "concurrency": 200,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
        "group_ids": [GROUP_ID],
        "credentials": {
            "access_token": at,
            "refresh_token": rt,
            "token_type": tok.get("token_type", "Bearer"),
            "scope": tok.get("scope", SCOPE),
            "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
            "client_id": CLIENT_ID,
            "base_url": "https://api.x.ai/v1",
            "email": email,
            "sub": uid,
            "subscription_tier": ("supergrok_heavy" if tier == "grok.ultra"
                                  else tier),
            "_token_version": int(time.time() * 1000),
        },
        "_probe": {"xai_models": verdict, "subscription": sub},
    }
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(bundle, fh, indent=1)
    log("  wrote %s (0600)" % out_path)
    return verdict.startswith("200")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--creds")
    ap.add_argument("--sso-file")
    ap.add_argument("--email")
    ap.add_argument("--out")
    ap.add_argument("--out-dir", default="/tmp/xai-bundles")
    a = ap.parse_args()

    if a.creds:
        creds = parse_creds(a.creds)
    elif a.sso_file and a.email:
        creds = [{"name": a.email, "sso": open(a.sso_file).read().strip(),
                  "userid": None}]
    else:
        ap.error("need --creds, or --sso-file with --email")

    os.makedirs(a.out_dir, exist_ok=True)
    os.chmod(a.out_dir, 0o700)
    ok, bad = [], []
    for c in creds:
        tag = c["name"].split("@")[0]
        out = a.out or os.path.join(a.out_dir, "bundle-%s.json" % tag)
        try:
            good = one(c["name"], c["sso"], out, c.get("userid"))
        except SystemExit as e:
            log("  FAILED: %s" % e)
            good = False
        (ok if good else bad).append(c["name"])

    log("\n=== %d/%d captured with a usable token ===" % (len(ok), len(creds)))
    for n in ok:
        log("  OK   %s" % n)
    for n in bad:
        log("  BAD  %s" % n)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
