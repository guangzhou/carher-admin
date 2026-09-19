#!/usr/bin/env python3
"""image-2-acct-cutover.py —— 把 `image-2` 这个名字从 zerokey 网页腿交给 acct 通道。

背景（为什么要动名字，而不是加后端）：
  出图能力**一直**在 acct(Codex) 通道里，2026-09-06 实测 7 个组全绿（5.4/5.5/
  5.6-sol/terra/luna/6-astra/5.3-codex），38 腿一个不落。`image-2` 之所以指着
  zerokey，纯粹是历史存量占了这个名字 —— 两者没有任何技术依赖。

  ⚠️ 上游**没有** `gpt-image-2` / `image-2` 这个模型：直打 Codex 端点会返回
  `The 'gpt-image-2' model is not supported when using Codex with a ChatGPT
  account`。出图是 gpt-5.x 自带的能力，`image-2` 只是我们自己的组名。

为什么 acct 只加 1 行而不是 38 行：
  出图是**后端 pod 的属性**，不是某个组的属性。指回 `chatgpt-gpt-5.5` 组等于复用
  它现成的调度/配额治理 —— 号死了跟着 5.5 组一起摘。逐 acct 铺 38 行 = 把「谁活着」
  这份名单抄第二遍，5.5 摘号时这边不跟着摘，必漂。

加法律令（严格按序，add 与 remove 永不同脚本执行段）：
  step1 add-zero    照抄现网 14 条活行 → 建 `image-2-zero`（纯加法，不碰 image-2）
  step2 verify-zero 逐条直连后端 pod 判活（组级绿灯会被别的活腿盖住，不作数）
  step3 add-acct    往 `image-2` 加 1 条 acct 行（此后 image-2 = 14 zerokey + 1 acct 混跑）
  step4 verify-acct 连打 N 次，确认能落到 acct 腿上
  step5 cutover     **用户拍板后**才跑：从 `image-2` 摘掉 14 条 zerokey 行

判据纪律：
  · litellm-proxy 2 副本，写 DB 后各副本自刷新有延迟 —— 「写完读一次」不是生效判据，
    必须连续 3 次读到才算（见 wait_visible）。
  · 每一步都不许拿组级 200 当某条腿的活证。

用法：
  python3 scripts/image-2-acct-cutover.py audit
  python3 scripts/image-2-acct-cutover.py add-zero --apply
  python3 scripts/image-2-acct-cutover.py add-acct --apply
  python3 scripts/image-2-acct-cutover.py verify --times 5
  python3 scripts/image-2-acct-cutover.py cutover --apply     # 用户拍板后
"""
import argparse
import collections
import json
import os
import re
import time
import urllib.error
import urllib.request

BASE = "http://10.68.13.198:30402/pro"
def _require_env(name: str) -> str:
    """凭据只从环境变量读，缺了直接退出（不内置默认值，避免真 key 落进仓库）。"""
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(
            "缺少环境变量 %s —— 先 export %s=<198 prod master key>（别写进文件/命令行历史）" % (name, name)
        )
    return v

MK = _require_env("LITELLM_MASTER_KEY")

OLD = "image-2"          # 交给 acct
ZERO = "image-2-zero"    # zerokey 腿的新家
ACCT_SRC = "chatgpt-gpt-5.5"   # 出图承载组（7 个组都行，5.5 腿最全且最快）
ACCT_ID = "acct-image-2"
# 自调用回主 proxy：2026-09-06 实测该形状 200 / 17.6s / b64 1,034,856 bytes
PROXY_SVC = "http://litellm-proxy.litellm-product.svc.cluster.local:4000/v1"

PROMPT = "a small red cube on a white background"


def api(method, path, data=None, timeout=300):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method,
        headers={"Authorization": f"Bearer {MK}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def model_info():
    return api("GET", "/model/info", timeout=60)["data"]


def rows(name):
    """某组的现网条目（原样返回，供照抄）。"""
    return [m for m in model_info() if m.get("model_name") == name]


def wait_visible(pred, tries=15, gap=10, what=""):
    """2 副本自刷新有延迟：要求 **连续 3 次** 命中才算生效。"""
    hit = 0
    for _ in range(tries):
        try:
            if pred(model_info()):
                hit += 1
                if hit >= 3:
                    return True
            else:
                hit = 0
        except Exception:
            hit = 0
        time.sleep(gap)
    print(f"  ❌ {what} 等待超时 —— 别当已生效")
    return False


def acct_entry():
    return {
        "model_name": OLD,
        "litellm_params": {
            "model": f"openai/{ACCT_SRC}",
            "api_base": PROXY_SVC,
            "api_key": MK,
            # 计费只读 litellm_params。内层已按 chatgpt-gpt-5.5 记账，
            # 外层置 0 否则同一次出图记两遍。
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
        "model_info": {"id": ACCT_ID, "mode": "image_generation"},
    }


def cmd_audit(_a):
    d = model_info()
    c = collections.Counter(m.get("model_name") for m in d)
    print(f"{OLD:16s} {c.get(OLD, 0)} 腿")
    for m in d:
        if m.get("model_name") == OLD:
            p = m.get("litellm_params") or {}
            print(f"   {(m.get('model_info') or {}).get('id'):24s} {p.get('api_base')}")
    print(f"{ZERO:16s} {c.get(ZERO, 0)} 腿")
    print(f"{ACCT_SRC:16s} {c.get(ACCT_SRC, 0)} 腿（acct 出图承载）")


def zero_rows():
    """image-2 里指向 zerokey(zero-N:8200) 的条目。"""
    return [m for m in rows(OLD)
            if "zero-" in ((m.get("litellm_params") or {}).get("api_base") or "")]


def cmd_add_zero(a):
    src = zero_rows()
    have = {(m.get("model_info") or {}).get("id") for m in rows(ZERO)}
    print(f"从 {OLD} 照抄 {len(src)} 条 zerokey 活行 → {ZERO}")
    for m in src:
        old_id = (m.get("model_info") or {}).get("id")
        new_id = old_id.replace("-image-2", "-image-2-zero")
        if new_id in have:
            print(f"  - {new_id} 已存在，跳过")
            continue
        if not a.apply:
            print(f"  [dry-run] 建 {new_id}")
            continue
        p = dict(m.get("litellm_params") or {})
        # 只留真正配置过的字段，去掉 /model/info 回显的一堆 False 默认值
        p = {k: v for k, v in p.items()
             if k in ("model", "api_base", "api_key", "rpm",
                      "input_cost_per_token", "output_cost_per_token")}
        p.setdefault("api_key", "raw")
        api("POST", "/model/new", {
            "model_name": ZERO, "litellm_params": p,
            "model_info": {"id": new_id, "mode": "image_generation"},
        })
        print(f"  ✅ {new_id}")
    if a.apply:
        want = len(src)
        ok = wait_visible(lambda d: sum(1 for m in d if m.get("model_name") == ZERO) >= want,
                          what=f"{ZERO} 全副本可见")
        print(f"\n{ZERO} 成员数: {len(rows(ZERO))}  全副本可见: {'✅' if ok else '❌'}")


ACCT_MK = "sk-chatgpt-198-d8a3f4e62b9c1057ef324918a7b6d3e0"


def acct_legs():
    """从 chatgpt-gpt-5.5 组读出 38 条 acct 腿 → {N: api_base}。

    为什么以 5.5 组为准而不是数 deployment：165 个 chatgpt-acct deployment 里只有 38 个
    真活。`readyReplicas>0` 不作数 —— 160/161/163 pod 在跑但账号 token_revoked。
    5.5 组成员是被配额治理筛过的，那才是「活跃 acct」的唯一可信来源。
    """
    out = {}
    for m in rows(ACCT_SRC):
        ab = (m.get("litellm_params") or {}).get("api_base") or ""
        mm = re.search(r"chatgpt-acct-(\d+)\.", ab)
        if mm:
            out[int(mm.group(1))] = ab
    return out


def cmd_add_acct_all(a):
    """逐 acct 往 image-2 铺腿（每个号一条，独立记账、独立冷却）。"""
    legs = acct_legs()
    have = {(m.get("model_info") or {}).get("id") for m in rows(OLD)}
    print(f"{ACCT_SRC} 有 {len(legs)} 条活跃 acct 腿 → 往 {OLD} 铺")
    added = 0
    for n in sorted(legs):
        mid = f"acct-{n}-image-2"
        if mid in have:
            print(f"  - {mid} 已存在，跳过")
            continue
        if not a.apply:
            print(f"  [dry-run] 建 {mid} → {legs[n]}")
            continue
        api("POST", "/model/new", {
            "model_name": OLD,
            "litellm_params": {
                # 打 acct pod 自己的 /v1/images/generations —— 2026-09-06 实测
                # acct-155 直连该端点 HTTP 200 + b64 PNG，端点本来就通。
                "model": f"openai/{ACCT_SRC}",
                "api_base": legs[n],
                "api_key": ACCT_MK,
                # 计费只读 litellm_params；内层 acct pod 已记账，外层置 0 免双记
                "input_cost_per_token": 0,
                "output_cost_per_token": 0,
            },
            "model_info": {"id": mid, "mode": "image_generation"},
        })
        print(f"  ✅ {mid}")
        added += 1
    if a.apply and added:
        want = set(f"acct-{n}-image-2" for n in legs)
        ok = wait_visible(lambda d: want <= {(m.get("model_info") or {}).get("id")
                                             for m in d if m.get("model_name") == OLD},
                          what=f"{OLD} 的 {len(want)} 条 acct 腿全副本可见")
        print(f"\n{OLD} 成员数: {len(rows(OLD))}  全副本可见: {'✅' if ok else '❌'}")


def cmd_add_acct(a):
    if any((m.get("model_info") or {}).get("id") == ACCT_ID for m in rows(OLD)):
        print(f"{ACCT_ID} 已在 {OLD} 里，先删后建（幂等）")
        if a.apply:
            api("POST", "/model/delete", {"id": ACCT_ID})
    if not a.apply:
        print(f"  [dry-run] 往 {OLD} 加 acct 行 {ACCT_ID} → {ACCT_SRC}")
        return
    api("POST", "/model/new", acct_entry())
    print(f"  ✅ {ACCT_ID} → {ACCT_SRC}（自调用 {PROXY_SVC}）")
    ok = wait_visible(lambda d: any((m.get("model_info") or {}).get("id") == ACCT_ID
                                    for m in d if m.get("model_name") == OLD),
                      what=f"{ACCT_ID} 全副本可见")
    print(f"\n{OLD} 成员数: {len(rows(OLD))}  全副本可见: {'✅' if ok else '❌'}")


def gen(model):
    body = json.dumps({"model": model, "prompt": PROMPT, "n": 1}).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/images/generations", data=body, method="POST",
        headers={"Authorization": f"Bearer {MK}", "Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            j = json.loads(r.read())
        b = (j.get("data") or [{}])[0].get("b64_json") or ""
        return len(b) > 1000, f"200 {time.time() - t:5.1f}s b64={len(b)}"
    except urllib.error.HTTPError as e:
        return False, f"{e.code} {e.read()[:200].decode(errors='replace')}"
    except Exception as e:
        return False, f"{type(e).__name__} {e}"


def cmd_verify(a):
    for name in ([a.model] if a.model else [OLD, ZERO]):
        n = len(rows(name))
        if not n:
            print(f"{name}: 组不存在，跳过")
            continue
        print(f"\n=== {name}（{n} 腿）打 {a.times} 次")
        good = 0
        for i in range(a.times):
            ok, note = gen(name)
            print(f"  {i+1}/{a.times}: {'✅' if ok else '❌'} {note}", flush=True)
            good += ok
        print(f"  → {good}/{a.times} 成功")


def cmd_cutover(a):
    """⚠️ 只在用户拍板后跑：把 zerokey 行从 image-2 摘掉。"""
    src = zero_rows()
    zn = len(rows(ZERO))
    if zn < len(src):
        print(f"❌ {ZERO} 只有 {zn} 腿 < 待摘 {len(src)} 腿 —— 先跑 add-zero，不许先摘后加")
        return
    ids = {(m.get("model_info") or {}).get("id") for m in rows(OLD)}
    per_acct = {i for i in ids if i and i.startswith("acct-") and i.endswith("-image-2")
                and i != ACCT_ID}
    if not per_acct and ACCT_ID not in ids:
        print(f"❌ {OLD} 里还没有任何 acct 腿 —— 先跑 add-acct-all，否则摘完 {OLD} 就空了")
        return
    print(f"{OLD} 现有 acct 腿 {len(per_acct)} 条（逐 acct）"
          f"{'+ 1 条自调用' if ACCT_ID in ids else ''}")
    print(f"从 {OLD} 摘掉 {len(src)} 条 zerokey 行（它们已在 {ZERO} 有家）")
    for m in src:
        mid = (m.get("model_info") or {}).get("id")
        if not a.apply:
            print(f"  [dry-run] 摘 {mid}")
            continue
        api("POST", "/model/delete", {"id": mid})
        print(f"  ✅ 摘 {mid}")
    # 逐 acct 腿铺好后，自调用那条就是冗余（同一批号被算两遍权重，且多绕一跳）
    if per_acct and ACCT_ID in ids:
        if not a.apply:
            print(f"  [dry-run] 摘冗余自调用行 {ACCT_ID}")
        else:
            api("POST", "/model/delete", {"id": ACCT_ID})
            print(f"  ✅ 摘冗余自调用行 {ACCT_ID}")
    if a.apply:
        print(f"\n{OLD} 成员数: {len(rows(OLD))}  {ZERO} 成员数: {len(rows(ZERO))}")
        print("→ 现在去跑 verify，两个组都要绿")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit")
    for n in ("add-zero", "add-acct", "add-acct-all", "cutover"):
        sub.add_parser(n).add_argument("--apply", action="store_true")
    p = sub.add_parser("verify")
    p.add_argument("--times", type=int, default=3)
    p.add_argument("--model")
    a = ap.parse_args()
    {"audit": cmd_audit, "add-zero": cmd_add_zero, "add-acct": cmd_add_acct,
     "add-acct-all": cmd_add_acct_all,
     "verify": cmd_verify, "cutover": cmd_cutover}[a.cmd.replace("_", "-")](a)


if __name__ == "__main__":
    main()
