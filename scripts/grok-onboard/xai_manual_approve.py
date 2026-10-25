#!/usr/bin/env python3
"""xai_manual_approve.py — 人工点一次 Allow，把 device flow 走完（Castle 拦住自动点时的出路）。

自动 approve 被 Castle 拦（`device/approve` 403 `Request could not be verified`）时用这条：
协议层照常拿 device code、打印用户码，**人在自己电脑的真实浏览器里点 Allow**，
脚本在 225 的 WARP 出口上轮询 token，拿到就写 bundle（格式与
`xai_sso_device_oauth.py` 完全一致，可直接喂 `create-grok-oauth-account.py`）。

🔴 **为什么必须人工**：`castle_request_token`（6k 字符）只有页面 JS 能生成，
而 Castle 给数据中心出口 + 自动化浏览器打的分过不了**首次授权**那一关
（复授权能过 —— acct 163 的阳性对照两次都一发过，它 10-13 已授权过 Grok Build）。
上游同病 grok2api #1078；绕法是换 Authorization Code + Next-Action 流（#1082，未合并）。

⚠️ **人点的浏览器出口 ≠ 轮询的出口**，这是故意的：approve 只要人过 Castle，
token 轮询走哪条出口都行。实测 163 在 225 出口上 approve 也能过，所以不必强求同出口。

用法（两段，中间等人）：
    # 1 在 198 上起：打印 user_code + 授权链接，然后阻塞轮询
    python3 xai_manual_approve.py --email <mail> --sso-file <0600> --out bundle.json
    # 2 人在浏览器里打开链接 → 登录（或已登录）→ 点 Continue → 点 Allow
    # 3 脚本自己收到 token，写 bundle，退出 0

设备码默认 10 分钟过期（`expires_in`），脚本按它算截止时间并打出北京时间。
⛔ 不打印 sso / token 明文。
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import xai_sso_device_oauth as x  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--sso-file", required=True, help="0600 file holding the sso cookie")
    ap.add_argument("--out", required=True, help="bundle path (0600)")
    ap.add_argument("--budget", type=int, default=0,
                    help="seconds to wait for the human; default = the code's own expires_in")
    a = ap.parse_args()

    sso = open(a.sso_file, encoding="utf-8").read().strip()

    u, err = x.check_sso(sso)
    if err:
        sys.exit("sso not usable: %s" % err)
    if (u.get("email") or "").lower() != a.email.lower():
        sys.exit("REFUSING: this sso belongs to %s, not %s" % (u.get("email"), a.email))
    uid = u.get("userId")
    sub = x.subscription_of(sso)
    tier = x.tier_of(sub) if hasattr(x, "tier_of") else None
    print("sso ok  userId=%s  tier=%s  expiry=%s"
          % (uid, tier, (sub or {}).get("expiryTime") or (sub or {}).get("billingPeriodEnd")),
          flush=True)

    dc = x.device_code()
    code, interval = dc["user_code"], int(dc.get("interval", 5))
    budget = a.budget or int(dc.get("expires_in", 600))
    deadline = time.time() + budget
    print("\n" + "=" * 62, flush=True)
    print("  在你自己电脑的浏览器里打开：", flush=True)
    print("    https://accounts.x.ai/oauth2/device?user_code=%s" % code, flush=True)
    print("  用户码（页面若要求手填）：  %s" % code, flush=True)
    print("  账号：%s" % a.email, flush=True)
    print("  步骤：登录（如未登录）→ 点 Continue → 点 Allow", flush=True)
    print("  截止：%s 北京时间（%d 秒后过期，过期就重跑本脚本换新码）"
          % (time.strftime("%H:%M:%S", time.localtime(deadline)), budget), flush=True)
    print("=" * 62 + "\n", flush=True)

    print("waiting for the approve ... (polling every %ds)" % interval, flush=True)
    tok = x.poll_token(dc["device_code"], interval, budget_s=budget)
    at, rt = tok["access_token"], tok.get("refresh_token", "")
    print("  token ok  access=%d chars  refresh=%d chars  expires_in=%s"
          % (len(at), len(rt), tok.get("expires_in")), flush=True)

    # 🔴 xai_models_verdict 返回的是**一个字符串**（带重试轨迹），不是 (verdict, ok)
    # 二元组 —— 那是内部 `_xai_models_once` 的形状。判活看字符串是否以 200 开头。
    verdict = x.xai_models_verdict(at, log=lambda *m: print("   ", *m, flush=True))
    ok = verdict.strip().startswith("200")
    print("  x.ai verdict: %s" % verdict, flush=True)
    if not ok:
        sys.exit("x.ai refuses this fresh token (%s) -- do NOT onboard it as capacity" % verdict)

    bundle = {
        "name": a.email, "platform": "grok", "type": "oauth", "status": "active",
        "concurrency": 200, "priority": 1, "rate_multiplier": 1,
        "auto_pause_on_expired": True, "group_ids": [x.GROUP_ID],
        "credentials": {
            "access_token": at, "refresh_token": rt,
            "token_type": tok.get("token_type", "Bearer"),
            "scope": tok.get("scope", x.SCOPE),
            "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
            "client_id": x.CLIENT_ID, "base_url": "https://api.x.ai/v1",
            "sub": uid, "subscription_tier": tier or "",
        },
        "_probe": {"subscription": sub, "xai_models": verdict, "approve": "manual-human"},
    }
    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(bundle, fh, ensure_ascii=False)
    print("\nOK %s -> %s (0600)" % (a.email, a.out), flush=True)
    print("next: python3 create-grok-oauth-account.py %s" % a.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
