#!/usr/bin/env python3
r"""验收 19 条 `kiro-*` 道的窗口/输出上限/价格 —— **逐在役副本**，判据是逐格相等。

为什么要有这个脚本
------------------
2026-10-01 改完 CM 后要证明三件事同时成立，而每一件都踩过已知的假绿：

1. 🔴 **只查一个副本 = 另一个可能在跑旧配置**（共用 CM 的多副本必须全查）。
2. 🔴 **`Terminating` 的副本会被算进验收 ⇒ 假红**（更新脚本吃过这一口，
   见 `project_198_midstream_budget_and_minimal_normalize_2026_09_28`）。
   本脚本只取 `Running` 且 `ready=true` 且**属于当前 RS** 的 pod。
3. 🔴 **`/model_group/info` 返回的 cap 是组内最大值，不是闸门** ⇒ 判据只能是
   `/v1/model/info` 的逐行 `model_info`，并且按 `model_info.id` 对齐，不按组名。
4. 🔴 **计费读 `litellm_params`**，只核 `model_info` 的价会漏 ——
   两块都要核（`litellm-198-fill-missing-prices.py` 的实证）。

取值真源同样是 kiro 自己的 catalog，不硬编。

用法
----
    python3 scripts/kiro-probe.py catalog --json > /tmp/kiro-cat.json
    python3 scripts/kiro-lane-limits-verify.py --catalog /tmp/kiro-cat.json
    python3 scripts/kiro-lane-limits-verify.py --catalog /tmp/kiro-cat.json --expect-bytes 66237

退出码：0 全绿 / 1 有不符 / 2 量具自己坏了（读不到副本或读不到行 ⇒ 作废这一轮）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# 198 的 22 口只在公司 IDC 内网开。不在内网时经一台能通的跳板走 ProxyJump：
#   export KIRO_SSH_JUMP=pubSrv
# 不设则保持原来的直连行为。
_JUMP = os.environ.get("KIRO_SSH_JUMP", "").strip()
SSH = ["ssh"] + (["-J", _JUMP] if _JUMP else []) + ["cltx@10.68.13.198"]
NS = "litellm-product"

# Anthropic / OpenAI 官方第一方价（claude.com/pricing 与 litellm
# model_prices_and_context_window.json 的第一方条目，2026-10-01 逐格核对一致）。
# 单位 USD/token。只列有第一方权威源的；glm-5 / minimax / qwen3-coder-next 在
# litellm 表里没有可比的第一方条目，**不在这里判对错**（只打印）。
OFFICIAL = {
    "claude-opus-5.5":  (4e-06, 2e-05,   2e-07, 5e-06),
    "claude-opus-5":    (5e-06, 2.5e-05, 5e-07, 6.25e-06),
    "claude-opus-4.8":  (5e-06, 2.5e-05, 5e-07, 6.25e-06),
    "claude-opus-4.7":  (5e-06, 2.5e-05, 5e-07, 6.25e-06),
    "claude-opus-4.6":  (5e-06, 2.5e-05, 5e-07, 6.25e-06),
    "claude-opus-4.5":  (5e-06, 2.5e-05, 5e-07, 6.25e-06),
    "claude-sonnet-5":  (2e-06, 1e-05,   2e-07, 2.5e-06),
    "claude-sonnet-4.6": (3e-06, 1.5e-05, 3e-07, 3.75e-06),
    "claude-sonnet-4.5": (3e-06, 1.5e-05, 3e-07, 3.75e-06),
    "claude-haiku-4.5": (1e-06, 5e-06,   1e-07, 1.25e-06),
    "gpt-5.6-sol":      (4e-06, 2e-05,   4e-07, 5e-06),
    "gpt-5.6-terra":    (2e-06, 1.2e-05, 2e-07, 2.5e-06),
    "gpt-5.6-luna":     (2e-07, 1.2e-06, 2e-08, 2.5e-07),
}
PRICE_FIELDS = ("input_cost_per_token", "output_cost_per_token",
                "cache_read_input_token_cost", "cache_creation_input_token_cost")


def sh(script: str) -> str:
    r = subprocess.run(SSH + ["bash -s"], input=script, capture_output=True, text=True)
    if r.stderr.strip():
        print(r.stderr.rstrip(), file=sys.stderr)
    if r.returncode:
        sys.exit(f"2: 远端命令失败（exit {r.returncode}）")
    return r.stdout


def live_pods() -> list[str]:
    """只取**当前 RS** 且 Running+ready 的副本 —— Terminating/旧 RS 一律排掉。"""
    # 🔴 两个踩过的坑都在这几行里：
    # ① 不要 `describe | awk '/NewReplicaSet/'` —— `Conditions` 里的
    #    `NewReplicaSetAvailable` 同样含这个词 ⇒ RS 名变成 "True\n<名字>"，
    #    后续比对恒不命中 ⇒ 「一个副本都没读到」的假红。
    # ② **`app=litellm-proxy` 这个标签只在 Pod 上，RS 上没有**（同族纪律见
    #    `feedback_prod_lane_label_is_on_pods_not_deployments`）⇒ `get rs -l app=...`
    #    返回空，按 revision 找 RS 这条路直接断掉。
    # ⇒ 当前 RS 从 **deploy 的 selector 查 RS**（不带 app 标签），再用
    #   Pod 自己的 `pod-template-hash` 对齐，Terminating / 旧 hash 一律排掉。
    out = sh(f"""
set -e
REV=$(sudo kubectl -n {NS} get deploy litellm-proxy \
      -o jsonpath='{{.metadata.annotations.deployment\\.kubernetes\\.io/revision}}')
[ -n "$REV" ] || {{ echo "NO_REVISION" >&2; exit 3; }}
RS=$(sudo kubectl -n {NS} describe deploy litellm-proxy | grep '^NewReplicaSet:' | awk '{{print $2}}')
[ -n "$RS" ] || {{ echo "NO_NEW_RS" >&2; exit 3; }}
HASH=$(sudo kubectl -n {NS} get rs "$RS" -o jsonpath='{{.metadata.labels.pod-template-hash}}')
RS_REV=$(sudo kubectl -n {NS} get rs "$RS" \
         -o jsonpath='{{.metadata.annotations.deployment\\.kubernetes\\.io/revision}}')
[ -n "$HASH" ] || {{ echo "NO_HASH_ON_$RS" >&2; exit 3; }}
# 断言这个 RS 真的是 deploy 当前那一版，不然就是读到了别人刚切的
[ "$RS_REV" = "$REV" ] || {{ echo "RS_REV_$RS_REV!=DEPLOY_REV_$REV" >&2; exit 3; }}
echo "# deploy revision=$REV RS=$RS pod-template-hash=$HASH" >&2
# 🔴 这里用 custom-columns 而不是 jsonpath：`pod-template-hash` 里的连字符会被
# jsonpath 当成减号，`{{.metadata.labels.pod-template-hash}}` 整条表达式静默失配、
# **每个字段都回空**（不报错）⇒ 看起来像「一个副本都没有」。2026-10-01 踩过。
sudo kubectl -n {NS} get po -l app=litellm-proxy --no-headers \
  -o custom-columns='N:.metadata.name,H:.metadata.labels.pod-template-hash,PH:.status.phase,RD:.status.containerStatuses[0].ready,DEL:.metadata.deletionTimestamp' \
  | awk -v h="$HASH" '$2==h && $3=="Running" && $4=="true" && ($5=="<none>" || $5=="") {{print $1}}'
""")
    pods = [l.strip() for l in out.split("\n") if l.strip()]
    if not pods:
        sys.exit("2: 当前 RS 里一个在役副本都没读到 ⇒ 空输出不是阴性结论，作废这一轮")
    return pods


RT_PY = r"""
import json, os, urllib.request
key = os.environ["LITELLM_MASTER_KEY"]
req = urllib.request.Request("http://127.0.0.1:4000/v1/model/info",
                             headers={"Authorization": "Bearer " + key})
rows = []
for r in json.load(urllib.request.urlopen(req, timeout=180))["data"]:
    mi = r.get("model_info") or {}
    if not str(mi.get("id", "")).startswith("kiro-rs/"):
        continue
    lp = r.get("litellm_params") or {}
    rows.append({"id": mi.get("id"), "group": r.get("model_name"),
                 "mi": mi.get("max_input_tokens"), "mo": mi.get("max_output_tokens"),
                 "up": mi.get("kiro_upstream_model_id"),
                 "mi_price": {k: mi.get(k) for k in
                              ("input_cost_per_token", "output_cost_per_token",
                               "cache_read_input_token_cost",
                               "cache_creation_input_token_cost")},
                 "lp_price": {k: lp.get(k) for k in
                              ("input_cost_per_token", "output_cost_per_token",
                               "cache_read_input_token_cost",
                               "cache_creation_input_token_cost")}})
print(json.dumps(rows))
"""


def read_pod(pod: str) -> tuple[list[dict], int]:
    """读一个副本的 /model/info（kiro 道）与 /app/config.yaml 字节数。"""
    import base64
    b64 = base64.b64encode(RT_PY.encode()).decode()
    out = sh(f"""
set -e
MK=$(sudo kubectl -n {NS} exec {pod} -- printenv LITELLM_MASTER_KEY)
echo {b64} | base64 -d > /tmp/kiro_rt_$$.py
sudo kubectl -n {NS} cp /tmp/kiro_rt_$$.py {pod}:/tmp/kiro_rt.py
rm -f /tmp/kiro_rt_$$.py
echo "---BYTES---"
sudo kubectl -n {NS} exec {pod} -- wc -c /app/config.yaml | awk '{{print $1}}'
echo "---JSON---"
sudo kubectl -n {NS} exec {pod} -- env LITELLM_MASTER_KEY="$MK" python3 /tmp/kiro_rt.py
""")
    if "---JSON---" not in out:
        sys.exit(f"2: {pod} 读不到 /model/info（输出里没有分隔符）⇒ 作废这一轮")
    head, body = out.split("---JSON---", 1)
    nbytes = int(head.split("---BYTES---", 1)[1].strip().split("\n")[0])
    rows = json.loads(body.strip().split("\n")[-1])
    if not rows:
        sys.exit(f"2: {pod} 的 /model/info 里 0 条 kiro 道 ⇒ 空列表是假红，作废这一轮")
    return rows, nbytes


def load_catalog(path: str) -> dict[str, dict]:
    """读 `kiro-probe.py catalog --json` 的输出，**合并整池每个号的目录**。

    🔴 原来只取 `raw.find("{")` 那**第一份**目录 —— 这是一个会随池子顺序翻面的假红：
    10-01 池子第一个号是付费档（20 个模型）⇒ 跑得通；
    10-04 第一个号变成 **FREE 档（只有 9 个模型）**⇒ 每一条 opus/sonnet-5/gpt 道
    全被判「catalog 里没有上游 X」，**19 条道里 13 条报红，而道本身完全没问题**。
    （FREE 档只有 9 个模型这件事见 SKILL §0「档位↔模型」。）
    ⇒ 判「上游有没有这个模型」必须按**整池并集**，而不是某一个号 ——
    谁排第一是池子状态，不是尺子该依赖的东西。
    ⚠️ 并集里同一个 modelId 的 tokenLimits 如果各号不一致，直接报错停手
    （那意味着档位之间窗口有差异，本脚本的「逐格相等」判据就不成立了）。
    """
    raw = Path(path).read_text(encoding="utf8")
    dec = json.JSONDecoder()
    out: dict[str, dict] = {}
    seen = 0
    i = 0
    while True:
        j = raw.find("{", i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(raw[j:])
            i = j + end
        except ValueError:
            i = j + 1
            continue
        models = obj.get("models") or []
        if not models:
            continue
        seen += 1
        for m in models:
            mid, tl = m["modelId"], (m.get("tokenLimits") or {})
            if mid in out and out[mid] != tl:
                sys.exit(f"2: 上游 {mid} 在不同号上 tokenLimits 不一致："
                         f"{out[mid]} vs {tl} ⇒ 「逐格相等」判据不成立，停手")
            out[mid] = tl
    if not out:
        sys.exit("2: catalog 里一个模型都没读到 ⇒ 空列表假红，作废这一轮")
    print(f"# catalog 并集：{seen} 份目录 -> {len(out)} 个上游模型")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--expect-bytes", type=int,
                    help="新 CM 的字节数；给了就断言每个在役副本都挂上了它")
    ap.add_argument("--expect-lanes", type=int, default=19)
    a = ap.parse_args()

    cat = load_catalog(a.catalog)
    pods = live_pods()
    print(f"# 在役副本 {len(pods)} 个：{', '.join(pods)}")

    bad = 0
    for pod in pods:
        rows, nbytes = read_pod(pod)
        tag = f"{pod} ({len(rows)} 条 kiro 道, config.yaml {nbytes}B)"
        if a.expect_bytes and nbytes != a.expect_bytes:
            print(f"🔴 {tag} —— 字节数 != 期望 {a.expect_bytes} ⇒ 这个副本还在跑旧 CM")
            bad += 1
            continue
        if len(rows) != a.expect_lanes:
            print(f"🔴 {tag} —— 道数 != {a.expect_lanes}")
            bad += 1
            continue
        errs = []
        for r in rows:
            up, group = r["up"], r["group"]
            tl = cat.get(up)
            if tl is None:
                errs.append(f"{group}: catalog 里没有上游 {up}")
                continue
            if r["mi"] != tl.get("maxInputTokens"):
                errs.append(f"{group}: max_input {r['mi']} != catalog {tl.get('maxInputTokens')}")
            if r["mo"] != tl.get("maxOutputTokens"):
                errs.append(f"{group}: max_output {r['mo']} != catalog {tl.get('maxOutputTokens')}")
            off = OFFICIAL.get(up)
            if off:
                for sec in ("lp_price", "mi_price"):
                    got = r[sec]
                    for f, want in zip(PRICE_FIELDS, off):
                        have = got.get(f)
                        # litellm_params 里 cache 两个字段本来就可能不存在（只在 model_info）
                        if have is None and sec == "lp_price" and f.startswith("cache_"):
                            continue
                        if have is None or abs(float(have) - want) > want * 1e-9:
                            errs.append(f"{group}.{sec}.{f} = {have} != 官方 {want}")
        if errs:
            print(f"🔴 {tag}")
            for e in errs:
                print(f"     {e}")
            bad += 1
        else:
            print(f"✅ {tag} —— 窗口/输出上限与 catalog 逐格相等；"
                  f"13 条有第一方价的道两块价全部 == 官方")

    print()
    if bad:
        print(f"🔴 {bad}/{len(pods)} 个在役副本不符")
        return 1
    print(f"✅ {len(pods)}/{len(pods)} 个在役副本全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
