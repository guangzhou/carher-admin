#!/usr/bin/env python3
"""Kiro 账号的权威量具：模型目录 + credit 余额，**整池逐号**。

为什么要有这个脚本
------------------
这几个查询我手搓过五次，错了四次，每次都是同一类错误：

1. `getUsageLimits` 我按 REST 直觉猜成 POST，三个变体全回 `UnknownOperationException`。
   它是 **GET**，参数在 query 上。正确形状抄自 kiro.rs 源码
   `src/kiro/token_manager.rs:341`，不是猜的。
2. header 少一个都不行（`x-amz-user-agent` 里的 `KiroIDE-<ver>-<machineId>` 是必需的）。
3. **只读 `credentials.json[0]`** —— 2026-09-16 加了第二个号才发现这是错的，
   kiro.rs 是真的多凭据池。只看 `[0]` 会把另一个号的耗尽/封停完全看不见。
4. **盘上的 accessToken 是陈旧快照**，过期就 403。手搓时我每次都得再写一遍
   OIDC refresh，写第二遍的时候就该固化了 —— 现在固化在 `refresh_token()`。
5. **不走境外出口直接打 AWS**。线上 kiro.rs 恒挂代理，探针不走就是在量另一条路。
   现在默认从 pod 的 `config.json` 里读 `proxyUrl/proxyUsername/proxyPassword`，
   和线上用**同一个出口**。

子命令
------
catalog  GET /ListAvailableModels  —— **判「某模型这个账号有没有」的唯一权威判据**。
         返回的是这个账号的真实 entitlement，带 rateMultiplier 和真实 token 上限，
         比 kiro.dev 文档、比任何代理的内置清单都硬。
         ⚠️ `kiro chat --list-models` 是 catalog 不是 entitlement，不算数。
         ⚠️ origin 只有 AI_EDITOR / KIRO_CLI / CLI / IDE 合法（CONSOLE 直接报不支持，
            CHAT 报 REQUEST_BODY_INVALID）。AI_EDITOR 与 KIRO_CLI 给全量（19），
            IDE 11、CLI 3，都是子集 —— **拿 CLI 的结果说"没有某模型"是假红**。

quota    GET /getUsageLimits     —— 查还剩多少 credit，并直接给「耗尽没有」的判语。
         ⚠️ `currentUsageWithPrecision` 是**整个计费周期**的累计值，不是本次调用的。
         ⚠️ **计量滞后数分钟**：打完立刻读恒 delta 0.00，「前后差」是坏尺子。

has      判一串模型名在不在池子里任一账号的 entitlement 里，退出码即判据。
         例：`kiro-probe.py has fable gpt-6 kimi-k3` → 全不在则 exit 1。

🔴 402 的正确读法（2026-09-16 血的教训）
---------------------------------------
真推理拿到 `402 Payment Required / reason=MONTHLY_REQUEST_COUNT` 时，
**底层量的就是这里的 credit**，不是另一把独立的"月请求数"尺子。
我当时没重新实探就断言"两码事、getUsageLimits 不预告"，是无证据归因 ——
实际一跑就是 `1000.0 / 1000`，它看得见。
⇒ **看到 402 的第一件事是跑 `quota`**，别拿 reason 字符串猜底层量具。

凭据来源
--------
默认从 198 集群的 kiro.rs pod 里读 `/app/config/credentials.json`（**整个 list**）
和 `/app/config/config.json`（拿代理）。
kiro.rs 是**懒刷新**：盘上的 `expiresAt` 可以显示早已过期而服务完全正常 ——
本脚本遇到 401/403 会自动 OIDC refresh 再重试一次，不要据此判服务死了。
`--creds <file>` 可以喂一个离线备份（比如摘号前的备份），用来查**已经不在池里**的号。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

SSH = ["ssh", "cltx@10.68.13.198"]
POD_CAT = ("sudo kubectl -n kiro-rs exec deploy/kiro-rs -c kiro-rs -- "
           "cat /app/config/%s")
KIRO_IDE_VER = "0.11.107"
OIDC = "https://oidc.%s.amazonaws.com/token"
# 合法 origin，来自上游自己的报错文案（CONSOLE 那次直接把清单吐出来了）
ORIGINS = ("AI_EDITOR", "KIRO_CLI", "CLI", "IDE")


def _pod_cat(name: str) -> str:
    return subprocess.run(SSH + [POD_CAT % name], capture_output=True,
                          text=True, check=True).stdout


def load_pool(path: str | None) -> list[dict]:
    """credentials.json 是个 **list = 真的多号池**，不是只用 [0]。"""
    raw = open(path, encoding="utf8").read() if path else _pod_cat("credentials.json")
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]


def build_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    """proxy=None 时显式走直连；不要依赖 HTTPS_PROXY 环境变量。

    环境变量是隐式的，跑脚本的人看不见自己在量哪条路 —— 出口必须打印出来。
    """
    if not proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def pod_proxy() -> str | None:
    """从线上 config.json 拼出带认证的代理 URL，和 kiro.rs 用同一个出口。"""
    cfg = json.loads(_pod_cat("config.json"))
    url = cfg.get("proxyUrl")
    if not url:
        return None
    host = url.split("//", 1)[1]
    user, pw = cfg.get("proxyUsername"), cfg.get("proxyPassword")
    return f"http://{user}:{pw}@{host}" if user else url


def mask(proxy: str | None) -> str:
    """打印出口时把口令打码 —— 243 的密码不该进终端回滚/日志。"""
    if not proxy:
        return "直连（⚠️ 与线上不同路）"
    scheme, _, rest = proxy.partition("//")
    if "@" not in rest:
        return proxy
    cred, _, host = rest.partition("@")
    return f"{scheme}//{cred.split(':')[0]}:***@{host}"


def refresh_token(creds: dict, opener, region: str) -> str:
    """AWS SSO OIDC 刷新。POST + JSON body（不是 form）。

    实测本账号 refreshToken **不轮转**（刷两次拿回同一个），所以这里不回写盘。
    """
    body = json.dumps({
        "clientId": creds["clientId"],
        "clientSecret": creds["clientSecret"],
        "grantType": "refresh_token",
        "refreshToken": creds["refreshToken"],
    }).encode()
    req = urllib.request.Request(OIDC % region, data=body,
                                 headers={"content-type": "application/json"})
    return json.load(opener.open(req, timeout=60))["accessToken"]


def machine_id_of(creds: dict) -> str:
    """凭据的 machineId：显式值优先，缺失时按 kiro.rs 的规则派生。

    派生式抄自 kiro.rs `src/kiro/machine_id.rs::generate_from_credentials`
    （OAuth 凭据走 `sha256("KotlinNativeAPI/<refreshToken>")`）。
    全新导出常常没有 machineId —— 派生而不是报错，探针才能量到它。
    """
    mid = (creds.get("machineId") or "").strip()
    if len(mid) == 64 and all(c in "0123456789abcdefABCDEF" for c in mid):
        return mid.lower()
    nodash = mid.replace("-", "")
    if len(nodash) == 32 and all(c in "0123456789abcdefABCDEF" for c in nodash):
        return (nodash + nodash).lower()
    rt = creds.get("refreshToken")
    if rt:
        return hashlib.sha256(f"KotlinNativeAPI/{rt}".encode()).hexdigest()
    sys.exit("凭据既无 machineId 也无 refreshToken，无法派生设备指纹")


def call(path: str, params: str, creds: dict, region: str, opener,
         _retried: bool = False) -> dict:
    # 全新导出没有 accessToken（只有 refreshToken）—— 先换一次，而不是 KeyError。
    if not creds.get("accessToken"):
        creds["accessToken"] = refresh_token(creds, opener, region)
        _retried = True          # 刚换过就别再因 401 重复刷
    tok, mid = creds["accessToken"], machine_id_of(creds)
    ua = f"KiroIDE-{KIRO_IDE_VER}-{mid}"
    req = urllib.request.Request(
        f"https://q.{region}.amazonaws.com/{path}?{params}",
        headers={
            "Authorization": "Bearer " + tok,
            "x-amz-user-agent": f"aws-sdk-js/1.0.0 {ua}",
            "user-agent": ("aws-sdk-js/1.0.0 ua/2.1 os/macos lang/js md/nodejs#22.0.0 "
                           f"api/codewhispererruntime#1.0.0 m/N,E {ua}"),
            "amz-sdk-invocation-id": str(uuid.uuid4()),
            "amz-sdk-request": "attempt=1; max=1",
            "Connection": "close",
        })
    try:
        return json.load(opener.open(req, timeout=60))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf8", "replace")[:500]
        if e.code in (401, 403) and not _retried:
            # 盘上快照过期是常态，不是故障。刷一次再判。
            creds["accessToken"] = refresh_token(creds, opener, region)
            return call(path, params, creds, region, opener, _retried=True)
        raise RuntimeError(f"HTTP {e.code} from {path}: {body}") from None


def fmt_ctx(n: int | None) -> str:
    if not n:
        return "-"
    return f"{n // 1000000}M" if n >= 1000000 else f"{n // 1000}K"


def model_ids(creds, region, origin, opener) -> list[str]:
    r = call("ListAvailableModels", f"origin={origin}&maxResults=100",
             creds, region, opener)
    return [(m.get("modelInfo") or m).get("modelId", "?") for m in r.get("models", [])]


def cmd_catalog(creds, region, origin, opener, as_json):
    r = call("ListAvailableModels", f"origin={origin}&maxResults=100",
             creds, region, opener)
    models = r.get("models", [])
    if as_json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return
    print(f"origin={origin} region={region} —— {len(models)} 个模型\n")
    print("%-24s %-8s %-8s %-8s" % ("上游 id", "倍率", "输入窗口", "输出上限"))
    for m in models:
        info = m.get("modelInfo") or m
        print("%-24s %-8s %-8s %-8s" % (
            info.get("modelId", "?"),
            f"{info.get('rateMultiplier', '?')}x",
            fmt_ctx((info.get("tokenLimits") or {}).get("maxInputTokens")),
            fmt_ctx((info.get("tokenLimits") or {}).get("maxOutputTokens"))))
    print("\n⚠️ 这是 entitlement，是唯一权威判据。目录里没有 = 这个账号真的用不了。")
    print("⚠️ 想说「没有某模型」必须用 AI_EDITOR/KIRO_CLI；IDE(11)/CLI(3) 是子集，会假红。")


def cmd_quota(creds, region, origin, opener, as_json) -> bool:
    """返回 True 表示这个号**已耗尽**。"""
    r = call("getUsageLimits", f"origin={origin}&resourceType=AGENTIC_REQUEST",
             creds, region, opener)
    if as_json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return False
    sub = r.get("subscriptionInfo") or {}
    b = (r.get("usageBreakdownList") or [{}])[0]
    used = b.get("currentUsageWithPrecision", 0.0)
    cap = b.get("usageLimitWithPrecision", 0.0)
    reset = datetime.datetime.fromtimestamp(int(b.get("nextDateReset", 0)),
                                            datetime.timezone.utc)
    over = r.get("overageConfiguration") or {}
    exhausted = cap and used >= cap
    print(f"订阅      {sub.get('subscriptionTitle', '?')} ({sub.get('type', '?')})")
    print(f"已用      {used} / {cap} credits  ({b.get('unit', '?')})")
    print(f"剩余      {round(cap - used, 2)}"
          f"  ⇒ 按 ≈0.66/次 粗算还能发 ≈{int((cap - used) / 0.66)} 次")
    print(f"重置      {reset:%Y-%m-%d %H:%M} UTC（实测为每月 1 日 00:00 UTC）")
    print(f"超额      {over.get('overageStatus', '?')} "
          f"(cap {b.get('overageCap')}, rate ${b.get('overageRate')}/credit)")
    if exhausted:
        print("\n🔴 **已耗尽** —— 真推理会拿到 402 / reason=MONTHLY_REQUEST_COUNT，"
              "kiro.rs 会自动禁用该号并回写 PVC。")
        if over.get("overageStatus") == "DISABLED" and b.get("overageCap"):
            print("   可续命：在 kiro.dev 打开 overage（付费）。**账单决定，别自己开。**")
    print("\n⚠️ 这个数是**整个计费周期**的累计值，不是本次调用的。")
    print("⚠️ 计量滞后数分钟——打完立刻读恒 delta 0.00，别拿前后差判成本。")
    print("⚠️ rateMultiplier 是**相对权重**，不是每发扣的绝对 credit。")
    print("   📏 09-16 实测均价 **≈0.66 credit/次**（窗口对齐：新号 06:18Z 起 10.16h，")
    print("      successCount 1073、credit 712.67，同期无其它消费者）。")
    print("      ⚠️ 随模型配比漂：后半段 6.6h 的增量切片是 **0.74/次**（业务流量更偏贵道），")
    print("      前 3.6h 含我的低倍率压测只有 0.57 ⇒ **这是混合均价，不是单模型价**。")
    print("      ⛔ 旧记的 ≈0.039/次 已作废——那次 successCount 是**进程生命周期**、")
    print("      credit 是**计费月**，分母根本不对齐。要单模型定价仍需 A/B + 等沉降。")
    return bool(exhausted)


def cmd_has(pool, region, origin, opener, needles) -> bool:
    """返回 True = 至少有一个 needle 在某个账号的 entitlement 里。"""
    hit = False
    for creds in pool:
        ids = model_ids(creds, region, origin, opener)
        tag = f"{creds.get('email', '?')} / {creds.get('subscriptionTitle', '?')}"
        print(f"# {tag} —— {len(ids)} 个模型")
        for n in needles:
            m = [i for i in ids if n.lower() in i.lower()]
            hit = hit or bool(m)
            print(f"   {'✅' if m else '❌'} {n:<20} {', '.join(m) if m else '不在目录里'}")
    if not hit:
        print("\n⛔ 全部不在 entitlement 里 = 这些账号**真的用不了**，"
              "不是配置问题、不是 tier 问题。")
    return hit


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["catalog", "quota", "both", "has"])
    p.add_argument("needles", nargs="*", help="has 子命令要找的模型名（子串匹配）")
    p.add_argument("--region", default="us-east-1",
                   help="IdC 凭据绑死 us-east-1，eu-central-1 会整个 403")
    p.add_argument("--origin", default="AI_EDITOR", choices=ORIGINS,
                   help="AI_EDITOR/KIRO_CLI 给全量；IDE/CLI 是子集（11/3 vs 19）")
    p.add_argument("--creds", help="本地 credentials.json；缺省从 198 的 pod 里读整池")
    p.add_argument("--account", help="只查某个号：id 或 email 子串；缺省整池逐个查")
    p.add_argument("--no-proxy", action="store_true",
                   help="不走境外出口直连 AWS。⚠️ 这不是线上那条路，只用于对照")
    p.add_argument("--json", action="store_true", help="吐原始响应")
    a = p.parse_args()

    pool = load_pool(a.creds)
    if a.account:
        pool = [c for c in pool
                if str(c.get("id")) == a.account or a.account in (c.get("email") or "")]
        if not pool:
            sys.exit(f"池子里没有匹配 {a.account!r} 的账号")

    # 代理恒从线上 config.json 读（哪怕凭据是本地备份）——判一个号，
    # 必须走它在线上会走的那条出口，否则量的是另一条路。
    proxy = None if a.no_proxy else pod_proxy()
    opener = build_opener(proxy)
    # 出口必须打印 —— 不知道自己在量哪条路，读数就没有意义
    print(f"# 出口 {mask(proxy)}", file=sys.stderr)
    print(f"# 池子 {len(pool)} 个账号\n", file=sys.stderr)

    if a.command == "has":
        if not a.needles:
            sys.exit("has 需要至少一个模型名，例：kiro-probe.py has fable gpt-6")
        sys.exit(0 if cmd_has(pool, a.region, a.origin, opener, a.needles) else 1)

    exhausted_all = True
    for i, creds in enumerate(pool):
        head = (f"===== id={creds.get('id')} {creds.get('email', '?')} / "
                f"{creds.get('subscriptionTitle', '?')}"
                f"{'  [disabled]' if creds.get('disabled') else ''} =====")
        print(("\n" if i else "") + head)
        if a.command in ("catalog", "both"):
            cmd_catalog(creds, a.region, a.origin, opener, a.json)
        if a.command == "both":
            print()
        if a.command in ("quota", "both"):
            exhausted_all &= cmd_quota(creds, a.region, a.origin, opener, a.json)
        else:
            exhausted_all = False
    if a.command in ("quota", "both") and exhausted_all:
        sys.exit("\n🔴 整池全部耗尽 ⇒ 18 条 kiro-* 道会全挂。")


if __name__ == "__main__":
    main()
