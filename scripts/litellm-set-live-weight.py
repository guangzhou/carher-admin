#!/usr/bin/env python3
"""litellm-set-live-weight.py — 立刻把 198 pro 池某些 acct 的 live router entry weight
推到指定值（quota-rebalance weight-align 的手动加速器）。

背景（2026-08-23 实证）：`quota-rebalance.py` 的 weight-align 只对**本 tick 实际 probe 的
online 号** PATCH weight —— align 块在 per-acct 循环里、位于 `should_probe`→`continue` 之后。
7d<50% 的低频号命中 `SKIP (7d=X%, <25min)` 分支直接 continue、**跳过 align**。所以刚 resume /
刚改 desired_weight 的低频号（撞顶 redeem 后 7d=0% 就是这一类）live entry weight 会停在
`None`(=LiteLLM 默认 1) 直到它下次真 probe(~25min)或 6h 强制探。表现：改了 state.json
desired_weight，但 `/model/info` 里 weight 仍是旧值/None、流量不跟权重走。

本脚本直接 PATCH `/model/{id}/update` 把匹配 acct 的所有 live entry weight 推到目标值，
当场生效，无需等 cron。

⚠️ 不变量（见 chatgpt-quota-rebalance skill §desired_weight 持久化）：
  state.json 的 `desired_weight` 才是**权威**；本脚本只加速当前传播。
  目标 weight **必须 == 该 acct 的 desired_weight**，否则下个 cron tick 的 weight-align
  发现 live ≠ desired 会把它覆盖回 desired_weight。
  正确顺序：① 188 state.json 设 desired_weight=W（权威）→ ② 本脚本把 live 推到 W（加速）。
  设成 == desired_weight 是"不要直接改 weight"规则的唯一安全例外（值相同 → align 判等跳过）。

用法（本地跑，走 jms → 198 AIYJY-litellm host 的 localhost:30402）：
  python3 scripts/litellm-set-live-weight.py --weight 100 81 82 83 84 85 89 90 91
  python3 scripts/litellm-set-live-weight.py --weight 100 --dry-run 84 91   # 只列将改的 entry
env：LITELLM_PRO_MK（缺省用 skill 记录的 pro master key，可 override）
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys

HOST = os.environ.get("LITELLM_PRO_HOST", "AIYJY-litellm")
PRO_BASE = os.environ.get("LITELLM_PRO_BASE", "http://localhost:30402/pro")
def _require_env(name: str) -> str:
    """凭据只从环境变量读，缺了直接退出（不内置默认值，避免真 key 落进仓库）。"""
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(
            "缺少环境变量 %s —— 先 export %s=<198 prod master key>（别写进文件/命令行历史）" % (name, name)
        )
    return v

MK = _require_env("LITELLM_PRO_MK")

# 在 198 host 上跑的 payload：GET /model/info → 过滤 chatgpt-acct-N-* → PATCH weight。
# accts/weight/dry 经环境变量传入，避免 shell 引号地狱。
# ⚠️ 2026-09-07 修：本 payload 必须走 **stdin heredoc**（`python3 - <<'EOF'`）下发，
# 不能用 `python3 -c "<json.dumps 出来的字符串>"`。jms ssh 把命令串交给远端 sh，
# 双引号内的 `\n` 原样保留成字面反斜杠+n，python3 -c 收到后首行就
# `SyntaxError: unexpected character after line continuation character`。
REMOTE = r'''
import json, os, urllib.request, urllib.error
BASE=os.environ["_BASE"]; MK=os.environ["_MK"]
accts=set(os.environ["_ACCTS"].split()); W=int(os.environ["_W"]); DRY=os.environ.get("_DRY")=="1"
def call(method, path, body=None):
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(BASE+path, data=data, method=method,
        headers={"Authorization":"Bearer "+MK, "Content-Type":"application/json"})
    try:
        r=urllib.request.urlopen(req, timeout=20); return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
st, raw = call("GET", "/v1/model/info")
if st!=200:
    print("MODEL_INFO_HTTP_%d"%st); raise SystemExit(3)
d=json.loads(raw)
prefixes=tuple("chatgpt-acct-%s-"%n for n in accts)
seen=set(); todo=[]
for e in d.get("data", []):
    mid=(e.get("model_info") or {}).get("id","")
    if mid.startswith(prefixes) and mid not in seen:
        seen.add(mid)
        cur=(e.get("litellm_params") or {}).get("weight")
        todo.append((mid, cur))
todo.sort()
ok=bad=noop=0
for mid, cur in todo:
    if cur==W:
        noop+=1; print("  noop  %-40s w=%s"%(mid, cur)); continue
    if DRY:
        print("  would %-40s %s->%s"%(mid, cur, W)); continue
    s2,_=call("PATCH", "/model/%s/update"%mid, {"litellm_params":{"weight":W}})
    if s2==200: ok+=1;  print("  set   %-40s %s->%s HTTP200"%(mid, cur, W))
    else:       bad+=1; print("  FAIL  %-40s HTTP%s"%(mid, s2))
print("accts=%d entries=%d set=%d noop=%d fail=%d dry=%s"%(len(accts), len(todo), ok, noop, bad, DRY))
'''


def jms(script_dir: str):
    j = os.path.join(script_dir, "jms")
    return j if os.access(j, os.X_OK) else "jms"


def main() -> int:
    ap = argparse.ArgumentParser(description="force live router weight for pro pool accts")
    ap.add_argument("accts", nargs="+", help="acct 编号（裸数字，如 81 82 ...）")
    ap.add_argument("--weight", type=int, required=True, help="目标 weight（必须 == desired_weight）")
    ap.add_argument("--dry-run", action="store_true", help="只列将改的 entry，不 PATCH")
    args = ap.parse_args()

    accts = " ".join(a.replace("acct-", "").strip() for a in args.accts)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_prefix = (
        f"_BASE={PRO_BASE!r} _MK={MK!r} _ACCTS={accts!r} "
        f"_W={args.weight} _DRY={'1' if args.dry_run else '0'} "
    )
    remote_cmd = (
        f"{env_prefix} python3 - <<'__WEIGHT_PY__'\n{REMOTE}\n__WEIGHT_PY__\n"
    )
    print(f"[litellm-set-live-weight] host={HOST} weight={args.weight} "
          f"accts=[{accts}] dry_run={args.dry_run}", file=sys.stderr)
    print("⚠ 目标 weight 必须 == 188 state.json 的 desired_weight，否则会被下个 cron tick 覆盖。",
          file=sys.stderr)
    r = subprocess.run([jms(script_dir), "ssh", HOST, remote_cmd], text=True)
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
