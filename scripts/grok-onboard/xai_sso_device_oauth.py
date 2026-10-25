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
本脚本在**已有 sso cookie**时用，不碰 Turnstile —— sso 就是登录态本身。

🔴 **2026-10-13 起 approve 默认走浏览器（`--approve browser`）**：consent 表单多了个
只由页面 JS 填的 `castle_request_token`（Castle 反机器人），纯 HTTP 提交恒 403
`Request could not be verified — We could not verify this browser`（jar 里
`__cf_bm`/`gb_anon_id` 都在，**不是** 09-28 那个 jar 没串的病）。浏览器模式 = patchright
经同一个 WARP socks5、把 sso/sso-rw 种到 `.x.ai`、点 Continue→Allow 到 `/device/done`
（acct 75 一把过）。需 `DISPLAY`（Xvfb）+ 225 的 pyenv（见 `browser_approve()`）。
`--approve http` 保留旧四跳纯 HTTP 路径，只用于诊断。
⚠️ 第一步 `get-user` 会随机回 `499 Timeout expired`（**假 sso 也同样回包**，不含信息），
已改成重试，只有非瞬时码才判 SSO DEAD。

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
          referer="https://accounts.x.ai/", origin=None,
          content_type=None, extra_headers=None):
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
        # content_type: Next.js Server Action 要 text/plain，不是表单编码（见
        # xai_authcode_grant.py）；默认仍是表单，老调用方行为不变。
        cmd += ["-X", "POST", "-H",
                "Content-Type: " + (content_type or "application/x-www-form-urlencoded"),
                "-H", "Referer: " + referer, "--data-binary", post]
    for k, v in (extra_headers or {}).items():
        cmd += ["-H", "%s: %s" % (k, v)]
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


def rotate_warp(wait=40):
    """Reconnect the WARP tunnel to land on a different exit IP.

    Needed because grok.com's Cloudflare blocks some WARP /64s outright, and
    the block follows the IP -- retrying on the same exit never clears it.
    `sudo -n` only: a password prompt under `ssh -n` would hang forever and
    look like the capture froze mid-step.  Best effort: a failure here is not
    fatal, the caller just retries on whatever exit it has.
    """
    import subprocess
    for args in (["disconnect"], ["connect"]):
        subprocess.run(["sudo", "-n", "warp-cli", "--accept-tos"] + args,
                       capture_output=True, text=True)
        time.sleep(3)
    for _ in range(max(1, wait // 2)):
        r = subprocess.run(["warp-cli", "--accept-tos", "status"],
                           capture_output=True, text=True)
        if "Connected" in (r.stdout or ""):
            return True
        time.sleep(2)
    log("  ⚠️ WARP did not report Connected after rotating")
    return False


def _cf_block(st, body):
    """403 from Cloudflare's own interstitial = the EGRESS was refused.

    2026-10-18: every WARP rotation on 225 lands on a random IPv6 /64, and some
    of them grok.com's Cloudflare blocks outright.  A/B on one sso: 5 rotations
    gave 403/401/401/401/403, and a request with NO cookie at all got the very
    same 403 page -- so it says nothing about the cookie.  Told apart from a
    real 403 by the body: grok.com answers about a cookie in JSON, Cloudflare
    answers with `<title>Attention Required! | Cloudflare</title>`.
    """
    if st != 403:
        return False
    head = body[:3000].lower()
    return "attention required" in head or "cloudflare" in head


def _get_user_transient(st, body):
    """grok.com's own timeout or a blocked egress, not a verdict on the cookie.

    2026-10-13: get-user answered `499 {"message":"Timeout expired"}` on 4 of 6
    tries from 225 and 2 of 2 from 198 -- and a FAKE sso got the very same
    reply, so it carries no information about the cookie.  Two runs dropped a
    good account as "SSO DEAD" on it.  5xx is treated the same way, and so is
    a Cloudflare 403 interstitial (see _cf_block).
    """
    return (st == 499 or st >= 500 or "Timeout expired" in body[:200]
            or _cf_block(st, body))


def check_sso(sso, attempts=6, pause=10):
    """Prove the cookie is alive BEFORE spending a device code on it.

    grok.com/rest/auth/get-user answers from any egress, so a failure here is
    genuinely the cookie; accounts.x.ai would confound cookie with egress.
    Returns the dict, so the caller can assert the email matches the line it
    came from -- a swapped sso would otherwise onboard the wrong xAI account
    under the right name.

    Only a NON-transient reply (401/403/...) may call the cookie dead; a 499
    timeout is retried, and the trail stays in the error if it never clears.
    A Cloudflare 403 is about the egress, so it is retried too -- but retrying
    on the same WARP exit is pointless (the block follows the IP), so the
    tunnel is rotated first.
    """
    trail = []
    for i in range(max(1, attempts)):
        try:
            st, _, body = _curl("https://grok.com/rest/auth/get-user", sso=sso)
        except SystemExit as e:  # curl itself failed: no status at all
            st, body = 0, str(e)
        if st == 200:
            try:
                return json.loads(body), None
            except Exception:
                return None, "get-user body not json: %s" % body[:120]
        msg = "get-user http=%d %s" % (st, body[:120].replace("\n", " "))
        trail.append(str(st))
        if not (st == 0 or _get_user_transient(st, body)):
            return None, msg  # a real answer about the cookie
        if i + 1 < attempts:
            if _cf_block(st, body):
                log("  get-user http=403 Cloudflare -- the EGRESS is blocked, "
                    "not the cookie; rotating WARP")
                rotate_warp()
            else:
                log("  %s -- grok.com timeout, not the cookie; retry in %ds"
                    % (msg, pause))
                time.sleep(pause)
    return None, "%s [never cleared: %s] -- TRANSIENT, the cookie was not judged" % (
        msg, " -> ".join(trail))


# grok.com's store productId -> the word x.ai's own API uses for the tier.
# Only mapped names may be written: an unmapped productId passed through as if
# it were a tier is what stored acct 157 as `grok.lite` (2026-10-18).
PRODUCT_TO_TIER = {
    "grok.ultra": "supergrok_heavy",
    "grok.premiumplus": "supergrok_plus",
    "grok.premium": "supergrok_plus",
    "grok.pro.monthly.30": "supergrok",     # 10-19 acct 158~161, apple, $30
    "grok.lite": "supergrok_lite",
}

# grok.com's own `dominantPlan.plan` word -> the sub2api tier word.  Checked
# BEFORE the productId map: it is xAI naming the plan, while productId is a
# store SKU that changes per store/promo.  2026-10-19: four accounts sold as
# "heavy" by the vendor answered plan=supergrok, tier=SUBSCRIPTION_TIER_GROK_PRO,
# JWT tier=1 (known heavy = 5) -- while the x.ai 53M/8300 headers and sub2api's
# plan_from_45 both read "heavy" for them, i.e. those two cannot tell tiers.
PLAN_TO_TIER = {
    "supergrok": "supergrok",               # observed 10-19 (158~161)
    # ⚠️ the three below are NOT observed yet (no heavy/plus/lite dominantPlan
    # dumped so far) -- an unseen word falls through to PRODUCT_TO_TIER anyway.
    "supergrok_heavy": "supergrok_heavy",
    "supergrok_plus": "supergrok_plus",
    "supergrok_lite": "supergrok_lite",
}
# JWT `tier` claim on device-code access tokens.  Only the values seen next to a
# confirmed plan are listed; anything else is "unknown", never guessed.
JWT_TIER = {5: "supergrok_heavy", 1: "supergrok"}


def jwt_tier_of(access_token):
    """The `tier` claim of an x.ai access token, or None (SSO-exchanged tokens have none)."""
    import base64
    try:
        p = access_token.split(".")[1].replace("-", "+").replace("_", "/")
        return json.loads(base64.b64decode(p + "=" * (-len(p) % 4))).get("tier")
    except Exception:
        return None


def tier_of(sub, log=None):
    """The x.ai tier word for a /rest/subscriptions entry, or "" if unknown.

    Order: grok.com `dominantPlan.plan` (xAI's own word) > PRODUCT_TO_TIER.
    Returns "" rather than the raw productId for an unmapped product: this
    field is read by humans in the sub2api UI and by scripts that filter on
    it, and a store name sitting in a tier column reads as a tier.  An empty
    value is visibly missing; a wrong one is not.
    ⛔ Not `plan_from_45_responses` / 53M-8300 headers: they read "heavy" for
    SuperGrok accounts too (2026-10-19, acct 158~161).
    """
    # Resolved at call time, not bound as a default: a module-level `log=log`
    # default captures the original function, so a caller (or a test) that
    # replaces the module's `log` would be silently ignored.
    log = log or globals()["log"]
    product = (sub or {}).get("productId") or ""
    plan = (sub or {}).get("plan") or ""
    by_plan = PLAN_TO_TIER.get(plan, "")
    by_product = PRODUCT_TO_TIER.get(product, "")
    if by_plan and by_product and by_plan != by_product:
        log("  ⚠️ dominantPlan %r says %s but productId %r maps to %s -- "
            "using the plan" % (plan, by_plan, product, by_product))
    tier = by_plan or by_product
    if (product or plan) and not tier:
        log("  ⚠️ unknown productId %r / plan %r -- leaving subscription_tier "
            "empty; add it to PRODUCT_TO_TIER/PLAN_TO_TIER" % (product, plan))
    if sub and sub.get("picked", "").startswith("first-of"):
        log("  ⚠️ no ACTIVE subscription among %d entries (%s) -- tier may be "
            "stale; trust the live plan probe" % (sub.get("n_entries"), sub.get("status")))
    return tier


def _sub_product(s):
    """The productId of one subscription entry, whichever store sold it."""
    for k in ("google", "apple", "stripe", "web"):
        if isinstance(s.get(k), dict) and s[k].get("productId"):
            return k, s[k]
    return None, None


def subscription_of(sso):
    """The tier as xAI itself reports it, from grok.com/rest/subscriptions.

    Used instead of the JWT `tier` claim: device-code tokens carry `tier: 5`
    while SSO-exchanged ones carry no such claim at all, so there is no mapping
    table to read it with -- filling one in from the number is inventing data.
    `productId` (grok.ultra / grok.premium...) is xAI's own word for it.

    🔴 2026-10-18: an ACTIVE entry is picked, never just the first one.  An
    account can carry its whole subscription history here: acct 157 listed
    `grok.lite` (expired 10-12), `grok.ultra` (expired 10-12) and only THIRD
    the live `grok.ultra` -- taking subs[0] stored a heavy account as
    `grok.lite`.  Every earlier account had exactly one entry, so the bug sat
    there unseen.  With no ACTIVE entry we fall back to the first one and say
    so in `picked`, rather than inventing a tier.

    🔴 2026-10-18 (second fault, same column): a FAILED probe used to return a
    bare None, the caller turned that into "" and the account went in with an
    EMPTY tier -- acct 155/156 (rebuilt 10-16) carry
    `_probe.subscription: null` in their own create request body, while their
    first incarnations (79/80, same `sub`) had probed `supergrok_heavy` fine.
    An empty column then hid from an audit that compared stored against the
    live probe, because both sides were empty and so "equal".  So a failure is
    now announced and retried, never quietly empty.
    """
    for attempt in range(3):
        st, _, body = _curl("https://grok.com/rest/subscriptions", sso=sso)
        if st == 200:
            break
        log("  ⚠️ /rest/subscriptions http=%d (%d/3) -- tier would be left "
            "EMPTY, retrying" % (st, attempt + 1))
        if _cf_block(st, body):
            rotate_warp()
        else:
            time.sleep(8)
    if st != 200:
        log("  🔴 /rest/subscriptions never answered 200 -- subscription_tier "
            "will be EMPTY; set it from the live plan probe after creating")
        return None
    try:
        doc = json.loads(body)
        subs = doc.get("subscriptions") or []
    except Exception:
        log("  🔴 /rest/subscriptions body not json -- subscription_tier EMPTY")
        return None
    plan = ((doc.get("dominantPlan") or {}).get("plan")) or None
    entries = [(s, _sub_product(s)) for s in subs]
    entries = [(s, k, p) for s, (k, p) in entries if p]
    if not entries:
        return None
    live = [e for e in entries
            if "ACTIVE" in (e[0].get("status") or "")
            and "INACTIVE" not in (e[0].get("status") or "")]
    s, k, p = (live or entries)[0]
    # apple entries have no expiryTime; the period end is top-level billingPeriodEnd
    return {"productId": p["productId"],
            "basePlanId": p.get("basePlanId"),
            "expiryTime": p.get("expiryTime") or s.get("billingPeriodEnd"),
            "tierEnum": s.get("tier"),
            "plan": plan,
            "autoRenew": p.get("autoRenewOn"),
            "offer": ((s.get("activeOffer") or {}).get("providerOfferId")),
            "status": s.get("status"),
            "store": k,
            "picked": "active" if live else "first-of-%d-none-active" % len(entries),
            "n_entries": len(entries)}


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
    if "castle_request_token" in out and not out["castle_request_token"]:
        # Castle anti-bot field, filled only by the page's JS.  Since 10-13 an
        # empty one is what /approve rejects with "could not verify this
        # browser" -- say so here, one hop before the 403 lands.
        log("  ⚠️ consent form has an EMPTY castle_request_token (JS-filled); "
            "pure-HTTP approve will 403 -- use --approve browser")
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


def browser_approve(sso, verify_url, shots_dir=None):
    """Steps 2-4 in a real browser, logged in by the sso cookie (2026-10-13).

    Why: the consent form grew an empty `castle_request_token` that only the
    page's JS fills; pure-HTTP /approve then gets 403 "We could not verify
    this browser" -- the csrf-titled 403, but the jar is complete, so the
    "thread the jar" advice does not apply.  patchright through the same WARP
    socks5 with sso/sso-rw planted on .x.ai: Continue -> Allow -> /device/done.
    Needs DISPLAY (Xvfb) and the 225 pyenv:
      PYTHONPATH=/Data/grok-onboard/pyenv/local/lib/python3.14/dist-packages
      PLAYWRIGHT_BROWSERS_PATH=/Data/grok-onboard/browsers

    🔴 2026-10-13 evening law, paid for with ~8 failed approves: DO NOT wait
    for castle_request_token before clicking.  Both successes (acct 75 14:32,
    acct 76 23:39) clicked Allow the moment the button appeared (~4-8s, the
    field still empty) and landed on /device/done; every run that idled 40s
    waiting for the field to fill got the 403.  An idle-then-click pattern
    reads as a bot; the Castle token evidently rides the submit, not the
    clock.  Same reason there is NO user_agent override here -- both wins ran
    patchright's native UA.  (Not fully isolated: WARP's egress IP also
    rotated between the failures and the win; the A/B was not run.  If a
    future approve 403s anyway, re-rotate WARP first, but do NOT reintroduce
    the wait.)
    Returns 200 once Allow was clicked and the page did not say "could not
    verify"; poll_token() right after is what proves the approve registered.
    """
    from patchright.sync_api import sync_playwright

    with sync_playwright() as p:
        br = p.chromium.launch(headless=False, proxy={"server": PROXY},
                               args=["--no-sandbox", "--disable-dev-shm-usage",
                                     "--use-gl=angle", "--use-angle=swiftshader"])
        try:
            ctx = br.new_context(locale="en-US",
                                 viewport={"width": 1400, "height": 950})
            ctx.add_cookies([{"name": n, "value": sso, "domain": ".x.ai",
                              "path": "/", "secure": True, "httpOnly": True,
                              "sameSite": "Lax"} for n in ("sso", "sso-rw")])
            pg = ctx.new_page()
            pg.goto(verify_url, wait_until="domcontentloaded", timeout=60000)
            for step in range(6):
                time.sleep(4)
                url = pg.url.split("?")[0]
                if shots_dir:
                    pg.screenshot(path=os.path.join(shots_dir, "approve-%d.png" % step))
                log("  [browser] %s" % url)
                allow = pg.query_selector("button[value='allow']")
                if allow:
                    allow.click()
                    log("  [browser] -> clicked Allow")
                    time.sleep(5)
                    if shots_dir:
                        pg.screenshot(path=os.path.join(shots_dir, "after-allow.png"))
                    after = ""
                    try:
                        after = pg.inner_text("body")[:200].replace("\n", " ")
                    except Exception:
                        pass
                    log("  [browser] after: %s | %s"
                        % (pg.url.split("?")[0], after))
                    if "could not verify" in after.lower():
                        # one() catches the word "castle" and switches to the
                        # authorization-code grant.  Rotating WARP does not fix
                        # this (10-22: positive control passed on the same egress).
                        raise SystemExit("browser approve rejected (castle) "
                                         "-- do NOT add a castle wait")
                    return 200
                for label in ("Continue", "Confirm", "Next"):
                    btn = pg.query_selector("button:has-text('%s')" % label)
                    if btn:
                        btn.click()
                        log("  [browser] -> clicked %s" % label)
                        break
                else:
                    body_txt = ""
                    try:
                        body_txt = pg.inner_text("body")[:160].replace("\n", " ")
                    except Exception:
                        pass
                    log("  [browser] no button: %s" % body_txt)
            raise SystemExit("browser approve never saw the Allow button "
                             "(last url %s)" % pg.url.split("?")[0])
        finally:
            br.close()


AUTHCODE_REDIRECT = "http://127.0.0.1:56121/callback"


def authcode_grant(sso, shots_dir=None):
    """Authorization Code + PKCE through a real browser -- bypasses Castle (10-22).

    Why it exists: `device/approve` needs a `castle_request_token` that only the
    page's JS mints, and Castle scores a datacenter egress + automated browser
    low enough that first-time grants get 403 "Request could not be verified"
    (xsohltwqbz 10-21: five runs, plus two human clicks, all 403).
    `/oauth2/authorize` lands on a DIFFERENT consent page (`/oauth2/consent`,
    "Authorize Grok Build") whose Allow button is not Castle-gated.  Clicking it
    redirects to the loopback redirect_uri with `?code=`; we read the code off
    that navigation and swap it for tokens with the PKCE verifier.
    Upstream: grok2api #1078 (diagnosis) / #1082 (same idea, protocol-only,
    unmerged as of 10-22).

    🔴 Must be the browser, not curl: `/oauth2/authorize` with only the sso
    cookie bounces to /sign-in -- it also wants `cf_clearance`, which only a
    browser gets, so the account page is visited first to seed it.
    🔴 Nothing listens on 127.0.0.1:56121 -- the navigation to it FAILS, and that
    is expected; the code is read from the request URL, not from a response.
    Returns the token dict (same shape as poll_token()).
    """
    import base64
    import hashlib
    import secrets
    import urllib.parse
    from patchright.sync_api import sync_playwright

    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    verifier = b64(secrets.token_bytes(32))
    state = secrets.token_hex(16)
    qs = _form({
        "client_id": CLIENT_ID, "redirect_uri": AUTHCODE_REDIRECT,
        "response_type": "code", "scope": SCOPE, "state": state,
        "nonce": secrets.token_hex(16),
        "code_challenge": b64(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256", "referrer": "grok-build",
    })
    got = {}
    base = AUTHCODE_REDIRECT.rsplit("/", 1)[0]
    with sync_playwright() as p:
        br = p.chromium.launch(headless=False, proxy={"server": PROXY},
                               args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = br.new_context(locale="en-US", viewport={"width": 1400, "height": 950})
            ctx.add_cookies([{"name": n, "value": sso, "domain": ".x.ai",
                              "path": "/", "secure": True, "httpOnly": True,
                              "sameSite": "Lax"} for n in ("sso", "sso-rw")])
            pg = ctx.new_page()

            def on_req(r):
                if r.url.startswith(base):
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(r.url).query)
                    if q.get("code"):
                        got["code"] = q["code"][0]
                        got["state"] = (q.get("state") or [""])[0]
            pg.on("request", on_req)

            pg.goto("https://accounts.x.ai/account", wait_until="domcontentloaded",
                    timeout=60000)
            time.sleep(4)
            pg.goto("https://auth.x.ai/oauth2/authorize?" + qs,
                    wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
            log("  [authcode] %s" % pg.url.split("?")[0])
            if "/sign-in" in pg.url:
                raise SystemExit("authcode: bounced to /sign-in even in the browser "
                                 "-- the sso is not accepted on accounts.x.ai")
            if shots_dir:
                pg.screenshot(path=os.path.join(shots_dir, "authcode-consent.png"))
            allow = (pg.query_selector("button[value='allow']")
                     or pg.query_selector("button:has-text('Allow')"))
            if not allow:
                body_txt = ""
                try:
                    body_txt = pg.inner_text("body")[:200].replace("\n", " | ")
                except Exception:
                    pass
                raise SystemExit("authcode: no Allow button on %s (%s)"
                                 % (pg.url.split("?")[0], body_txt))
            allow.click()
            log("  [authcode] -> clicked Allow")
            for _ in range(15):
                time.sleep(2)
                if got.get("code"):
                    break
            if shots_dir:
                pg.screenshot(path=os.path.join(shots_dir, "authcode-after.png"))
        finally:
            br.close()

    if not got.get("code"):
        raise SystemExit("authcode: Allow clicked but no ?code= ever reached %s" % base)
    if got.get("state") != state:
        raise SystemExit("authcode: state mismatch -- refusing the code")
    log("  [authcode] code %d chars, state ok" % len(got["code"]))
    st, _, body = _curl("https://auth.x.ai/oauth2/token", post=_form({
        "grant_type": "authorization_code", "code": got["code"],
        "redirect_uri": AUTHCODE_REDIRECT, "client_id": CLIENT_ID,
        "code_verifier": verifier}))
    try:
        j = json.loads(body)
    except Exception:
        j = {}
    if st != 200 or "access_token" not in j:
        raise SystemExit("authcode token exchange http=%d %s" % (st, body[:200]))
    return j


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


def one(email, sso, out_path, expect_userid=None, approve_mode="browser",
        shots_dir=None):
    log("=== %s ===" % email)
    u, err = check_sso(sso)
    if err:
        # "TRANSIENT" = grok.com never answered; that is NOT a dead cookie and
        # must not be reported as one (10-13: two runs dropped acct 75 so).
        log("  %s: %s" % ("SSO UNCHECKED (grok.com timeout, re-run)"
                          if "TRANSIENT" in err else "SSO DEAD", err))
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
    tok = None
    if approve_mode == "authcode":
        tok = authcode_grant(sso, shots_dir)
    else:
        jar = tempfile.mkstemp(prefix=".xai-jar-")[1]
        os.chmod(jar, 0o600)
        try:
            d = device_code(jar)
            log("  user_code=%s" % d["user_code"])
            if approve_mode == "browser":
                try:
                    st = browser_approve(sso, d["verification_uri_complete"], shots_dir)
                except SystemExit as e:
                    # 10-22: the castle 403 is not fixed by retrying or by
                    # rotating WARP (positive control on the same egress
                    # passes) -- switch grant type instead of looping.
                    if "castle" not in str(e):
                        raise
                    log("  device approve hit Castle (%s) -> falling back to "
                        "authorization-code grant" % str(e)[:60])
                    tok = authcode_grant(sso, shots_dir)
                    st = 200
            else:
                landing(sso, d["user_code"], jar)
                consent_url = verify_code(sso, d["user_code"], jar)
                fields = consent_token(sso, consent_url, jar)
                log("  consent_token len=%d principal=%s/%s"
                    % (len(fields["consent_token"]), fields.get("principal_type"),
                       fields.get("principal_id") or "-"))
                st = approve(sso, fields, consent_url, jar)
            log("  approve (%s) http=%d" % (approve_mode, st))
        finally:
            try:
                os.unlink(jar)
            except OSError:
                pass
    if tok is None:
        tok = poll_token(d["device_code"], int(d.get("interval", 5)))
    at, rt = tok.get("access_token", ""), tok.get("refresh_token", "")
    log("  token ok  access=%s…(%d)  refresh=%s…(%d)  expires_in=%s"
        % (at[:12], len(at), rt[:8], len(rt), tok.get("expires_in")))

    verdict = xai_models_verdict(at, log=log)
    log("  x.ai verdict: %s" % verdict)

    tier = tier_of(sub, log=log)
    jt = jwt_tier_of(at)
    log("  TIER %s  (plan=%s enum=%s product=%s jwt_tier=%s->%s)  expires=%s autoRenew=%s"
        % (tier or "(EMPTY)", (sub or {}).get("plan"), (sub or {}).get("tierEnum"),
           (sub or {}).get("productId"), jt, JWT_TIER.get(jt, "?"),
           (sub or {}).get("expiryTime"), (sub or {}).get("autoRenew")))
    if tier and jt in JWT_TIER and JWT_TIER[jt] != tier:
        log("  🔴 TIER MISMATCH: subscription says %s, JWT tier=%s says %s -- "
            "check before telling anyone it is heavy" % (tier, jt, JWT_TIER[jt]))
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
            "subscription_tier": tier,
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
    ap.add_argument("--approve", choices=("browser", "authcode", "http"), default="browser",
                    help="browser (default since 10-13; falls back to authcode on "
                         "a Castle 403 since 10-22), authcode (skip the device "
                         "flow entirely), or http (the 09-28 pure-HTTP path, "
                         "kept for diagnosis)")
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
        os.makedirs(os.path.join(a.out_dir, "shots-" + tag), mode=0o700, exist_ok=True)
        try:
            good = one(c["name"], c["sso"], out, c.get("userid"),
                       approve_mode=a.approve,
                       shots_dir=os.path.join(a.out_dir, "shots-" + tag))
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
