#!/usr/bin/env python3
"""把 Antigravity(Google AI Pro)账号批量加进 **sub2api**。

⚠️ **加号是两条独立的路，两条都要走一遍。** 2026-09-09 加三个新号时我只走了第一条，
用户第二天问「为什么我在 sub2api 上看不到」才发现：

    路① cli-proxy-api  ← 生产路，LiteLLM 的 12 条 ag-*/claude-ag-* entry 挂在它上面
        脚本 scripts/cliproxy-antigravity-add-account.py（凭据写进 k8s Secret）
    路② sub2api        ← 本脚本（凭据写进 sub2api 的 Postgres accounts 表）

本脚本**从路①的 Secret 里读凭据**再灌进路②，所以顺序固定：先 cliproxy 后 sub2api。
反过来不行 —— Secret 是凭据的源头。

在 198 上跑（要 sudo kubectl）：

    sudo python3 sub2api-antigravity-add-account.py list            # 只读：两边对账，看差哪几个号
    sudo python3 sub2api-antigravity-add-account.py apply           # 默认 dry-run
    sudo python3 sub2api-antigravity-add-account.py apply --yes     # 真写（自动备份 accounts 表）
    sudo python3 sub2api-antigravity-add-account.py verify          # 逐号真流量判活
    sudo python3 sub2api-antigravity-add-account.py rollback --ids 21,22,23 --yes

设计纪律（每一条都是踩出来的，别简化掉）：

  * **查重按 credentials->>'email'，不按账号名。** 名字是人取的会漂，email 才是身份。
  * **创建响应里的 `group_ids` 是 None，但组其实绑上了。** 判据只认建完再 GET 一次。
  * **建完必须紧跟一刀 batch-refresh。** Secret 里的 access_token 是陈旧快照，
    本脚本故意把 `expires_at` 写成过期值逼它换新；不刷的话 sub2api 的
    `extra.privacy_mode` 会停在 **`privacy_set_failed`**，刷完才变 `privacy_set`。
  * **batch-refresh 的参数名是 `account_ids`，不是 `ids`。** 传 `ids` 返 400。
  * **verify 判逐条腿只能看 `usage_logs.account_id`**，池子级 nonce 全绿是假绿。
  * **切口用 `max(usage_logs.id)` 水位线，不许用时间窗。** 时间窗会把上一轮 verify
    的请求算成本轮的，把 0 发的腿显示成有单 —— 尺子在自证。
  * **但单轮样本会出假红**：sub2api 按「谁用得少先给谁」补，2026-09-10 实测第一轮
    12 发有两个号一发没接、第二轮 12 发全落到那两个号上。所以 verify 默认发
    4×号数 —— 别拿单轮的 0 发去判一条腿死了。

背景 / 三层判据 / 路①的坑：skill `cliproxy-antigravity-ops`。
sub2api 本身的运维（grok/并发闸/admin API）：skill `sub2api-grok-ops`。
"""
import argparse
import base64
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

NS = "litellm-dev"
KUBECFG = "/etc/rancher/k3s/k3s.yaml"

# 路①：凭据的源头
CLIPROXY_SECRET = "cliproxy-secrets"

# 路②：sub2api
S2A = os.environ.get("S2A", "http://127.0.0.1:31880")
S2A_SECRET = "sub2api-secrets"
S2A_ADMIN_EMAIL = os.environ.get("S2A_EMAIL", "admin@sub2api.local")
S2A_PG_LABEL = "app=sub2api-postgres"
S2A_DB = ("-U", "sub2api", "-d", "sub2api")

# 2026-09-10 现状：三个老号建在这个组里，新号照抄。要换组用 --group。
DEFAULT_GROUP_ID = 10
BACKUP_DIR = "/root/cliproxy-manifests"

# verify 用的模型。选 gemini-2.5-flash 是因为历史上这把 probe key 打通过它；
# ⚠️ 所有 gemini-* 共享**同一个**配额池（2026-09-10 从 fetchAvailableModels 实测），
# 换个 gemini 名字换不来额度，但 24 发小请求对池子可以忽略。
VERIFY_MODEL = "gemini-2.5-flash"
# 推理模型会先烧一截 thinking token 才吐正文，给小了会把好腿判成红。别往下调。
VERIFY_MAX_TOKENS = 512


# --------------------------------------------------------------- 基础设施
def kubectl_ns(ns, *args, check=True):
    cmd = ["kubectl", "--kubeconfig", KUBECFG, "-n", ns] + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit("kubectl failed: %s\n%s" % (" ".join(cmd), p.stderr))
    return p.stdout


def kubectl(*args, check=True):
    return kubectl_ns(NS, *args, check=check)


def secret_value(secret, key):
    """读 k8s Secret 里的一个 key。jsonpath 里的点要转义。"""
    out = kubectl("get", "secret", secret, "-o",
                  "jsonpath={.data['%s']}" % key.replace(".", "\\."))
    if not out.strip():
        sys.exit("Secret %s/%s 里没有 key %s" % (NS, secret, key))
    return base64.b64decode(out)


def pg_pod():
    """⚠️ 必须带 field-selector：不带的话会选中 Succeeded 的 pod，
    然后报 `cannot exec into a container in a completed pod`。"""
    out = kubectl("get", "pod", "-l", S2A_PG_LABEL,
                  "--field-selector=status.phase=Running",
                  "-o", "jsonpath={.items[0].metadata.name}").strip()
    if not out:
        sys.exit("找不到 Running 的 sub2api-postgres pod")
    return out


def psql(sql, pod=None):
    """跑一条只读 SQL 拿 `-At` 输出。

    ⚠️ 这里的 SQL 一律**不许带双引号标识符** —— psql -c 会把双引号当标识符，
    需要双引号的（比如 LiteLLM 那些表名）必须落成 .sql 文件再 kubectl cp。
    sub2api 的表名全是小写，用不着。
    """
    assert '"' not in sql, "带双引号的 SQL 不能走 -c，见 docstring"
    return kubectl("exec", pod or pg_pod(), "--", "psql", *S2A_DB, "-At", "-c", sql)


def http_json(url, body=None, headers=None, timeout=120, method=None):
    data = json.dumps(body).encode() if body is not None else None
    hdr = dict(headers or {})
    if data is not None:
        hdr.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}


_TOK = None


def s2a(method, path, body=None):
    """打 sub2api admin API（和 scripts/grok-onboard/sub2api_admin.py 同一套认证）。"""
    global _TOK
    if _TOK is None:
        pw = secret_value(S2A_SECRET, "ADMIN_PASSWORD").decode().strip()
        st, j = http_json(S2A + "/api/v1/auth/login",
                          {"email": S2A_ADMIN_EMAIL, "password": pw})
        if st != 200:
            sys.exit("sub2api 登录失败 (%s): %s" % (st, json.dumps(j)[:300]))
        _TOK = j["data"]["access_token"]
    return http_json(S2A + path, body, {"Authorization": "Bearer " + _TOK}, method=method)


def nonce():
    return "NONCE-%d" % random.randint(10 ** 8, 10 ** 9)


# ------------------------------------------------------------- 两边的清单
def cliproxy_creds():
    """路①的凭据（运行时真相）。返回 {email: cred_dict}。"""
    cur = json.loads(kubectl("get", "secret", CLIPROXY_SECRET, "-o", "json"))
    out = {}
    for k, v in sorted(cur.get("data", {}).items()):
        if not k.startswith("antigravity-"):
            continue
        try:
            c = json.loads(base64.b64decode(v))
        except Exception as e:
            print("  ⚠️ Secret key %s 解不开: %s" % (k, e))
            continue
        # ⚠️ 老号的 key 名是遗留的 `antigravity-auth.json`，和 slug 规则对不上。
        # 查重必须按 JSON 里的 email，不能按 key 名（按 key 名会把同一个号装两份）。
        email = c.get("email")
        if not email:
            print("  ⚠️ Secret key %s 里没有 email 字段，跳过" % k)
            continue
        c["_secret_key"] = k
        out[email] = c
    return out


def s2a_accounts():
    """sub2api 里的 antigravity 账号。返回 [account_dict]（tokens 已被 API 剥掉）。"""
    st, j = s2a("GET", "/api/v1/admin/accounts?page=1&page_size=200")
    if st != 200:
        sys.exit("拉 sub2api 账号失败 (%s): %s" % (st, json.dumps(j)[:300]))
    d = j.get("data", {})
    items = d if isinstance(d, list) else (d.get("items") or d.get("accounts") or d.get("list") or [])
    return [a for a in items if a.get("platform") == "antigravity"]


def account_email(a):
    return (a.get("credentials") or {}).get("email") or ""


# ------------------------------------------------------------------- list
def cmd_list(args):
    """两边对账。只读，一个上游请求都不发，不花任何额度。"""
    creds = cliproxy_creds()
    accts = s2a_accounts()
    by_email = {}
    for a in accts:
        by_email.setdefault(account_email(a), []).append(a)

    print("路① cli-proxy-api Secret %s/%s：%d 个凭据" % (NS, CLIPROXY_SECRET, len(creds)))
    print("路② sub2api accounts(platform=antigravity)：%d 个账号\n" % len(accts))

    print("%-34s %-5s %-22s %-6s %-8s %-9s %s" %
          ("email", "路①", "路② name", "id", "组", "状态", "备注"))
    for email in sorted(set(creds) | set(by_email)):
        c = creds.get(email)
        rows = by_email.get(email, [])
        ua = (c or {}).get("user_agent") or ""
        note = []
        if c and not c.get("refresh_token"):
            note.append("⚠️ 路①没有 refresh_token")
        if c and c.get("disabled"):
            note.append("⚠️ 路① disabled=true")
        if ua:
            note.append("钉 UA " + ua_version(ua))
        if len(rows) > 1:
            note.append("🔴 sub2api 里有 %d 份重复！" % len(rows))
        if not rows:
            print("%-34s %-5s %-22s %-6s %-8s %-9s %s" %
                  (email, "✅" if c else "—", "（缺）", "-", "-", "-",
                   " / ".join(note) or "← apply 会加这个"))
            continue
        for a in rows:
            e = a.get("extra") or {}
            n = list(note)
            if e.get("privacy_mode") and e["privacy_mode"] != "privacy_set":
                n.append("⚠️ privacy_mode=%s（补一刀 refresh）" % e["privacy_mode"])
            if a.get("error_message"):
                n.append("err: " + a["error_message"].split("|")[0].strip()[:48])
            print("%-34s %-5s %-22s %-6s %-8s %-9s %s" %
                  (email, "✅" if c else "🔴 路①没有", a.get("name"), a.get("id"),
                   a.get("group_ids"), a.get("status"), " / ".join(n)))

    missing = sorted(set(creds) - set(by_email))
    extra = sorted(set(by_email) - set(creds))
    print()
    if missing:
        print("sub2api 缺 %d 个：%s" % (len(missing), ", ".join(missing)))
    if extra:
        print("🔴 sub2api 有但路①没有 %d 个（凭据源头不在了，刷不了 token）：%s"
              % (len(extra), ", ".join(extra)))
    if not missing and not extra:
        print("两边一致。")
    print("\n提醒：路① Secret 是凭据源头；sub2api 里那份是拷贝，两边各自刷 token。"
          "\n      换号 / 撤号要**两边都动**，只动一边会留下一条查不出来的僵尸腿。")

    # sub2api 侧真正管 UA 的是这个**全局**设置，不是账号 extra 里那个同名死字段。
    st, j = s2a("GET", "/api/v1/admin/settings")
    d = j.get("data") or {}
    if isinstance(d, dict) and "antigravity_user_agent_version" in d:
        print("      sub2api 全局 antigravity_user_agent_version = %r"
              "（对这台上所有 antigravity 号一起生效；账号 extra 里那个同名字段是死的）"
              % d["antigravity_user_agent_version"])


def ua_version(ua):
    """`antigravity/1.0.0 windows/amd64` → `1.0.0`。sub2api 只存版本号，不存整条 UA。"""
    m = re.search(r"antigravity/(?:hub/)?([0-9][0-9.]*)", ua or "")
    return m.group(1) if m else ""


# ------------------------------------------------------------------ apply
def build_body(email, c, group_id):
    """照抄现有账号的形状（2026-09-10 从 id 15/16/17 逐字段读出来的）。"""
    cred = {
        "email": email,
        "project_id": c.get("project_id") or "aicode-consumers",
        "plan_type": "Pro",
        "token_type": "Bearer",
        "access_token": c["access_token"],
        "refresh_token": c["refresh_token"],
        # ⚠️ 故意写成过期：Secret 里的 access_token 是陈旧快照，
        # 写过期值 → sub2api 首次使用/刷新时就拿 refresh_token 换新的。
        "expires_at": "1000000000",
    }
    extra = {"privacy_mode": "privacy_set"}   # sub2api 会自己覆写，送它只是对齐现有形状
    v = ua_version(c.get("user_agent"))
    if v:
        # ⚠️ **这个字段是死的，写了不生效**（2026-09-09 实测：只写它仍 403，
        # 改全局设置才通）。真正管用的是 sub2api 的全局设置
        # `GET/PUT /api/v1/admin/settings` → `antigravity_user_agent_version`，
        # 09-09 起已经是 "1.0.0"，**对这台上所有 antigravity 账号一起生效**。
        # 这里仍然写，只是为了和现有号 15/16 的形状对齐、以及留个人可读的标记。
        # ⇒ 将来要加一个「已过验证、想用新 UA 吃 33 模型目录」的号，
        #    在 sub2api 上做不到（全局设置只有一个值）。
        extra["antigravity_user_agent_version"] = v
    return {
        "name": "ag-" + email.split("@")[0],
        "platform": "antigravity",
        "type": "oauth",
        "credentials": cred,
        "extra": extra,
        "group_ids": [group_id],
        "concurrency": 0,
        "priority": 0,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
        "status": "active",
    }


def backup_accounts():
    ts = time.strftime("%Y%m%dT%H%M%S")
    path = os.path.join(BACKUP_DIR, "sub2api-accounts-backup-%s.json" % ts)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    sql = ("select json_agg(row_to_json(t)) from (select a.*, "
           "(select json_agg(row_to_json(g)) from account_groups g where g.account_id=a.id) "
           "as ag from accounts a) t;")
    with open(path, "w") as f:
        f.write(psql(sql))
    os.chmod(path, 0o600)
    try:
        n = len(json.load(open(path)) or [])
    except Exception:
        sys.exit("备份写出来解不开，中止：%s" % path)
    print("📦 备份 %d 行 → %s (600)" % (n, path))
    return path


def cmd_apply(args):
    creds = cliproxy_creds()
    accts = s2a_accounts()
    have = {account_email(a) for a in accts}

    want = sorted(set(creds) - have)
    if args.only:
        pick = {e.strip() for e in args.only.split(",") if e.strip()}
        unknown = pick - set(creds)
        if unknown:
            sys.exit("这些 email 在路① Secret 里没有：%s" % ", ".join(sorted(unknown)))
        dup = pick & have
        if dup:
            # 显式点名到已存在的号 = 硬拒（和路①脚本同一条纪律）：
            # 同账号两份会互相抢 token。
            sys.exit("这些号 sub2api 里已经有了，拒绝重复添加：%s" % ", ".join(sorted(dup)))
        want = sorted(pick)

    if not want:
        print("没有要加的号（sub2api 已覆盖路① Secret 里的全部 %d 个）。" % len(creds))
        return 0

    print("将要加进 sub2api（组 %d）：" % args.group)
    for e in want:
        c = creds[e]
        miss = [k for k in ("access_token", "refresh_token") if not c.get(k)]
        print("  %-34s name=ag-%-20s ua=%-8s %s" %
              (e, e.split("@")[0], ua_version(c.get("user_agent")) or "(默认)",
               "🔴 缺 " + ",".join(miss) if miss else ""))
        if miss:
            sys.exit("凭据不全，中止。先在路①把这个号修好。")

    if not args.yes:
        print("\n（dry-run。确认无误后加 --yes 真写。）")
        return 0

    backup_accounts()
    created = []
    for e in want:
        st, j = s2a("POST", "/api/v1/admin/accounts", build_body(e, creds[e], args.group))
        if st != 200:
            print("  ❌ %s 创建失败 (%s): %s" % (e, st, json.dumps(j, ensure_ascii=False)[:300]))
            continue
        aid = (j.get("data") or {}).get("id")
        created.append(aid)
        # ⚠️ 这里响应里的 group_ids 是 None，别据此判断没绑上 —— 下面 GET 回读才是判据。
        print("  ✅ %s → id=%s" % (e, aid))

    if not created:
        print("一个都没建成。")
        return 1

    # 建完必须刷一刀，否则 privacy_mode 停在 privacy_set_failed。参数名是 account_ids。
    st, j = s2a("POST", "/api/v1/admin/accounts/batch-refresh", {"account_ids": created})
    print("\nbatch-refresh：%s %s" % (st, json.dumps(j.get("data"), ensure_ascii=False)))

    print("\n回读（GET 才是判据）：")
    ok = True
    for aid in created:
        st, j = s2a("GET", "/api/v1/admin/accounts/%s" % aid)
        d = j.get("data") or {}
        e = (d.get("extra") or {})
        exp = (d.get("credentials") or {}).get("expires_at")
        fresh = str(exp).isdigit() and int(exp) > time.time()
        bad = []
        if args.group not in (d.get("group_ids") or []):
            bad.append("没绑上组")
        if e.get("privacy_mode") != "privacy_set":
            bad.append("privacy_mode=%s" % e.get("privacy_mode"))
        if not fresh:
            bad.append("token 没刷新（expires_at=%s）" % exp)
        if d.get("error_message"):
            bad.append("err=" + d["error_message"][:60])
        ok = ok and not bad
        print("  %-4s %-22s groups=%-6s %s" %
              (aid, d.get("name"), d.get("group_ids"),
               "🔴 " + " / ".join(bad) if bad else "✅"))

    print("\n下一步：`verify` 打真流量逐号判活。写入本身成功 ≠ 这几条腿能服务。")
    return 0 if ok else 1


# ----------------------------------------------------------------- verify
def group_key(group_id):
    k = psql("select key from api_keys where group_id=%d and status='active' "
             "and deleted_at is null order by id limit 1;" % group_id).strip()
    if not k:
        sys.exit("组 %d 下没有可用 key，verify 没法打真流量。"
                 "先在 sub2api 建一把绑到这个组的 key。" % group_id)
    return k


def cmd_verify(args):
    accts = [a for a in s2a_accounts() if args.group in (a.get("group_ids") or [])]
    if not accts:
        sys.exit("组 %d 下没有 antigravity 账号" % args.group)
    ids = {a["id"]: a["name"] for a in accts}

    # 默认 4×号数：sub2api 按「谁用得少先给谁」补，单轮小样本会让某些号一发没接，
    # 那是调度顺序不是腿死了（2026-09-10 实测第一轮 12 发有两个号 0 发，
    # 第二轮 12 发全落到那两个号上）。
    n = args.n or max(12, 4 * len(accts))
    key = group_key(args.group)
    print("组 %d 共 %d 个号，发 %d 发唯一 nonce（模型 %s）…" %
          (args.group, len(accts), n, args.model))

    # ⚠️ 用「开跑前的 max(id)」做切口，不要用时间窗。按 `created_at > now()-interval`
    # 统计会把**上一轮 verify 的请求**算进来 —— 2026-09-10 第一版就是这么把三条
    # 本轮 0 发的腿显示成有单的（数字和上一轮一字不差）。那是尺子在自证，不是证据。
    watermark = int(psql("select coalesce(max(id),0) from usage_logs;").strip() or 0)

    ok = fail = 0
    for i in range(n):
        nc = nonce()
        st, j = http_json(S2A + "/v1/chat/completions", {
            "model": args.model,
            "max_tokens": VERIFY_MAX_TOKENS,
            "messages": [{"role": "user",
                          "content": "Reply with exactly this token and nothing else: " + nc}],
        }, {"Authorization": "Bearer " + key}, timeout=120)
        body = json.dumps(j, ensure_ascii=False)
        if st == 200 and nc in body:
            ok += 1
        else:
            fail += 1
            print("  MISS[%d] %s %s" % (i + 1, st, body[:180]))
    print("\nnonce %d/%d 原样回读（HTTP 200 不算数，只认 nonce）" % (ok, n))

    # ⚠️ 逐条腿的判据只有这一张表：池子级 nonce 全绿也可能是一条好腿在扛全部。
    rows = psql("select u.account_id, count(*) from usage_logs u "
                "where u.id > %d and u.account_id in (%s) group by 1 order by 1;"
                % (watermark, ",".join(str(i) for i in ids))).strip()
    got = {}
    for line in rows.splitlines():
        if "|" in line:
            a, c = line.split("|")[:2]
            got[int(a)] = int(c)

    print("\n本轮新增的逐号接单数（usage_logs.id > %d）：" % watermark)
    silent = []
    for aid, name in sorted(ids.items()):
        c = got.get(aid, 0)
        print("  %-4s %-22s %s" % (aid, name, c if c else "0  ← 本窗口没接单"))
        if not c:
            silent.append("%s(%s)" % (name, aid))

    print()
    if fail:
        print("🔴 有 %d 发没回 nonce，先看上面的 MISS 行。" % fail)
    if silent:
        print("⚠️ 这些号本轮 0 发：%s" % ", ".join(silent))
        print("   **别据此判它死了** —— 再跑一次 verify，调度器会优先补它们；"
              "两轮都 0 发才值得单独查（那时用路①的 "
              "cliproxy-antigravity-model-matrix.py --only <email> 直打上游）。")
    if not fail and not silent:
        print("✅ %d 条腿本轮都真的接过单并成功返回。" % len(ids))
    return 1 if fail else 0


# --------------------------------------------------------------- rollback
def cmd_rollback(args):
    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    accts = {a["id"]: a for a in s2a_accounts()}
    print("将要从 sub2api 删除：")
    for i in ids:
        a = accts.get(i)
        if not a:
            print("  %-4s （不是 antigravity 账号或已不存在）" % i)
            continue
        print("  %-4s %-22s %-34s created=%s" %
              (i, a.get("name"), account_email(a), a.get("created_at")))
    if not args.yes:
        print("\n（dry-run。加 --yes 真删。）")
        print("注意：只删 sub2api 这一份拷贝，路① Secret 里的凭据不受影响。")
        return 0
    backup_accounts()
    rc = 0
    for i in ids:
        st, j = s2a("DELETE", "/api/v1/admin/accounts/%d" % i)
        print("  %s → %s %s" % (i, st, json.dumps(j, ensure_ascii=False)[:120]))
        if st != 200:
            rc = 1
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="两边对账（只读，不发上游请求）")

    p = sub.add_parser("apply", help="把路① Secret 里有、sub2api 里没有的号加进去")
    p.add_argument("--only", help="只加这些 email（逗号分隔）")
    p.add_argument("--group", type=int, default=DEFAULT_GROUP_ID)
    p.add_argument("--yes", action="store_true", help="真写（默认 dry-run）")

    p = sub.add_parser("verify", help="打真流量，按 usage_logs 逐号判活")
    p.add_argument("--group", type=int, default=DEFAULT_GROUP_ID)
    p.add_argument("-n", type=int, default=0, help="发几发（默认 4×号数，最少 12）")
    p.add_argument("--model", default=VERIFY_MODEL)

    p = sub.add_parser("rollback", help="删掉 sub2api 里的账号（路①不受影响）")
    p.add_argument("--ids", required=True, help="account id，逗号分隔")
    p.add_argument("--yes", action="store_true")

    a = ap.parse_args()
    return {"list": cmd_list, "apply": cmd_apply,
            "verify": cmd_verify, "rollback": cmd_rollback}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
