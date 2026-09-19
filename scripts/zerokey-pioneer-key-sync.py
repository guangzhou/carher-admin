#!/usr/bin/env python3
"""
zerokey 先锋 cursor key 批量切换 / 回滚（本机即可跑，纯 LiteLLM 管理 API，不碰 DB / 不 rollout）。

背景与定型（2026-07-23，见记忆 reference_feishu_cursor_cc_bitable /
project_zerokey_198_acct_bridge_via_188_docker_2026_07_21）：

  飞书「cursor & claude code 账户表格」的「先锋」列 = 池名（zerokey / ccmax / 空）。
  先锋=zerokey 的 cursor key 要把 GPT 请求重写到独立的 zerokey-pool（188 codex-pool 轮询）。

  切法 = 逐 key 调 198 prod LiteLLM `/pro/key/update`，**caller 端 merge**：
    - 先 GET /pro/key/info 读现有 aliases/models
    - 追加 10 条 alias（5 个 GPT 产品名 × {裸名, chatgpt- 前缀} → zerokey-pool-*）
    - models allowlist 追加 5 个 zerokey-pool-*
    - 保留各 key 已有的 glm/kimi/deepseek 经济路由 aliases 与原 models（不覆盖！）
  走管理 API 缓存自动刷新，**不需要动 DB、不需要 rollout**。

  ⚠️ 不切 gpt-5.3-codex：codex 后端对该 slug 返 400，切过去只会 fail→fallback 拖慢。
     已切户（如 cursor-zhangkairui）也不含 5.3-codex 的 zk alias。
  ⚠️ 这是新范式；仓库里旧的 scripts/prod-patch-key-primary-zerokey.py（单 key + kubectl
     改 configmap + fallback 链）和 skill zerokey-pool-add 里的全量替换 SQL 都是旧写法，勿混用。

用法（本机已装 authed lark-cli，bot ready）：
    # 1) 只读盘点：飞书先锋=zerokey 名单里，哪些已切 / 哪些待切
    python3 scripts/zerokey-pioneer-key-sync.py status

    # 2) 批量切换所有待切户（幂等，已切的跳过）
    python3 scripts/zerokey-pioneer-key-sync.py switch
    python3 scripts/zerokey-pioneer-key-sync.py switch --only cursor-foo,cursor-bar   # 限定
    python3 scripts/zerokey-pioneer-key-sync.py switch --dry-run                       # 预览

    # 3) 回滚：把目标 key 恢复到与某个参照 key 完全一致（全量拷贝其 aliases+models）
    #    典型：把误切户恢复到 acct-pool 基线（参照一个非先锋 key，如 cursor-biancaoming）
    python3 scripts/zerokey-pioneer-key-sync.py rollback --ref cursor-biancaoming \
        --only cursor-guran,cursor-linsen

    # 4) 冒烟：对某 key 发流式请求，验响应头落 zk-*
    python3 scripts/zerokey-pioneer-key-sync.py smoke --only cursor-foo

    # 5) 全量基线对齐：把所有 cursor-* key 拉成与某个参照完全一致（不依赖飞书名单！）
    #    典型：整体撤回 acct-pool 基线，排除自己在用的 bridge key
    python3 scripts/zerokey-pioneer-key-sync.py align --ref-alias cursor-linsen-03gc \
        --exclude cursor-liuguoxian-l08v --dry-run
    python3 scripts/zerokey-pioneer-key-sync.py align --ref-alias cursor-linsen-03gc \
        --exclude cursor-liuguoxian-l08v

⚠️ 名单来源有两条，别混：
    switch/status/rollback/smoke 走**飞书先锋列**（子集操作，靠 `--only` 点名）。
    align 走 **LiteLLM /key/list 全量 cursor-\\* key**（2026-07-27 加）——因为"所有 cursor
    key"这类需求跟飞书先锋列无关，飞书只有 120 条先锋，而 LiteLLM 里有 530 把 cursor key。

环境变量：
    LITELLM_MASTER_KEY   必填（无内置默认值，缺了直接退出）
    LITELLM_BASE         默认 http://10.68.13.198:30402/pro
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://10.68.13.198:30402/pro")
def _require_env(name: str) -> str:
    """凭据只从环境变量读，缺了直接退出。

    不设内置默认值：脚本里写死一个真 master key 等于把凭据提交进仓库，
    而且改 key 之后老默认值还会静默生效。缺了就报错，比悄悄用错的 key 好。
    """
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(
            "缺少环境变量 %s —— 先 export %s=<198 prod master key>（别写进文件/命令行历史）" % (name, name)
        )
    return v

MASTER = _require_env("LITELLM_MASTER_KEY")

# 飞书 bitable 坐标（reference_feishu_cursor_cc_bitable）
FS_BASE = "DlT9bsrwMad12VsogEpcK9Ptncc"
FS_TABLE = "tblJT2s6Y6xjYj5A"

# zerokey-pool 可服务的 5 个 GPT 产品名（不含 5.3-codex）
ZK_MODELS = [
    "gpt-5.5", "gpt-5.4", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
]
# alias map：裸名 + chatgpt- 前缀 → zerokey-pool-*
ZK_ALIASES = {}
ZK_MODEL_NAMES = []
for _m in ZK_MODELS:
    _target = f"zerokey-pool-{_m}"
    ZK_ALIASES[_m] = _target
    ZK_ALIASES[f"chatgpt-{_m}"] = _target
    ZK_MODEL_NAMES.append(_target)


def api(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {MASTER}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def key_info(token: str) -> dict:
    # URL-encode the token: sk- tokens can contain '+', '/', '=' etc. that would
    # otherwise be mis-parsed as query syntax and return wrong/empty info.
    q = urllib.parse.quote(token, safe="")
    return api("GET", f"/key/info?key={q}").get("info", {}) or {}


def key_info_safe(token: str) -> dict | None:
    """key_info that returns None instead of raising, so one dead/expired token
    (401/400 from /key/info) doesn't abort a whole batch loop."""
    try:
        return key_info(token)
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        print(f"  ! key_info failed: {e}", file=sys.stderr)
        return None


def fetch_pioneers() -> dict[str, str]:
    """飞书先锋=zerokey → {key_alias: sk-token}。用 records/search 服务端过滤（分页 list 有 bug）。"""
    payload = {
        "filter": {"conjunction": "and", "conditions": [
            {"field_name": "先锋", "operator": "is", "value": ["zerokey"]}]},
        "field_names": ["key_alias", "API Key"],
    }
    out = subprocess.run(
        ["lark-cli", "api", "POST",
         f"/open-apis/bitable/v1/apps/{FS_BASE}/tables/{FS_TABLE}/records/search?page_size=500",
         "--as", "bot", "--data", json.dumps(payload)],
        capture_output=True, text=True).stdout
    items = json.loads(out).get("data", {}).get("items", []) or []

    def g(fields, k):
        v = fields.get(k)
        if isinstance(v, list):
            return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v)
        return v

    m = {}
    for it in items:
        f = it.get("fields", {})
        ka, tok = g(f, "key_alias"), g(f, "API Key")
        if ka and tok:
            m[ka] = tok
    return m


def list_all_keys() -> list[dict]:
    """/key/list 全量拉取（分页 size=100，14 页 ≈ 1344 把）。

    ⚠️ 两个坑（2026-07-27 实证）：
      - 分页参数是 `size` 而非 `page_size`；传 page_size 会 422。
      - 不传 `return_full_object=true` 时 keys 是 str 列表（只有 token），拿不到
        aliases/models，必须传。
    返回的每项 `token` 是 **hash**（不是 sk- 明文），但 `/key/update` 的 `key` 字段
    接受 hash——所以批量对齐无需回飞书查明文 token。
    """
    out: list[dict] = []
    for page in range(1, 100):
        d = api("GET", f"/key/list?page={page}&size=100&return_full_object=true")
        ks = d.get("keys") or []
        if not ks:
            break
        out.extend(k for k in ks if isinstance(k, dict))
        if d.get("total_pages") and page >= d["total_pages"]:
            break
    return out


def fetch_key_token(key_alias: str) -> str | None:
    """按 key_alias 精确查飞书的 API Key（用于 rollback 的参照 key，可能非先锋）。"""
    payload = {
        "filter": {"conjunction": "and", "conditions": [
            {"field_name": "key_alias", "operator": "is", "value": [key_alias]}]},
        "field_names": ["key_alias", "API Key"],
    }
    out = subprocess.run(
        ["lark-cli", "api", "POST",
         f"/open-apis/bitable/v1/apps/{FS_BASE}/tables/{FS_TABLE}/records/search?page_size=5",
         "--as", "bot", "--data", json.dumps(payload)],
        capture_output=True, text=True).stdout
    items = json.loads(out).get("data", {}).get("items", []) or []
    for it in items:
        v = it.get("fields", {}).get("API Key")
        tok = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v) if isinstance(v, list) else v
        if tok:
            return tok
    return None


def is_switched(info: dict) -> bool:
    al = info.get("aliases") or {}
    zk_al = sum(1 for v in al.values() if "zerokey-pool" in str(v))
    zk_md = sum(1 for x in (info.get("models") or []) if x in ZK_MODEL_NAMES)
    return zk_al >= len(ZK_ALIASES) and zk_md >= len(ZK_MODEL_NAMES)


def filter_only(m: dict[str, str], only: str | None) -> dict[str, str]:
    if not only:
        return m
    want = {x.strip() for x in only.split(",") if x.strip()}
    return {k: v for k, v in m.items() if k in want}


def cmd_status(args):
    m = filter_only(fetch_pioneers(), args.only)
    switched, need, errored = [], [], []
    for name, tok in m.items():
        info = key_info_safe(tok)
        if info is None:
            errored.append(name)
        elif is_switched(info):
            switched.append(name)
        else:
            need.append(name)
    print(f"飞书先锋=zerokey: {len(m)}  已切: {len(switched)}  待切: {len(need)}  查询失败: {len(errored)}")
    if need:
        print("待切:")
        for n in need:
            print("  ", n)
    if errored:
        print("查询失败(token 可能失效):")
        for n in errored:
            print("  ", n)


def cmd_switch(args):
    m = filter_only(fetch_pioneers(), args.only)
    ok = fail = skip = 0
    for name, tok in m.items():
        info = key_info_safe(tok)
        if info is None:
            fail += 1
            print(f"FAIL {name}: key_info 查询失败（token 可能失效）")
            continue
        if is_switched(info):
            skip += 1
            continue
        cur_al = dict(info.get("aliases") or {})
        cur_md = list(info.get("models") or [])
        new_al = {**cur_al, **ZK_ALIASES}
        new_md = sorted(set(cur_md) | set(ZK_MODEL_NAMES))
        if args.dry_run:
            print(f"[dry] {name}: +{len(new_al) - len(cur_al)} alias, models {len(cur_md)}->{len(new_md)}")
            continue
        try:
            api("POST", "/key/update", {"key": tok, "aliases": new_al, "models": new_md})
            ok += 1
            print(f"OK   {name}: alias {len(cur_al)}->{len(new_al)}, models {len(cur_md)}->{len(new_md)}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"FAIL {name}: {e}")
    print(f"\n=== switch done: ok={ok} skip(already)={skip} fail={fail} ===")


def cmd_rollback(args):
    if not args.ref or not args.only:
        sys.exit("rollback 需要 --ref <参照 key_alias> 和 --only <目标 key_alias,...>")
    ref_tok = fetch_key_token(args.ref)
    if not ref_tok:
        sys.exit(f"飞书查不到参照 key {args.ref} 的 API Key")
    ref = key_info(ref_tok)
    ref_al, ref_md = ref.get("aliases") or {}, sorted(ref.get("models") or [])
    print(f"参照 {args.ref}: {len(ref_al)} aliases, {len(ref_md)} models")
    m = fetch_pioneers()
    targets = {}
    for ka in (x.strip() for x in args.only.split(",") if x.strip()):
        tok = m.get(ka) or fetch_key_token(ka)
        if not tok:
            print(f"跳过 {ka}: 查不到 API Key")
            continue
        targets[ka] = tok
    for name, tok in targets.items():
        if args.dry_run:
            print(f"[dry] {name} <= {args.ref} (aliases={len(ref_al)}, models={len(ref_md)})")
            continue
        api("POST", "/key/update", {"key": tok, "aliases": ref_al, "models": ref_md})
        chk = key_info(tok)
        eq = (chk.get("aliases") or {}) == ref_al and sorted(chk.get("models") or []) == ref_md
        print(f"{'OK' if eq else 'MISMATCH'} {name} <= {args.ref}")


def cmd_align(args):
    """把所有 cursor-* key 的 aliases+models 全量拉成与 --ref-alias 完全一致。

    与 rollback 的区别：rollback 按飞书名单 + `--only` 点名做子集；align 直接以
    LiteLLM 全量 cursor-* key 为范围，用于"所有 cursor key 都改成 X 那样"这类需求。
    """
    if not args.ref_alias:
        sys.exit("align 需要 --ref-alias <参照 LiteLLM key_alias>，如 cursor-linsen-03gc")
    exclude = {x.strip() for x in (args.exclude or "").split(",") if x.strip()}
    allk = list_all_keys()
    ref = next((k for k in allk if k.get("key_alias") == args.ref_alias), None)
    if ref is None:
        sys.exit(f"/key/list 里找不到参照 key_alias={args.ref_alias}")
    ref_al = ref.get("aliases") or {}
    ref_md = sorted(ref.get("models") or [])
    print(f"参照 {args.ref_alias}: {len(ref_al)} aliases, {len(ref_md)} models")
    print(f"排除: {sorted(exclude) or '(无)'}")

    cursor = [k for k in allk if str(k.get("key_alias") or "").startswith(args.prefix)]
    todo = [k for k in cursor
            if k["key_alias"] not in exclude
            and not ((k.get("aliases") or {}) == ref_al
                     and sorted(k.get("models") or []) == ref_md)]
    print(f"{args.prefix}* 共 {len(cursor)} 把，已一致 {len(cursor) - len(todo) - len(exclude & {k['key_alias'] for k in cursor})}，待改 {len(todo)}")

    # 落盘备份：改前的 aliases+models，逐 key 可回滚
    bak = {"ref": {"key_alias": args.ref_alias, "aliases": ref_al, "models": ref_md},
           "exclude": sorted(exclude),
           "targets": [{"key_alias": k["key_alias"], "token": k["token"],
                        "aliases": k.get("aliases") or {},
                        "models": sorted(k.get("models") or [])} for k in todo]}
    if args.backup:
        with open(args.backup, "w") as fh:
            json.dump(bak, fh, ensure_ascii=False, indent=1)
        print(f"备份已写: {args.backup}")

    if args.dry_run:
        for k in todo:
            al = k.get("aliases") or {}
            print(f"  [dry] {k['key_alias']:30s} aliases {len(al)}->{len(ref_al)} "
                  f"models {len(k.get('models') or [])}->{len(ref_md)}")
        return
    if not args.backup:
        sys.exit("align 非 dry-run 必须给 --backup <path>（改前状态要落盘，否则无法回滚）")

    ok = fail = 0
    for i, k in enumerate(todo, 1):
        try:
            api("POST", "/key/update",
                {"key": k["token"], "aliases": ref_al, "models": ref_md})
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  FAIL {k['key_alias']}: {e}")
            if fail >= 2:
                print("!! 连续 2 次失败，停止并人工核查（fail>=2 stop 规程）!!")
                break
        if i % 20 == 0:
            print(f"  ...{i}/{len(todo)}")
    print(f"\n=== align done: ok={ok} fail={fail} ===")

    # 全量复查：重拉一遍确认落盘，并报残余 zerokey 引用
    fresh = [k for k in list_all_keys()
             if str(k.get("key_alias") or "").startswith(args.prefix)]
    bad = [k["key_alias"] for k in fresh
           if k["key_alias"] not in exclude
           and not ((k.get("aliases") or {}) == ref_al
                    and sorted(k.get("models") or []) == ref_md)]
    zkleft = [k["key_alias"] for k in fresh
              if any("zerokey" in str(v) for v in (k.get("aliases") or {}).values())]
    print(f"复查: {len(fresh)} 把，仍不一致(排除项外)={bad or '无'}")
    print(f"仍带 zerokey alias 的 key: {zkleft or '无'}")


def cmd_smoke(args):
    m = filter_only(fetch_pioneers(), args.only)
    if not m:
        sys.exit("smoke 需要 --only 指定至少一个先锋 key_alias")
    for name, tok in list(m.items())[:3]:
        for mdl in ("gpt-5.5", "gpt-5.6-sol"):
            body = {"model": mdl, "messages": [{"role": "user", "content": "hi"}],
                    "stream": True, "max_tokens": 12}
            req = urllib.request.Request(f"{BASE}/v1/chat/completions",
                                         data=json.dumps(body).encode(), method="POST")
            req.add_header("Authorization", f"Bearer {tok}")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    mid = r.headers.get("x-litellm-model-id")
                    print(f"{name} {mdl}: HTTP {r.status}  x-litellm-model-id={mid}")
            except Exception as e:  # noqa: BLE001
                print(f"{name} {mdl}: ERR {e}")


def main():
    p = argparse.ArgumentParser(description="zerokey 先锋 cursor key 批量切换/回滚")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "switch", "rollback", "smoke", "align"):
        sp = sub.add_parser(name)
        sp.add_argument("--only", help="逗号分隔的 key_alias（如 cursor-guran,cursor-linsen）")
        sp.add_argument("--dry-run", action="store_true")
        if name == "rollback":
            sp.add_argument("--ref", help="参照 key_alias（拷贝其 aliases+models）")
        if name == "align":
            sp.add_argument("--ref-alias", help="参照 LiteLLM key_alias，如 cursor-linsen-03gc")
            sp.add_argument("--exclude", default="",
                            help="逗号分隔的 LiteLLM key_alias，不改（如自己在用的 bridge key）")
            sp.add_argument("--prefix", default="cursor-", help="key_alias 前缀，默认 cursor-")
            sp.add_argument("--backup", help="改前状态落盘路径（非 dry-run 必填）")
    args = p.parse_args()
    {"status": cmd_status, "switch": cmd_switch, "rollback": cmd_rollback,
     "smoke": cmd_smoke, "align": cmd_align}[args.cmd](args)


if __name__ == "__main__":
    main()
