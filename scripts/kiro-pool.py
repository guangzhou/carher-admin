#!/usr/bin/env python3
"""kiro.rs 多号池的**写操作**闸门：读池子 / 加号 / 摘号 / 改优先级。

读操作（目录、额度、判死活）在 `kiro-probe.py`，两个别混。

为什么要有这个脚本
------------------
2026-09-16、09-17 三次动池子，全是现搓 shell，每次踩同一批坑：

1. **pod 里没有能打 DELETE 的工具。** 容器里是 BusyBox wget（不认
   `--method=DELETE`），且**没有 curl、没有 python3**。
   ⇒ 所有 admin 调用都在 198 host 上用 curl 直打 ClusterIP `10.43.109.5:8990`。
2. **`adminApiKey` 差点进 argv/history。** 现在恒经 `curl -K <临时配置文件>` 传，
   umask 077 + trap 删；POST body 同理走 `--data @文件`。
   ⛔ 曾经用嵌套 ssh + `\\x27` 转义传 body：body 被 shell 搞坏回 **400**，
   而 `/{id}/disabled` 那次**回了 400 却仍然把号禁用了** ⇒ **别信返回码，只信复核**。
3. **key 不在 `cm/kiro-rs-config`** —— ns 里没有这个 ConfigMap，
   真源是 `secret/kiro-rs-bootstrap` 的 `config.json`。
4. **`delete_credential` 硬拦未禁用的号**（`token_manager.rs:1797`
   `if !entry.disabled { bail!("只能删除已禁用的凭据") }`）
   ⇒ 摘一个还活着的号必须先 disable，本脚本显式分两步做、并各自复核。
5. **加号那一刻 `currentId` 通常不切到新号，但这不是稳态保证。** priority 模式是
   `min_by_key(priority)`、**数字小=优先**，新号 priority 0 与老号并列时取**先出现的那条**
   ⇒ add 完立刻复核，没切就告诉你压哪个号。
   🔴 09-28 加 id13 后几十分钟，`currentId` 从 10 自己变成 13，**没执行过 `priority`**，
   且老号还剩 1575/2000（不满足 `QuotaExceeded`）。为什么切没有数据，不下归因。
   ⇒ 判在役是谁一律现读 `show`，别拿加号那一刻的 currentId 当后续事实。
6. **写完不复核。** 每个写操作之后都重新 `GET /credentials` **并且**读盘上文件 ——
   admin 的写自带 `persist_credentials()`，落盘了才算完（所以**不需要 restart**）。

安全约定（每条都是踩过的）
--------------------------
- 任何写操作**先备份** `credentials.json` 到 `198:~cltx/kiro-deploy/backup/`（0600），
  并把备份路径与回滚方式打出来。
- `extract` 出来的单号文件含 **live refreshToken**，探完立刻删。
- 不用 `2>/dev/null`：错误必须看得见。
- 出口/凭据都不在本脚本里改；换 IP 走 skill `kiro-rs-ops` §D3。
- `--dry-run` 只对照请求形状，**不能替代写完的复核**（show 那两条尺子）。
  ⛔ 改动这个 flag 时别去掉 `default=argparse.SUPPRESS`：`parents=` 会让子 parser
  把自己的 default 写回 namespace，`--dry-run` 放在子命令**前**就被静默吃掉
  —— 09-17 因此把生产 priority 真改成了 9。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 198 的 22 口只在公司 IDC 内网开。不在内网时经一台能通的跳板走 ProxyJump：
#   export KIRO_SSH_JUMP=pubSrv
# 不设则保持原来的直连行为。
_JUMP = os.environ.get("KIRO_SSH_JUMP", "").strip()
SSH = ["ssh"] + (["-J", _JUMP] if _JUMP else []) + ["cltx@10.68.13.198"]
NS = "kiro-rs"
BACKUP_DIR = "~/kiro-deploy/backup"
CLUSTER_IP = "10.43.109.5:8990"
BASE = f"http://{CLUSTER_IP}/api/admin"


def sh(script: str, check: bool = True) -> str:
    """在 198 上跑一段 bash。stderr 不吞（见模块 docstring 的安全约定）。"""
    r = subprocess.run(SSH + ["bash -s"], input=script, capture_output=True,
                       text=True)
    if r.stderr.strip():
        print(r.stderr.rstrip(), file=sys.stderr)
    if check and r.returncode:
        sys.exit(f"远端命令失败（exit {r.returncode}）")
    return r.stdout


# adminApiKey 经 curl -K 文件传，不进 argv/history。
# 真源是 secret/kiro-rs-bootstrap 的 config.json —— ns 里没有 cm/kiro-rs-config。
CURLRC = f"""
umask 077
CFG=$(mktemp /tmp/.kiro_curlrc.XXXXXX)
trap 'rm -f "$CFG" /tmp/.kiro_body.*' EXIT
{{
  printf 'header = "x-api-key: '
  sudo kubectl -n {NS} get secret kiro-rs-bootstrap -o jsonpath='{{.data.config\\.json}}' \\
    | base64 -d | python3 -c 'import sys,json;sys.stdout.write(json.load(sys.stdin)["adminApiKey"])'
  printf '"\\nsilent\\nshow-error\\n'
}} > "$CFG"
"""


DRY = False

# 打印 body 时要脱敏的字段：这些是活凭据，进终端就等于进 scrollback/日志。
# 09-17：dry-run 曾把完整 refreshToken + 4.8KB clientSecret 原样吐到屏幕上。
SECRET_KEYS = ("refreshToken", "clientSecret", "accessToken",
               "kiroApiKey", "proxyPassword")


def redact(body: dict) -> dict:
    """脱敏后的 body 副本：密文只留长度与首尾，够判「传对没传丢」，不够复用。"""
    out = {}
    for k, v in body.items():
        if k in SECRET_KEYS and isinstance(v, str) and v:
            out[k] = f"<{k} len={len(v)} {v[:6]}…{v[-4:]}>"
        else:
            out[k] = v
    return out


def api(method: str, path: str, body: dict | None = None) -> str:
    """打 admin API。body 经临时文件传（曾被 shell 转义搞坏回 400）。"""
    if DRY and method != "GET":
        return (f"[dry-run] 会打 {method} {path}"
                + (f" body={json.dumps(redact(body), ensure_ascii=False)}" if body else ""))
    data = ""
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False)
        data = (f"B=$(mktemp /tmp/.kiro_body.XXXXXX)\n"
                f"cat > \"$B\" <<'JSON'\n{payload}\nJSON\n")
        data += ' EXTRA="-H Content-Type:application/json --data @$B"\n'
    else:
        data = ' EXTRA=""\n'
    return sh(CURLRC + data +
              f'curl -K "$CFG" -X {method} $EXTRA "{BASE}{path}"\n')


def get_pool() -> dict:
    return json.loads(api("GET", "/credentials"))


def disk_pool() -> list[dict]:
    """盘上的 credentials.json —— 判「写操作有没有落盘」的那把尺子。"""
    out = sh(f"sudo kubectl -n {NS} exec deploy/kiro-rs -c kiro-rs -- "
             "cat /app/config/credentials.json")
    d = json.loads(out)
    return d if isinstance(d, list) else [d]


def pod_gen() -> tuple[str, int, str]:
    """(pod 名, restartCount, startTime) —— 判「零重启」的那把尺子。"""
    out = sh(f"sudo kubectl -n {NS} get pod -o "
             "jsonpath='{.items[0].metadata.name} {.items[0].status."
             "containerStatuses[0].restartCount} {.items[0].status.startTime}'")
    name, restarts, start = out.split()
    return name, int(restarts), start


def live_quota(disk: list[dict]) -> dict[str, str]:
    """每个号的 live 余额，走 **admin 自己的** `GET /credentials/{id}/balance`。

    🔴 **为什么不复用 `kiro-probe.py quota`**：探针拿的是 pod 盘上那份凭据，
    401/403 时会**自己发起刷新**；而 social 号刷新**会轮转 refreshToken**
    （`token_manager.rs:206`）—— 轮转后的新值只留在我这边，服务端那份当场变死 token。
    admin 这条路由是服务端用自己的 token manager 去刷并 `persist_credentials()`，
    ⇒ 既不会把线上凭据探死，也不用从本机绕境外出口，快一个数量级。

    ⚠️ **它判不出封停。** 09-17 `bradburybruns64` 余额还剩 33.66，
    `ListAvailableModels` 已经 403 suspended ⇒ 判「能不能用」仍要
    `kiro-probe.py catalog`，这里只回答「还剩多少」。
    """
    out: dict[str, str] = {}
    for c in disk:
        cid = str(c.get("id"))
        try:
            b = json.loads(api("GET", f"/credentials/{cid}/balance"))
        except Exception as e:                      # 一个号读不到不该拖垮整张表
            out[cid] = f"quota=读取失败({type(e).__name__})"
            continue
        cap = b.get("usageLimit") or 0.0
        left = b.get("remaining")
        # 🔴 档位不止 2000：09-24 出现 KIRO PRO MAX = 5000 ⇒ 恒读 live cap，别硬算 2000
        out[cid] = (f"{b.get('subscriptionTitle', '?')} {left}/{cap}"
                    + ("  🔴耗尽" if cap and left is not None and left <= 0 else ""))
    return out


def live_tier(disk: list[dict]) -> dict[str, str]:
    """只取 enabled 号的 subscriptionTitle（比 live_quota 快：跳过 disabled 号）。"""
    out: dict[str, str] = {}
    for c in disk:
        if c.get("disabled"):
            continue
        cid = str(c.get("id"))
        try:
            b = json.loads(api("GET", f"/credentials/{cid}/balance"))
        except Exception as e:
            out[cid] = f"?({type(e).__name__})"
            continue
        title = b.get("subscriptionTitle", "?")
        short = (title.replace("KIRO ", "").replace(" ", "")
                 if title.startswith("KIRO ") else title)
        out[cid] = short
    return out


def summary() -> None:
    """盘上 subscriptionTitle 是加号/刷新时 kiro.rs 记下的档位快照，不是 live 值（换档要 --tier 现查）。"""
    from collections import Counter
    disk = disk_pool()
    cnt = Counter((auth_method_of(c), c.get("subscriptionTitle") or "?",
                   "禁用" if c.get("disabled") else "启用", "有ARN" if c.get("profileArn") else "无ARN")
                  for c in disk)
    print(f"盘上 {len(disk)} 个号（档位=盘上快照）")
    print("  %-7s %-16s %-4s %-5s %s" % ("auth", "档位", "状态", "ARN", "个数"))
    for (am, t, st, arn), n in sorted(cnt.items(), key=lambda x: (x[0][2], -x[1])):
        print("  %-7s %-16s %-4s %-5s %d" % (am, t, st, arn, n))
    on = [c for c in disk if not c.get("disabled")]
    print(f"启用 {len(on)}：" + "，".join(f"{k}×{v}" for k, v in
          Counter(f"{auth_method_of(c)}/{c.get('subscriptionTitle') or '?'}" for c in on).most_common()))
    print("⚠️ idc 号无 profileArn 是正常的（Builder ID 个人号没有真 profile）；"
          "报 KiroIDE ≥0.11.133 时要注入 BuilderId 占位 ARN，见 skill kiro-rs-ops §G")


def show(tag: str, with_quota: bool = False, with_tier: bool = False) -> dict:
    p = get_pool()
    disk = disk_pool()
    name, restarts, start = pod_gen()
    # authMethod 只在盘上那份里，API 不回 —— 池子现在是 idc/social 混编（09-24 起），
    # 不打出来就分不清哪个号走哪条刷新路（两条路的轮转行为相反，见 kiro-probe.py）。
    am_by_id = {str(c.get("id")): auth_method_of(c) for c in disk}
    quota = live_quota(disk) if with_quota else {}
    tier = live_tier(disk) if (with_tier and not with_quota) else {}
    print(f"\n=== {tag} ===")
    print(f"pod {name}  restarts={restarts}  startTime={start}")
    print(f"API  total={p.get('total')} available={p.get('available')} "
          f"currentId={p.get('currentId')}")
    for c in p.get("credentials", []):
        cid = str(c.get("id"))
        tier_col = (f"tier=%-8s " % tier.get(cid, "")) if tier else ""
        print("  id=%-3s %-30s auth=%-7s prio=%-3s disabled=%-6s current=%-6s "
              "succ=%-6s fail=%-4s %s%sreason=%s" % (
                  c.get("id"), c.get("email"), am_by_id.get(cid, "?"),
                  c.get("priority"), c.get("disabled"), c.get("isCurrent"),
                  c.get("successCount"), c.get("failureCount"),
                  tier_col,
                  (quota.get(cid, "") + "  ") if with_quota else "",
                  c.get("disabledReason")))
    ids_api = sorted(str(c.get("id")) for c in p.get("credentials", []))
    ids_disk = sorted(str(c.get("id")) for c in disk)
    ok = ids_api == ids_disk
    print(f"盘上 {len(disk)} 条 {ids_disk} —— 与 API {'一致 ✅（已落盘）' if ok else '不一致 🔴'}")
    if not ok:
        print("🔴 API 与盘上不一致：admin 写自带 persist_credentials()，"
              "不一致说明有别的写入方或 PVC 有问题，先查清再动。")
    return p


def backup() -> str:
    """写操作前必做。返回 198 上的备份路径。"""
    if DRY:
        print("# [dry-run] 会先备份 credentials.json 到 "
              f"198:{BACKUP_DIR}/credentials.json.<ts>（0600）")
        return "(dry-run)"
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = f"{BACKUP_DIR}/credentials.json.{ts}"
    sh(f"mkdir -p {BACKUP_DIR}\n"
       f"sudo kubectl -n {NS} exec deploy/kiro-rs -c kiro-rs -- "
       f"cat /app/config/credentials.json > {path}\n"
       f"chmod 600 {path}\n"
       f"ls -l {path}\n")
    real = path.replace("~", "/home/cltx")
    print(f"# 备份 -> 198:{real}（0600，含完整凭据，可据此 POST 复活）")
    return real


# ---- 子命令 ----

ADD_FIELDS = ("refreshToken", "authMethod", "clientId", "clientSecret",
              "region", "authRegion", "apiRegion", "machineId", "email")

# 按 authMethod 分流的必需字段。⛔ 不要读导出里的 `provider`（Github/BuilderId/…）：
# kiro.rs 源码对它零引用，`AddCredentialRequest` 里也没有这个字段 ⇒ 传进去只会被忽略。
# 真正决定走哪条刷新路的是 `authMethod`（`token_manager.rs:122-138`）：
#   idc / builder-id / iam → AWS SSO OIDC（要 clientId+clientSecret）
#   其余（social）        → https://prod.<region>.auth.desktop.kiro.dev/refreshToken
#                          body 只有 refreshToken，**压根没有 clientId/clientSecret**
REQUIRED = {
    "idc":    ("refreshToken", "clientId", "clientSecret"),
    "social": ("refreshToken",),
}


def auth_method_of(src: dict) -> str:
    """与 kiro.rs `token_manager.rs:122-138` 逐字对齐的分流。

    🔴 **不能无脑 setdefault("idc")**：`AddCredentialRequest.auth_method` 的服务端
    default 是 **"social"**（`admin/types.rs:142`），而 Github/Google 这类第三方登录
    导出**只有 refreshToken**，硬写 idc 会让它去打 AWS OIDC —— 那条路第一件事就是
    `ok_or_else(|| anyhow!("IdC 刷新需要 clientId"))`，直接失败。
    """
    am = (src.get("authMethod") or "").strip()
    if not am:
        am = "idc" if (src.get("clientId") and src.get("clientSecret")) else "social"
    am = am.lower()
    return am if am in ("idc", "builder-id", "iam") else "social"


class DupFound(SystemExit):
    """判重命中。

    继承 `SystemExit` ⇒ 单号 `add` 的行为**逐字节不变**（照样打同一句、非零退出），
    但 `add-batch` 能 `except DupFound` 把它和「真失败」分开。
    ⛔ 别改回「按消息文本 `in` 判断」—— dup_check 有三条不同的消息
    （rt_sha / machineId / email），漏一条就会把重复号记成失败。
    """


def dup_check(body: dict) -> None:
    """判「这是不是已经在池里的号」—— **三把判据：refreshToken sha256 / machineId / email**。

    🔴 **不看导出里的 `usage`/`id`/`userId`**：09-21 用户发来的导出里 `usage` 写着
    `0/2000` 像个满额新号，实际三个字段的 sha256 与线上 id4 **逐字节相同**，
    live 读出来是 `2000/2000` 已耗尽（导出里的数字是历史快照）。
    ⛔ 也别比长度：多个号的 `clientSecret` 都是 4797 字符的三段 JWT，长度毫无区分度。
    ⚠️ 这里以前写着「**不看邮箱**」—— 已改：邮箱/machineId 相同而 refreshToken 不同，
    正是"同一个号换了 OAuth client 后重新导出"的形状，10-01 因此真漏进去一个
    （靠人工比才发现）。现在邮箱与 machineId 都是**硬拦**，不是提醒。

    🔴 **先断言集合非空**：09-17 第一次手搓比对打出「相同: False（比对了 0 个）」——
    那是**空集假绿**，不是通过。
    """
    disk = disk_pool()
    if not disk:
        sys.exit("🔴 盘上读到 0 条凭据 ⇒ 重复性检查是空集假绿，先查清 PVC 再加号")
    rt = body.get("refreshToken") or ""
    if not rt:
        return
    h = hashlib.sha256(rt.encode()).hexdigest()
    dup = [c for c in disk
           if c.get("refreshToken")
           and hashlib.sha256(c["refreshToken"].encode()).hexdigest() == h]
    print(f"# 重复性检查：比对了盘上 {len(disk)} 个号（按 refreshToken sha256）")
    if dup:
        who = [f"id={c.get('id')} {c.get('email')}" for c in dup]
        raise DupFound(f"🔴 这个 refreshToken 已经在池里了：{who}"
                 "\n   ⇒ 重复加会白占一个 id 且两条共用同一个额度池，停手。")
    # 🔴 第二把判据：machineId。**只比 refreshToken 的 sha 拦不住"同一个号的重新导出"**
    # —— 重新注册 OAuth client 会把 clientId + refreshToken 全换掉，sha 自然对不上。
    # 2026-10-01 一批 10 个号里就有 1 个是池里 id14 的重复导出（machineId 逐字相同，
    # 只有 client 凭据换了）；那次是靠邮箱的 ⚠️ 提醒才人工比出来的，**脚本本该拦住**。
    # ⛔ 不能用 `userId` 判：导出里有，但**服务端不落盘**（线上 16 个键里没有它）
    #    ⇒ 拿它跟池子比是拿一个池里不存在的字段比，恒不命中。
    # 实测盘上 15/15 的 machineId 互不相同 ⇒ 它在这个池子里有身份区分度。
    mid = (body.get("machineId") or "").strip()
    if mid:
        same_mid = [c for c in disk
                    if (c.get("machineId") or "").strip().lower() == mid.lower()]
        if same_mid:
            who = [f"id={c.get('id')} {c.get('email')}" for c in same_mid]
            raise DupFound(
                f"🔴 machineId 与池里 {who} 相同，但 refreshToken 不同 ⇒ "
                "**这是同一个号的重新导出**（换过 OAuth client）。\n"
                "   额度按**账号**算：加进去只会白占一个 id 且两条共用同一个 credits 池，停手。\n"
                "   真要替换那条旧的：先 `remove <旧id>` 再 add，别两条并存。")

    same_mail = [f"id={c.get('id')}" for c in disk
                 if c.get("email") and c.get("email") == body.get("email")]
    if same_mail:
        raise DupFound(f"🔴 邮箱与池里 {same_mail} 相同但 refreshToken/machineId 不同。\n"
                 "   额度是按账号算的 ⇒ 同邮箱两条**不会**变成两份配额，大概率是重新导出。\n"
                 "   确认真要并存就临时注释掉这道门；要替换则 `remove <旧id>` 后再 add。")
    print(f"# ⇒ 非重复 ✅（比了 refreshToken sha256 + machineId + email 三把）")


def assert_landed(before: dict, after: dict, body: dict, resp: str) -> None:
    """🔴 服务端拒了也要**非零退出** —— 10-08 的坑，批量时这条最致命。

    admin API 拒绝时回的是 HTTP 200 + body `{"error":{...}}`，而 `curl` 对此
    rc=0 ⇒ `sh()` 不报错 ⇒ 整个 `add` 安静地 exit 0，看着像加成功了。
    10-08 加 24 个号时 3 个 refreshToken 已死（`invalid_grant / Resource not found`），
    服务端逐个拒，循环里**24 行全是 rc=0**；真相只在「比对了盘上 N 个号」那个 N
    停涨上看得出来（46/46、51/51、53/53），而最终池子是 66 不是 45+24=69。
    ⇒ 判据必须是**池子真多了一条、且就是我这个号**，不是返回码、也不是 resp 文本。

    两道都要：
    - 条数门：after > before（拦「什么都没发生」）
    - 身份门：新增那条的 email/machineId 与我提交的一致
      （拦「别人同时也在加号，条数涨的是他那条」—— 并发下条数门单独会假绿）
    """
    def ids(p: dict) -> set:
        return {c.get("id") for c in p.get("credentials", [])}

    gained = ids(after) - ids(before)
    if not gained:
        sys.exit(
            f"🔴 加号没落地：池子条数没变（{len(ids(before))} → {len(ids(after))}）。\n"
            f"   服务端返回：{resp.strip()[:400]}\n"
            "   ⚠️ admin API 拒绝时回 200 + {\"error\":…}，curl rc=0 ⇒ 别信返回码。\n"
            "   常见原因：refreshToken 已死（`invalid_grant`/`Resource not found`）\n"
            "     ⇒ 先单独跑 `kiro-probe.py catalog --creds <file>` 确认，\n"
            "       照样 400 的话这个号只能重新登录，刷新救不回。")
    # 🔴 身份门读**盘上**那份，不读 API：API 的行里没有 `machineId`，
    # 且 social 号的 `email` 可能是 null（池里 id46~52 就是这样），
    # 拿 API 比等于只有一把半判据。
    rows = {c.get("id"): c for c in disk_pool()}
    mine = []
    for i in sorted(gained):
        r = rows.get(i) or {}
        for k in ("email", "machineId"):
            want, got = body.get(k), r.get(k)
            # ⛔ 两边都空不算命中 —— 那是「两个字段都没有」，不是「两个字段相同」。
            if want and got and str(want) == str(got):
                mine.append(i)
                break
    if not mine:
        sys.exit(
            f"🔴 池子多了 {sorted(gained)}，但没有一条的 email/machineId 与我提交的一致。\n"
            f"   我提交的是 email={body.get('email')} "
            f"machineId={(body.get('machineId') or '')[:16]}…\n"
            f"   新增那几条盘上是 {[(rows.get(i) or {}).get('email') for i in sorted(gained)]}\n"
            "   ⇒ 可能是别人同时在加号。停手，现读 `show` 看清楚再动。")
    print(f"✅ 落地复核：新增 id={sorted(mine)}，email/machineId 与提交一致（比的是盘上那份）")


def cmd_add(a):
    """加一个号。⚠️ 加之前必须先用 kiro-probe.py --creds 验通，本脚本不替你验。"""
    src = json.loads(Path(a.file).read_text(encoding="utf8"))
    # v1.7.x account-manager 导出是 {"accounts":[…]} 或裸 dict/list，都收
    if isinstance(src, dict) and "accounts" in src:
        src = src["accounts"]
    if isinstance(src, list):
        if len(src) != 1:
            sys.exit(f"文件里有 {len(src)} 个账号，一次只加一个，先拆开")
        src = src[0]
    body = {k: src[k] for k in ADD_FIELDS if k in src}
    am = auth_method_of(src)
    body["authMethod"] = am
    body.setdefault("priority", a.priority)
    # machineId 自己派生并**钉死**，而不是留给服务端每次算。
    # 服务端派生式是 sha256("KotlinNativeAPI/<refreshToken>")（machine_id.rs），
    # 而 social 刷新会轮转 refreshToken（`token_manager.rs:206`）⇒ 不钉就会漂。
    if not body.get("machineId"):
        rt = body.get("refreshToken") or ""
        if rt:
            body["machineId"] = hashlib.sha256(
                f"KotlinNativeAPI/{rt}".encode()).hexdigest()
            print(f"# machineId 缺失 ⇒ 按 kiro.rs 同一条规则派生并钉死："
                  f"{body['machineId'][:16]}…")
    need = REQUIRED["idc" if am == "idc" else "social"]
    missing = [k for k in need if not body.get(k)]
    if missing:
        sys.exit(f"导出文件缺 authMethod={am} 必需的字段：{missing}")
    # 🔴 10-16 id147：导出带 `apiRegion: eu-central-1` ⇒ kiro.rs 对话/余额都打
    # `q.eu-central-1`（`effective_api_region()` 凭据级优先），同一 token 在那里
    # getUsageLimits / ListAvailableModels 全 403「User is not authorized」，
    # 而 `q.us-east-1` 两个都 200。add 不会报错，症状只在 Admin 看余额时冒出来。
    api_region = body.get("apiRegion")
    if api_region and api_region != "us-east-1" and not getattr(a, "allow_api_region", False):
        sys.exit(f"导出的 apiRegion={api_region}（非 us-east-1）。10-16 实测这种号在 "
                 f"q.{api_region} 全 403，q.us-east-1 正常。\n"
                 f"   ⇒ 先用 kiro-probe.py --region {api_region} 验，过了再带 --allow-api-region；"
                 f"否则把文件里的 apiRegion 改成 us-east-1 再加。")
    print(f"# 将要加入：{body.get('email')} authMethod={am} "
          f"region={body.get('region', '<回落 config.json 全局值>')} "
          f"priority={body['priority']}")
    if am == "social":
        print("# ⚠️ 这是 social（第三方登录）号：不带 clientId/clientSecret 是**正常的**，"
              "刷新走 kiro.dev 端点而不是 AWS OIDC")
    print("# ⚠️ 导出里的 expiresAt 是毫秒 epoch，这里不传 —— 服务端自己换 token")

    dup_check(body)
    before = show("加号前")
    backup()
    resp = api("POST", "/credentials", body)
    print(resp)
    p = show("加号后")
    if not DRY:
        assert_landed(before, p, body, resp)
    new = max((c for c in p.get("credentials", [])),
              key=lambda c: c.get("id") or 0, default=None)
    # dry-run 没真加 ⇒ max(id) 是旧号，别打这段（10-15 dry-run 报「没切到 id=140」误导）
    if new and not DRY and p.get("currentId") != new.get("id"):
        print(f"\n⚠️ 加号这一刻 currentId 仍是 {p.get('currentId')}，没切到新号 id={new.get('id')}。"
              f"\n   原因：priority 模式 min_by_key(priority)，平级取先出现的那条。"
              f"\n   🔴 **这不一定要修** —— 先看在役那个号还剩多少："
              f"\n      python3 scripts/kiro-pool.py show --with-quota"
              f"\n      （别用 kiro-probe.py quota：social 号刷新会轮转 refreshToken）"
              f"\n      · 还有余额 ⇒ **别切**，新号当备用。老号打满时 kiro.rs 自己"
              f" QuotaExceeded + min_by_key 重选，这才是真兜底；提前切等于把两个号"
              f"的余额摊薄成半个。"
              f"\n      · 已耗尽/被封 ⇒ 才切：kiro-pool.py priority <老号id> 9"
              f"\n   ⚠️ 这只描述加号这一刻。09-28 currentId 后来自己从 10 变成 13，"
              f"没执行 priority、老号也没打满。判在役是谁一律再跑一次 show。")
    print("\n下一步：20 条道（`kiro-lane-regress.sh --rounds 1`）回归（skill kiro-rs-ops §E），判据是 input_tokens 分层不是 200。")


def cmd_add_batch(a):
    """一批号逐个 add，**一个失败不中断**，收尾打一张成败表 + 非零退出码。

    10-08 的三个教训都在这里固化（SOP 里那段手搓 for 一个都挡不住）：
    - ⛔ 手搓 `for f in …; do kiro-pool.py add "$f"; done` **看不出成败** ——
      每个 `add` 的输出有上百行池子表格，成败那一行被刷走；而服务端拒了还 rc=0。
    - ⛔ `dup_check` 命中会 `sys.exit` ⇒ 手搓循环会在第一个重复上**整批停住**。
      这里把重复当「跳过」而不是「失败」，继续往下走。
    - ✅ 收尾一张表：加成 / 跳过（重复）/ 失败（含服务端那句 error），**有失败就非零退出**。

    ⚠️ 仍然**不替你验号** —— 先跑 `kiro-batch-prepare.py` 筛新增，再逐个
    `kiro-probe.py both --summary-json` 验通，最后才用这个入口。
    """
    files = [Path(f) for f in a.files]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        sys.exit(f"这些文件不存在：{missing}")
    # 🔴 先断言输入非空：空列表会让下面每一条断言都「通过」⇒ 一张全绿的空表。
    if not files:
        sys.exit("没有要加的文件 —— 空集假绿，先确认 kiro-batch-prepare.py 产出了东西")
    print(f"### 批量加 {len(files)} 个号，priority={a.priority}"
          f"（一个失败不中断，收尾出表）\n")
    ok, dup, bad = [], [], []
    for i, f in enumerate(files, 1):
        print(f"\n{'='*70}\n# [{i}/{len(files)}] {f.name}\n{'='*70}")
        ns = argparse.Namespace(file=str(f), priority=a.priority, allow_api_region=False)
        try:
            cmd_add(ns)
            ok.append(f.name)
        except DupFound as e:
            # 判重用**异常类型**分流，不按消息文本 —— dup_check 有三条不同的消息
            print(f"# ⏭ 跳过（判重命中）：{str(e.code).splitlines()[0]}")
            dup.append(f.name)
        except SystemExit as e:
            msg = str(e.code) if e.code not in (0, None) else ""
            print(f"# 🔴 失败：{msg.splitlines()[0] if msg else '(无消息)'}")
            bad.append((f.name, msg.splitlines()[0] if msg else ""))
        except Exception as e:  # 网络抖动等，同样不该中断整批
            print(f"# 🔴 失败（异常）：{type(e).__name__}: {e}")
            bad.append((f.name, f"{type(e).__name__}: {e}"))

    print(f"\n{'='*70}\n### 批量结果\n{'='*70}")
    print(f"加成 {len(ok)} / 跳过(重复) {len(dup)} / 失败 {len(bad)}  共 {len(files)}")
    for n in ok:
        print(f"  ✅ {n}")
    for n in dup:
        print(f"  ⏭ {n}  （已在池里）")
    for n, why in bad:
        print(f"  🔴 {n}  {why[:160]}")
    if bad:
        sys.exit(f"\n🔴 {len(bad)} 个没加进去 —— **别把这当成功**。"
                 "\n   refreshToken 死（invalid_grant）的只能重新登录；"
                 "\n   其余先单独 `kiro-probe.py catalog --creds <file>` 看响应体。")
    print("\n下一步：回归（skill kiro-rs-ops §E），判据是 input_tokens 分层不是 200。")


def cmd_remove(a):
    """摘一个号：先 disable（未禁用的删不掉）再 DELETE。零重启。"""
    p = show("摘号前")
    target = next((c for c in p.get("credentials", []) if c.get("id") == a.id), None)
    if not target:
        sys.exit(f"池子里没有 id={a.id}")
    print(f"\n# 将要摘除 id={a.id} {target.get('email')} "
          f"(disabled={target.get('disabled')})")
    backup()
    if not target.get("disabled"):
        # token_manager.rs:1797 —— 未禁用的号 DELETE 会被硬拦
        print(f"# 该号仍启用 ⇒ 先 disable（delete_credential 硬拦未禁用的号）")
        print(api("POST", f"/credentials/{a.id}/disabled", {"disabled": True}))
        got = next((c for c in get_pool().get("credentials", [])
                    if c.get("id") == a.id), {})
        if not got.get("disabled") and not DRY:
            sys.exit("🔴 disable 没生效（别信返回码，这里就是复核）——停手，别继续删")
    print(api("DELETE", f"/credentials/{a.id}"))
    p = show("摘号后")
    if any(c.get("id") == a.id for c in p.get("credentials", [])) and not DRY:
        sys.exit(f"🔴 id={a.id} 还在池子里，删除没生效")
    if not p.get("credentials"):
        print("\n🔴 池子空了 ⇒ 18 条 kiro-* 道全挂。立刻加号。")
    elif p.get("total") == 1:
        print("\n⚠️ 单号无兜底：这个号一旦打满/被封，20 条道（`kiro-lane-regress.sh --rounds 1`）立刻全挂。")
    print("\n下一步：20 条道（`kiro-lane-regress.sh --rounds 1`）回归（skill kiro-rs-ops §E）。"
          "\n重置日（每月 1 日 00:00 UTC）后想复活：从上面的备份取回该条 ⇒ kiro-pool.py add。")


def cmd_priority(a):
    """改优先级。数字小=优先；内部会触发 select_highest_priority() 重选 currentId。"""
    show("改优先级前")
    print(api("POST", f"/credentials/{a.id}/priority", {"priority": a.priority}))
    show("改优先级后")


def cmd_priority_batch(a):
    """批量改优先级：一次备份 + N 次写 + 一次复核（省 2N-2 次 show SSH 往返）。"""
    ids = [int(x) for x in a.ids.split(",")]
    if a.by_tier:
        disk = disk_pool()
        tier = live_tier(disk)
        needle = a.by_tier.upper().replace("KIRO ", "").replace(" ", "")
        ids = [int(cid) for cid, t in tier.items() if t.upper() == needle]
        if not ids:
            sys.exit(f"没有 enabled 且档位匹配 {a.by_tier!r} 的号")
        print(f"# --by-tier {a.by_tier!r} 解析到 {len(ids)} 个号：{ids}")
    show("批量改优先级前")
    bk = backup()
    ok, fail = [], []
    for cid in ids:
        try:
            resp = api("POST", f"/credentials/{cid}/priority",
                       {"priority": a.priority})
            print(f"  id={cid} -> prio={a.priority}  {resp.strip()}")
            ok.append(cid)
        except SystemExit:
            print(f"  id={cid} 🔴 失败")
            fail.append(cid)
    show("批量改优先级后")
    print(f"\n# 成功 {len(ok)}/{len(ids)}：{ok}")
    if fail:
        print(f"# 🔴 失败 {len(fail)}：{fail}")
    print(f"# 备份 -> {bk}")


def cmd_extract(a):
    """从备份里提出单个号到本地文件，喂给 kiro-probe.py --creds 判死活。

    ⚠️ 输出文件含 live refreshToken，探完立刻删。
    """
    raw = sh(f"cat {a.backup}")
    pool = json.loads(raw)
    pool = pool if isinstance(pool, list) else [pool]
    hits = [c for c in pool
            if a.who in (c.get("email") or "") or str(c.get("id")) == a.who]
    if not hits:
        have = [f"id={c.get('id')}:{c.get('email')}" for c in pool]
        sys.exit(f"备份里没有匹配 {a.who!r} 的号。里面有：{have}")
    if len(hits) > 1:
        sys.exit(f"{a.who!r} 匹配到 {len(hits)} 条，说具体点")
    out = a.out or tempfile.mkstemp(prefix="kiro_hist_", suffix=".json")[1]
    Path(out).write_text(json.dumps(hits, ensure_ascii=False, indent=2),
                         encoding="utf8")
    Path(out).chmod(0o600)
    print(f"# {hits[0].get('email')} -> {out} (0600)")
    print(f"# 判死活（两把尺子都要）：")
    print(f"#   python3 scripts/kiro-probe.py both --creds {out}")
    print(f"# 🔴 credit 余额判不出封停：09-17 bradburybruns64 余额还剩 33.66，"
          f"catalog 却 403 suspended。")
    print(f"# ⚠️ 探完删掉：rm -f {out}   （里面是 live refreshToken）")


def main():
    # default=SUPPRESS 是必须的：parents= 让子 parser 也带上这个 flag，而子 parser
    # 会把自己的 default 写回 namespace —— 若 default=False，`--dry-run priority 3 9`
    # （flag 在子命令前）会被静默改回 False 然后真写进生产池。09-17 踩过。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        default=argparse.SUPPRESS,
                        help="只读 + 打印将要发的写请求，不真写。⚠️ 只是形状对照，"
                             "不能替代写完的复核")

    p = argparse.ArgumentParser(
        description=__doc__, parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("show", parents=[common],
                        help="读池子 + 盘上文件 + pod 代数（纯只读）")
    sp.add_argument("--with-quota", action="store_true",
                    help="顺带打每个号的 live 余额（走 admin /balance，服务端自己刷，"
                         "不会把 social 号的 refreshToken 探轮转掉）。"
                         "⚠️ 余额判不出封停，判死活仍用 kiro-probe.py catalog")
    sp.add_argument("--tier", action="store_true",
                    help="只对 enabled 号查档位（STUDENT/PRO+/…），比 --with-quota 快")
    sp.add_argument("--summary", action="store_true",
                    help="只出 认证方式×档位×启用 的计数表 + 有无 profileArn（读盘上字段，不打上游，秒出）。"
                         "回答「学生号有几个/启用的都是什么号」用这个，别手搓 python 管道")

    ap = sub.add_parser("add", parents=[common], help="加一个号（零重启）")
    ap.add_argument("file", help="账号导出 json（v1.7.x account-manager 格式或裸 dict）")
    ap.add_argument("--priority", type=int, default=0, help="数字小=优先，默认 0")
    ap.add_argument("--allow-api-region", action="store_true",
                    help="放行非 us-east-1 的 apiRegion（先在那个 region 验通再用）")

    bp = sub.add_parser("add-batch", parents=[common],
                        help="一批号逐个加，一个失败不中断，收尾出成败表（10-08 加的）")
    bp.add_argument("files", nargs="+",
                    help="多个单号 json（kiro-batch-prepare.py --out 的产物）")
    bp.add_argument("--priority", type=int, default=0,
                    help="数字小=优先，默认 0。🔴 先 show 看在役号是几，对齐着传")

    rp = sub.add_parser("remove", parents=[common],
                        help="摘一个号（先 disable 再 DELETE，零重启）")
    rp.add_argument("id", type=int)

    pp = sub.add_parser("priority", parents=[common],
                        help="改优先级并触发 currentId 重选")
    pp.add_argument("id", type=int)
    pp.add_argument("priority", type=int)

    pb = sub.add_parser("priority-batch", parents=[common],
                        help="批量改优先级（一次备份 + 一次复核，省 SSH 往返）")
    pb.add_argument("--ids", default="",
                    help="逗号分隔的 id 列表（与 --by-tier 二选一）")
    pb.add_argument("--by-tier", default="",
                    help="按档位名过滤 enabled 号（STUDENT / PRO+ / …），"
                         "自动查 balance 解析，与 --ids 二选一")
    pb.add_argument("priority", type=int, help="目标优先级，数字小=优先")

    ep = sub.add_parser("extract", parents=[common],
                        help="从备份里提单个号，喂 kiro-probe.py 判死活")
    ep.add_argument("backup", help=f"198 上的备份路径，如 {BACKUP_DIR}/credentials.json.…")
    ep.add_argument("who", help="email 子串或 id")
    ep.add_argument("--out", help="本地输出文件（默认临时文件，0600）")

    a = p.parse_args()
    global DRY
    DRY = getattr(a, "dry_run", False)  # SUPPRESS ⇒ 没给就压根不在 namespace 里
    if DRY:
        print("### dry-run：不会发任何写请求")
    {"show": lambda _a: summary() if getattr(_a, "summary", False) else
             show("当前", getattr(_a, "with_quota", False), getattr(_a, "tier", False)),
     "add": cmd_add, "add-batch": cmd_add_batch, "remove": cmd_remove,
     "priority": cmd_priority, "priority-batch": cmd_priority_batch,
     "extract": cmd_extract}[a.cmd](a)


if __name__ == "__main__":
    main()


