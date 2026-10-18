#!/usr/bin/env python3
"""三方核对：飞书凭据表 × 本机 AcctMgr × 198 kiro 池 —— 判「这号到底加了没」。

为什么要有这个脚本（2026-10-19 踩出来的）
----------------------------------------
用户问「表里 59~81 行加没加」，我按**邮箱**比对飞书表和池子，报了
「5 个有缺口」。其中 4 个（laetitia/fanny/anisa/dameria）**其实早在池里**
（id49~52）—— social 号在池里存的邮箱被改过标签（binus→outlook，见
`feedback_export_email_relabel_looks_like_a_new_account`），按邮箱匹配必然漏判。
拿 refreshToken 的 sha256 一比就对上了。

⇒ **判在不在池里，邮箱是三把判据里最弱的一把**，必须和 rt_sha256 + machineId
一起用（与 `kiro-pool.py dup_check()` / `kiro-batch-prepare.py` 同一套判据）。

第二件事：**飞书「添加状态」列是人填的，不可信**。同一轮核查里 126 行有 44 行
对不上实际：20 个标「待处理」的其实早加好了，3 个标「已添加」的两边都没有。
所以这个脚本只输出「实际」，并把表里写的状态并排打出来给人看差异。

用法
----
    # 全表核对
    python3 scripts/kiro-account-reconcile.py

    # 只看某几行（1-indexed，按 record-list 导出顺序）
    python3 scripts/kiro-account-reconcile.py --rows 59-81

    # 导出可直接喂 lark-cli 的状态修正 payload（不自动写）
    python3 scripts/kiro-account-reconcile.py --emit-fix fix.json

判据优先级：email → rt_sha256 → machineId，命中任一即「在池」，输出里标明靠哪把。
⚠️ 读不到线上凭据就直接退出，不出「全都不在池」的空集假绿。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ACCOUNTS = Path.home() / "Library/Application Support/.kiro-account-manager/accounts.json"
BASE_TOKEN = "IhSNbXxl5azo9xsKxyhcO0lEnnO"
TABLE_ID = "tbludyQfSIZq6pLM"


def sha(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()


def mail_of(raw) -> str:
    """表里「账户」列可能是 `[x@y](mailto:x@y)` 这种 markdown 链接形态。"""
    m = re.search(r"[\w.+-]+@[\w.-]+", str(raw or ""))
    return m.group(0).lower() if m else ""


def disk_pool() -> list[dict]:
    """复用 kiro-batch-prepare.py 的线上读法，别自己再写一遍 ssh+kubectl。"""
    spec = importlib.util.spec_from_file_location(
        "bp", str(REPO / "scripts" / "kiro-batch-prepare.py"))
    bp = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(bp)
    except SystemExit:
        pass
    return bp.disk_pool()


def lark_rows(cache: Path | None) -> list[dict]:
    if cache and cache.exists():
        return [json.loads(l) for l in cache.read_text().splitlines() if l.strip()]
    out = Path(os.environ.get("TMPDIR", "/tmp")) / "kiro-reconcile-lark.ndjson"
    r = subprocess.run(
        ["lark-cli", "base", "+record-list", "--base-token", BASE_TOKEN,
         "--table-id", TABLE_ID, "--format", "ndjson", "--output", str(out),
         "--overwrite", "--as", "user"],
        capture_output=True, text=True)
    if not out.exists():
        sys.exit(f"🔴 读飞书表失败：{(r.stderr or r.stdout)[:300]}")
    out.chmod(0o600)
    return [json.loads(l) for l in out.read_text().splitlines() if l.strip()]


def build_index(live: list[dict]) -> dict:
    """rt_sha / machineId / email 三把判据各建一张表（与 dup_check 同一套）。"""
    idx = {"rt": {}, "mid": {}, "mail": {}}
    for c in live:
        ref = {"id": c.get("id"), "email": (c.get("email") or "").lower()}
        if c.get("refreshToken"):
            idx["rt"][sha(c["refreshToken"])] = ref
        if c.get("machineId"):
            idx["mid"][c["machineId"].lower()] = ref
        if c.get("email"):
            idx["mail"][c["email"].lower()] = ref
    return idx


def locate(email: str, am_entry: dict | None, idx: dict) -> tuple[str | None, str | None]:
    """返回 (pool_id, 靠哪把判据)。email 最弱，放最后兜底也要试前两把。"""
    if email in idx["mail"]:
        return idx["mail"][email]["id"], "email"
    if am_entry:
        k = sha(am_entry.get("refreshToken") or "")
        if k in idx["rt"]:
            return idx["rt"][k]["id"], "rt_sha"
        mid = (am_entry.get("machineId") or "").lower()
        if mid and mid in idx["mid"]:
            return idx["mid"][mid]["id"], "machineId"
    return None, None


def parse_rows(spec: str, total: int) -> set[int]:
    want: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            want |= set(range(int(a), int(b) + 1))
        else:
            want.add(int(part))
    return {n for n in want if 1 <= n <= total}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", help="只核对这些行，如 59-81 或 3,7,12-15（1-indexed）")
    ap.add_argument("--lark-cache", type=Path, help="已有的 record-list ndjson，省一次导出")
    ap.add_argument("--accounts", type=Path, default=ACCOUNTS)
    ap.add_argument("--emit-fix", type=Path,
                    help="把状态修正写成 lark-cli +record-batch-update 的 --json（不自动写表）")
    a = ap.parse_args()

    live = disk_pool()
    if not live:
        sys.exit("🔴 线上读到 0 条凭据 ⇒ 全判「不在池」是空集假绿，先查 PVC")
    idx = build_index(live)
    am_list = json.loads(a.accounts.read_text())
    am = {(x.get("email") or "").lower(): x for x in am_list}
    rows = lark_rows(a.lark_cache)
    pick = parse_rows(a.rows, len(rows)) if a.rows else None

    print(f"# 飞书 {len(rows)} 行 / AcctMgr {len(am_list)} / 池 {len(live)}")
    print(f"# 判据：email → rt_sha256 → machineId（email 最弱，social 号改标签后会漏判）\n")
    print(f"{'行':>4} {'邮箱':40} {'表状态':8} {'实际':6} {'池id':>5} {'判据':9} AcctMgr")

    fix: dict[str, dict] = {}
    weak = []
    stat = {"一致": 0, "表说加了实际没有": 0, "表说没加实际有": 0}
    for i, r in enumerate(rows, 1):
        if pick and i not in pick:
            continue
        email = mail_of(r.get("账户"))
        if not email:
            continue
        said = (r.get("添加状态") or ["(空)"])[0]
        entry = am.get(email)
        pid, how = locate(email, entry, idx)
        truth = "已添加" if pid else "待处理"
        if how and how != "email":
            weak.append((i, email, pid, how))
        ams = (entry or {}).get("status", "-") if entry else "不在"
        flag = "" if said == truth else "  ←"
        print(f"{i:>4} {email:40} {said:8} {truth:6} {str(pid or '-'):>5} "
              f"{how or '-':9} {ams}{flag}")
        if said == truth:
            stat["一致"] += 1
        elif truth == "待处理":
            stat["表说加了实际没有"] += 1
        else:
            stat["表说没加实际有"] += 1
        if said != truth:
            note = (r.get("备注") or "").strip()
            why = (f"已在 198 池 id={pid}"
                   + (f"（池里邮箱已改标签，靠 {how} 匹配）" if how and how != "email" else "")
                   ) if pid else (
                   "不在池。AcctMgr token 已失效(status=invalid)，需重登"
                   if ams == "invalid" else
                   "不在池（AcctMgr 里有条目）" if entry else
                   "不在池，也不在 AcctMgr（从未加过）")
            fix[r["record_id"]] = {"添加状态": [truth],
                                   "备注": (note + " | " if note else "") + f"核查: {why}"}

    print("\n# 汇总：" + "  ".join(f"{k}={v}" for k, v in stat.items()))
    if weak:
        print(f"\n🔴 这 {len(weak)} 行按邮箱会漏判，靠 rt_sha/machineId 才匹配上：")
        for i, e, pid, how in weak:
            print(f"   行{i} {e} → id{pid}（{how}）")
    if a.emit_fix and fix:
        a.emit_fix.write_text(json.dumps({"update_records": fix}, ensure_ascii=False, indent=1))
        a.emit_fix.chmod(0o600)
        print(f"\n修正 payload（{len(fix)} 行）-> {a.emit_fix}")
        print("  写表：lark-cli base +record-batch-update --base-token "
              f"{BASE_TOKEN} --table-id {TABLE_ID} --json \"$(cat {a.emit_fix})\" --as user")
        print("  🔴 写完必须重读复核（+record-batch-create/update 回 ok=true 也可能空结果）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
