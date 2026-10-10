#!/usr/bin/env python3
"""cursor_menu_grant.py —— 把装机包菜单里那 30 个名字，一次性配齐到全部 cursor-* key。

解决的用户面症状：**菜单里看得见、一点就报错**。
两道闸**独立、症状不同**，所以这个脚本必须同时管两件事（只管一件 = 修一半）：

    allowlist (`models`)   缺 → 403 `key_model_access_denied`
    per-key `aliases`      缺 → 400（名字在白名单里放行了，但路由找不到落点）

⛔ 所以「`/v1/models` 里有这个名」既不证明能用，也不是 400 的免责。

── 哪些名字需要 alias？判据是**实测**，不是猜 ────────────────────────────────
2026-09-21 在 198 的 proxy pod 内拿 master key 读 `/v1/models`：309 个可见名，
30 个菜单名里**只有 3 个** proxy 认不出：

    kimi-k2.7-code / glm-5.3-flash / qwen3-coder-next

这 3 个在网关里**没有同名真实组**，只作为 per-key alias 入口存在 ⇒ 必须补 alias。
其余 27 个按名字就能落点，补 allowlist 即可。
⚠️ `LiteLLM_ProxyModelTable` 里 `model_name` 的行数**不是**这把尺子：config 文件里
   定义的组根本不进那张表（照它读会得出「10 个名字没有组」的假结论，我踩过）。
   真尺子只有 proxy 自己的 `/v1/models`，所以 --check-router 每次都重新量一遍。

── alias 目标怎么定的 ──────────────────────────────────────────────────────
不是我挑的，是数现役 key 里的多数派（2026-09-21 全库计数）：
    glm-5.3-flash    → openrouter-glm-5.3-flash  (651 把;另有 6 把指 zai-coding-*)
    kimi-k2.7-code   → claude-kimi-k2.7-code     (512 把;另有 1 把指 openrouter-*)
    qwen3-coder-next → kiro-qwen3-coder-next     (663 把)
**已经有 alias 的 key 一个字都不动**（哪怕它指的是少数派目标）——
那可能是别人有意配的，收敛别人的供应商是零症状的破坏。

── 写入语义（两个都是整表覆盖，必须先读后并再整份写回）──────────────────────
`/key/update` 的 `models` 和 `aliases` **都是整表替换**，不是追加。
直接 POST 一个只含新名字的表 = 把人家原有的全删了。所以：读旧 → 本地并集 → 整份写回。

── 安全设计 ────────────────────────────────────────────────────────────────
* **dry-run 是默认**；`--apply` 才写。门贴在写入那一步正前方，不套在整条流水线外。
* 改任何一把之前，**全量旧 (models, aliases) 先落盘**到 --backup-out；回滚 = 逐把写回。
* 每把写完**读回逐名核对**，核不上算 fail 并打印，不计入成功。
* `models` 为空/含 `'*'` 的 key **跳过**：那是"全部可用"，塞名单反而是收窄权限。
* 不碰 blocked 的 key。

用法（在 198 的 proxy pod 内跑，master key 不出集群）：
    sudo kubectl -n litellm-product exec -i <proxy-pod> -- \
        env LITELLM_MASTER_KEY=... python3 - --names-file /tmp/names30.txt        # dry-run
    ... 同上 ... --names-file /tmp/names30.txt --apply                            # 真写
一键包装见 cursor_menu_grant_198.sh（它负责取名单、挑 pod、拉备份回本机）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:4000"

# 只有这 3 个需要 alias(2026-09-21 实测)。--check-router 会重新验证这张表还准不准:
# 如果某天这些名字被建成了真实组、或者又有别的名字掉出 /v1/models,这里必须跟着改,
# 否则脚本会给已经能用的名字塞一条多余 alias(无害但脏),或者漏掉真正需要的那个(400 照旧)。
ALIAS_TARGETS = {
    "kimi-k2.7-code": "claude-kimi-k2.7-code",
    "glm-5.3-flash": "openrouter-glm-5.3-flash",
    "qwen3-coder-next": "kiro-qwen3-coder-next",
}


def call(method: str, path: str, payload=None, timeout: int = 60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + MK, "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        return {"HTTP_ERROR": e.code, "body": e.read()[:400].decode("utf-8", "replace")}
    except Exception as e:  # noqa: BLE001 —— 传输层断掉要当场说,不许吞
        return {"HTTP_ERROR": "transport", "body": repr(e)}


def router_known_names() -> set:
    """proxy 自己认得的名字。这是判「这个名字需不需要 alias」的**唯一**尺子。"""
    r = call("GET", "/v1/models")
    if "HTTP_ERROR" in r:
        print("!! 读 /v1/models 失败:%s %s" % (r["HTTP_ERROR"], r["body"][:200]))
        sys.exit(4)
    return {m["id"] for m in r.get("data", [])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--names-file", required=True,
                    help="每行一个菜单名(由 cursor_team_setup.js 的 DEFAULT_MODELS 生成)")
    ap.add_argument("--keys-file", required=True,
                    help="每行 `key_alias<TAB>token_hash`(从 DB 导出的未 blocked cursor-* key)")
    ap.add_argument("--backup-out", default="/tmp/cursor_menu_grant_backup.json")
    ap.add_argument("--apply", action="store_true", help="真写。不给 = dry-run")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 把(试点用)")
    args = ap.parse_args()

    names = [l.strip() for l in open(args.names_file) if l.strip()]
    if not names:
        print("!! 名单为空 —— 拒绝跑(空名单下这个脚本是 no-op,会假装成功)")
        return 4
    rows = [l.rstrip("\n").split("\t") for l in open(args.keys_file) if "\t" in l]
    if not rows:
        print("!! key 清单为空 —— 拒绝跑")
        return 4

    known = router_known_names()
    need_alias = [n for n in names if n not in known]
    print("菜单名 %d 个 | proxy 可见 %d 个 | 需要 alias 的 %d 个:%s"
          % (len(names), len(known), len(need_alias), ",".join(need_alias) or "(无)"))
    unmapped = [n for n in need_alias if n not in ALIAS_TARGETS]
    if unmapped:
        # 没有落点又没有 alias 目标 = 我不知道该把它指到哪。硬停,不许"尽力而为"地跳过:
        # 跳过的后果是脚本报绿、那个名字在用户手里照样 400。
        print("!! 这些名字 proxy 认不出、ALIAS_TARGETS 里也没有目标:%s" % ",".join(unmapped))
        print("   先用 litellm-198-cursor-newnames-probe.py 实打确认落点,再把映射写进 ALIAS_TARGETS。")
        return 4

    if args.limit:
        rows = rows[:args.limit]
    print("待处理 key:%d  模式:%s" % (len(rows), "APPLY(真写)" if args.apply else "DRY-RUN(只看)"))

    backup, plan = [], []
    stats = {"ok": 0, "skip_wildcard": 0, "skip_done": 0, "fail": 0}
    t0 = time.time()

    for i, (alias, tok) in enumerate(rows, 1):
        info = call("GET", "/key/info?key=" + tok)
        if "HTTP_ERROR" in info:
            stats["fail"] += 1
            print("  ❌ %s /key/info %s %s" % (alias, info["HTTP_ERROR"], str(info.get("body"))[:120]))
            continue
        d = info.get("info") or {}
        old_models = d.get("models") or []
        old_aliases = dict(d.get("aliases") or {})
        if not old_models or "*" in old_models:
            # 空表/通配 = 全部可用。往里塞具体名字会把它**收窄**成只有这些名字。
            stats["skip_wildcard"] += 1
            continue

        add_models = [n for n in names if n not in old_models]
        add_aliases = {n: ALIAS_TARGETS[n] for n in need_alias if n not in old_aliases}
        if not add_models and not add_aliases:
            stats["skip_done"] += 1
            continue

        backup.append({"key_alias": alias, "token_prefix": tok[:12],
                       "models_before": old_models, "aliases_before": old_aliases})
        plan.append({"key_alias": alias, "add_models": add_models,
                     "add_aliases": add_aliases})
        if not args.apply:
            stats["ok"] += 1
            if len(plan) <= 15:
                print("  · %-34s +models %-2d %s | +alias %s"
                      % (alias, len(add_models),
                         ",".join(add_models[:4]) + ("…" if len(add_models) > 4 else ""),
                         ",".join(add_aliases) or "-"))
            continue

        new_models = list(old_models) + add_models
        new_aliases = dict(old_aliases); new_aliases.update(add_aliases)
        # models / aliases 都是整表覆盖 —— 这里发的是**合并后的整份**,不是增量。
        r = call("POST", "/key/update", {"key": tok, "models": new_models, "aliases": new_aliases})
        if "HTTP_ERROR" in r:
            stats["fail"] += 1
            print("  ❌ %s /key/update %s %s" % (alias, r["HTTP_ERROR"], str(r.get("body"))[:160]))
            continue
        # 读回核对。写成功 ≠ 写对了:整表覆盖下少发一个字段就是静默丢数据。
        back = call("GET", "/key/info?key=" + tok)
        gm = (back.get("info") or {}).get("models") or []
        ga = (back.get("info") or {}).get("aliases") or {}
        miss_m = [n for n in names if n not in gm]
        miss_a = [n for n in need_alias if n not in ga]
        lost = [n for n in old_models if n not in gm]  # 原有的有没有被覆盖掉
        if miss_m or miss_a or lost:
            stats["fail"] += 1
            print("  ❌ %s 读回核不上 missing_models=%s missing_alias=%s LOST=%s"
                  % (alias, miss_m, miss_a, lost))
            continue
        stats["ok"] += 1
        if i % 50 == 0:
            print("  … %d/%d  %.0fs" % (i, len(rows), time.time() - t0), flush=True)

    if backup:
        with open(args.backup_out, "w") as f:
            json.dump(backup, f, ensure_ascii=False, indent=1)
        print("\n改动前快照(回滚依据)-> %s  共 %d 把" % (args.backup_out, len(backup)))
    else:
        print("\n没有需要改的 key —— 不写快照。")

    print("统计:成功/待改 %d | 已经齐了 %d | 通配跳过 %d | 失败 %d | 耗时 %.0fs"
          % (stats["ok"], stats["skip_done"], stats["skip_wildcard"], stats["fail"],
             time.time() - t0))
    if not args.apply and plan:
        print("以上是 DRY-RUN。确认无误后加 --apply 真写。")
        if len(plan) > 15:
            print("(只印了前 15 把,完整计划在 %s 的 backup 里能对照)" % args.backup_out)
    # 有失败就返红 —— 半绿不许报成功。
    return 1 if stats["fail"] else 0


if __name__ == "__main__":
    MK = os.environ.get("LITELLM_MASTER_KEY")
    if not MK:
        print("!! 需要 LITELLM_MASTER_KEY(在 proxy pod 内跑,别把它带出集群)")
        sys.exit(2)
    sys.exit(main())
