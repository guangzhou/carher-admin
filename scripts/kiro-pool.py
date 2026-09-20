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
5. **`currentId` 不会自己切到新号。** priority 模式是 `min_by_key(priority)`、
   **数字小=优先**，新号 priority 0 与老号并列时取**先出现的那条** ⇒ 已耗尽的老号
   仍占 `isCurrent`。本脚本 add 完自己复核 currentId，没切就直接告诉你压哪个号。
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
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SSH = ["ssh", "cltx@10.68.13.198"]
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


def show(tag: str) -> dict:
    p = get_pool()
    disk = disk_pool()
    name, restarts, start = pod_gen()
    print(f"\n=== {tag} ===")
    print(f"pod {name}  restarts={restarts}  startTime={start}")
    print(f"API  total={p.get('total')} available={p.get('available')} "
          f"currentId={p.get('currentId')}")
    for c in p.get("credentials", []):
        print("  id=%-3s %-30s prio=%-5s disabled=%-6s current=%-6s "
              "succ=%-6s fail=%-4s reason=%s" % (
                  c.get("id"), c.get("email"), c.get("priority"),
                  c.get("disabled"), c.get("isCurrent"),
                  c.get("successCount"), c.get("failureCount"),
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
    body.setdefault("authMethod", "idc")
    body.setdefault("priority", a.priority)
    missing = [k for k in ("refreshToken", "clientId", "clientSecret", "machineId")
               if not body.get(k)]
    if missing:
        sys.exit(f"导出文件缺必需字段：{missing}")
    print(f"# 将要加入：{body.get('email')} region={body.get('region')} "
          f"priority={body['priority']}")
    print("# ⚠️ 导出里的 expiresAt 是毫秒 epoch，这里不传 —— 服务端自己 OIDC 换 token")
    show("加号前")
    backup()
    print(api("POST", "/credentials", body))
    p = show("加号后")
    new = max((c for c in p.get("credentials", [])),
              key=lambda c: c.get("id") or 0, default=None)
    if new and p.get("currentId") != new.get("id"):
        print(f"\n🔴 currentId 仍是 {p.get('currentId')}，没切到新号 id={new.get('id')}。"
              f"\n   原因：priority 模式 min_by_key(priority)，平级取先出现的那条。"
              f"\n   解：kiro-pool.py priority <老号id> 9   （内部触发 select_highest_priority）")
    print("\n下一步：18 条道回归（skill kiro-rs-ops §E），判据是 input_tokens 分层不是 200。")


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
        print("\n⚠️ 单号无兜底：这个号一旦打满/被封，18 条道立刻全挂。")
    print("\n下一步：18 条道回归（skill kiro-rs-ops §E）。"
          "\n重置日（每月 1 日 00:00 UTC）后想复活：从上面的备份取回该条 ⇒ kiro-pool.py add。")


def cmd_priority(a):
    """改优先级。数字小=优先；内部会触发 select_highest_priority() 重选 currentId。"""
    show("改优先级前")
    print(api("POST", f"/credentials/{a.id}/priority", {"priority": a.priority}))
    show("改优先级后")


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

    sub.add_parser("show", parents=[common],
                   help="读池子 + 盘上文件 + pod 代数（纯只读）")

    ap = sub.add_parser("add", parents=[common], help="加一个号（零重启）")
    ap.add_argument("file", help="账号导出 json（v1.7.x account-manager 格式或裸 dict）")
    ap.add_argument("--priority", type=int, default=0, help="数字小=优先，默认 0")

    rp = sub.add_parser("remove", parents=[common],
                        help="摘一个号（先 disable 再 DELETE，零重启）")
    rp.add_argument("id", type=int)

    pp = sub.add_parser("priority", parents=[common],
                        help="改优先级并触发 currentId 重选")
    pp.add_argument("id", type=int)
    pp.add_argument("priority", type=int)

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
    {"show": lambda _a: show("当前"), "add": cmd_add, "remove": cmd_remove,
     "priority": cmd_priority, "extract": cmd_extract}[a.cmd](a)


if __name__ == "__main__":
    main()


