#!/usr/bin/env python3
"""image-groups-selfcall-repair.py —— 把 `gpt-image-2` / `image-2` 从「逐 acct 铺腿」
换成「一条自调用腿指回 chatgpt-gpt-5.5」，并摘掉已烂掉的僵腿。

为什么要换形状（2026-09-28 实测，不是推的）：
  出图能力是**后端 pod 的属性**，不是某个组的属性。逐 acct 铺腿等于把「谁活着」这份
  名单抄第二遍 —— 5.5 组摘号时这边不跟着摘，必漂。实测漂成什么样：

    chatgpt-gpt-5.5   33 条活腿
    gpt-image-2       25 条腿，与 5.5 交集只有 6 条(165~172)，那 6 条 token 也已废
                      → 27 个活号一条腿都没有；19 条僵腿的号 deployment 是 0/0
    image-2           31 条腿，25 条僵腿（115~162 全 0/0）

  实测判据（每条都有阳性/阴性对照，组级绿灯不作数）：
    活叶 183 + 正确 leaf key 打 chatgpt-image → OK b64=1,170,376B 16s
    死叶 165 同一形状                         → H500 codex images 上游 401
                                                {"code":"token_invalidated"}
    全池 63 号扇出：24 号 IMG=OK img=1 (15~20s)，其余 401/403/超时
    组级连打 ×4：gpt-image-2 0/4、image-2 0/4、image-2-zero 4/4

  IMG 与 CHAT 双面对照定死了「是号死了、不是出图路径坏了」：165~172 六条腿
  IMG(H500) 与 CHAT(H401) 都回 token_invalidated。

为什么自调用腿不会再漂：
  api_base 指 litellm-proxy 自己，model 填 `chatgpt-gpt-5.5` —— 复用 5.5 组现成的
  调度/配额治理，号死了跟着 5.5 组一起摘。实测该形状 5/5 OK（并发 5，17~20s）。
  188 governor 也碰不到它：pause_acct 按 acct 的 api_base 匹配删 entry，自调用腿的
  api_base 不是 chatgpt-acct-N，天然在匹配范围外。
  （对照：governor 的 models_for() 不含任何 image 模型 → 逐 acct 腿被 pause 删掉后
   resume 永远不会重建，这是第二个必漂的理由。）

真因（2026-09-28 定论，三段式）：
  假设   坏腿的 `model` 指 `openai/chatgpt-image`（走 CustomLLM
         `/app/chatgpt_images.py` 打 chatgpt.com/backend-api/codex/images/generations），
         该 codex token 已失效；`image-2` 的腿指 `openai/chatgpt-gpt-5.5`
         （chat 通道自带出图），不经过那个 401。
  证伪   若假设错，同一个号在两个组名下应同生同死，我那条两组完全相同的自调用腿
         也应表现一致。
  数据   acct-167 的两条腿逐字段 diff 只差这一个字段；去掩码日志给
         `litellm.aimage_generation(model=openai/chatgpt-image)` →
         `codex images 上游 401: token_invalidated`，栈顶
         `/app/chatgpt_images.py:139`，`LiteLLM Retried: 5 times`
         （5×14s ≈ 那个恒定的 72s）；两组的自调用腿 litellm_params 逐键相同，
         按 model_id 直打都 17~21s OK。
  ⇒ 改法：腿的 `model` 一律填 `openai/chatgpt-gpt-5.5`，**永不填 chatgpt-image**。

加法律令（add 与 remove 永不同一执行段）：
  step1 audit        看清两组现状 + 与 5.5 的漂移
  step2 add-selfcall 往两组各加 1 条自调用腿（纯加法，不碰任何现有腿）
  step3 verify       连打 N 次，两组都要绿
  step4 probe-accts  逐叶实打出图，产出真能出图的名单（**别跳过这步**）
  step5 add-accts    按 step4 的名单补 acct 腿
  step6 prune-dead   **verify 绿之后**才摘僵腿；每条摘前复核 deployment 是 0/0
                     或实测 token_invalidated

判据纪律：
  · litellm-proxy 4 副本，写 DB 后各副本自刷新有延迟 —— 「写完读一次」不是生效判据，
    必须连续 3 次读到才算（wait_visible）。
  · 不许拿组级 200 当某条腿的活证。
  · prune 前必须先确认 add 已生效，否则摘完组就空了。
  · 🔴 **出图名单三个不可信来源**（2026-09-28 全部实测踩过）：
      飞书 acct 池表 `status=ONLINE` —— 那是 chat 探针，165-178 全 ONLINE 但出图 401
      `chatgpt-gpt-5.5` 组成员      —— 33 个里只 25 个真能出图
      deployment `READY>0`          —— 209-213/226-229 表上在线，实为 0/0，直打 refused
    唯一合法来源 = `probe-accts` 逐叶实打拿到 b64 或 url。
  · 🔴 判出图绿只认「拿到 b64 或 url」，**不认 HTTP 200**，也不能只认 b64 ——
    有的上游返 url（踩过：只量 b64 把健康的 sa-grok-imagine 判成假绿）。

两把 master key 别混（混了是假红）：
  母路由 litellm-proxy → secret `litellm-secrets`      / env LITELLM_MASTER_KEY
  叶子 chatgpt-acct-N  → secret `chatgpt-pool-master-key` / env POOL_MASTER_KEY
  拿母路由那把打叶子 → H400 `{"error":{"message":"No connected db."}}`

用法（在 198 上跑；本机连不到 30402）：
  export LITELLM_MASTER_KEY=<198 母路由 master key>
  export POOL_MASTER_KEY=<叶子池 master key>
  python3 scripts/image-groups-selfcall-repair.py audit
  python3 scripts/image-groups-selfcall-repair.py add-selfcall --apply
  python3 scripts/image-groups-selfcall-repair.py verify --times 4
  python3 scripts/image-groups-selfcall-repair.py probe-accts            # 产名单
  python3 scripts/image-groups-selfcall-repair.py add-accts --accts <名单> --apply
  python3 scripts/image-groups-selfcall-repair.py prune-dead          # dry-run
  python3 scripts/image-groups-selfcall-repair.py prune-dead --apply  # 拍板后
"""
import argparse
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

BASE = "http://10.68.13.198:30402/pro"


def _require_env(name: str) -> str:
    """凭据只从环境变量读，缺了直接退出（不内置默认值，避免真 key 落进仓库）。"""
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(
            "缺少环境变量 %s —— 先 export %s=<198 prod master key>"
            "（别写进文件/命令行历史）" % (name, name)
        )
    return v


MK = _require_env("LITELLM_MASTER_KEY")

# 出图承载组：7 个 chatgpt 组都能出图，5.5 腿最全最快，且它是配额治理的主组
CARRIER = "chatgpt-gpt-5.5"
PROXY_SVC = "http://litellm-proxy.litellm-product.svc.cluster.local:4000/v1"

# 要修的两个组 → 自调用腿的 model_info.id
TARGETS = {
    "gpt-image-2": "selfcall-gpt-image-2",
    "image-2": "selfcall-image-2",
}

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


def rows(name, data=None):
    d = data if data is not None else model_info()
    return [m for m in d if m.get("model_name") == name]


def leg_id(m):
    return (m.get("model_info") or {}).get("id") or ""


def leg_base(m):
    return (m.get("litellm_params") or {}).get("api_base") or ""


def acct_of(m):
    mm = re.search(r"chatgpt-acct-(\d+)\.", leg_base(m))
    return int(mm.group(1)) if mm else None


def carrier_accts(data):
    return {acct_of(m) for m in rows(CARRIER, data)} - {None}


def selfcall_entry(group, mid):
    return {
        "model_name": group,
        "litellm_params": {
            "model": f"openai/{CARRIER}",
            "api_base": PROXY_SVC,
            "api_key": MK,
            # 计价只读 litellm_params。内层已按 chatgpt-gpt-5.5 记账，
            # 外层必须置 0，否则同一次出图记两遍。
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
        "model_info": {"id": mid, "mode": "image_generation"},
    }


def wait_visible(pred, tries=15, gap=10, what=""):
    """4 副本自刷新有延迟：要求 **连续 3 次** 命中才算生效。"""
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


def cmd_audit(_a):
    d = model_info()
    live = carrier_accts(d)
    print(f"{CARRIER}: {len(live)} 条活腿（配额治理筛过，这是「活跃 acct」唯一可信来源）")
    for group, mid in TARGETS.items():
        rs = rows(group, d)
        accts = {acct_of(m) for m in rs} - {None}
        has_self = any(leg_id(m) == mid for m in rs)
        print(f"\n{group}: {len(rs)} 腿  自调用腿={'✅ 有' if has_self else '❌ 无'}")
        print(f"  在 {CARRIER} 里但本组缺 = {len(live - accts)}: {sorted(live - accts)}")
        print(f"  本组有但 {CARRIER} 已摘（僵腿）= {len(accts - live)}: {sorted(accts - live)}")


def cmd_add_selfcall(a):
    d = model_info()
    for group, mid in TARGETS.items():
        rs = rows(group, d)
        if any(leg_id(m) == mid for m in rs):
            print(f"- {group}: {mid} 已存在，跳过（幂等）")
            continue
        if not a.apply:
            print(f"  [dry-run] {group} 加自调用腿 {mid} → {CARRIER}")
            continue
        api("POST", "/model/new", selfcall_entry(group, mid))
        print(f"  ✅ {group} 加 {mid} → {CARRIER}（自调用 {PROXY_SVC}）")
    if not a.apply:
        return
    want = set(TARGETS.values())
    ok = wait_visible(
        lambda dd: want <= {leg_id(m) for g in TARGETS for m in rows(g, dd)},
        what="两条自调用腿全副本可见",
    )
    print(f"\n全副本可见: {'✅' if ok else '❌'}")


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
        return False, f"{e.code} {e.read()[:160].decode(errors='replace')}"
    except Exception as e:
        return False, f"{type(e).__name__} {e}"


def cmd_verify(a):
    """连打 N 次量**失败率**。

    🔴 次数少了「全绿」和「真健康」长得一模一样 —— 组里混着死腿时，
    轮到死腿那次才 0s H401。2026-10-10 实证（用户报 gpt-image-2 不可用）：
    `--times 3` → 3/3 全绿（据此会回「没问题」）；`--times 8` → **7/8**，
    同一时刻腿层实打只 15/40、25 条 acct 腿里 21 条是死腿。
    ⇒ 默认 8 次，别往下调。失败一次就非零退出，自动化才判得了成败。
    """
    names = [a.model] if a.model else list(TARGETS)
    failed = {}
    for name in names:
        n = len(rows(name))
        if not n:
            print(f"{name}: 组不存在，跳过")
            failed[name] = "组不存在"
            continue
        print(f"\n=== {name}（{n} 腿）打 {a.times} 次")
        good = 0
        for i in range(a.times):
            ok, note = gen(name)
            print(f"  {i + 1}/{a.times}: {'✅' if ok else '❌'} {note}", flush=True)
            good += ok
        print(f"  → {good}/{a.times} 成功")
        if good < a.times:
            failed[name] = f"{good}/{a.times}"
    if failed:
        # 间歇失败 = 组里还有死腿，不是「基本可用」。
        raise SystemExit(
            "\n❌ 不是全绿：" + "、".join(f"{k} {v}" for k, v in failed.items())
            + "\n   组级时好时坏 = 死腿还在组里被轮到（既不是坏了也不是好着）。"
              "\n   下一步：进 pod 跑 probe-accts 看腿层，别报「基本可用」。")
    print(f"\n✅ 全绿：{len(names)} 组各 {a.times}/{a.times}")


def cmd_prune_dead(a):
    """摘僵腿：只摘 CARRIER 组已经不认的 acct 腿。自调用腿必须先在位。"""
    d = model_info()
    live = carrier_accts(d)
    for group, mid in TARGETS.items():
        rs = rows(group, d)
        if not any(leg_id(m) == mid for m in rs):
            print(f"❌ {group}: 自调用腿 {mid} 不在位 —— 先跑 add-selfcall 并 verify 绿，"
                  f"否则摘完组就空了")
            continue
        dead = [m for m in rs if acct_of(m) is not None and acct_of(m) not in live]
        print(f"\n{group}: {len(rs)} 腿 → 摘 {len(dead)} 条僵腿"
              f"（{CARRIER} 已不认这些号）")
        # 🔴 判据只是「5.5 组已摘」，摘不到「5.5 还认着但出图实打 401」的腿。
        # 2026-10-10 实证：修复前 25 条腿里 21 条的号出不了图，其中只有 15 条
        # 被 5.5 也摘了；剩 6 条（182/183/185/195/198/205）5.5 认着、出图 0s H401，
        # prune-dead 跑完它们仍在组里 —— 这就是 verify 8 次里那 1 次失败的来源。
        # ⇒ 清这类腿用 `prune-by-probe`（按实打结果摘），别以为本命令清干净了。
        for m in dead:
            mid_d, n = leg_id(m), acct_of(m)
            if not a.apply:
                print(f"  [dry-run] 摘 {mid_d} (acct-{n})")
                continue
            api("POST", "/model/delete", {"id": mid_d})
            print(f"  ✅ 摘 {mid_d} (acct-{n})")
        if a.apply:
            left = rows(group)
            print(f"  {group} 剩 {len(left)} 腿: {sorted(leg_id(m) for m in left)}")
    if a.apply:
        print("\n→ 现在再跑一次 verify，两组都要绿")
        print("→ 再跑 prune-by-probe：本命令摘不到「5.5 认着但出图 401」的腿")


def cmd_prune_by_probe(a):
    """按**出图实打**结果摘腿：组里的号凡实打不通过就摘。

    与 `prune-dead` 的分工（两条判据不是一回事，都要跑）：
      prune-dead      判据 = 「CARRIER 组已摘这个号」——摘的是跟着 5.5 一起退役的
      prune-by-probe  判据 = 「这个号出图实打不过」——摘的是 5.5 还认着但出图已死的

    2026-10-10：后者比前者多逮到 6 条（182/183/185/195/198/205）。

    🔴 必须在 pod 内跑（探叶子要集群 DNS），且**先补腿 + verify 绿再摘**，
    否则可能把组摘空。摘之前自调用腿必须在位。
    """
    d = model_info()
    # 候选 = 两组腿上现有的号（不是 5.5 成员）—— 要判的就是「组里这些号还行不行」
    on_legs = sorted({acct_of(m) for g in TARGETS for m in rows(g, d)} - {None})
    if not on_legs:
        raise SystemExit("❌ 两组都没有 acct 腿 —— 没什么可摘的")
    print(f"逐叶实打组里现有的 {len(on_legs)} 个号：{on_legs}")

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        res = sorted(ex.map(probe_leaf, on_legs))

    nodns = [n for n, _, s in res if s.startswith("NODNS")]
    if nodns:
        # 不加这道门禁，在 host 上跑会把全部活腿当成死腿摘光。
        raise SystemExit(
            f"❌ {len(nodns)}/{len(res)} 个是 DNS 解析失败（探针没出门）——"
            f"在 198 host 上跑会把活腿全判死并摘光。先进 pod 再跑本命令。")

    ok = {n for n, good, _ in res if good}
    bad = {n for n, good, _ in res if not good}
    print(f"\n实打通过 {len(ok)}/{len(res)}：{sorted(ok)}")
    print(f"实打不过 {len(bad)}：{sorted(bad)}")
    if not ok:
        raise SystemExit("❌ 一个都没通过 —— 摘光就全断了。先读错误字串分病，"
                         "再 probe-accts 找新号补腿。")

    for group, mid in TARGETS.items():
        rs = rows(group, d)
        if not any(leg_id(m) == mid for m in rs):
            print(f"\n❌ {group}: 自调用腿 {mid} 不在位 —— 先 add-selfcall，跳过")
            continue
        doomed = [m for m in rs if acct_of(m) in bad]
        keep = len(rs) - len(doomed)
        print(f"\n{group}: {len(rs)} 腿 → 摘 {len(doomed)} 条（实打不过），剩 {keep}")
        if keep < 2:
            print(f"  ⛔ 摘完只剩 {keep} 腿，太少了 —— 先补腿再来，本组跳过")
            continue
        for m in doomed:
            if not a.apply:
                print(f"  [dry-run] 摘 {leg_id(m)} (acct-{acct_of(m)})")
                continue
            api("POST", "/model/delete", {"id": leg_id(m)})
            print(f"  ✅ 摘 {leg_id(m)} (acct-{acct_of(m)})")
    if a.apply:
        print("\n→ 连读 3 次确认收敛（副本刷新有延迟），再 verify --times 8")


def acct_entry(group, n):
    """给 acct-n 铺一条本组的 acct 腿。

    `model` 必须是 `openai/chatgpt-gpt-5.5`（chat 通道自带出图），
    **不能**写 `openai/chatgpt-image` —— 后者走 CustomLLM 打
    chatgpt.com/backend-api/codex/images/generations，codex token 一失效
    就是 401 token_invalidated，且 LiteLLM 会重试 5 次 ≈ 72s 才报错。
    2026-09-28 实测：同一个 acct-167 在 chatgpt-image 下 401、
    在 chatgpt-gpt-5.5 下正常出图。
    """
    return {
        "model_name": group,
        "litellm_params": {
            "model": f"openai/{CARRIER}",
            "api_base": f"http://chatgpt-acct-{n}.litellm-product.svc.cluster.local:4000",
            # 叶子用池 master key（secret chatgpt-pool-master-key），
            # 不是母路由那把。填错 → H400 "No connected db." 假红。
            "api_key": _require_env("POOL_MASTER_KEY"),
            # 内层叶子已按 chatgpt-gpt-5.5 记账，外层置 0 防重复计费。
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
        "model_info": {"id": f"acct-{n}-{group}", "mode": "image_generation"},
    }


def _is_dns_failure(exc) -> bool:
    """这个异常是不是「名字解析不了」—— 即探针根本没发出请求。

    `*.svc.cluster.local` 只有集群内的 DNS 认，在 198 host 上一律
    `gaierror(-3, 'Temporary failure in name resolution')`。
    """
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, socket.gaierror):
        return True
    return "name resolution" in str(reason).lower()


def probe_leaf(n, timeout=240):
    """直打 acct-n 叶子的 /v1/images/generations，判它**出图**活不活。

    为什么必须直打叶子、不能用组级或 chat 探针：
      · 组级 200 会被别的活腿盖住，答不了「这一个号活不活」。
      · 飞书 acct 池表的 `status=ONLINE` 是 **chat** 探针 —— 2026-09-28 实测
        165-178 表里全 ONLINE，codex 出图通道全 401 token_invalidated。
      · `chatgpt-gpt-5.5` 组成员同理：33 个成员只 25 个真能出图。
    返回 (n, ok, 说明)。判绿只认拿到 b64 或 url，不认 HTTP 200。
    """
    body = json.dumps({"model": CARRIER, "prompt": PROMPT, "n": 1}).encode()
    req = urllib.request.Request(
        f"http://chatgpt-acct-{n}.litellm-product.svc.cluster.local:4000"
        "/v1/images/generations",
        data=body,
        headers={"Authorization": "Bearer " + _require_env("POOL_MASTER_KEY"),
                 "Content-Type": "application/json"})
    t0 = time.time()
    try:
        z = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        d0 = (z.get("data") or [{}])[0]
        nb = len(d0.get("b64_json") or "")
        url = d0.get("url") or ""
        # 只量 b64 会把返 url 的上游判成假绿（2026-09-28 踩过：sa-grok-imagine
        # 返 url，我的探针只认 b64，差点把健康组排除出兜底名单）。
        ok = nb > 1000 or bool(url)
        return (n, ok, f"b64={nb}B url={'有' if url else '无'} {time.time()-t0:.0f}s")
    except urllib.error.HTTPError as e:
        return (n, False, f"H{e.code} {time.time()-t0:.0f}s "
                          f"{e.read().decode()[:90].replace(chr(10), ' ')}")
    except Exception as e:  # Connection refused = deployment 0/0，不是号坏
        # 🔴 DNS 解析失败是**探针没出门**，不是号坏 —— 必须和真死号分开返回，
        # 否则 40 行 ❌ 里混着两种病，只看「通过 0 个」分不出是池子全烂
        # 还是脚本跑错了位置（2026-10-10 实证：在 198 host 上跑 0/40 全败，
        # 进 pod 再跑 15/40）。判据是错误字串，不是通过数。
        if isinstance(e, urllib.error.URLError) and _is_dns_failure(e):
            return (n, False, f"NODNS {time.time()-t0:.0f}s {e.reason!r}")
        return (n, False, f"ERR {time.time()-t0:.0f}s {e!r}")


def cmd_probe_accts(a):
    """逐叶实打出图，产出可直接喂给 add-accts 的名单。

    候选池默认取 `chatgpt-gpt-5.5` 的成员（那是配额治理筛过的**聊天**活号），
    但**结论只认本函数实打的结果** —— 候选池只是候选。
    """
    from concurrent.futures import ThreadPoolExecutor

    d = model_info()
    cand = sorted(carrier_accts(d)) if not a.accts else \
        [int(x) for x in a.accts.split(",") if x.strip()]
    print(f"阳性对照: 候选 {len(cand)} 个号"
          f"（来源：{'chatgpt-gpt-5.5 成员' if not a.accts else '--accts 指定'}）")
    if not cand:
        raise SystemExit("❌ 候选池是空的 —— 先查 model_info 读得到没，别当「全死」")
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        res = sorted(ex.map(probe_leaf, cand))
    ok = [n for n, good, _ in res if good]
    bad = [(n, s) for n, good, s in res if not good]
    for n, s in bad:
        print(f"  ❌ acct-{n:<4} {s}")

    # 🔴 断言「我量得到」排在「量出多少」之前。NODNS 占满 = 探针没出门，
    # 这时候的「通过 0 个」是尺子的读数、不是池子的读数，**不许产名单**。
    nodns = [n for n, _, s in res if s.startswith("NODNS")]
    if nodns and not ok:
        raise SystemExit(
            f"\n❌ 尺子坏了，不是号坏：{len(nodns)}/{len(res)} 个是 DNS 解析失败"
            f"（探针根本没发出请求），0 个真出图。\n"
            f"   *.svc.cluster.local 只有集群内 DNS 认 —— 你多半在 198 host 上跑。\n"
            f"   改成进 pod 跑（skill litellm-198-image-groups §B）：\n"
            f"     POD=$(sudo kubectl -n litellm-product get pods \\\n"
            f"       -l carher.net/litellm-production-route=enabled "
            f"-o jsonpath='{{.items[0].metadata.name}}')\n"
            f"     sudo kubectl -n litellm-product cp {os.path.basename(__file__)} "
            f"$POD:/tmp/igr.py\n"
            f"     sudo kubectl -n litellm-product exec $POD -- \\\n"
            f"       env LITELLM_MASTER_KEY=\"$MK\" POOL_MASTER_KEY=\"$PK\" "
            f"python3 /tmp/igr.py probe-accts")
    if nodns:
        print(f"\n⚠️ {len(nodns)} 个是 DNS 解析失败（探针没出门，不是号坏）："
              f"{nodns} —— 这些号本轮**没被测到**，别当成死号")

    print(f"\n出图实测通过 {len(ok)}/{len(res)}")
    print("  " + ",".join(str(n) for n in ok))
    if not ok:
        raise SystemExit("\n❌ 0 个通过 —— 先逐行读上面的错误字串分病"
                         "（H401=号真死 / NODNS=探针没出门 / H429=冷却），"
                         "别直接当「池子全烂」")
    print(f"\n→ 补腿: {os.path.basename(__file__)} add-accts --accts "
          f"{','.join(str(n) for n in ok)} --apply")


def cmd_add_accts(a):
    """按 **实测出图通过** 的 acct 名单补腿。

    判据只认 --accts 给的名单（用 `probe-accts` 产出，或调用方自己逐叶实打
    /v1/images/generations 拿到 b64/url），**不认**飞书表的 status=ONLINE ——
    那是 chat 探针，2026-09-28 实测 165-178 表里 ONLINE 但 codex 出图全 401；
    也**不认** chatgpt-gpt-5.5 组成员（同日实测 33 个里只 25 个真能出图）。
    """
    want = [int(x) for x in a.accts.split(",") if x.strip()]
    if not want:
        # 空名单会一条不铺，然后 wait_visible 对空集合恒真 → 打印「全副本可见 ✅」。
        # 那是假绿。probe-accts 全败时正好会生成 `--accts ` 空参的建议命令。
        raise SystemExit("❌ --accts 是空的 —— 不会铺任何腿，但后面会打出"
                         "「全副本可见 ✅」的假绿。先跑 probe-accts 拿到真名单。")
    d = model_info()
    for group in TARGETS:
        have = {acct_of(m) for m in rows(group, d)} - {None}
        todo = [n for n in want if n not in have]
        print(f"\n{group}: 已有 acct 腿 {len(have)} 条，补 {len(todo)} 条")
        for n in todo:
            if not a.apply:
                print(f"  [dry-run] 加 acct-{n}-{group}")
                continue
            api("POST", "/model/new", acct_entry(group, n))
            print(f"  ✅ 加 acct-{n}-{group}")
    if not a.apply:
        return
    need = {f"acct-{n}-{g}" for g in TARGETS for n in want}
    ok = wait_visible(
        lambda dd: need <= {leg_id(m) for g in TARGETS for m in rows(g, dd)},
        tries=30, what=f"{len(need)} 条新腿全副本可见")
    print(f"\n全副本可见: {'✅' if ok else '❌'}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit")
    for n in ("add-selfcall", "prune-dead"):
        sub.add_parser(n).add_argument("--apply", action="store_true")
    p = sub.add_parser("verify")
    # 默认 8：3~4 次抽不到死腿，「全绿」会伪装成「真健康」（见 cmd_verify docstring）
    p.add_argument("--times", type=int, default=8)
    p.add_argument("--model")
    p = sub.add_parser("probe-accts")
    p.add_argument("--accts", default="",
                   help="限定候选（逗号分隔）；留空 = 取 chatgpt-gpt-5.5 成员做候选")
    p.add_argument("--jobs", type=int, default=10)
    p = sub.add_parser("add-accts")
    p.add_argument("--accts", required=True,
                   help="逗号分隔的 acct 编号，**必须是 probe-accts 实打通过的**")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("prune-by-probe")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--jobs", type=int, default=10)
    a = ap.parse_args()
    {"audit": cmd_audit, "add-selfcall": cmd_add_selfcall,
     "verify": cmd_verify, "prune-dead": cmd_prune_dead,
     "probe-accts": cmd_probe_accts,
     "add-accts": cmd_add_accts,
     "prune-by-probe": cmd_prune_by_probe}[a.cmd](a)


if __name__ == "__main__":
    main()
