#!/usr/bin/env python3
"""copilot2api-pool-swap-lane

在 198 LiteLLM 的 copilot2api 循环池上做加/删腿。

三个子命令覆盖 09-02 起沉淀下来的三种操作：

  clone SRC_HOST DST_HOST
      按 host 匹配从 /model/info 拉全部 SRC 腿，复刻成 DST 腿（api_base 只换 host、
      其他字段原样；确定式 id `SRC_HOST/anthropic/...` 会同步改前缀，其余保留自动 UUID），
      通过 /model/new 加入。要在 litellm-proxy pod 内跑（能直连 127.0.0.1:4000）。

  delete-host HOST
      拉全部 host 匹配的 DB 腿，逐条 /model/delete。仅删 db_model=true 的条目；
      CM 腿删不掉（LiteLLM 会报 "not found in db"）。要在 pod 内跑。

  strip-cm-host HOST [--dry-run]
      拉 CM litellm-config → 解析 data.config.yaml → 从 model_list 剔除 host 匹配的条目 →
      dump 回 CM → kubectl apply。dry-run 只打印将要剔除的名单。
      在 AIYJY-litellm（K3s 控制节点）上跑，需 python3-yaml、sudo kubectl。
      **单独 apply 不生效**，必须之后 `kubectl -n litellm-product rollout restart deploy/litellm-proxy`。

统一约定
--------
- host 匹配用 `//<HOST>` 严格前缀（避免 `copilot2api` 匹配到 `copilot2api-2/3`）。
- 有 `/v1` 后缀或没有的 api_base 都能命中（openai 腿有、anthropic 孪生腿没）。
- 备份策略：本脚本自己不备份；调用方在跑 clone/delete/strip 之前必须先各自留档，
  因为出错回滚的判据（原始 arm 列表、原 CM）只有调用方知道。
- 用法示例见 `~/.codex/skills/copilot2api-ops/SKILL.md` §"LiteLLM Handoff"。

判据纪律
--------
- /model/delete 走的是 pro/model/delete；DB-only 才有效。CM 腿要走 strip-cm-host。
- 改 CM 后必须 rollout restart（4 副本滚动 4~5 分钟），单独 kubectl apply CM 不改
  运行中 proxy 的路由表。
- clone 之后要用真实用户 key 打一发验证 c3 被选中；master key 撞预算会返 402，
  不能作为端到端探针（管理 API 仍可用）。
"""
from __future__ import annotations
import argparse, json, os, sys, subprocess, tempfile, urllib.request, urllib.error

PROXY = "http://127.0.0.1:4000"
NS = "litellm-product"
CM = "litellm-config"
CM_KEY = "config.yaml"


def _mk() -> str:
    mk = os.environ.get("MK", "").strip()
    if not mk:
        sys.exit("MK env var required (LITELLM master key)")
    return mk


def _api(mk: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        PROXY + path, data=data,
        headers={"Authorization": "Bearer " + mk, "content-type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:400]}")


def _match_host(api_base: str, host: str) -> bool:
    return f"//{host}/" in api_base + "/" or f"//{host}:" in api_base


KEEP_INFO = {"base_model", "input_cost_per_token", "output_cost_per_token",
             "cache_read_input_token_cost", "cache_creation_input_token_cost"}


def cmd_clone(args):
    mk = _mk()
    d = _api(mk, "/v1/model/info")["data"]
    src = [m for m in d if _match_host(str(m.get("litellm_params", {}).get("api_base") or ""), args.src)]
    print(f"src arms matching //{args.src}: {len(src)}")
    if not src:
        sys.exit(1)
    ok = fail = 0
    for m in src:
        name = m["model_name"]
        lp = dict(m["litellm_params"])
        lp["api_base"] = lp["api_base"].replace(args.src, args.dst)
        # /model/info 把 api_key 抹成掩码 → 照抄会克隆出一条没 key 的 openai 腿，
        # 运行时才炸 `AuthenticationError: The api_key client option must be set`
        # （09-04 c4 十三条 openai 腿全踩）。openai/ 载体一律回填占位 key。
        cur_key = str(lp.get("api_key") or "")
        masked = (not cur_key) or "*" in cur_key
        if str(lp.get("model") or "").startswith("openai/") and masked:
            lp["api_key"] = args.api_key
        elif masked:
            lp.pop("api_key", None)
        old_info = m.get("model_info", {})
        new_info = {k: v for k, v in old_info.items() if k in KEEP_INFO}
        old_id = old_info.get("id", "")
        # port deterministic id prefix (e.g. "copilot2api-2/anthropic/...")
        src_prefix = args.src.split(".")[0] + "/"
        dst_prefix = args.dst.split(".")[0] + "/"
        if isinstance(old_id, str) and old_id.startswith(src_prefix):
            new_info["id"] = old_id.replace(src_prefix, dst_prefix, 1)
        body = {"model_name": name, "litellm_params": lp, "model_info": new_info}
        try:
            _api(mk, "/model/new", body)
            print(f"{name:35s} → {new_info.get('id','(auto)')}  OK")
            ok += 1
        except Exception as e:
            print(f"{name:35s} FAIL: {e}")
            fail += 1
    print(f"\nsummary: ok={ok} fail={fail} total={len(src)}")
    sys.exit(0 if fail == 0 else 1)


def cmd_delete_host(args):
    mk = _mk()
    d = _api(mk, "/v1/model/info")["data"]
    victims = [m for m in d
               if _match_host(str(m.get("litellm_params", {}).get("api_base") or ""), args.host)
               and m.get("model_info", {}).get("db_model")]
    non_db = [m for m in d
              if _match_host(str(m.get("litellm_params", {}).get("api_base") or ""), args.host)
              and not m.get("model_info", {}).get("db_model")]
    print(f"db arms to delete: {len(victims)}; non-db (skipped, use strip-cm-host): {len(non_db)}")
    if not victims and non_db:
        print("no DB arms — hint: run `strip-cm-host` for CM-managed entries.")
    ok = fail = 0
    for m in victims:
        mid = m["model_info"]["id"]
        name = m["model_name"]
        try:
            _api(mk, "/model/delete", {"id": mid})
            print(f"{name:35s} id={mid} DELETED")
            ok += 1
        except Exception as e:
            print(f"{name:35s} id={mid} FAIL: {e}")
            fail += 1
    print(f"\nsummary: ok={ok} fail={fail}")
    sys.exit(0 if fail == 0 else 1)


def _sh(cmd, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def cmd_strip_cm_host(args):
    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML required on this host (apt install python3-yaml)")
    raw = _sh(["sudo", "kubectl", "-n", NS, "get", "cm", CM, "-o", "yaml"]).stdout
    cm = yaml.safe_load(raw)
    cfg_str = cm["data"][CM_KEY]
    cfg = yaml.safe_load(cfg_str)
    ml = cfg.get("model_list", [])
    before = len(ml)
    kept, dropped = [], []
    for m in ml:
        ab = str(m.get("litellm_params", {}).get("api_base", ""))
        if _match_host(ab, args.host):
            dropped.append((m.get("model_name"), ab))
        else:
            kept.append(m)
    after = len(kept)
    print(f"model_list: {before} → {after}  (would drop {len(dropped)} entries with host //{args.host})")
    for n, ab in dropped:
        print(f"  DROP {n:35s} {ab}")
    if args.expect is not None and len(dropped) != args.expect:
        sys.exit(f"expected exactly {args.expect} drops, got {len(dropped)} — refusing to apply")
    if args.dry_run:
        return
    if not dropped:
        print("no changes; skip apply")
        return
    cfg["model_list"] = kept
    cm["data"][CM_KEY] = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, default_flow_style=False)
    for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields", "generation"):
        cm.get("metadata", {}).pop(k, None)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(cm, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
        path = f.name
    print(f"wrote {path}")
    res = _sh(["sudo", "kubectl", "-n", NS, "apply", "-f", path], check=False)
    print("apply.stdout:", res.stdout.strip())
    if res.stderr.strip():
        print("apply.stderr:", res.stderr.strip())
    print("\nREMINDER: run `sudo kubectl -n litellm-product rollout restart deploy/litellm-proxy`")
    print("          then wait for `rollout status` to say `successfully rolled out`.")
    sys.exit(res.returncode)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("clone", help="clone arms from SRC host to DST host via /model/new")
    c.add_argument("src", help="e.g. copilot2api-2.copilot2api.svc.cluster.local")
    c.add_argument("dst", help="e.g. copilot2api-3.copilot2api.svc.cluster.local")
    c.add_argument("--api-key", default="copilot2api-noauth",
                   help="openai/ 载体腿的占位 api_key（/model/info 会把原值抹成掩码，"
                        "不回填的话新腿运行时报 api_key must be set）")
    c.set_defaults(func=cmd_clone)

    d = sub.add_parser("delete-host", help="delete DB arms whose api_base host matches")
    d.add_argument("host")
    d.set_defaults(func=cmd_delete_host)

    s = sub.add_parser("strip-cm-host", help="strip CM litellm-config entries whose api_base host matches; needs kubectl+yaml on control node")
    s.add_argument("host")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--expect", type=int, help="require exactly N drops, else refuse (guard for mis-typed host)")
    s.set_defaults(func=cmd_strip_cm_host)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
