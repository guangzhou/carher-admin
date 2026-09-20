#!/usr/bin/env python3
"""阿里云 litellm(ns carher):单把 key 的模型重指向 —— 真相表 / 改 alias / 判别式探针。

场景:「把 carher-N 的某个模型(族)改打某个上游」。只动 **LiteLLM 的 key**,
不碰 CM、不 rollout、不碰 her 配置 / HerInstance CRD。
2026-09-10 实战定型(carher-1 的 opus 从 ChatGPT 账户池切网宿直连)。

**在 k8s-work-226 宿主机上执行**(需要 kubectl:既要读 secret 取 master key,
也要读 CM 解析每个组的真实上游)。Mac 侧投递:

    B64=$(base64 < scripts/litellm-aliyun-key-repoint.py | tr -d '\n')
    echo "$B64" | jms ssh k8s-work-226 "base64 -d > /tmp/repoint.py && python3 /tmp/repoint.py <子命令>"

子命令
------
    groups [--grep PAT]
        只读。从 CM `litellm-config` 解析 model_list,输出
        组名 → litellm_params.model / api_base / model_info.id。
        ⚠ **必跑这一条再决定 target**:阿里云 CM 里组名与真实上游大面积不一致
        (见下方"组名会撒谎"),按名字猜 provider 必然选错。

    inspect --key carher-1
        只读。打印该 key 的 models allowlist、aliases,并把每条 alias 的
        target(以及没有 alias 的裸名)解析到真实上游 —— 一眼看出每个模型现在打谁。

    plan   --key carher-1 --map 'claude-opus-4-8='
    apply  --key carher-1 --map 'claude-opus-4-8=' [--allow a,b]
        改 per-key alias。`--map NAME=TARGET` 可重复;
        **TARGET 留空 = 摘掉该条 alias**,让裸名落到同名真实组
        (当目标组本身就是想要的上游时,这是最干净的做法,别写自指 alias)。
        `--allow` 把名字 union 进 models allowlist(只增不删)。
        apply 会先落备份到 /root/litellm-key-repoint/<alias>-<ts>.json。

    probe  --key carher-1 --model claude-opus-4-8 --expect wangsu-direct5/claude-opus-4-8
        判别式验收。**key 明文取不到(库里只有 hash)**,所以造一把复刻目标 key
        当前形状(同 models + 同 aliases)的临时 key,对**每一个** proxy pod
        各打一发真推理,判据 = 响应头 `x-litellm-model-id`,并校验唯一 nonce
        真的被吐回来。跑完必删临时 key 并断言零残留。

    rollback --key carher-1 [--backup FILE]
        用备份里的 aliases + models 整体写回(默认取该 key 最新一份备份)。

组名会撒谎(2026-09-10 实测,别按名字猜)
-------------------------------------
    claude-opus-4-8                → 网宿  wangsu-direct5/claude-opus-4-8
    openrouter-claude-opus-4-8     → **网宿**,不是 OpenRouter
    anthropic.claude-opus-4-7      → **快汇** kuaihuiai.com,不是网宿
    wangsu-gemini-3.1-pro-preview  → 真实 model 是 **gemini-3.5-flash**
    wangsu-glm-5.1                 → 真实 model 是 **glm-5.2**
判据只有三样:`litellm_params.api_base`、`litellm_params.model`、`model_info.id`。

其它红线
--------
  - `/key/update` 的 `models` / `aliases` / `router_settings` 都是**整字段替换**,
    必须 caller-side merge;本脚本写前断言"除目标外逐条相同、零删除"。
  - alias 的 target **不需要**在 models allowlist 里(实测:carher-1 的
    `chatgpt-gpt-5.6-terra` 不在 allowlist 却正常落地)。
  - 摘 alias 让裸名落同名组之前,先确认 CM 里 `model_group_alias` 为空 ——
    它会无条件压过同名真实组。本脚本 plan/apply 会替你查。
  - `carher-N` 这个 key alias **不保证有对应 HerInstance**(CRD 名是 `her-N`,
    且可能根本不存在),别拿 CRD 当 key 的索引,也别指望从 CRD 掏 key 明文。
  - her key 只在阿里云有效,198 上的同名 `carher-*` 不被使用。
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

NS = "carher"
BASE = "http://127.0.0.1:4000"
BACKUP_DIR = "/root/litellm-key-repoint"


# ---------------------------------------------------------------- 集群/HTTP

def sh(cmd):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"[FATAL] {cmd}\n{p.stderr.strip()}")
    return p.stdout


def master_key():
    raw = sh(
        f"kubectl -n {NS} get secret litellm-secrets "
        "-o jsonpath='{.data.LITELLM_MASTER_KEY}'"
    ).strip()
    return base64.b64decode(raw).decode()


def proxy_pod_ips():
    out = sh(
        f"kubectl -n {NS} get pods -l app=litellm-proxy "
        "--field-selector=status.phase=Running "
        "-o jsonpath='{range .items[*]}{.metadata.name}={.status.podIP} {end}'"
    ).split()
    return [x.split("=", 1) for x in out if "=" in x]


def http(method, path, body=None, key=None, base=BASE, timeout=180):
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers), e.read().decode()
        except Exception:
            return e.code, {}, ""


def api(method, path, body=None, key=None):
    st, _, txt = http(method, path, body, key)
    if st != 200:
        sys.exit(f"[FATAL] {method} {path} -> HTTP {st}\n{txt[:600]}")
    return json.loads(txt) if txt.strip() else {}


# ---------------------------------------------------------------- CM 真相表

def load_cm():
    """返回 (group_name -> 真实上游 dict, model_group_alias)。"""
    import yaml  # 226 宿主机自带;缺就 pip install pyyaml

    raw = sh(
        f"kubectl -n {NS} get cm litellm-config "
        "-o jsonpath='{.data.config\\.yaml}'"
    )
    cfg = yaml.safe_load(raw)
    groups = {}
    for m in cfg.get("model_list", []):
        lp = m.get("litellm_params", {}) or {}
        groups.setdefault(m["model_name"], []).append(
            {
                "model": lp.get("model"),
                "api_base": lp.get("api_base"),
                "id": (m.get("model_info") or {}).get("id"),
            }
        )
    mga = (cfg.get("router_settings") or {}).get("model_group_alias") or cfg.get(
        "model_group_alias"
    )
    return groups, mga


def upstream_str(groups, name):
    hits = groups.get(name)
    if not hits:
        return "!! CM 里无此组"
    if len(hits) == 1:
        h = hits[0]
        return f"{h['id'] or '-'}  model={h['model']}  base={h['api_base'] or '-'}"
    return f"池({len(hits)} 腿) ids=" + ",".join(str(h["id"]) for h in hits[:6])


# ---------------------------------------------------------------- key 读写

def find_key(mk, alias):
    rows = api("GET", "/spend/keys?limit=100000", key=mk)
    hits = [r for r in rows if (r.get("key_alias") or "") == alias]
    if not hits:
        sys.exit(f"[FATAL] 没找到 key_alias == {alias}")
    if len(hits) > 1:
        sys.exit(f"[FATAL] {alias} 命中 {len(hits)} 把 key,拒绝盲改")
    return hits[0]["token"]


def key_info(mk, token):
    return api("GET", f"/key/info?key={token}", key=mk)["info"]


def backup(alias, info):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = f"{BACKUP_DIR}/{alias}-{time.strftime('%Y%m%dT%H%M%S')}.json"
    with open(path, "w") as f:
        json.dump(info, f, ensure_ascii=False, indent=1, default=str)
    return path


def latest_backup(alias):
    import glob

    hits = sorted(glob.glob(f"{BACKUP_DIR}/{alias}-*.json"))
    if not hits:
        sys.exit(f"[FATAL] {BACKUP_DIR} 下没有 {alias} 的备份")
    return hits[-1]


def parse_maps(pairs):
    out = {}
    for p in pairs:
        if "=" not in p:
            sys.exit(f"[FATAL] --map 要写成 NAME=TARGET(TARGET 留空表示摘掉):{p}")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def compute(info, maps, allow):
    """返回 (new_aliases, new_models, 变更说明行)。纯函数,不写。"""
    cur_a, cur_m = dict(info["aliases"]), list(info["models"])
    new_a, lines = dict(cur_a), []
    for name, target in maps.items():
        old = cur_a.get(name)
        if target == "":
            if name in new_a:
                new_a.pop(name)
                lines.append(f"  摘除 alias  {name}: {old!r}  ⇒ 裸名落同名真实组")
            else:
                lines.append(f"  跳过        {name} 本来就没有 alias")
        else:
            if old == target:
                lines.append(f"  跳过        {name} 已经是 {target}")
            else:
                new_a[name] = target
                lines.append(f"  重指        {name}: {old!r} ⇒ {target!r}")
    new_m = list(cur_m)
    for a in allow:
        if a not in new_m:
            new_m.append(a)
            lines.append(f"  白名单 +    {a}")
    # 断言:除被点名的以外逐条相同;models 零删除
    untouched = set(cur_a) - set(maps)
    assert all(new_a.get(k) == cur_a[k] for k in untouched), "误伤了未点名的 alias"
    assert set(new_a) - set(cur_a) <= set(maps), "凭空多出 alias"
    assert set(cur_m) <= set(new_m), "models 出现删除"
    return new_a, new_m, lines


# ---------------------------------------------------------------- 子命令

def cmd_groups(a):
    groups, mga = load_cm()
    print(f"# CM 共 {len(groups)} 个组;model_group_alias = {mga!r}")
    for name in sorted(groups):
        if a.grep and a.grep not in name:
            continue
        print(f"{name:42s} {upstream_str(groups, name)}")


def cmd_inspect(a):
    mk = master_key()
    groups, mga = load_cm()
    info = key_info(mk, find_key(mk, a.key))
    al = info["aliases"]
    print(f"# {a.key}  models={len(info['models'])}  aliases={len(al)}")
    print(f"# model_group_alias = {mga!r}  (非空则会压过同名真实组)")
    print(f"# per-key router_settings = {info.get('router_settings')!r}")
    print("\n请求名 → 实际落点:")
    for name in sorted(info["models"]):
        tgt = al.get(name, name)
        via = "" if name in al else "  (无 alias,裸名直落)"
        print(f"  {name:26s} → {tgt:32s} {upstream_str(groups, tgt)}{via}")
    extra = sorted(set(al) - set(info["models"]))
    if extra:
        print("\n⚠ 有 alias 但不在 allowlist 的请求名(通常是死条目):", extra)


def cmd_plan(a, do_apply=False):
    mk = master_key()
    groups, mga = load_cm()
    token = find_key(mk, a.key)
    info = key_info(mk, token)
    maps, allow = parse_maps(a.map), [x for x in (a.allow or "").split(",") if x]
    new_a, new_m, lines = compute(info, maps, allow)

    print(f"# {a.key}  token={token[:12]}…")
    print("\n".join(lines) or "  (无变更)")
    print("\n变更后落点:")
    for name in sorted(set(maps) | set(allow)):
        tgt = new_a.get(name, name)
        print(f"  {name:26s} → {tgt:32s} {upstream_str(groups, tgt)}")
        if tgt not in groups:
            print(f"    !! CM 里没有组 {tgt},这条会 400")
    if mga:
        print(f"\n⚠ model_group_alias 非空({mga!r}):裸名可能被它改写,别只靠摘 alias")

    if not do_apply:
        print("\n(plan 模式,未写入)")
        return
    if new_a == info["aliases"] and sorted(new_m) == sorted(info["models"]):
        print("\n已经是目标状态,不写。")
        return
    path = backup(a.key, info)
    print(f"\n备份 → {path}")
    api("POST", "/key/update", {"key": token, "aliases": new_a, "models": new_m}, key=mk)
    back = key_info(mk, token)
    ok = back["aliases"] == new_a and sorted(back["models"]) == sorted(new_m)
    print("回读一致:", ok)
    sys.exit(0 if ok else 1)


def cmd_apply(a):
    cmd_plan(a, do_apply=True)


def cmd_probe(a):
    mk = master_key()
    info = key_info(mk, find_key(mk, a.key))
    nonce = hashlib.sha1(str(time.time()).encode()).hexdigest()[:12]
    palias = f"zz-probe-{a.key}-{nonce}"
    tmp = api(
        "POST",
        "/key/generate",
        {
            "key_alias": palias,
            "models": info["models"],
            "aliases": info["aliases"],
            "duration": "15m",
        },
        key=mk,
    )["key"]
    print(f"# 临时探针 key {palias}  复刻 {a.key} 的 models+aliases")

    failures = []
    try:
        for pod, ip in proxy_pod_ips():
            st, hdr, body = http(
                "POST",
                "/v1/chat/completions",
                {
                    "model": a.model,
                    "max_tokens": 24,
                    "messages": [
                        {
                            "role": "user",
                            "content": f"reply with exactly this token and nothing else: {nonce}",
                        }
                    ],
                },
                key=tmp,
                base=f"http://{ip}:4000",
                timeout=180,
            )
            mid = hdr.get("x-litellm-model-id", "")
            abase = hdr.get("x-litellm-model-api-base", "")
            try:
                txt = json.loads(body)["choices"][0]["message"]["content"]
            except Exception:
                txt = body[:160]
            hit_id = (not a.expect) or mid == a.expect
            hit_nonce = nonce in str(txt)
            print(
                f"  {pod:34s} HTTP {st}  id={mid or '-'}  base={abase or '-'}\n"
                f"    text={str(txt)[:70]!r}  模型判据={'PASS' if hit_id else 'FAIL'}"
                f"  真吐字={'PASS' if hit_nonce else 'FAIL'}"
            )
            if st != 200 or not hit_id or not hit_nonce:
                failures.append(pod)
    finally:
        api("POST", "/key/delete", {"keys": [tmp]}, key=mk)
        rows = api("GET", "/spend/keys?limit=100000", key=mk)
        left = [r["key_alias"] for r in rows if (r.get("key_alias") or "").startswith("zz-probe")]
        print("探针残留:", left or "无")
        if left:
            failures.append("探针未清干净")

    print("\n结论:", "全绿" if not failures else f"FAIL {failures}")
    sys.exit(0 if not failures else 1)


def cmd_rollback(a):
    mk = master_key()
    token = find_key(mk, a.key)
    path = a.backup or latest_backup(a.key)
    old = json.load(open(path))
    print(f"# 从 {path} 恢复 {a.key}")
    print("  aliases →", json.dumps(old["aliases"], ensure_ascii=False))
    api(
        "POST",
        "/key/update",
        {"key": token, "aliases": old["aliases"], "models": old["models"]},
        key=mk,
    )
    back = key_info(mk, token)
    ok = back["aliases"] == old["aliases"] and sorted(back["models"]) == sorted(old["models"])
    print("回读一致:", ok)
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("groups", help="CM 组名 → 真实上游 真相表")
    p.add_argument("--grep", default="")
    p.set_defaults(fn=cmd_groups)

    p = sub.add_parser("inspect", help="key 的每个请求名现在打谁")
    p.add_argument("--key", required=True)
    p.set_defaults(fn=cmd_inspect)

    for name, fn in (("plan", cmd_plan), ("apply", cmd_apply)):
        p = sub.add_parser(name, help="改 per-key alias / 扩 allowlist")
        p.add_argument("--key", required=True)
        p.add_argument("--map", action="append", default=[], help="NAME=TARGET;TARGET 留空=摘掉 alias")
        p.add_argument("--allow", default="", help="逗号分隔,union 进 models(只增不删)")
        p.set_defaults(fn=fn)

    p = sub.add_parser("probe", help="临时 key 复刻形状,逐 pod 判 x-litellm-model-id")
    p.add_argument("--key", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--expect", default="", help="期望的 x-litellm-model-id,如 wangsu-direct5/claude-opus-4-8")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("rollback", help="从备份整体写回 aliases+models")
    p.add_argument("--key", required=True)
    p.add_argument("--backup", default="")
    p.set_defaults(fn=cmd_rollback)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
