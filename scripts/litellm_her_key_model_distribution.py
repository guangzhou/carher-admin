#!/usr/bin/env python3
"""
只读统计：LiteLLM 上 carher-* 虚拟 key 的 models allowlist 分布（及可选 route_policy）。

安全说明
--------
- 仅使用 GET /spend/keys、GET /key/info，以及可选的只读 kubectl exec psql；不会调用 /key/update、/key/delete 等写接口。
- 默认单次拉取 spend/keys；仅在行内缺少 models 时对缺失项并发 GET /key/info（可用 --max-key-info 限制次数）。
- 建议在已 port-forward 到 litellm-proxy 时使用 http://127.0.0.1:4000，减少公网路径上的请求量。

反复验证
--------
- 连续两次拉取 /spend/keys，比对 carher-* 数量与 token 集合是否一致。
- 可选：对随机样本重复 /key/info，比对 models 是否一致。
- 可选：与 PostgreSQL 只读查询结果比对（需 kubectl + litellm-db）。

环境变量
--------
  LITELLM_BASE_URL   默认 http://127.0.0.1:4000
  LITELLM_MASTER_KEY LiteLLM master key（Bearer）

示例
----
  export LITELLM_MASTER_KEY="$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)"
  kubectl port-forward -n carher svc/litellm-proxy 4000:4000 &
  python3 scripts/litellm_her_key_model_distribution.py --verify-rounds 3

  # 与数据库交叉验证（只读）
  python3 scripts/litellm_her_key_model_distribution.py --psql-verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any


def _get_env(key: str, default: str | None = None) -> str | None:
    v = os.environ.get(key, "").strip()
    return v if v else default


def _http_get_json(url: str, master_key: str, timeout: float = 60.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {master_key}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return None
    return json.loads(raw.decode())


def fetch_spend_keys(base_url: str, master_key: str, limit: int) -> list[dict[str, Any]]:
    """GET /spend/keys — 与运维文档一致。"""
    url = f"{base_url.rstrip('/')}/spend/keys?limit={limit}"
    data = _http_get_json(url, master_key)
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected /spend/keys response type: {type(data)}")
    return data


def extract_models_from_row(row: dict[str, Any]) -> list[str] | None:
    """尝试从 spend/keys 单行解析 models（不同 LiteLLM 版本字段名可能不同）。"""
    for k in ("models", "allowed_models", "allowed_models_list"):
        v = row.get(k)
        if isinstance(v, list) and v:
            return [str(x) for x in v]
    return None


def extract_route_policy(row: dict[str, Any]) -> str:
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = None
    if isinstance(meta, dict):
        v = meta.get("litellm_route_policy")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "(none)"


def key_info_models(base_url: str, master_key: str, token: str) -> list[str]:
    """GET /key/info?key=<token>，返回 models 列表。"""
    q = urllib.parse.quote(token, safe="")
    url = f"{base_url.rstrip('/')}/key/info?key={q}"
    data = _http_get_json(url, master_key, timeout=30.0)
    if not isinstance(data, dict):
        raise RuntimeError("key/info: expected object")
    info = data.get("info") if isinstance(data.get("info"), dict) else data
    raw = None
    if isinstance(info, dict):
        raw = info.get("models")
    if raw is None:
        raw = data.get("models")
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw]


def models_fingerprint(models: list[str]) -> str:
    s = json.dumps(sorted(models), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def verify_spend_keys_stable(base_url: str, master_key: str, prefix: str, limit: int, rounds: int) -> None:
    sets: list[set[str]] = []
    counts: list[int] = []
    for r in range(rounds):
        rows = fetch_spend_keys(base_url, master_key, limit)
        carher = [x for x in rows if str(x.get("key_alias") or "").startswith(prefix)]
        tokens = {str(x.get("token") or "") for x in carher if x.get("token")}
        sets.append(tokens)
        counts.append(len(carher))
        if r < rounds - 1:
            time.sleep(0.3)
    if len(set(counts)) != 1:
        raise RuntimeError(f"不稳定：各轮 carher 行数不一致 {counts}")
    if len(set(len(s) for s in sets)) != 1:
        raise RuntimeError("不稳定：各轮 token 集合大小不一致")
    if len(set(tuple(sorted(s)) for s in sets)) != 1:
        raise RuntimeError("不稳定：各轮 token 集合内容不一致")
    print(f"  ✓ /spend/keys 连续 {rounds} 轮一致：{counts[0]} 条 {prefix}*")


def verify_key_info_stable(
    base_url: str,
    master_key: str,
    tokens: list[str],
    sample_n: int,
    inner_rounds: int,
) -> None:
    if not tokens or sample_n <= 0:
        return
    sample = random.sample(tokens, min(sample_n, len(tokens)))
    for t in sample:
        seq = []
        for _ in range(inner_rounds):
            seq.append(tuple(sorted(key_info_models(base_url, master_key, t))))
            time.sleep(0.05)
        if len(set(seq)) != 1:
            raise RuntimeError(f"/key/info 对 token {t[:8]}… 的 models 在 {inner_rounds} 次调用中不一致")
    print(f"  ✓ 随机 {len(sample)} 个 key 的 /key/info models 各重复 {inner_rounds} 次一致")


def fetch_psql_models_map(namespace: str, prefix: str) -> dict[str, list[str]]:
    """
    只读 SQL：key_alias -> models。
    依赖 kubectl 与集群连通；失败则抛错由调用方捕获。
    prefix 仅允许字母数字 _ -，防止 SQL 注入。
    """
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", prefix):
        raise ValueError(f"unsafe --prefix for SQL: {prefix!r}")
    esc = prefix.replace("'", "''")
    sql = (
        'SELECT key_alias, array_to_json(models) '
        'FROM "LiteLLM_VerificationToken" '
        f"WHERE key_alias LIKE '{esc}%' "
        "ORDER BY key_alias;"
    )
    cmd = [
        "kubectl",
        "exec",
        "litellm-db-0",
        "-n",
        namespace,
        "--",
        "psql",
        "-U",
        "litellm",
        "-d",
        "litellm",
        "-tA",
        "--field-separator=|",
        "-c",
        sql,
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=120)
    text = out.decode().strip()
    result: dict[str, list[str]] = {}
    if not text:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        alias, raw_models = line.split("|", 1)
        alias = alias.strip()
        try:
            parsed = json.loads(raw_models)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            result[alias] = [str(x) for x in parsed]
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=" carher-* LiteLLM key models 分布（只读）")
    ap.add_argument(
        "--base-url",
        default=_get_env("LITELLM_BASE_URL", "http://127.0.0.1:4000"),
        help="LiteLLM Proxy 根 URL（默认 env LITELLM_BASE_URL 或 localhost:4000）",
    )
    ap.add_argument(
        "--master-key",
        default=_get_env("LITELLM_MASTER_KEY"),
        help="Master key（默认 env LITELLM_MASTER_KEY）",
    )
    ap.add_argument("--prefix", default="carher-", help="key_alias 前缀")
    ap.add_argument("--limit", type=int, default=100_000, help="/spend/keys limit")
    ap.add_argument("--verify-rounds", type=int, default=2, help="连续拉取 spend/keys 的轮数（≥2 推荐）")
    ap.add_argument("--sample-key-info", type=int, default=8, help="随机抽样做 /key/info 重复一致性校验的数量")
    ap.add_argument("--key-info-repeats", type=int, default=2, help="每个抽样 token 重复调用 /key/info 次数")
    ap.add_argument(
        "--max-key-info",
        type=int,
        default=500,
        help="当 spend/keys 行内缺少 models 时，最多补充 GET /key/info 的次数（防刷爆）",
    )
    ap.add_argument("--concurrency", type=int, default=6, help="补充 key/info 时的并发度")
    ap.add_argument("--psql-verify", action="store_true", help="用 litellm-db 只读 SQL 与 API 结果交叉验证")
    ap.add_argument("--k8s-namespace", default="carher", help="kubectl exec litellm-db 的 namespace")
    ap.add_argument("--json-out", help="将完整明细写入 JSON 文件（含每个 alias 的 models）")
    args = ap.parse_args()

    if not args.master_key:
        print("错误：请设置 LITELLM_MASTER_KEY 或传入 --master-key", file=sys.stderr)
        return 2

    random.seed()

    print("=== 1) 稳定性：/spend/keys ===")
    verify_spend_keys_stable(
        args.base_url, args.master_key, args.prefix, args.limit, max(2, args.verify_rounds)
    )

    rows = fetch_spend_keys(args.base_url, args.master_key, args.limit)
    carher_rows = [x for x in rows if str(x.get("key_alias") or "").startswith(args.prefix)]
    carher_rows.sort(key=lambda x: str(x.get("key_alias") or ""))

    # 解析 models：优先行内字段，否则 key/info
    alias_models: dict[str, list[str]] = {}
    missing: list[tuple[str, str]] = []  # (alias, token)

    for row in carher_rows:
        alias = str(row.get("key_alias") or "")
        token = str(row.get("token") or "")
        inline = extract_models_from_row(row)
        if inline:
            alias_models[alias] = inline
        elif token:
            missing.append((alias, token))

    print()
    print(f"=== 2) 数据：共 {len(carher_rows)} 个 {args.prefix}* key（spend/keys）")
    print(f"    其中行内带 models：{len(alias_models)}；需补充 /key/info：{len(missing)}")

    if missing:
        n = min(len(missing), args.max_key_info)
        if len(missing) > args.max_key_info:
            print(
                f"    警告：仅对前 {n}/{len(missing)} 个缺失行调用 /key/info（可用 --max-key-info 提高上限）",
                file=sys.stderr,
            )
        to_fetch = missing[:n]

        def one(pair: tuple[str, str]) -> tuple[str, list[str]]:
            alias, token = pair
            return alias, key_info_models(args.base_url, args.master_key, token)

        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
            futs = {ex.submit(one, p): p for p in to_fetch}
            for fut in as_completed(futs):
                alias, models = fut.result()
                alias_models[alias] = models

    # 抽样重复 key/info
    tokens_for_sample = [
        str(x.get("token") or "")
        for x in carher_rows
        if x.get("token") and str(x.get("key_alias") or "").startswith(args.prefix)
    ]
    tokens_for_sample = [t for t in tokens_for_sample if t]
    print()
    print("=== 3) 稳定性：/key/info（抽样）===")
    verify_key_info_stable(
        args.base_url,
        args.master_key,
        tokens_for_sample,
        args.sample_key_info,
        max(2, args.key_info_repeats),
    )

    # 分布统计
    fp_to_aliases: dict[str, list[str]] = defaultdict(list)
    route_ctr: Counter[str] = Counter()
    for row in carher_rows:
        alias = str(row.get("key_alias") or "")
        route_ctr[extract_route_policy(row)] += 1
        models = alias_models.get(alias)
        if models is None:
            fp = "(missing_models)"
        else:
            fp = models_fingerprint(models)
        fp_to_aliases[fp].append(alias)

    print()
    print("=== 4) models allowlist 分布（按内容哈希分组，同组表示 allowlist 完全一致）===")
    groups = sorted(fp_to_aliases.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for fp, aliases in groups:
        sample_alias = sorted(aliases)[0]
        ms = alias_models.get(sample_alias)
        n_models = len(ms) if ms else 0
        print(f"  count={len(aliases):4d}  fp={fp}  models_count={n_models}")
        if ms and fp != "(missing_models)":
            preview = ", ".join(sorted(ms)[:6])
            if len(ms) > 6:
                preview += ", …"
            print(f"           example_alias={sample_alias}")
            print(f"           models_preview: {preview}")

    print()
    print("=== 5) metadata.route_policy（litellm_route_policy）分布 ===")
    for pol, c in route_ctr.most_common():
        print(f"  {c:4d}  {pol}")

    # 可选：PostgreSQL 交叉验证
    if args.psql_verify:
        print()
        print("=== 6) 交叉验证：PostgreSQL（只读）vs API 合并结果 ===")
        try:
            db_map = fetch_psql_models_map(args.k8s_namespace, args.prefix)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f"  ✗ psql 验证跳过：{e}", file=sys.stderr)
        else:
            mismatches = 0
            only_db = 0
            only_api = 0
            for alias in sorted(set(db_map) | set(alias_models)):
                a = tuple(sorted(alias_models.get(alias, [])))
                b = tuple(sorted(db_map.get(alias, [])))
                if alias not in alias_models:
                    only_db += 1
                    continue
                if alias not in db_map:
                    only_api += 1
                    continue
                if a != b:
                    mismatches += 1
                    print(f"  ✗ models 不一致: {alias}")
            if mismatches == 0:
                print(f"  ✓ 所有共有 alias 的 models 与 DB 一致（共有 {len(set(db_map) & set(alias_models))} 个）")
            if only_db:
                print(f"  注意：仅在 DB 出现（不在本次 spend/keys 列表）: {only_db} 个")
            if only_api:
                print(f"  注意：仅在 API 合并结果中（DB 无此行）: {only_api} 个")

    if args.json_out:
        payload = {
            "prefix": args.prefix,
            "total_keys": len(carher_rows),
            "alias_models": {k: sorted(v) for k, v in sorted(alias_models.items())},
            "fingerprint_counts": {fp: len(ls) for fp, ls in fp_to_aliases.items()},
            "route_policy_counts": dict(route_ctr),
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print()
        print(f"已写入 {args.json_out}")

    print()
    print("完成（只读请求；未修改任何 key）。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as e:
        print(f"HTTP 错误: {e.code} {e.reason}\n{e.read().decode()[:2000]}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as e:
        print(f"连接错误: {e}", file=sys.stderr)
        raise SystemExit(1)
