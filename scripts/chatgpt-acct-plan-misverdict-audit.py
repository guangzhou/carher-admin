#!/usr/bin/env python3
"""chatgpt-acct-plan-misverdict-audit.py — 找出「可能被误判成 free」的 acct。

2026-10-10 起因：我把 acct-227 报成「已掉 free」，依据是 pod 内 id_token 的
`chatgpt_plan_type=free`。**那是 10-05 签发时刻的冻结快照，不是现在的档位**，
而该号 token 已过期 ⇒ 实探拿不到档位 ⇒ 快照成了唯一可见的"档位"。用户纠正：227 是 pro。

误判的结构：
    实探 401（探不到档位） + JWT 快照说 free  ⇒  读者只能看到 "free"
这个形状不是 227 独有的。本脚本把它批量找出来，**只分类、不给档位结论**。

用法：
    python3 chatgpt-acct-plan-misverdict-audit.py            # 全量审计
    python3 chatgpt-acct-plan-misverdict-audit.py --json      # 结构化输出

⚠ 本脚本**只读**。它不打 refresh grant —— RT 成功即轮转、必须写回，没有只读版本，
那是一次凭据写入，必须由人决定（见 skill chatgpt-acct-token-expiry-triage）。

## 为什么不能用「打过 gpt-6 就是 pro」当判据

2026-10-10 实测对照组推翻了这条：**acct-120 实探确认是 `free`，却有 591 次
gpt-6 success（最近一次 10-10）**。所以"有 pro-only 成功"**不是**充分判据，
只能当强度信号。可能的解释（未验证，故不写进结论）：档位变化与流量有时间差、
或 model 列记的是入口名而非真正落到上游的模型。

**唯一无歧义的判据仍是实探**（refresh grant 拿到新 token 后打 /codex/usage）。
本脚本的产出是「该去实探谁」的优先级，不是档位答案本身。
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time

HOST_198 = "cltx@10.68.13.198"
NS = "litellm-product"
# 实探确认 free 的号在本窗口的 gpt-6 success 上界（2026-10-10 实测 acct-120=591）。
# 超过它**不代表**该号是 pro，只代表「这个号的 pro-only 流量强度已经不在 free 档
# 观测到的区间里」—— 是排序信号，不是判据。
FREE_BAND_MAX = 591

POD_JWT_SWEEP = r'''
import base64, json, os, re, subprocess, time
from concurrent.futures import ThreadPoolExecutor
os.environ["KUBECONFIG"] = os.path.expanduser("~/.kube/config")
NS = "litellm-product"
PAT = re.compile(r"^chatgpt-acct-(\d+)$")
pods = json.loads(subprocess.check_output(
    ["kubectl", "-n", NS, "get", "pod", "-o", "json"], text=True, timeout=60))
targets = {}
for it in pods.get("items", []):
    app = ((it.get("metadata") or {}).get("labels") or {}).get("app", "")
    m = PAT.match(app)
    if not m or (it.get("status") or {}).get("phase") != "Running":
        continue
    targets["acct-" + m.group(1)] = it["metadata"]["name"]

def claims(tok):
    try:
        s = tok.split(".")[1]; s += "=" * (-len(s) % 4)
        return json.loads(base64.urlsafe_b64decode(s))
    except Exception:
        return {}

def one(item):
    acct, pod = item
    try:
        raw = subprocess.check_output(
            ["kubectl", "-n", NS, "exec", pod, "--", "sh", "-c",
             "stat -c %Y /chatgpt-auth/auth.json; cat /chatgpt-auth/auth.json"],
            text=True, timeout=20)
        mt, _, body = raw.partition("\n")
        a = json.loads(body)
        idc = claims(a.get("id_token") or "")
        au = idc.get("https://api.openai.com/auth") or {}
        return acct, {"mtime": float(mt.strip()) if mt.strip() else None,
                      "id_iat": idc.get("iat"),
                      "jwt_plan": au.get("chatgpt_plan_type"),
                      "jwt_sub_until": au.get("chatgpt_subscription_active_until")}
    except Exception as e:
        return acct, {"err": type(e).__name__}

out = {}
with ThreadPoolExecutor(max_workers=12) as ex:
    for acct, info in ex.map(one, sorted(targets.items())):
        out[acct] = info
print(json.dumps({"now": int(time.time()), "n_pods": len(targets), "accts": out}))
'''


def ssh_json(code: str, *, label: str) -> dict:
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no",
         HOST_198, "python3", "-"],
        input=code, capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0 or not r.stdout.strip():
        print(f"[{label}] 取数失败 rc={r.returncode} {(r.stderr or '')[-200:]}",
              file=sys.stderr)
        return {}
    return json.loads(r.stdout)


def spend_by_served_model(accts: list[str]) -> dict[str, dict]:
    """按 **`model`（真正落到上游的模型）** 而非 model_id（腿名）统计。

    2026-10-10 踩到：leg `chatgpt-acct-227-gpt-6-sol` 的行里 `model` 有
    `chatgpt-gpt-5.6-luna` —— 腿名和实际服务的模型**不是一回事**，按腿名数会
    把 5.6 的流量算进 gpt-6。
    """
    if not accts:
        return {}
    nums = [a.split("-")[-1] for a in accts]
    pat = ",".join("'chatgpt-acct-%s-%%'" % n for n in nums)
    sql = f"""SELECT substring(model_id from 'acct-[0-9]+') a,
 count(*) FILTER (WHERE status='success' AND model LIKE '%gpt-6%') g6_ok,
 max("startTime") FILTER (WHERE status='success' AND model LIKE '%gpt-6%')::date g6_last,
 count(*) FILTER (WHERE status='success') all_ok,
 max("startTime") FILTER (WHERE status='success')::date all_last
FROM "LiteLLM_SpendLogs" WHERE model_id LIKE ANY (ARRAY[{pat}])
 AND "startTime" > now() - interval '21 days' GROUP BY 1;"""
    # psql 经 ssh：判成败看有无 `ERROR:`，不看 rc（rc 恒 0）。
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no", HOST_198,
         "export KUBECONFIG=~/.kube/config; kubectl -n %s exec -i litellm-db-0 -- "
         "psql -U litellm -d litellm -t -A -F'|' -f /dev/stdin" % NS],
        input=sql, capture_output=True, text=True, timeout=300,
    )
    if "ERROR:" in (r.stdout + r.stderr):
        print(f"[spend] psql 报错: {(r.stdout + r.stderr)[-300:]}", file=sys.stderr)
        return {}
    out: dict[str, dict] = {}
    for line in r.stdout.splitlines():
        p = line.split("|")
        if len(p) < 5 or not p[0].startswith("acct-"):
            continue
        out[p[0]] = {"g6_ok": int(p[1] or 0), "g6_last": p[2] or None,
                     "all_ok": int(p[3] or 0), "all_last": p[4] or None}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows-json", default="/tmp/chatgpt-acct-quota-rows.json",
                    help="已有的 quota rows（提供 live_probe/plan 两列）")
    ap.add_argument("--json", action="store_true", help="结构化输出")
    args = ap.parse_args()

    try:
        rows = {r["acct"]: r for r in
                json.load(open(args.rows_json))["rows"]}
    except Exception as e:
        print(f"!!! 读不到 {args.rows_json}: {e} —— 先跑一轮 "
              f"chatgpt-acct-quota.sh --json-rows", file=sys.stderr)
        return 2

    sw = ssh_json(POD_JWT_SWEEP, label="jwt")
    if not sw:
        return 1
    now = sw["now"]

    # 分三类。关键是第三类：JWT 说 free 但实探探不到 —— 227 所在的那一类。
    live_free, live_pro, suspect = [], [], []
    for acct, v in sw["accts"].items():
        if v.get("err"):
            continue
        r = rows.get(acct) or {}
        live_plan, probe = r.get("plan"), r.get("live_probe")
        if probe == "live" and live_plan:
            (live_free if live_plan == "free" else live_pro).append(acct)
        elif v.get("jwt_plan") == "free":
            suspect.append(acct)

    spend = spend_by_served_model(suspect + live_free)
    key = lambda a: int(a.split("-")[1])

    def band(acct: str) -> str:
        g6 = (spend.get(acct) or {}).get("g6_ok") or 0
        return "ABOVE_FREE_BAND" if g6 > FREE_BAND_MAX else "IN_FREE_BAND"

    suspect.sort(key=lambda a: -((spend.get(a) or {}).get("g6_ok") or 0))
    result = {
        "checked_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)),
        "n_running_pods": sw["n_pods"],
        "live_free": sorted(live_free, key=key),
        "live_pro": sorted(live_pro, key=key),
        "free_band_max_g6_ok": FREE_BAND_MAX,
        "suspects": [
            {"acct": a, "jwt_plan": "free",
             "live_probe": (rows.get(a) or {}).get("live_probe"),
             "jwt_age_days": round((now - (sw["accts"][a].get("id_iat") or now)) / 86400, 1),
             "g6_ok": (spend.get(a) or {}).get("g6_ok") or 0,
             "g6_last": (spend.get(a) or {}).get("g6_last"),
             "all_ok": (spend.get(a) or {}).get("all_ok") or 0,
             "band": band(a)}
            for a in suspect
        ],
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    print(f"# 档位误判审计 @ {result['checked_at']}  "
          f"（{result['n_running_pods']} 个 Running pod）")
    print(f"\n实探确认 free ({len(result['live_free'])}): "
          f"{' '.join(result['live_free'])}")
    print(f"实探确认 pro  ({len(result['live_pro'])}): "
          f"{' '.join(result['live_pro'])}")
    print(f"\n⚠ JWT 快照说 free 但**实探探不到档位** —— 与 acct-227 同形 "
          f"({len(result['suspects'])} 个)。**这些号的档位目前是未知，不是 free**：\n")
    print(f"{'acct':<11} {'live_probe':<20} {'JWT龄':<7} {'gpt6_ok':>8} "
          f"{'g6_last':<11} {'band'}")
    for s in result["suspects"]:
        print(f"{s['acct']:<11} {str(s['live_probe']):<20} {s['jwt_age_days']:<7} "
              f"{s['g6_ok']:>8} {str(s['g6_last'] or '-'):<11} {s['band']}")
    above = [s for s in result["suspects"] if s["band"] == "ABOVE_FREE_BAND"]
    print(f"\n→ {len(above)} 个的 gpt-6 成功量超出实探-free 档观测区间上界 "
          f"({FREE_BAND_MAX})：{' '.join(s['acct'] for s in above)}")
    print("  这是**排序信号不是判据** —— 实探确认 free 的 acct-120 也有 591 次 "
          "gpt-6 success。判档位只有实探一条路（refresh grant → /codex/usage）。")
    print("\n⚠ 本脚本不打 refresh grant：RT 成功即轮转、必须写回，属凭据写入，"
          "由人决定（skill chatgpt-acct-token-expiry-triage）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
