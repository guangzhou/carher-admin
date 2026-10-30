#!/usr/bin/env python3
"""muse cookie 过期看门狗：每小时跑一次，续不上就报警（不自动重登 —— 重登要卡密）。

为什么需要（10-30 查清）：
  - cookie 只有约 48h。服务**自己**每 15 分钟会 `/api/session` 续签，所以服务不停就不该过期；
  - 但续签失败是**静默**的：`_probe_account_sync` 里 `MuseAuthError` 只写进 note，
    `ok=False` 的号会被 harden #5 跳过 ⇒ 池子悄悄变小，没有任何人收到通知；
  - 198 上也没有任何 cron / systemd timer / k8s CronJob 碰 muse 凭据（10-30 逐项查过）。

它做三件事（都不动号，只读 + 推一下）：
  1. 读每个启用号的 `expires_at`，<THRESH 小时的列成「快过期」；
  2. 对快过期或 `ok != True` 的号，调 `/admin/accounts/<id>/keepalive` **主动踢一次续签**
     （这是上游自己的接口，走该号自己的出口，不开浏览器、不占生成槽）；
  3. 踢完再读一遍：还没拉起来的，发飞书告警，说清哪个号、剩几小时、要怎么救
     （`add_batch.sh` 同一行卡密重跑，`login_http.py --relogin` 保留 id/用量）。

    python3 cookie_watch.py              # 只看 + 踢续签，有问题 exit 1
    python3 cookie_watch.py --notify     # 还不行就发飞书告警（cron 用这个）
    python3 cookie_watch.py --thresh 18  # 改「快过期」的门槛（默认 24h）

🔴 不打印 cookie；号池 key 从 Secret 现读，只在内存里。
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

POOL = "http://127.0.0.1:30610"
NS = "muse2api"


def sh(argv, timeout=90):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return r.stdout


def pool_key():
    raw = sh(["k3s", "kubectl", "-n", NS, "get", "secret", "muse2api-env",
              "-o", "jsonpath={.data.MUSE2API_KEY}"]).strip()
    return base64.b64decode(raw).decode().strip()


def api(key, method, path, timeout=300):
    req = urllib.request.Request(POOL + path, method=method,
                                 headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}") | {"http": e.code}
        except ValueError:
            return {"http": e.code}
    except Exception as e:
        return {"err": "%s: %s" % (type(e).__name__, e)}


SENDER = "/opt/chatgpt-acct-console/feishu_send_message.py"
# 凭据从现成的 Secret 现读（feishu-broadcast-cred，和飞书广播那套同一个 app），不写进本脚本
CRED_SECRET = ("feishu-broadcast", "feishu-broadcast-cred")
NOTIFY_TO = "liuguoxian@auto-link.com.cn"


def lark(text, to=NOTIFY_TO):
    """复用仓里现成的 scripts/feishu_send_message.py（skill feishu-bot-send-message）。
    凭据只经环境变量传给子进程，不进命令行、不打印。"""
    import os
    ns, name = CRED_SECRET
    env = dict(os.environ)
    for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET"):
        raw = sh(["k3s", "kubectl", "-n", ns, "get", "secret", name,
                  "-o", "jsonpath={.data.%s}" % k]).strip()
        if not raw:
            return False, "Secret %s/%s 里没有 %s" % (ns, name, k)
        env[k] = base64.b64decode(raw).decode().strip()
    try:
        r = subprocess.run(["python3", SENDER, "--to", to, "--text", "-"],
                           input=text, env=env, capture_output=True, text=True, timeout=90)
    except OSError as e:
        return False, "跑不起来 %s：%s" % (SENDER, e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-200:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresh", type=float, default=24.0, help="剩余小时数低于它就算快过期")
    ap.add_argument("--notify", action="store_true", help="救不回来时发飞书")
    a = ap.parse_args()

    key = pool_key()
    d = api(key, "GET", "/admin/accounts")
    accs = d.get("accounts", d if isinstance(d, list) else [])
    if not accs:
        print("STOP 读不到号池：%s" % json.dumps(d, ensure_ascii=False)[:200])
        return 2
    now = time.time()
    live = [x for x in accs if x.get("enabled")]
    need = [x for x in live
            if ((x.get("expires_at") or 0) - now) / 3600 < a.thresh or x.get("ok") is not True]
    print("启用号 %d 个；要踢续签的 %d 个（门槛 %.0fh）" % (len(live), len(need), a.thresh))

    for x in need:
        left = ((x.get("expires_at") or 0) - now) / 3600
        r = api(key, "POST", "/admin/accounts/%s/keepalive" % x["id"])
        print("  踢 %-18s 剩 %5.1fh ok=%s ⇒ 续签 ok=%s %s" % (
            x["label"], left, x.get("ok"), r.get("ok"),
            (r.get("error") or "")[:80]))

    # 踢完回读：判据是被操作对象自身的状态，不是接口回了 200
    d2 = api(key, "GET", "/admin/accounts")
    accs2 = d2.get("accounts", d2 if isinstance(d2, list) else [])
    now = time.time()
    bad = []
    for x in accs2:
        if not x.get("enabled"):
            continue
        left = ((x.get("expires_at") or 0) - now) / 3600
        if left < a.thresh or x.get("ok") is not True:
            bad.append("%s 剩 %.1fh ok=%s（%s）" % (x["label"], left, x.get("ok"),
                                                   (x.get("note") or "")[:60]))
    if not bad:
        print("OK   所有启用号 cookie 都 > %.0fh 且 ok=True" % a.thresh)
        return 0

    msg = ("🔴 muse 号池 cookie 要过期了，自动续签没救回来（%d 个）：\n" % len(bad)
           + "\n".join("· " + b for b in bad)
           + "\n\n救法：用同一行卡密重跑 `bash muse2api/add_batch.sh <卡密文件>`"
             "（login_http.py --relogin 会保留 id 和用量，出口按台账复用同一个 IP）。")
    print(msg)
    if a.notify:
        ok, where = lark(msg)
        print("飞书告警：%s（%s）" % ("已发" if ok else "没发出去", where))
    return 1


if __name__ == "__main__":
    sys.exit(main())
