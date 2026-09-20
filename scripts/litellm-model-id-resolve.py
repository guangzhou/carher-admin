#!/usr/bin/env python3
"""x-litellm-model-id / x-litellm-model-group 假名 ↔ 真名的反查工具。

为什么需要它
------------
198 的 ``error_sanitize`` 回调把这两个响应头换成了 HMAC 假名
（`chatgpt-acct-109-gpt-5.6-luna` → `a749e6b80342`，`chatgpt-gpt-5.5` → 另一个），
对外不再暴露拓扑，也不再暴露"客户要的 gpt-5.5 其实由谁承接"。同时整个
``llm_provider-*`` 前缀的头被删掉（那里面有账号订阅档位、配额消耗、上游 JWT cookie）。

但一堆运维判据本来是直接读这两个头的：

* ``scripts/litellm-vip-pool/vip-group-create.sh`` 的 smoke（硬闸门，不等就 exit 1）
* ``scripts/litellm-pro-gpt-fallback-target.py`` 的 gpt55_probe 证据
* ``scripts/litellm-sticky-verify.sh`` / ``probe-affinity.py``（只比"两次是否相等"，
  假名不影响，**不需要改**）
* ``chatgpt-pool-on-198`` / ``litellm-gpt-fallback-target-swap`` 两份 SOP

假名是稳定单向的：同名恒等、不同名不同，但不可逆。要知道"到底落在哪台/哪组"，
就把 ``/model/info`` 里所有 deployment id 和 model_group 都算一遍假名建反查表 ——
``/model/info`` 是**管理端点，不经过 error_sanitize**，所以它照旧给真名。

用法
----
在 198 上（或任何能连到 proxy 的地方）::

    # 反查一个假名（deployment 和 model_group 两个命名空间都查）
    LITELLM_MASTER_KEY=sk-pro-... python3 litellm-model-id-resolve.py resolve a749e6b80342

    # 正算：真名 → 假名（离线，不需要连 proxy）
    python3 litellm-model-id-resolve.py id chatgpt-acct-109-gpt-5.6-luna chatgpt-gpt-5.5

    # 全表
    LITELLM_MASTER_KEY=sk-pro-... python3 litellm-model-id-resolve.py list | head

环境变量：``LITELLM_MASTER_KEY``（必填，list/resolve 用）、
``LITELLM_BASE``（默认 http://127.0.0.1:30402）、
``ERROR_SANITIZE_ID_SALT``（只在线上也改过 salt 时才需要设，要与 proxy 一致）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import urllib.request
from pathlib import Path

# 必须与 k8s/litellm-callbacks/error_sanitize.py 的 _ID_SALT 默认值一致。
# 下面 _assert_salt_matches_callback() 会在源文件可见时实查，防静默漂移。
DEFAULT_SALT = "carher-198-model-id-pseudonym-v1"
SALT = (os.environ.get("ERROR_SANITIZE_ID_SALT") or DEFAULT_SALT).encode("utf-8")

BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402").rstrip("/")
CALLBACK_SRC = (Path(__file__).resolve().parent.parent
                / "k8s" / "litellm-callbacks" / "error_sanitize.py")


def pseudonym(model_id: str) -> str:
    """与回调里同名函数逐字节等价的实现（12 位 HMAC-SHA256 前缀）。"""
    if not model_id:
        return "-"
    return hmac.new(SALT, str(model_id).encode("utf-8", "replace"),
                    hashlib.sha256).hexdigest()[:12]


def _assert_salt_matches_callback() -> None:
    """源文件在手边时，实查回调里的默认 salt，别靠记忆。"""
    if os.environ.get("ERROR_SANITIZE_ID_SALT"):
        return  # 显式覆盖，跳过比对
    try:
        src = CALLBACK_SRC.read_text(encoding="utf-8")
    except OSError:
        return  # 不在仓库里跑（例如只 scp 了这个脚本），跳过
    if f'"{DEFAULT_SALT}"' not in src:
        sys.exit(
            f"salt 漂移：{CALLBACK_SRC} 里找不到 {DEFAULT_SALT!r}。\n"
            "回调的 _ID_SALT 默认值被改过了，本脚本算出来的假名不会匹配线上。"
        )


def fetch_deployments() -> list[dict]:
    key = os.environ.get("LITELLM_MASTER_KEY")
    if not key:
        sys.exit("需要 LITELLM_MASTER_KEY（/model/info 是管理端点，要 master key）")
    req = urllib.request.Request(
        f"{BASE}/model/info", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.load(r)
    rows = payload.get("data") if isinstance(payload, dict) else payload
    out = []
    for row in rows or []:
        mid = ((row.get("model_info") or {}).get("id")) if isinstance(row, dict) else None
        if mid:
            out.append({"id": str(mid), "group": row.get("model_name") or "-"})
    if not out:
        sys.exit("/model/info 返回 0 个带 model_info.id 的 deployment，拒绝给空表")
    return out


def cmd_list() -> None:
    rows = fetch_deployments()
    print(f"# {len(rows)} deployments  base={BASE}")
    print(f"{'pseudonym':<14}{'deployment_id':<44}model_group")
    for r in sorted(rows, key=lambda x: x["id"]):
        print(f"{pseudonym(r['id']):<14}{r['id']:<44}{r['group']}")


def cmd_resolve(wanted: list[str]) -> None:
    rows = fetch_deployments()
    # /model/info 会把同一台 deployment 按每个 model_group 别名各列一行（行数被
    # alias 撑大），所以必须先按 id 归并 —— 否则"同一个 id 出现多次"会被误判成
    # 假名碰撞（第一版就这么误报了 202 组）。
    by_id: dict[str, set[str]] = {}
    for r in rows:
        by_id.setdefault(r["id"], set()).add(r["group"])
    # 两个命名空间都要查：deployment id 和 model_group 都被假名化了
    table: dict[str, list[tuple[str, str]]] = {}
    for mid, groups in by_id.items():
        table.setdefault(pseudonym(mid), []).append(("deployment", mid))
    for g in {g for gs in by_id.values() for g in gs}:
        table.setdefault(pseudonym(g), []).append(("model_group", g))

    # 真碰撞 = 一个假名对上多个**不同**目标。反查会给错答案，宁可当场喊出来。
    dupes = {p: v for p, v in table.items() if len(v) > 1}
    if dupes:
        print(f"!! 假名碰撞 {len(dupes)} 组，这些反查结果不可信：", file=sys.stderr)
        for p, v in list(dupes.items())[:5]:
            print(f"   {p} -> {v}", file=sys.stderr)
    rc = 0
    for w in wanted:
        hits = table.get(w.strip().lower())
        if hits:
            for kind, name in hits:
                extra = ",".join(sorted(by_id[name])) if kind == "deployment" else ""
                print(f"{w}\t{kind}\t{name}\t{extra}")
        else:
            print(f"{w}\t<NOT FOUND in {len(by_id)} deployments / "
                  f"{len({g for gs in by_id.values() for g in gs})} groups>")
            rc = 1
    sys.exit(rc)


def main() -> None:
    _assert_salt_matches_callback()
    argv = sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":
        cmd_list()
    elif cmd == "resolve":
        if not rest:
            sys.exit("resolve 需要至少一个假名")
        cmd_resolve(rest)
    elif cmd == "id":
        if not rest:
            sys.exit("id 需要至少一个真实 deployment id")
        for mid in rest:
            print(f"{mid}\t{pseudonym(mid)}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
