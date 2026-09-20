#!/usr/bin/env python3
"""crg_pool_register.py —— 把 82 上验好的 14 个 cr-g-* 名字铺成四腿池(81/83/84/85)。

**在 litellm-proxy pod 内跑**(要 LITELLM_MASTER_KEY 和 localhost:4000)：
    kubectl -n litellm-product exec -i <proxy-pod> -- python3 - [参数] < crg_pool_register.py

设计上的三条硬规矩，都是踩出来的：

1. **copy 源是 `/model/info` 的解密视图，不是我的记忆**。老模板 pool_register.py 把
   `litellm_params` 写死在脚本里(`model: openai/gpt-5.6-terra` 那一坨)，一旦 82 上真实的
   载体/档位与模板不符，铺出来的池就是"看着对、跑起来不是那个模型"。这里逐行整份拷贝，
   **只改三个键**：`api_base`(换腿) / `model_info.id`(全局唯一) / `model_name`(去 `-82`)。
   `model`(载体)和 `reasoning_effort`(档位)原样带过去，一个字都不许默写。

2. **`api_key` 必须显式补，且它不是占位符 —— 它是 IDE 选择器**。`/model/info` 对它脱敏
   (127 行读出来全是 `None`)，照抄就会漏掉 —— 而 openai/ provider 真流量没有 api_key 直接 401。
   ⚠️ **2026-09-20 实测推翻了原来那个占位符 `sk-zerokey-web-noop`**：lane 侧
   `server.js:17` 把 Bearer 原样当 IDE 名 —— `req.ide = authHeader.slice(7).trim().toLowerCase()`，
   `engine/index.js:22` 再 `getIDEMapper(ideName)` 取 `user` 函数。只有 `vscode` / `cursor`
   两个键供得出 `user`；喂任何别的值(包括那个占位符)**请求死在 IDE 映射那一步**：
       Bearer sk-zerokey-web-noop  → 500  TypeError: user is not a function
       Bearer cursor               → 200  暗号命中
   所以值必须是 `cursor`。这条不能从 `/model/info` 读回来自证(脱敏)，也不能从 DB 读
   (`ProxyModelTable` 的相关列是密文)，**唯一判据是入池后从 proxy 侧实打一次**。

3. **两条 `-max` 的裸载体腿(id = `gpt-5.6-luna-wm` / `gpt-5.6-thinking`)不复制**。
   它们是 xhigh 旗的种子：旗立在 `litellm.model_cost` 的**裸载体名键**上，是全局单例，
   与 model_name 组无关。82 那两条已经把旗立好了，池子跟着受益；再建同名 id 也建不出来。
   ⚠️ 反过来说：**删掉 82 那两条 = 池的 `-max` 静默退回 standard，不报错**。
   这条耦合没有任何自动化门能发现，只能靠文档(docs/cr-g-pool-rollback-20260902.md)记着。

用法：
    ... python3 - --dump                        # 只打印现状(建前的备份判据)
    ... python3 - --only cr-g-5.6 --lane 81     # dry-run 单行探针
    ... python3 - --only cr-g-5.6 --lane 81 --apply
    ... python3 - --lanes 181,182 --with-direct --apply   # 池名 + 独占直连名一起建

`--with-direct` 顺带建 `cr-g-5.6-mini-<lane>`：**验单条腿死活的唯一入口**。
池名是 key 级亲和，拿池名打新腿是碰运气（09-20 实测有整轮没落到 175）。
少了这个名字，"新腿到底在不在服务"就只能靠抽样蒙。
    ... python3 - --apply                       # 补齐其余
回滚：/model/delete 掉打印出来的那些 `zerokey-cr-g-{81,83,84,85}-*` id。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
# 池腿。**2026-09-02 换过一次**：原来是 81/83/84/85，但 81/83/85 背后的账号是 **free 档**
# （服务端 accounts/check 实读 plan=free，模型目录只有 10 个 slug，没有 thinking/pro/instant）
# —— 它们物理上就答不出这 14 个名字里的大多数。换成 135~140 六个 pro 号 + 保留 84。
# 判据是 lane_model_catalog.py：入池前每条腿必须 19+ slug 且含 thinking/pro/instant。
DEFAULT_LANES = ["84", "135", "136", "137", "138", "139", "140"]
LANES = list(DEFAULT_LANES)               # 82 是 canary，不进池；101 是被抛弃的旧线，不碰
SVC = "http://zero-cursor-bpi-%s.litellm-product.svc.cluster.local:8201/v1"
# **不是占位符，是 lane 侧的 IDE 选择器**（见 docstring 第 2 条，09-20 实测）。
# 改这个值前先读 server.js:17 + engine/index.js:22，只有 vscode / cursor 供得出 `user` 函数。
API_KEY_IDE = "cursor"
EXPECT_NAMES = 14


def get(path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + MK})
    return json.load(urllib.request.urlopen(req))


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Authorization": "Bearer " + MK,
                                          "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        return {"HTTP_ERROR": e.code, "body": e.read().decode()[:400]}


def main():
    args = sys.argv[1:]
    apply = "--apply" in args
    only = args[args.index("--only") + 1] if "--only" in args else None
    lane_filter = args[args.index("--lane") + 1] if "--lane" in args else None
    if "--lanes" in args:                 # 覆盖整张腿表（换池腿时用，别改代码常量）
        LANES[:] = [x.strip() for x in args[args.index("--lanes") + 1].split(",") if x.strip()]
    print("池腿: %s" % ",".join(LANES))

    rows = [r for r in get("/model/info")["data"]
            if (r.get("model_name") or "").startswith("cr-g-")]
    existing = {(r["model_name"], (r.get("model_info") or {}).get("id")) for r in rows}
    existing_ids = {i for _, i in existing}

    print("== 建前现状：cr-g-* 共 %d 行 ==" % len(rows))
    for name, i in sorted(existing):
        print("  %-30s %s" % (name, i))
    if "--dump" in args:
        return 0

    # 源 = 82 的直连名，且只要 `zerokey-` 开头的那条腿（裸载体腿是 xhigh 旗种子，见 docstring）
    src = {}
    for r in rows:
        n = r["model_name"]
        i = (r.get("model_info") or {}).get("id") or ""
        if n.endswith("-82") and i.startswith("zerokey-"):
            src[n[:-3]] = r          # 去掉 "-82" 就是池名
    print("\n== 拷贝源：82 上 %d 个名字 ==" % len(src))
    if len(src) != EXPECT_NAMES:
        # 不是"少几个也能凑合"：82 的菜单如果变了，我对目标形状的理解就已经过期，
        # 闷头铺出去 = 池子和 canary 长得不一样。停手让人来看。
        print("❌ 期望 %d 个，实际 %d 个 —— 82 的菜单变过了，停手" % (EXPECT_NAMES, len(src)))
        return 2

    plan, skip = [], []
    for name in sorted(src):
        s = src[name]
        sp = dict(s.get("litellm_params") or {})
        mode = (s.get("model_info") or {}).get("mode") or "chat"
        variant = name[len("cr-g-"):]
        for lane in LANES:
            if lane_filter and lane != lane_filter:
                continue
            if only and name != only:
                continue
            new_id = "zerokey-cr-g-%s-%s" % (lane, variant)
            lp = dict(sp)                       # 整份拷贝
            lp["api_base"] = SVC % lane         # 只改这一个键
            lp["api_key"] = API_KEY_IDE         # /model/info 脱敏掉了，必须补；值 = IDE 名
            lp.pop("input_cost_per_token", None)
            lp.pop("output_cost_per_token", None)
            payload = {"model_name": name, "litellm_params": lp,
                       "model_info": {"id": new_id, "mode": mode}}
            if new_id in existing_ids:
                skip.append(new_id)
            else:
                plan.append(payload)

    # --with-direct：顺手建 `cr-g-5.6-mini-<lane>` 这个**独占直连名**。
    # 为什么必须有：池名走 weighted_affinity，是 **key 级**亲和，一把 key 被钉在一条腿上。
    # 想证明"新加的这条腿在服务"，用池名打是碰运气 —— 09-20 实测 4 把 key × 14 发抽 5 条腿，
    # 有一整轮压根没落到 175。**独占名是唯一能单独判一条腿死活的入口**。
    # 只建 5.6-mini 一个变体：验的是"这条腿通不通"，不是"这条腿每个档位都对"
    # （后者由目录门禁 lane_model_catalog.py 负责，那是建腿前就该过的）。
    if "--with-direct" in args:
        base = src.get("cr-g-5.6-mini")
        if not base:
            print("❌ --with-direct 拿不到 cr-g-5.6-mini 的 82 源行，不瞎猜形状，停手")
            return 2
        bp = dict(base.get("litellm_params") or {})
        for lane in LANES:
            if lane_filter and lane != lane_filter:
                continue
            dname = "cr-g-5.6-mini-%s" % lane
            did = "zerokey-cr-g-%s-direct-5.6-mini" % lane
            if did in existing_ids:
                skip.append(did)
                continue
            lp = dict(bp)
            lp["api_base"] = SVC % lane
            lp["api_key"] = API_KEY_IDE
            lp.pop("input_cost_per_token", None)
            lp.pop("output_cost_per_token", None)
            plan.append({"model_name": dname, "litellm_params": lp,
                         "model_info": {"id": did, "mode": "chat"}})

    print("\n== 计划新建 %d 行（已存在跳过 %d 行）==" % (len(plan), len(skip)))
    for p in plan[:60]:
        lp = p["litellm_params"]
        print("  %-26s <- %-22s effort=%-8s api_base=…-%s  id=%s"
              % (p["model_name"], lp.get("model"), lp.get("reasoning_effort"),
                 lp["api_base"].split("zero-cursor-bpi-")[1].split(".")[0],
                 p["model_info"]["id"]))
    if skip:
        print("  (跳过已存在: %s)" % ", ".join(skip[:8]) + (" …" if len(skip) > 8 else ""))
    if not apply:
        print("\n(dry-run。加 --apply 执行)")
        return 0

    okn, fails = 0, []
    for p in plan:
        r = post("/model/new", p)
        if "HTTP_ERROR" in r:
            fails.append((p["model_info"]["id"], r))
            print("  ❌ %s => %s" % (p["model_info"]["id"], r))
        else:
            okn += 1
            print("  ✅ %s" % p["model_info"]["id"])
    print("\nSUMMARY: %d/%d OK" % (okn, len(plan)))
    if fails:
        return 1
    print("⚠️ 还没完：动过 LiteLLM_ProxyModelTable 必须 rollout restart deploy/litellm-proxy，"
          "再做 scoped-key 回归；只查一个副本是假绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
