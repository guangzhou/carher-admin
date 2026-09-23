#!/usr/bin/env python3
"""给 198（ns litellm-product）建一个 chatgpt acct 池的模型组：每个可用号一行 deployment。

这是 2026-09-23 给 `chatgpt-gpt-6-luna` / `chatgpt-gpt-6-sol` 建组时实际跑的那份脚本，
从 198 `/home/cltx/add_sol_fleet.py` 泛化收进 git（此前它只在磁盘上，不在注册表里）。

⛔ 与 `scripts/chatgpt-pool-model/fanout-pool-model.sh` 的关键区别（别拿错）：
   - 那份会对**每个 acct `rollout restart`**。重启在服务的号可能**永久打死它且回退救不回**
     （acct-237）。本脚本**一个 pod 都不碰**，只写外层 router 行。
     acct pod 的 config.yaml 里没有这个 slug 是 CM 那一步没做完 —— 去做 CM + 受控 rollout，
     不要让建组脚本顺手帮你重启整池。
   - 那份先 `/model/delete` 再 `/model/new`。本脚本**幂等跳过已存在的 id**，不删。
   - 那份不写价格/窗口/base_model。漏了价格 ⇒ 计费按 0；漏 `max_input_tokens` ⇒ 闸门关掉。

🔴 `litellm_params.api_key` 必须带（secret `chatgpt-pool-master-key`）。漏了 acct pod 会返
   `No connected db.`，而它在用户面被 `error_sanitize.py` 打码成「API 异常 (req: xxxx)」,
   真因只在生产车道 proxy pod 日志里 `grep 'masked req=<id>'`。

用法：
    # ① 先用 survey 脚本产出 targets（带阳性/阴性对照，别手写这个文件）
    python3 chatgpt-pool-codex-slug-survey.py --slug gpt-6-sol --out ~/sol-targets.txt
    # ② dry-run 看计划 + 样本行
    python3 chatgpt-pool-add-codex-model.py --slug gpt-6-sol --targets ~/sol-targets.txt \
        --in-cost 2e-06 --out-cost 1e-05
    # ③ 写
    python3 chatgpt-pool-add-codex-model.py --slug gpt-6-sol --targets ~/sol-targets.txt \
        --in-cost 2e-06 --out-cost 1e-05 --apply
    # ④ 回滚（只撤 targets 里这些 id，不会误删别的组）
    python3 chatgpt-pool-add-codex-model.py --slug gpt-6-sol --targets ~/sol-targets.txt --delete

建完组还有三步，本脚本**不做**，见 skill `chatgpt-pool-codex-slug-onboard`：
兜底链（这类 slug 会翻面，兜底是硬要求）→ key 白名单+alias → 三层验收。
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

NS_DEFAULT = "litellm-product"
PROXY_DEFAULT = "http://127.0.0.1:30402"


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("cmd failed: %s\n%s" % (cmd, r.stderr[:300]))
    return r.stdout.strip()


def build_row(acct, group, slug, ns, api_key, in_cost, out_cost, max_in, max_out):
    """一行 deployment。形状对齐现有 chatgpt-gpt-6-astra 族，逐字段照抄，别自创。"""
    mid = "chatgpt-acct-%s-%s" % (acct, slug)
    return mid, {
        "model_name": group,
        "litellm_params": {
            # 🔴 这里是 openai/<组名>，不是 openai/<slug>。acct pod 内部才把组名翻成
            #    chatgpt/<slug>。写错会在 acct pod 侧报 Invalid model name。
            "model": "openai/" + group,
            "api_base": "http://chatgpt-acct-%s.%s.svc.cluster.local:4000" % (acct, ns),
            "api_key": api_key,
            "input_cost_per_token": in_cost,
            "output_cost_per_token": out_cost,
        },
        "model_info": {
            "id": mid,
            "base_model": slug,
            "mode": "responses",
            "max_input_tokens": max_in,
            "max_output_tokens": max_out,
            "input_cost_per_token": in_cost,
            "output_cost_per_token": out_cost,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True, help="上游 codex slug，如 gpt-6-sol")
    ap.add_argument("--group", default=None, help="198 上的组名，默认 chatgpt-<slug>")
    ap.add_argument("--targets", required=True,
                    help="每行一个 acct 号；用 chatgpt-pool-codex-slug-survey.py 产出，别手写")
    ap.add_argument("--in-cost", type=float, required=True,
                    help="input_cost_per_token（官方价 / 1M。漏了按 0 计费）")
    ap.add_argument("--out-cost", type=float, required=True, help="output_cost_per_token")
    ap.add_argument("--max-in", type=int, default=872000,
                    help="上游 max_context_window。⛔ 别填 10000000，那是把闸门关掉")
    ap.add_argument("--max-out", type=int, default=128000)
    ap.add_argument("--ns", default=NS_DEFAULT)
    ap.add_argument("--proxy", default=PROXY_DEFAULT)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--delete", action="store_true", help="撤回 targets 里这些 id（回滚）")
    ap.add_argument("--no-sudo", action="store_true")
    args = ap.parse_args()

    if args.max_in >= 10_000_000:
        sys.exit("🔴 max_input_tokens >= 1e7 = 把闸门关掉，不是能力声明。拒跑。")
    group = args.group or ("chatgpt-" + args.slug)
    k = ("kubectl " if args.no_sudo else "sudo -n kubectl ") + "-n %s " % args.ns
    mk = sh(k + "get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d")
    ak = sh(k + "get secret chatgpt-pool-master-key "
                "-o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d")
    if len(mk) != 39:
        sys.exit("🔴 master key 长度 %d（期望 39）—— 取错了，别往下写" % len(mk))
    if not ak:
        sys.exit("🔴 chatgpt-pool-master-key 取空 —— 漏了它 acct pod 会返 No connected db.")
    H = {"Authorization": "Bearer " + mk, "Content-Type": "application/json"}

    def post(path, body):
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                args.proxy + path, data=json.dumps(body).encode(), headers=H), timeout=60)
            return r.status, r.read()[:120].decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:200].decode(errors="replace")

    accts = [l.strip() for l in open(args.targets) if l.strip() and not l.startswith("#")]
    if not accts:
        sys.exit("targets 为空 —— survey 说 0 个可用就不该建组")

    def row(a):
        return build_row(a, group, args.slug, args.ns, ak,
                         args.in_cost, args.out_cost, args.max_in, args.max_out)

    existing = set()
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(
            args.proxy + "/model/info", headers=H), timeout=60).read())["data"]
        existing = {m["model_info"].get("id") for m in d if m["model_name"] == group}
    except Exception as e:
        # 🔴 读不到不等于没有。读失败就不许往下写（否则可能建重复行）。
        sys.exit("🔴 /model/info 读失败，无法判重，拒跑: %s" % e)

    print("group=%s slug=%s targets=%d 库里已有本组 %d 行" % (group, args.slug, len(accts), len(existing)))

    if args.delete:
        n = 0
        for a in accts:
            mid, _ = row(a)
            if mid in existing:
                c, t = post("/model/delete", {"id": mid})
                n += (c == 200)
                print("  delete", mid, c, t[:60])
        print("deleted=%d（只动 targets 里的 id，别的组没碰）" % n)
        return 0

    todo = [a for a in accts if row(a)[0] not in existing]
    print("already=%d todo=%d" % (len(accts) - len(todo), len(todo)))
    if not args.apply:
        print("\nDRY-RUN。样本行：")
        print(json.dumps(row(todo[0])[1], indent=1, ensure_ascii=False) if todo else "(没有要建的)")
        return 0

    ok = bad = 0
    for a in todo:
        mid, body = row(a)
        c, t = post("/model/new", body)
        if c == 200:
            ok += 1
        else:
            bad += 1
            print("FAIL", mid, c, t)
    print("added=%d failed=%d" % (ok, bad))
    print("\n⚠️ `/model/info` 紧接着读可能只回到一部分（写入传播中的快照，不是漏写）。"
          "\n   判行数要么等一会儿重读，要么换尺子直接查 LiteLLM_ProxyModelTable。"
          "\n⚠️ 组在表里 ≠ 能用。下一步必须带唯一 nonce 实打一次，再配兜底链。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
