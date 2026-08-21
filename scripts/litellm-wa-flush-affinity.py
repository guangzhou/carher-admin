#!/usr/bin/env python3
"""litellm-wa-flush-affinity.py — flush WA session-affinity pins（在 litellm-proxy pod 内跑）

沉淀自 2026-08-21：把 acct-81..91 的 router weight 设 100、其余设 1 后，实测流量**不跟权重走**
——weight=1 的老热号(acct-108 独吞 321 req/10min)反而吃大头，weight=100 的号成功数接近 0。
根因是 WA session-affinity hook（`weighted_affinity.proxy_handler_instance` +
`optional_pre_call_checks: encrypted_content_affinity`，见 scripts/litellm-weighted-affinity-with-exclusion.py）
把**存量会话钉在"改权重之前"就在服务它的 deployment 上**（120s 滑动 TTL，会话只要持续活跃就一直粘）。
weight（simple-shuffle）只决定**新会话/冷启会话**落哪 → 改完权重看着"流量不对"。

**手法**：删掉 Redis 里的 v2 亲和 pin，存量会话失去 pin → 下一个请求按 weight 重新路由并重建 pin
→ 流量在几分钟内涌向高权号。实测高权 8 号成功份额 12.2% → 80.1%（3min 窗口，仍在向理论 ~97% 收敛）。

⚠️ **只删 `weighted_affinity:v2:*`（会话/key 级 pin）**。默认**保留** `weighted_affinity:fail:v1:*`
（fail-mark 是"避开已知坏号"的保护键，180s 自动过期；删了会把量灌向正在 429 的号）。
其它一切 Redis key（预算/spend/router 状态，占 dbsize 大头）**绝不触碰**。

用法（对齐 litellm-wa-probe.py，在 proxy pod 内跑，用 pod env 的 redis 连通性）:
  POD=$(kubectl -n litellm-product get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
  kubectl -n litellm-product cp scripts/litellm-wa-flush-affinity.py $POD:/tmp/wa-flush.py -c litellm

  # ① DRY-RUN（默认，不删）：按 model_group + 按被 pin 的 deployment 出分布，先看清偏斜
  kubectl -n litellm-product exec $POD -c litellm -- python3 /tmp/wa-flush.py

  # ② 全量 flush（所有 model_group 的 v2 pin）
  kubectl -n litellm-product exec $POD -c litellm -- python3 /tmp/wa-flush.py --apply

  # ③ 只 flush 某个 model_group（例如只想让 terra 组重路由）
  kubectl -n litellm-product exec $POD -c litellm -- python3 /tmp/wa-flush.py --model-group chatgpt-gpt-5.6-terra --apply

  # ④ 连 fail-mark 一起清（少用；确认坏号已修复、想立刻让它重新参与选路时）
  kubectl -n litellm-product exec $POD -c litellm -- python3 /tmp/wa-flush.py --apply --include-fail-marks

  # 用完删除临时脚本（feedback_temp_probe_must_be_inside_model_gate_and_removed）

验收（flush 后等 ~3min 让 pin 重建，再数 acct pod 真实请求分布）:
  for P in $(kubectl -n litellm-product get pod --field-selector=status.phase=Running -o name \
             | grep -oE 'chatgpt-acct-[0-9]+-[a-z0-9]+-[a-z0-9]+' | sort -u); do
    N=$(echo $P | grep -oE 'acct-[0-9]+')
    L=$(kubectl -n litellm-product logs $P --since=3m --tail=4000 2>/dev/null | grep 'POST /responses')
    T=$(echo "$L" | grep -c 'POST /responses'); OK=$(echo "$L" | grep -c ' 200 OK')
    [ "$T" -gt 0 ] && printf '%-10s POST=%-4d 200=%-4d err=%d\n' "$N" "$T" "$OK" "$((T-OK))"
  done | sort -t- -k2 -n
  # 判据：高权号成功数应显著超过 weight-1 号；死号(paused)仍为 0（weight 再高也接不到量）。
"""
import argparse
import json
import os
import sys

V2_PREFIX = "weighted_affinity:v2:"
FAIL_PREFIX = "weighted_affinity:fail:v1:"


def _connect(host, port):
    import redis
    return redis.Redis(host=host, port=port, socket_timeout=6)


def _match(model_group):
    # model_group 不含冒号(如 chatgpt-gpt-5.6-terra) → 前缀精确到组即可
    return f"{V2_PREFIX}{model_group}:*" if model_group else f"{V2_PREFIX}*"


def _group_of(key_str):
    # weighted_affinity:v2:{model_group}:{user_hash}[:s:{fp}] → parts[2]
    parts = key_str.split(":")
    return parts[2] if len(parts) > 2 else "?"


def main():
    ap = argparse.ArgumentParser(description="flush WA v2 session-affinity pins")
    ap.add_argument("--apply", action="store_true",
                    help="真正删除（缺省=DRY-RUN 只报告分布）")
    ap.add_argument("--model-group", default=None,
                    help="只作用于该 model_group（如 chatgpt-gpt-5.6-terra）；缺省=全部")
    ap.add_argument("--include-fail-marks", action="store_true",
                    help="连 weighted_affinity:fail:v1:* 一起清（默认保留）")
    ap.add_argument("--redis-host",
                    default=os.environ.get("WA_REDIS_HOST",
                                           "litellm-redis.litellm-product.svc.cluster.local"))
    ap.add_argument("--redis-port", type=int,
                    default=int(os.environ.get("WA_REDIS_PORT", "6379")))
    args = ap.parse_args()

    r = _connect(args.redis_host, args.redis_port)
    dbsize_before = r.dbsize()

    # ── 扫描 v2 pin，边扫边统计（按 model_group + 按被 pin 的 deployment）───────────
    keys = list(r.scan_iter(match=_match(args.model_group), count=500))
    by_group = {}
    by_dep = {}
    if keys:
        # 值是 {"model_id": dep_id}（或裸串）；批量取回解出 deployment 分布
        vals = r.mget(keys)
        for k, v in zip(keys, vals):
            ks = k.decode() if isinstance(k, bytes) else k
            by_group[_group_of(ks)] = by_group.get(_group_of(ks), 0) + 1
            dep = "?"
            if v:
                vs = v.decode() if isinstance(v, bytes) else v
                try:
                    dep = (json.loads(vs) or {}).get("model_id", vs)
                except Exception:
                    dep = vs
            by_dep[dep] = by_dep.get(dep, 0) + 1

    print(f"redis={args.redis_host}:{args.redis_port} dbsize={dbsize_before}")
    print(f"scope={'model_group='+args.model_group if args.model_group else 'ALL groups'}")
    print(f"v2_affinity_pins matched = {len(keys)}")
    print("── pins by model_group ──")
    for g, c in sorted(by_group.items(), key=lambda x: -x[1]):
        print(f"  {g:<28} {c}")
    print("── pins by pinned deployment (top 15；偏斜就在这里) ──")
    for d, c in sorted(by_dep.items(), key=lambda x: -x[1])[:15]:
        print(f"  {str(d):<40} {c}")

    if not args.apply:
        print("\nDRY-RUN（未删除）。加 --apply 执行 flush。")
        return

    # ── 执行删除：只删已匹配的 v2 pin（按批 delete，绝不 FLUSHALL）───────────────
    deleted = 0
    for i in range(0, len(keys), 200):
        chunk = keys[i:i + 200]
        if chunk:
            deleted += r.delete(*chunk)

    fail_deleted = 0
    if args.include_fail_marks:
        fkeys = list(r.scan_iter(match=f"{FAIL_PREFIX}*", count=500))
        for i in range(0, len(fkeys), 200):
            chunk = fkeys[i:i + 200]
            if chunk:
                fail_deleted += r.delete(*chunk)

    resid = sum(1 for _ in r.scan_iter(match=_match(args.model_group), count=500))
    fail_left = sum(1 for _ in r.scan_iter(match=f"{FAIL_PREFIX}*", count=500))
    print(f"\ndeleted_v2_pins = {deleted}")
    print(f"deleted_fail_marks = {fail_deleted}")
    print(f"residual_v2 = {resid}   (期望 0)")
    print(f"fail_marks_left = {fail_left}")
    print(f"dbsize before/after = {dbsize_before} / {r.dbsize()}")
    if resid != 0:
        print("⚠ 残留非 0 — 可能有并发写入正在重建 pin；正常，重跑一次即可归零。", file=sys.stderr)


if __name__ == "__main__":
    main()
