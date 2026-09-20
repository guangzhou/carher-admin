#!/usr/bin/env python3
"""litellm-key-budget-reset.py — 198 prod：对一把/多把 key 做**全部重置**。

背景（2026-08-22 起 198 上有两套独立记账，拦截提示几乎一样，但清法不同）：
  1) 总额度         —— DB LiteLLM_VerificationToken.spend vs max_budget
  2) 系列日额度 ④   —— redis budget_notice:fam:{gpt53|other}:{token}:{北京日}
只重置总额度（改 max_budget / spend）**碰不到 redis 系列桶**，key 仍会被 ④ 拦
（实证：cursor-zhuge-zlcb 总 spend $18/$500 却因 other 桶 $544/$500 被软拦截）。
本脚本一条命令两套都清，避免"重置了还是不可用"。

运行位置：**198 host**（kubectl 本地可用；clusterIP 可直连 proxy）。
  sshpass ... ssh cltx@10.68.13.198 'echo PW | sudo -S python3 /tmp/litellm-key-budget-reset.py cursor-zhuge-zlcb --apply'
或直接在 198 上跑。默认 dry-run，必须 --apply 才动。

用法：
  # 全部重置（spend=0 + 清所有系列桶），不改限额
  litellm-key-budget-reset.py cursor-zhuge-zlcb --apply
  # 顺带把总额度上限设成 500
  litellm-key-budget-reset.py cursor-zhuge-zlcb --max-budget 500 --apply
  # 多把 / 前缀模糊匹配
  litellm-key-budget-reset.py cursor-zhuge --like --apply
  # 只清系列桶不动 spend（key 没超总额、只是被系列桶拦时）
  litellm-key-budget-reset.py cursor-zhuge-zlcb --keep-spend --apply
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

NS = "litellm-product"
DB_POD = "litellm-db-0"
REDIS_POD = "litellm-redis-0"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def kubectl(args):
    return sh(f"kubectl -n {NS} {args}")


def _db_creds():
    r = kubectl("exec deploy/litellm-proxy -- env")
    for line in r.stdout.splitlines():
        if line.startswith("DATABASE_URL="):
            url = line.split("=", 1)[1]
            # postgresql://user:pw@host:port/db
            pw = url.split("://", 1)[1].split(":", 1)[1].split("@", 1)[0]
            return pw
    sys.exit("FATAL: DATABASE_URL not found on proxy pod")


def _master_key():
    r = kubectl("get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}'")
    import base64
    return base64.b64decode(r.stdout.strip().strip("'")).decode()


def _proxy_base():
    r = kubectl("get svc litellm-proxy -o jsonpath='{.spec.clusterIP}'")
    ip = r.stdout.strip().strip("'")
    return f"http://{ip}:4000"


def psql(pw, sql):
    q = (
        f"exec {DB_POD} -- env PGPASSWORD={pw} psql -U litellm -d litellm "
        f"-h localhost -At -F '|' -c \"{sql}\""
    )
    return kubectl(q)


def redis_cli(*parts):
    quoted = " ".join(f"'{p}'" if " " in str(p) else str(p) for p in parts)
    return kubectl(f"exec {REDIS_POD} -- redis-cli {quoted}")


def find_keys(pw, aliases, like):
    rows = []
    for a in aliases:
        op = "ILIKE" if like else "="
        val = f"%{a}%" if like else a
        sql = (
            "SELECT key_alias, token, ROUND(spend::numeric,4), max_budget, "
            "COALESCE(budget_duration,'') FROM \\\"LiteLLM_VerificationToken\\\" "
            f"WHERE key_alias {op} '{val}' ORDER BY key_alias"
        )
        r = psql(pw, sql)
        for line in r.stdout.strip().splitlines():
            if not line or "|" not in line:
                continue
            ka, tok, spend, mx, dur = line.split("|")
            rows.append({"alias": ka, "token": tok, "spend": spend,
                         "max_budget": mx, "dur": dur})
    # dedup by token
    seen, out = set(), []
    for r in rows:
        if r["token"] in seen:
            continue
        seen.add(r["token"])
        out.append(r)
    return out


def family_keys_for(token, today_only):
    if today_only:
        import datetime
        bj = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y%m%d")
        pats = [f"budget_notice:fam:*:{token}:{bj}"]
    else:
        pats = [f"budget_notice:fam:*:{token}:*"]
    found = []
    for pat in pats:
        r = redis_cli("--scan", "--pattern", pat)
        found += [ln for ln in r.stdout.strip().splitlines() if ln]
    return found


def api_key_update(base, mk, token, payload):
    body = {"key": token}
    body.update(payload)
    req = urllib.request.Request(
        base + "/key/update", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + mk, "Content-Type": "application/json"},
        method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa
        return 0, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("aliases", nargs="+", help="key_alias（可多个）")
    ap.add_argument("--like", action="store_true", help="按前缀/子串 ILIKE 匹配")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    ap.add_argument("--keep-spend", action="store_true", help="不把 spend 归零")
    ap.add_argument("--max-budget", type=float, default=None,
                    help="顺带设新的总额度上限")
    ap.add_argument("--today-only", action="store_true",
                    help="只清今天的系列桶（默认清该 token 所有日期）")
    args = ap.parse_args()

    pw = _db_creds()
    rows = find_keys(pw, args.aliases, args.like)
    if not rows:
        sys.exit("no key matched: " + ", ".join(args.aliases))

    mk = _master_key()
    base = _proxy_base()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== {mode} · {len(rows)} key(s) ===")

    for r in rows:
        tok = r["token"]
        fkeys = family_keys_for(tok, args.today_only)
        fam_vals = []
        for fk in fkeys:
            v = redis_cli("GET", fk).stdout.strip()
            fam_vals.append(f"{fk.split(':')[2]}={v}")
        print(f"\n· {r['alias']}  (token {tok[:12]}…)")
        print(f"    总额度: spend={r['spend']} / max_budget={r['max_budget']} dur={r['dur']}")
        print(f"    系列桶: {', '.join(fam_vals) if fam_vals else '<none>'}")
        if not args.apply:
            actions = []
            if not args.keep_spend:
                actions.append("spend→0")
            if args.max_budget is not None:
                actions.append(f"max_budget→{args.max_budget}")
            actions.append(f"del {len(fkeys)} 系列桶")
            print(f"    将执行: {', '.join(actions)}")
            continue

        # 1) 总额度：走 /key/update（热更新，比 SQL 立即生效）
        payload = {}
        if not args.keep_spend:
            payload["spend"] = 0.0
        if args.max_budget is not None:
            payload["max_budget"] = args.max_budget
        if payload:
            st, b = api_key_update(base, mk, tok, payload)
            ok = st == 200
            print(f"    key/update {payload} -> {st} {'OK' if ok else b[:200]}")

        # 2) 系列桶：redis DEL（立即生效）
        deleted = 0
        for fk in fkeys:
            rc = redis_cli("DEL", fk).stdout.strip()
            deleted += 1 if rc == "1" else 0
        print(f"    redis DEL 系列桶: {deleted}/{len(fkeys)}")

    if args.apply:
        print("\n注意：系列桶 DEL 立即生效；spend 走 /key/update 也是热更新，"
              "但各 proxy 副本 in-memory auth 缓存 ≤60s 才全部刷新。")
    else:
        print("\n（dry-run，未改动。加 --apply 执行。）")


if __name__ == "__main__":
    main()
