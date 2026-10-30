#!/usr/bin/env python3
"""muse 号 → 出口 IP 的**持久台账**，以及「登录 IP == 服务 IP」的唯一写入口。

一号一辈子一个 IP（10-30 用户要求「每个 ip 绑定一个账户，而不是一个 ip 绑定多账户」）：
  - 台账 `198:/opt/chatgpt-acct-console/data/muse_egress_assign.json`（owner cltx，0600），
    每条 {label: {"exit_ip", "proxy_name", "assigned_at", "note"}}；**凭据不落台账**，
    只记出口名字，用的时候再去目录（kiro 槽位表 / muse-px-* 口令文件）现取。
  - 登录（add_batch.sh / login_http.py）和服务（Secret MUSE2API_EGRESS_MAP）都从这里取，
    所以重登一个号会复用同一个 IP，而不是按行号重新轮一遍。
  - `claim(label)` 幂等：台账里有就原样返回；没有就从「可用且没被占」的出口里挑一个。
    出口不够时**明说不够**并返回 None（调用方决定落默认隧道），不偷偷两号共用一个 IP。

    python3 egress_assign.py show                      # 台账 + 当前 Secret 对照
    python3 egress_assign.py claim <label> [出口名]     # 分配/读回一个号的出口（幂等）
    python3 egress_assign.py sync [--apply]            # 台账 -> Secret（dry-run 默认）

🔴 这个脚本在 198 上跑（要读 kiro 槽位配置和 k3s）。输出只有 host:port / IP，绝不打印凭据。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, "/opt/chatgpt-acct-console")

STORE = "/opt/chatgpt-acct-console/data/muse_egress_assign.json"
# 永不分配给 muse 的出口：
# - 阿里云那几个 squid 只放行白名单域名，muse.ai 不在表里（CONNECT 403，10-30 复测）；
# - 🔴 `cac-us-egress` 的出口 IP **会漂**（10-30 同一个 pod、restarts=0，先后量到
#   67.21.77.249 / 170.178.162.206 / 67.21.77.249）⇒ 钉不住一个号，比不钉更坏；
# - `us-170.178.173.10` = kiro-us-egress，上游是 5 节点 fallback **组**，IP 会在组内漂，同理不用。
NEVER = ("sg-47.84.85.100", "sg-47.84.101.218", "sg-47.84.112.136", "sg-47.236.200.98",
         "cac-us-egress", "us-170.178.173.10")


def load() -> dict:
    try:
        with open(STORE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(d: dict):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)


def catalog() -> dict:
    """出口名 -> 带凭据的 URL。和 8796 管理台「换」按钮同一份目录 + muse 专属出口。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "muse_egress_catalog", "/opt/chatgpt-acct-console/muse_egress_catalog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.catalog()


def proxy_url(name: str) -> str:
    """出口名 -> 带凭据的代理 URL（只在内存里，调用方不要打印）。"""
    u = catalog().get(name)
    if not u:
        raise SystemExit("STOP 出口目录里没有 %r" % name)
    return u


def claim(label: str, prefer: str = "") -> dict | None:
    """给一个号定出口；台账里已有就直接返回（幂等，重登复用同一个 IP）。
    返回 {"label","exit_ip","proxy_name",...}；出口不够返回 None。"""
    d = load()
    if label in d and not prefer:
        return dict(d[label], label=label)
    cat = catalog()
    taken = {v["proxy_name"] for k, v in d.items() if k != label}
    if prefer:
        if prefer not in cat:
            raise SystemExit("STOP 出口目录里没有 %r" % prefer)
        if prefer in taken:
            owner = [k for k, v in d.items() if v["proxy_name"] == prefer][0]
            raise SystemExit("STOP %s 已经被 %s 占了（一个 IP 只给一个号）" % (prefer, owner))
        pick = prefer
    else:
        free = [n for n in sorted(cat) if n not in taken and n not in NEVER]
        # muse 专属出口优先（不和 kiro 槽位共用一条腿）
        free.sort(key=lambda n: (not n.startswith("muse-px-"), n))
        if not free:
            return None
        pick = free[0]
    from urllib.parse import urlsplit
    d[label] = {"exit_ip": pick.split("-", 1)[-1] if pick[:3] in ("sg-", "jp-", "kr-", "ph-", "de-", "uk-", "us-") else "",
                "proxy_name": pick, "upstream": "%s:%s" % (urlsplit(cat[pick]).hostname, urlsplit(cat[pick]).port),
                "assigned_at": int(time.time())}
    save(d)
    return dict(d[label], label=label)


def record_exit_ip(label: str, ip: str):
    """把实测到的真实出口 IP 记进台账（名字里带不出 IP 的出口，如 muse-px-*、cac-us-egress）。"""
    d = load()
    if label in d:
        d[label]["exit_ip"] = ip
        save(d)


def secret_value() -> str:
    """台账 -> MUSE2API_EGRESS_MAP 的值（含凭据，只喂给 kubectl，不打印）。"""
    cat, d = catalog(), load()
    pairs = []
    for label in sorted(d):
        name = d[label]["proxy_name"]
        if name in cat:
            pairs.append("%s=%s" % (label, cat[name]))
    return ",".join(pairs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("show", "claim", "sync"))
    ap.add_argument("label", nargs="?", default="")
    ap.add_argument("prefer", nargs="?", default="")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if a.cmd == "show":
        d = load()
        print("台账 %s（%d 条）" % (STORE, len(d)))
        for label in sorted(d):
            v = d[label]
            print("  %-18s %-22s exit=%-16s upstream=%-22s %s" % (
                label, v["proxy_name"], v.get("exit_ip") or "?", v.get("upstream", "?"),
                time.strftime("%m-%d %H:%M", time.localtime(v.get("assigned_at", 0)))))
        ips = [v.get("exit_ip") for v in d.values() if v.get("exit_ip")]
        dup = {i for i in ips if ips.count(i) > 1}
        print("不同 IP %d 个；重复 %s" % (len(set(ips)), ", ".join(sorted(dup)) or "无"))
        return 0

    if a.cmd == "claim":
        if not a.label:
            raise SystemExit("STOP claim 要 label")
        r = claim(a.label, a.prefer)
        if r is None:
            print("STOP 没有空闲出口了，%s 只能走默认隧道（和别的号共用 IP）" % a.label)
            return 1
        print("OK   %s -> %s（upstream %s）" % (a.label, r["proxy_name"], r.get("upstream")))
        return 0

    # sync：台账 -> Secret
    import base64
    import subprocess
    val = secret_value()
    d = load()
    print("台账 %d 条 ⇒ 对照表 %d 条" % (len(d), len([x for x in val.split(",") if x])))
    for label in sorted(d):
        print("  %-18s %-22s %s" % (label, d[label]["proxy_name"], d[label].get("upstream", "?")))
    if not a.apply:
        print("dry-run：加 --apply 才写 Secret（写完还要 rollout restart 才生效）")
        return 0
    body = json.dumps({"apiVersion": "v1", "kind": "Secret",
                       "metadata": {"name": "muse2api-egress-map", "namespace": "muse2api"},
                       "type": "Opaque",
                       "data": {"MUSE2API_EGRESS_MAP": base64.b64encode(val.encode()).decode()}})
    r = subprocess.run(["k3s", "kubectl", "-n", "muse2api", "apply", "-f", "-"],
                       input=body, capture_output=True, text=True, timeout=90)
    if r.returncode:
        raise SystemExit("STOP 写 Secret 失败：%s" % r.stderr.strip()[:200])
    print("OK   Secret muse2api-egress-map 已写入（%d 条）；还要 rollout restart 才生效" % len(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
