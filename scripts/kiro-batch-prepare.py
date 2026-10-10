#!/usr/bin/env python3
"""把一份 kiro 账号导出整库快照拍平成「只含新增号」的 per-account 文件。

用户给的导出**不是新号清单，是账号管理器的整库快照** —— 2026-10-02 那份
`kiro-accounts-32-…json` 32 个号里 12 个已经在池里。所以批量加号的第一步不是拍平，
而是**跟线上逐号比**，比完才知道要拍几份。

⛔ **别指望 `kiro-pool.py add` 的 `dup_check()` 在循环里逐个拦** —— 它命中就
`sys.exit`，20 个号的 for 循环会停在第一个重复上。那道门是**单个号的最后一道保险**，
不是批量入口的筛子。这个脚本就是那个筛子。

判重三把判据（与 `kiro-pool.py dup_check()` 逐字一致，各拦一种漂移组合）：

| 形状 | 10-02 样本 | 命中 |
|---|---|---|
| 原样重复导出 | 10 个 | rt sha256 + machineId + email 三把全中 |
| 换过 OAuth client | `ferril.wananda` ↔ id14 | machineId + email（**rt sha 不中**）|
| social 刷新过 | `shyntha.respati` ↔ id25 | **只有 email 中**（rt 与 machineId 都漂了）|

🔴 最后一行是「邮箱那把门」第一次成为唯一有效判据：social 号的 machineId 派生式是
`sha256("KotlinNativeAPI/<refreshToken>")`，**跟着 rt 一起漂** ⇒ 10-01 补 machineId 时
那句「rt 换了 machineId 不变」对 social 不成立。三把不是冗余，是覆盖三种漂移。

拍平时的门（任一条不过就整个 exit，一个文件都不写）：
  · 字段集 ⊆ 线上实际键集合（现读盘上 credentials.json，不硬编）
  · authMethod 拍平成小写、按 kiro.rs 的分流规则归一（服务端 default 是 social！）
  · 无不可见字符（零宽/NBSP/BOM —— 凭据里这类病会静默 401）
  · machineId 是 UUID 或 64hex（`normalize_machine_id` 只认这两种）
  · idc 必须有三段 JWT 的 clientSecret；social 必须**没有** client 凭据
  · 组内 refreshToken sha 互不相同（别自己跟自己重）

用法：
    # 只比对、不写文件（看看这份导出里有几个是新的）
    python3 scripts/kiro-batch-prepare.py ~/Downloads/kiro-accounts-32.json --dry-run

    # 真拍平到一个目录（0600），再用 add-batch 整批加
    python3 scripts/kiro-batch-prepare.py ~/Downloads/kiro-accounts-32.json --out /tmp/kb
    python3 scripts/kiro-pool.py add-batch /tmp/kb/creds/*.json --priority 2

⛔ **别再手搓 `for f in …; do kiro-pool.py add "$f"; done`**（10-08 前的 SOP 写法）：
那段 for 挡不住三件事 —— `dup_check` 命中会 `sys.exit` 整批停在第一个重复上；
每个 `add` 输出上百行池子表格把成败那行刷走；而**服务端拒了 `add` 也曾 exit 0**
（admin API 拒绝回 200 + `{"error":…}`，curl rc=0）。`add-batch` 三条都修了。

⚠️ 它**不验号**（不打 AWS）。拍完必须逐个 `kiro-probe.py both --creds` 只读验，
social 号带 `--out-rotated` 并拿轮转后那份去 add。
🔴 验号的判据是 `ok=true` + `modelCount` 够数，**不是 `remaining>0`** ——
`remaining=0` 的「耗尽但活」号照样能加，每月 1 日 00:00 UTC 自动重置复活
（10-08 那批 24 个里有 10 个是这形状）。见 skill kiro-rs-ops §D3 / §D4.2。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

NS = "kiro-rs"
HOST = "cltx@10.68.13.198"

# 198 的 22 口只在公司 IDC 内网开（判据是 `nc -z 10.68.13.198 22`，**不是 ping**）。
# 不在内网时经一台能通的跳板走 ProxyJump，与 kiro-pool.py / kiro-probe.py /
# kiro-lane-regress.sh 同一个变量名：
#   export KIRO_SSH_JUMP=pubSrv
# 不设则保持原来的直连行为。
# 🔴 10-10 踩过：这个脚本是四件套里唯一硬写 `ssh HOST` 的，掉出内网时它第一步就
# `Connection refused`，而同一轮 kiro-pool.py 能照常跑 ⇒ 看着像「只有筛子坏了」。
_JUMP = os.environ.get("KIRO_SSH_JUMP", "").strip()
SSH = ["ssh"] + (["-J", _JUMP] if _JUMP else []) + [HOST]

# 零宽 / 双向控制 / NBSP / BOM —— 凭据里粘上这些会在出网前就抛或静默 401
INVISIBLE = re.compile(r"[​-‏‪-‮⁠﻿ ]")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                     r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
HEX64_RE = re.compile(r"[0-9a-fA-F]{64}")

# 与 kiro-pool.py ADD_FIELDS 一致 —— POST /credentials 只认这些
ADD_FIELDS = ("refreshToken", "authMethod", "clientId", "clientSecret",
              "region", "authRegion", "apiRegion", "machineId", "email")


def sha(s: str | None) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()


def auth_method_of(src: dict) -> str:
    """与 kiro.rs `token_manager.rs:122-138` 对齐；服务端 default 是 social，别无脑 idc。"""
    am = (src.get("authMethod") or "").strip()
    if not am:
        am = "idc" if (src.get("clientId") and src.get("clientSecret")) else "social"
    am = am.lower()
    return am if am in ("idc", "builder-id", "iam") else "social"


def disk_pool() -> list[dict]:
    """盘上那份 credentials.json —— 判重的基准，也是「线上字段集」的真源。"""
    cmd = (f"sudo kubectl -n {NS} exec deploy/kiro-rs -c kiro-rs -- "
           "cat /app/config/credentials.json")
    r = subprocess.run(SSH + [cmd], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"🔴 读线上 credentials.json 失败：{r.stderr[:300]}")
    d = json.loads(r.stdout)
    return d if isinstance(d, list) else [d]


def classify(exp: list[dict], live: list[dict]) -> tuple[list, list]:
    """三把判据逐号比对，返回 (新增, 已在池里)。

    🔴 **先断言 live 非空**：空集比对是「一路放行」的假绿，和一把好尺子长得一样
    （09-17 手搓时打出过「相同: False（比对了 0 个）」）。
    """
    if not live:
        sys.exit("🔴 线上读到 0 条凭据 ⇒ 判重是空集假绿，先查清 PVC 再加号")
    by_rt = {sha(c["refreshToken"]): c for c in live if c.get("refreshToken")}
    by_mid = {(c.get("machineId") or "").lower(): c
              for c in live if c.get("machineId")}
    by_mail = {c["email"]: c for c in live if c.get("email")}
    new, dup = [], []
    for i, a in enumerate(exp, 1):
        hits = []
        if sha(a.get("refreshToken")) in by_rt:
            hits.append(("rt_sha", by_rt[sha(a["refreshToken"])].get("id")))
        mid = (a.get("machineId") or "").lower()
        if mid and mid in by_mid:
            hits.append(("machineId", by_mid[mid].get("id")))
        if a.get("email") in by_mail:
            hits.append(("email", by_mail[a["email"]].get("id")))
        (dup if hits else new).append((i, a, hits))
    return new, dup


def check_invisible(label: str, s: str) -> None:
    m = INVISIBLE.search(s)
    if m:
        sys.exit(f"🔴 {label} 含不可见字符 U+{ord(m.group()):04X} @pos{m.start()} "
                 "⇒ 这类字符会让请求在出网前就抛，或静默 401。先清理导出再来。")
    for ch in s:
        if not ch.isprintable():
            sys.exit(f"🔴 {label} 含不可打印字符 U+{ord(ch):04X}")


def flatten(a: dict, live_keys: set[str]) -> dict:
    """拍平成线上形状并过所有门。任一条不过就 exit —— 批量时宁可一个都不写。"""
    mail = a.get("email", "<无 email>")
    am = auth_method_of(a)
    flat: dict = {"email": a.get("email"), "refreshToken": a.get("refreshToken"),
                  "authMethod": am, "machineId": a.get("machineId")}
    if not flat["refreshToken"]:
        sys.exit(f"🔴 {mail} 没有 refreshToken")
    if am == "idc":
        for k in ("clientId", "clientSecret"):
            if not a.get(k):
                sys.exit(f"🔴 {mail} authMethod=idc 却缺 {k} —— "
                         "AWS OIDC 那条路第一件事就是 ok_or_else(IdC 刷新需要 clientId)")
            flat[k] = a[k]
        if a["clientSecret"].count(".") != 2:
            sys.exit(f"🔴 {mail} clientSecret 不是三段 JWT "
                     f"（{a['clientSecret'].count('.') + 1} 段）⇒ 大概率被 heredoc 截断过")
        if not a.get("region"):
            sys.exit(f"🔴 {mail} authMethod=idc 却没有 region")
        for k in ("region", "authRegion", "apiRegion"):
            flat[k] = a["region"]
    else:
        # 🔴 social：缺 client 凭据是**正常形状**，不是残缺导出。region 也是 null ⇒
        # 这三个字段一律不传，让服务端回落 config.json 全局值（与线上 id25 落盘一致）。
        if a.get("clientId") or a.get("clientSecret"):
            sys.exit(f"🔴 {mail} 判成 social 却带着 client 凭据 —— 形状矛盾，先查清")
    # 门：字段集 ⊆ 线上实际键集合（现读，不硬编）
    extra = set(flat) - live_keys
    if extra:
        sys.exit(f"🔴 {mail} 多出线上没有的字段 {extra}（少一个字段不报错但行为会变）")
    # 门：machineId 形状（normalize_machine_id 只认 UUID 或 64hex）
    mid = (flat.get("machineId") or "").strip()
    if not mid:
        # 服务端派生式，钉死而不是留给它每次算（social 轮转后会漂）
        flat["machineId"] = sha(f"KotlinNativeAPI/{flat['refreshToken']}")
        print(f"#   {mail} machineId 缺失 ⇒ 按 kiro.rs 同一条规则派生并钉死")
    elif not (UUID_RE.fullmatch(mid) or HEX64_RE.fullmatch(mid)):
        sys.exit(f"🔴 {mail} machineId 形状不认（要 UUID 或 64hex）：{mid}")
    # 门：无不可见字符
    for k, v in flat.items():
        if isinstance(v, str):
            check_invisible(f"{mail}.{k}", v)
    return flat


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", help="账号管理器导出的 json（整库快照）")
    ap.add_argument("--out", help="拍平到这个目录（建 <out>/creds/，0600）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只比对不写文件 —— 先用这个看看有几个是新的")
    a = ap.parse_args()
    if not a.dry_run and not a.out:
        sys.exit("要么 --dry-run，要么给 --out <目录>")

    src = json.loads(Path(a.export).expanduser().read_text(encoding="utf8"))
    if isinstance(src, dict) and "accounts" in src:
        src = src["accounts"]
    if not isinstance(src, list):
        src = [src]
    print(f"# 导出里 {len(src)} 个号")

    live = disk_pool()
    live_keys: set[str] = set()
    for c in live:
        live_keys |= set(c.keys())
    print(f"# 线上 {len(live)} 个号，字段集 {len(live_keys)} 键")

    new, dup = classify(src, live)

    print(f"\n=== 已在池里（跳过）{len(dup)} 个 ===")
    for i, acct, hits in dup:
        shapes = "+".join(k for k, _ in hits)
        ids = sorted({str(v) for _, v in hits})
        print(f"  #{i:2d} {acct.get('email', '?'):34s} 命中 {shapes:28s} → id {','.join(ids)}")
    if dup:
        only_mail = [m for _, m, h in dup if [k for k, _ in h] == ["email"]]
        if only_mail:
            print(f"  🔴 其中 {len(only_mail)} 个**只有 email 命中**"
                  f"（rt 与 machineId 都漂了，social 刷新过）"
                  f"⇒ 三把判据少一把就会漏进去")

    print(f"\n=== 新增 {len(new)} 个 ===")
    for i, acct, _ in new:
        print(f"  #{i:2d} {acct.get('email', '?'):34s} {auth_method_of(acct)}")
    if not new:
        print("  （这份导出里没有新号 —— 整库快照全都已经在池里了）")
        return

    # 组内互查：三把判据在**组内**也要比，别自己跟自己重
    seen: dict[tuple[str, str], int] = {}
    for i, acct, _ in new:
        for kind, val in (("rt", sha(acct.get("refreshToken"))),
                          ("mid", (acct.get("machineId") or "").lower()),
                          ("mail", acct.get("email") or "")):
            if not val:
                continue
            if (kind, val) in seen:
                sys.exit(f"🔴 组内重复 {kind}：#{i} 与 #{seen[(kind, val)]} 相同 —— "
                         "同一个号在导出里出现了两次，先去掉一份")
            seen[(kind, val)] = i
    print(f"\n✅ 组内三把判据互不相同（{len(new)} 个号）")

    flats = [(acct, flatten(acct, live_keys)) for _, acct, _ in new]
    print(f"✅ 拍平门禁全过（{len(flats)} 份）")
    from collections import Counter
    print(f"   authMethod 分布：{dict(Counter(f['authMethod'] for _, f in flats))}")

    if a.dry_run:
        print("\n### dry-run：没有写任何文件")
        return

    out = Path(a.out).expanduser()
    creds = out / "creds"
    creds.mkdir(parents=True, exist_ok=True)
    for acct, flat in flats:
        slug = (acct.get("email") or "x").split("@")[0].replace(".", "_")
        p = creds / f"{slug}.json"
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf8") as f:
            json.dump(flat, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 写了 {len(flats)} 份到 {creds}/ (0600)")
    print(f"⚠️ 里面是 live refreshToken，加完立刻 rm -rf {out}")
    print("\n下一步（**别跳过验号**，这个脚本不打 AWS）：")
    print(f"  for f in {creds}/*.json; do")
    print("    am=$(python3 -c \"import json,sys;print(json.load(open(sys.argv[1]))"
          "['authMethod'])\" \"$f\")")
    print(f"    # social 必带 --out-rotated，并拿轮转后那份去 add")
    print("    python3 scripts/kiro-probe.py both --creds \"$f\" "
          "${am:+--out-rotated \"${f%.json}.rotated.json\"}")
    print("  done")
    print("  # 验完逐个 add，--priority 对齐在役号（先 show 看它是几）")


if __name__ == "__main__":
    main()
