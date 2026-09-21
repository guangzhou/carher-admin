#!/usr/bin/env python3
"""Insert a fallback target *before* an anchor, in a named set of 198 chains.

Motivation
----------
2026-09-21: the cursor 装机包 gpt-* 段（`gpt-5.6-sol` / `gpt-5.6-luna` /
`gpt-5.6-terra` / `gpt-5.5` / `gpt-6-astra`）already had fallback chains whose
grok leg was `sa-grok-4.6`. 用户要把那条 grok 腿升到 `sa-grok-4.20`
（window 1,000,000 vs 4.6 的 500,000），但**不删** 4.6 —— 4.20 历史流量只有
7 天 24 条，没有承接量级的证据，所以它插在 4.6 *之前*，4.6 保留为下一跳。

与 `litellm-198-gpt-fallback-append.py` 的区别（为什么不复用它）：

1. 语义：那个是 append-after-anchor 且全表扫「以 anchor 结尾」的链；
   这里要 insert-before-anchor，且只动**点名的 group**，别的行一律原样抄。
2. 🔴 那个脚本把 `PROXY_SELECTOR` 写死成 `app=litellm-proxy` —— 2026-09-21
   生产车道是 `litellm-proxy-gray`（4 副本），`litellm-proxy` 是 1 副本闲置。
   拿它验会验到不在服务的 pod（假绿），`rollout restart` 会滚错 Deployment。
   本脚本按 **Service `litellm-proxy-nodeport` 的 selector**
   （`carher.net/litellm-production-route=enabled`）选 pod，永不写死 Deployment 名。
   ⚠️ 该标签在 **Pod** 上，不在 Deployment 对象上 —— `get deploy -l <它>`
   返回 "No resources found" 不代表标签不存在。

写法只允许 `jsonb_set(param_value, '{fallbacks}', ...)`：`STORE_MODEL_IN_DB=True`
⇒ 权威源是 Postgres `LiteLLM_Config`，改 ConfigMap 是 no-op，`POST /config/update`
会静默抹掉 `model_group_alias`，整块重写 `param_value` 会丢顶层键。

幂等：链里已有 `--insert` 的 group 直接跳过。fail-safe：点名的 group 不在
fallbacks 里、或它的链里没有 anchor —— 都是**拒绝动手**，不是静默跳过。

Runtime
-------
在能跑 `kubectl -n litellm-product ...` 的地方执行（198 上 `cltx` 需要
`KUBECTL='sudo -n kubectl'`）。默认 dry-run，`--apply` 必须带 `--backup`。

Examples
--------
  # dry-run（无写入）
  KUBECTL='sudo -n kubectl' python3 litellm-198-fallback-insert-before.py \
      --groups gpt-5.5,gpt-5.6-sol,gpt-5.6-luna,gpt-5.6-terra,gpt-6-astra,chatgpt-gpt-6-astra

  # apply
  KUBECTL='sudo -n kubectl' python3 litellm-198-fallback-insert-before.py \
      --groups ... --backup /tmp/rs-$(date +%Y%m%dT%H%M%S).json --apply

  # 回滚
  KUBECTL='sudo -n kubectl' python3 litellm-198-fallback-insert-before.py \
      --restore /tmp/rs-<pre>.json --apply
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request

NAMESPACE = "litellm-product"
DB_POD = "litellm-db-0"
DB_USER = "litellm"
DB_NAME = "litellm"
SECRET = "litellm-secrets"
PROXY_PORT = "4000"
# 生产车道判据：Service litellm-proxy-nodeport（nodePort 30402）的 selector。
PROD_POD_SELECTOR = "carher.net/litellm-production-route=enabled"

DEFAULT_ANCHOR = "sa-grok-4.6"
DEFAULT_INSERT = "sa-grok-4.20"


# --------------------------------------------------------------------------- #
# pure planning (unit-testable, no I/O)
# --------------------------------------------------------------------------- #
def plan_insert_before(
    fallbacks: list[dict], groups: list[str], anchor: str, insert: str
) -> tuple[list[dict], list[tuple[str, list, list]], list[str]]:
    """Return (new_fallbacks, changes, skipped).

    For every entry whose single key is in ``groups``: insert ``insert``
    immediately before the first occurrence of ``anchor``. Entries not named in
    ``groups`` are copied through byte-identical. Raises on a named group that
    is missing from ``fallbacks`` or whose chain lacks ``anchor`` — a silent
    no-op there would read exactly like success.
    """
    wanted = list(dict.fromkeys(groups))  # de-dup, keep order
    seen: set[str] = set()
    new: list[dict] = []
    changes: list[tuple[str, list, list]] = []
    skipped: list[str] = []

    for entry in fallbacks:
        if len(entry) != 1:
            raise ValueError(f"fallback entry is not a single-key dict: {entry!r}")
        (group, chain), = entry.items()
        chain = list(chain)
        if group not in wanted:
            new.append({group: chain})
            continue
        if group in seen:
            raise ValueError(f"group appears twice in fallbacks: {group!r}")
        seen.add(group)
        if insert in chain:
            skipped.append(group)
            new.append({group: chain})
            continue
        if anchor not in chain:
            raise SystemExit(
                f"refusing: group {group!r} has no anchor {anchor!r} in its chain "
                f"{chain!r} — insertion point is undefined"
            )
        i = chain.index(anchor)
        new_chain = chain[:i] + [insert] + chain[i:]
        new.append({group: new_chain})
        changes.append((group, chain, new_chain))

    missing = [g for g in wanted if g not in seen]
    if missing:
        raise SystemExit(f"refusing: named groups absent from fallbacks: {missing}")
    return new, changes, skipped


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def _kubectl(*args: str) -> str:
    base = shlex.split(os.environ.get("KUBECTL", "kubectl"))
    return subprocess.run(
        [*base, "-n", NAMESPACE, *args],
        check=True, text=True, capture_output=True,
    ).stdout


def _read_router_settings() -> dict:
    raw = _kubectl(
        "exec", "-i", DB_POD, "--",
        "psql", "-U", DB_USER, "-d", DB_NAME, "-tAc",
        "SELECT param_value FROM \"LiteLLM_Config\" WHERE param_name='router_settings';",
    ).strip()
    # kubectl exec 偶发往 stdout 混一行 "E... websocket.go ... Unknown stream id"
    raw = "\n".join(l for l in raw.splitlines() if not l.startswith("E0")).strip()
    if not raw:
        raise SystemExit("router_settings row not found in LiteLLM_Config")
    return json.loads(raw)


def _write_fallbacks(new_fb: list[dict]) -> str:
    sql = (
        "UPDATE \"LiteLLM_Config\" SET param_value = "
        "jsonb_set(param_value, '{fallbacks}', $json$"
        + json.dumps(new_fb) +
        "$json$::jsonb) WHERE param_name='router_settings';"
    )
    return _kubectl("exec", "-i", DB_POD, "--",
                    "psql", "-U", DB_USER, "-d", DB_NAME, "-c", sql).strip()


def _master_key() -> str:
    raw = _kubectl("get", "secret", SECRET, "-o",
                   "jsonpath={.data.LITELLM_MASTER_KEY}")
    return base64.b64decode(raw).decode()


def _prod_pod_ips() -> list[str]:
    out = _kubectl("get", "pods", "-l", PROD_POD_SELECTOR,
                   "--field-selector=status.phase=Running",
                   "-o", "jsonpath={range .items[*]}{.status.podIP}{'\\n'}{end}")
    return [ip for ip in out.split("\n") if ip.strip()]


def _pod_fallbacks(pod_ip: str, mk: str) -> list[dict]:
    req = urllib.request.Request(
        f"http://{pod_ip}:{PROXY_PORT}/get/config/callbacks",
        headers={"Authorization": f"Bearer {mk}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["router_settings"]["fallbacks"]


def _verify_preserved(before: dict, after: dict, expected_fb: list[dict]) -> list[str]:
    problems: list[str] = []
    if set(before) != set(after):
        problems.append(f"router_settings keys changed: {sorted(before)} -> {sorted(after)}")
    for k in before:
        if k == "fallbacks":
            continue
        if before.get(k) != after.get(k):
            problems.append(f"non-fallbacks key mutated: {k}")
    if after.get("fallbacks") != expected_fb:
        problems.append("fallbacks in DB != expected after write")
    if len(after.get("fallbacks", [])) != len(before.get("fallbacks", [])):
        problems.append("fallback entry count changed")
    return problems


def _await_live(new_fb: list[dict], timeout: int) -> bool:
    mk = _master_key()
    ips = _prod_pod_ips()
    if not ips:
        raise SystemExit(f"no Running pod matched {PROD_POD_SELECTOR} — refusing to "
                         "declare propagation without a ruler")
    print(f"polling {len(ips)} production pods: {', '.join(ips)}")
    deadline = time.time() + timeout
    while True:
        states = {}
        for ip in ips:
            try:
                states[ip] = _pod_fallbacks(ip, mk) == new_fb
            except Exception as exc:  # noqa: BLE001
                states[ip] = f"err:{exc}"
        if all(v is True for v in states.values()):
            print(f"live propagation OK on all {len(ips)} production pods.")
            return True
        if time.time() >= deadline:
            print("LIVE VERIFY INCOMPLETE:", json.dumps(states, default=str),
                  file=sys.stderr)
            return False
        time.sleep(10)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--groups", default="",
                   help="comma-separated model_group names to touch")
    p.add_argument("--anchor", default=DEFAULT_ANCHOR)
    p.add_argument("--insert", dest="insert_target", default=DEFAULT_INSERT)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--backup", help="required with --apply: pre-change router_settings json")
    p.add_argument("--restore", metavar="FILE",
                   help="write this file's fallbacks array back (needs --apply)")
    p.add_argument("--verify", action="store_true",
                   help="read-only: print the chains of --groups from DB and from "
                        "every production pod")
    p.add_argument("--reload-timeout", type=int, default=150,
                   help="seconds to wait for the 30s router_settings hot reload")
    a = p.parse_args()

    groups = [g.strip() for g in a.groups.split(",") if g.strip()]

    if a.verify:
        rs = _read_router_settings()
        db_fb = rs.get("fallbacks", [])
        mk = _master_key()
        ips = _prod_pod_ips()
        for entry in db_fb:
            (g, chain), = entry.items()
            if not groups or g in groups:
                print(f"DB   {g}: {chain}")
        for ip in ips:
            try:
                live = _pod_fallbacks(ip, mk)
                same = live == db_fb
                print(f"POD  {ip}: fallbacks == DB ? {same} ({len(live)} entries)")
            except Exception as exc:  # noqa: BLE001
                print(f"POD  {ip}: ERR {exc}", file=sys.stderr)
        return 0

    if a.restore:
        if not a.apply:
            raise SystemExit("--restore requires --apply")
        rs = _read_router_settings()
        saved = json.load(open(a.restore))
        new_fb = saved["fallbacks"] if isinstance(saved, dict) else saved
        print(f"restoring {len(new_fb)} fallback entries from {a.restore}")
        print(_write_fallbacks(new_fb))
        after = _read_router_settings()
        if after.get("fallbacks") != new_fb:
            print("RESTORE VERIFY FAILED", file=sys.stderr)
            return 2
        print("DB restore verified.")
        return 0 if _await_live(new_fb, a.reload_timeout) else 2

    if not groups:
        raise SystemExit("--groups is required (comma-separated)")

    rs = _read_router_settings()
    fb = rs.get("fallbacks", [])
    new_fb, changes, skipped = plan_insert_before(
        fb, groups, a.anchor, a.insert_target)

    print(f"anchor={a.anchor}  insert={a.insert_target}")
    print(f"total fallback entries={len(fb)}  named={len(groups)}  "
          f"to_change={len(changes)}  already_present={len(skipped)}")
    for group, old, new in changes:
        print(f"  {group}:")
        print(f"    - {old}")
        print(f"    + {new}")
    for group in skipped:
        print(f"  {group}: already contains {a.insert_target} — untouched")

    if not a.apply:
        print("[dry-run] no writes. add --apply --backup <path> to write.")
        return 0
    if not a.backup:
        raise SystemExit("--apply requires --backup <path>")
    if not changes:
        print("nothing to change; DB already in desired state.")
        return 0

    with open(a.backup, "w") as f:
        json.dump(rs, f, ensure_ascii=False, indent=2)
    print(f"backup written: {a.backup}")

    print("writing via jsonb_set ...")
    print(_write_fallbacks(new_fb))

    after = _read_router_settings()
    problems = _verify_preserved(rs, after, new_fb)
    if problems:
        print("DB VERIFY FAILED:", file=sys.stderr)
        print(json.dumps(problems, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    n_delta = sum(
        1 for b, x in zip(rs["fallbacks"], after["fallbacks"]) if b != x)
    print(f"DB verify OK: {n_delta} entries differ (expected {len(changes)}), "
          "all other router_settings preserved byte-for-byte.")
    if n_delta != len(changes):
        print("DELTA COUNT MISMATCH", file=sys.stderr)
        return 2

    if _await_live(new_fb, a.reload_timeout):
        return 0
    print("router_settings hot-reloads every 30s; if this stays stale, the "
          "production lane is the Deployment behind "
          f"'{PROD_POD_SELECTOR}' — restart that one, never a hard-coded name.",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
