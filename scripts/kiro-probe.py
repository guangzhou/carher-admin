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
            CHAT 报 REQUEST_BODY_INVALID）。AI_EDITOR 与 KIRO_CLI 给全量
            （**条数随档位与上游变**：09 月记 19、10-01 付费档 20 / FREE 9、
            10-02 付费档 **21**（上游新出 `claude-sonnet-5.5`）
            ⇒ 别把某个条数写成判据；条数不对先查档位、再查上游是否出了新模型），
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
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

# 198 的 22 口只在公司 IDC 内网开。不在内网时经一台能通的跳板走 ProxyJump：
#   export KIRO_SSH_JUMP=pubSrv
# 不设则保持原来的直连行为。
_JUMP = os.environ.get("KIRO_SSH_JUMP", "").strip()
SSH = ["ssh"] + (["-J", _JUMP] if _JUMP else []) + ["cltx@10.68.13.198"]
POD_CAT = ("sudo kubectl -n kiro-rs exec deploy/kiro-rs -c kiro-rs -- "
           "cat /app/config/%s")
KIRO_IDE_VER = "0.11.107"   # = config.rs default_kiro_version()，线上 config.json 未配置该键
OIDC = "https://oidc.%s.amazonaws.com/token"
# Social（Github/Google 等第三方登录）走 kiro.dev 自己的端点，不是 AWS OIDC。
# 形状抄自 kiro.rs `token_manager.rs::refresh_social_token`（:142）。
SOCIAL = "https://prod.%s.auth.desktop.kiro.dev/refreshToken"
# 合法 origin，来自上游自己的报错文案（CONSOLE 那次直接把清单吐出来了）
ORIGINS = ("AI_EDITOR", "KIRO_CLI", "CLI", "IDE")

# 09-16 实测的**混合**均价（窗口对齐：新号 06:18Z 起 10.16h，successCount 1073、
# credit 712.67）。⚠️ 随模型配比漂：同一轮后半段 6.6h 的增量切片是 0.74、
# 前 3.6h 含低倍率压测只有 0.57 ⇒ **这是混合均价，不是单模型价**，只用于"粗算还能发多少次"。
# ⛔ 别拿它算单模型成本，也别把它当预算依据。提成常量是为了别散落在输出里各写一遍。
AVG_CREDIT_PER_CALL = 0.66


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
    """从线上 config.json 拼出带认证的代理 URL，和 kiro.rs 用同一个出口。

    出口 `10.68.13.243:8120` 只在公司 IDC 内网可达。不在内网时先开一条到它的
    本地隧道，再用 `KIRO_PROXY_HOSTPORT` 只改写 host:port：

        ssh -f -N -L 18120:10.68.13.243:8120 <跳板>
        export KIRO_PROXY_HOSTPORT=127.0.0.1:18120

    改写只换接入点，**代理本体仍是线上那一个**（口令现读、落地 IP 不变）⇒
    量的还是线上那条路。不设则保持原来的直连行为。
    """
    cfg = json.loads(_pod_cat("config.json"))
    url = cfg.get("proxyUrl")
    if not url:
        return None
    host = url.split("//", 1)[1]
    override = os.environ.get("KIRO_PROXY_HOSTPORT", "").strip()
    if override:
        print(f"# ⚠️ 出口接入点改写 {host} -> {override}（同一个代理，仅换接入点）",
              file=sys.stderr)
        host = override
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


def auth_method_of(creds: dict) -> str:
    """凭据走哪条刷新路 —— 与 kiro.rs `token_manager.rs:122-138` 逐字对齐。

    ⛔ **不要读导出里的 `provider` 字段**（`Github` / `BuilderId` / …）：
    kiro.rs 源码里对它零引用，`AddCredentialRequest` 也没有它。
    真正分发刷新路径的是 `authMethod`；未指定时按「有没有 clientId+clientSecret」推断。
    """
    am = (creds.get("authMethod") or "").strip()
    if not am:
        am = "idc" if (creds.get("clientId") and creds.get("clientSecret")) else "social"
    return am.lower()


def refresh_token(creds: dict, opener, region: str) -> str:
    """刷新 accessToken，按 authMethod 分流 idc / social。

    🔴 **两条路的轮转行为相反，别互推：**
    - IdC（AWS SSO OIDC）：实测 refreshToken **不轮转**（刷两次拿回同一个）。
    - Social（kiro.dev）：`token_manager.rs:206` 明写「响应里带 refreshToken 就覆盖」
      ⇒ **一次探测就可能把用户给的那个 refreshToken 换掉**。
      所以这里把轮转后的值写回 `creds`，调用方必须拿 `--out-rotated` 落盘、
      **并用轮转后的那个去 `kiro-pool.py add`** —— 否则交给 kiro.rs 的可能是个死 token。
    """
    if auth_method_of(creds) in ("idc", "builder-id", "iam"):
        body = json.dumps({
            "clientId": creds["clientId"],
            "clientSecret": creds["clientSecret"],
            "grantType": "refresh_token",
            "refreshToken": creds["refreshToken"],
        }).encode()
        req = urllib.request.Request(OIDC % region, data=body,
                                     headers={"content-type": "application/json"})
        try:
            return json.load(opener.open(req, timeout=60))["accessToken"]
        except urllib.error.HTTPError as e:
            # 🔴 10-08：原来这里不接 HTTPError ⇒ 一个死 token 吐 20 行 urllib
            # traceback，末尾只有 `HTTP Error 400: Bad Request`，**响应体被丢掉**。
            # 而 400 在 kiro 这条链上至少三种含义，不看 body 分不开：
            #   · `invalid_grant`/`Resource not found` ⇒ refreshToken 永久失效，要重登
            #   · `getUsageLimits` 缺 profileArn ⇒ 配额取不到但号是好的（别误判成号坏）
            #   · 真的参数拼错
            # 10-08 为此手搓了一段脚本去读 body 才定位到。现在直接打出来。
            body_text = e.read().decode("utf8", "replace")[:500]
            if e.code == 400 and "invalid_grant" in body_text:
                raise RuntimeError(
                    f"🔴 IdC refreshToken 已永久失效 (invalid_grant)：{body_text}\n"
                    "   ⇒ 只能重新登录（skill kiro-account-manager-ops），"
                    "刷新/重试都救不回。\n"
                    "   ⚠️ 这与「`getUsageLimits` 缺 profileArn」那个 400 **不同源**：\n"
                    "      那个只是配额读不到、号是好的；这个是 catalog 前的 refresh 就死。"
                ) from None
            raise RuntimeError(f"HTTP {e.code} from idc refresh: {body_text}") from None

    # Social：body 只有 refreshToken，认证靠它本身；UA 必须带设备指纹。
    mid = machine_id_of(creds)
    req = urllib.request.Request(
        SOCIAL % region,
        data=json.dumps({"refreshToken": creds["refreshToken"]}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": f"KiroIDE-{KIRO_IDE_VER}-{mid}",
            "Connection": "close",
        })
    try:
        data = json.load(opener.open(req, timeout=60))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf8", "replace")[:500]
        if e.code == 400 and "invalid_grant" in body_text:
            # token_manager.rs:183 把这个形状单独归成「永久失效」，不是限流不是网络
            raise RuntimeError(
                f"🔴 Social refreshToken 已永久失效 (invalid_grant)：{body_text}\n"
                "   ⇒ 只能让用户重新导出，刷新/重试都救不回。") from None
        raise RuntimeError(f"HTTP {e.code} from social refresh: {body_text}") from None
    rotated = data.get("refreshToken")
    if rotated and rotated != creds["refreshToken"]:
        creds["refreshToken"] = rotated
        creds["_rotated"] = True
        print("# 🔴 social 刷新**轮转了 refreshToken** —— 后续 add 必须用轮转后的值",
              file=sys.stderr)
    return data["accessToken"]



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
    # 🔴 超额时 cap-used 为负，拿它去除均价会打出「还能发 ≈-674 次」这种无意义输出
    # （10-01 在掉成 FREE 的 id13 上真打出来过：`-444.93 ⇒ ≈-674 次`）。
    # 掉档的号 currentUsage 可以远超 cap（FREE cap=50 实测用到 494.93）⇒ 这不是边缘情况。
    left = round(cap - used, 2)
    if left > 0:
        print(f"剩余      {left}  ⇒ 按 ≈{AVG_CREDIT_PER_CALL}/次 粗算还能发 "
              f"≈{int(left / AVG_CREDIT_PER_CALL)} 次")
    else:
        print(f"剩余      {left}  🔴 **已耗尽/超额**（超出 {round(used - cap, 2)}）"
              f"—— 不给「还能发多少次」，负数没有意义")
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


def harvest(creds, region, origin, opener) -> dict:
    """一个号的结构化读数 —— `--summary-json` 用，批量验号的那把尺子。

    🔴 **为什么要有这个**：`--json` 吐的是**两个拼在一起的原始响应**，再加一行
    `===== id=… =====` 人类表头 ⇒ **整个 stdout 不是合法 JSON，解析不了**。
    2026-10-02 验 20 个号时我因此退回去 grep 人类表格，三条 grep 全写错格式
    （表格行没有前导空白、档位在 `订阅` 那一行），**20/20 全打出
    `tier=[] models=0 err=0`，看着像整批凭据都废了** —— 实际全都好的。
    「三个独立的量同时全 0、连报错数也是 0」是**尺子坏了**的形状，不是对象全死。
    ⇒ 批量时别在 shell 里 grep 人类输出，用这个。
    """
    cat = call("ListAvailableModels", f"origin={origin}&maxResults=100",
               creds, region, opener)
    models = [((m.get("modelInfo") or m).get("modelId", "?"))
              for m in cat.get("models", [])]
    q = call("getUsageLimits", f"origin={origin}&resourceType=AGENTIC_REQUEST",
             creds, region, opener)
    sub = q.get("subscriptionInfo") or {}
    b = (q.get("usageBreakdownList") or [{}])[0]
    used = b.get("currentUsageWithPrecision", 0.0)
    cap = b.get("usageLimitWithPrecision", 0.0)
    return {
        "email": creds.get("email"),
        "id": creds.get("id"),
        "authMethod": creds.get("authMethod"),
        "tier": sub.get("subscriptionTitle"),
        "tierType": sub.get("type"),
        "models": models,
        "modelCount": len(models),
        "used": used,
        "cap": cap,
        # 🔴 掉档的号 used 可以远超 cap（FREE cap=50 实测用到 494）⇒ 可能是负数，
        # 不 clamp —— 负数本身就是「超额」这个事实，抹平了就读不出来。
        "remaining": round(cap - used, 2),
        "exhausted": bool(cap and used >= cap),
        "resetUnix": b.get("nextDateReset"),
        "overageStatus": (q.get("overageConfiguration") or {}).get("overageStatus"),
        # social 刷新会轮转 refreshToken ⇒ 这一位决定「手上那份还能不能用」
        "rotated": bool(creds.get("_rotated")),
    }


def cmd_summary_json(pool, region, origin, opener) -> None:
    """整池一个合法 JSON 数组。**每个号带 `ok` 与 `error`** ——

    🔴 一个号抛异常不中断整批（20 个号跑一半炸掉等于白跑），但
    **失败必须留在数据里**，不能变成"安静地少一行" —— 那正是 10-02 那种
    「全 0 却零报错」的假象来源。
    """
    out = []
    for creds in pool:
        try:
            row = harvest(creds, region, origin, opener)
            row["ok"] = True
            row["error"] = None
        except Exception as e:                       # noqa: BLE001
            row = {"email": creds.get("email"), "id": creds.get("id"),
                   "authMethod": creds.get("authMethod"), "ok": False,
                   "error": f"{type(e).__name__}: {e}"[:300],
                   "rotated": bool(creds.get("_rotated"))}
        out.append(row)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()
    bad = [r for r in out if not r["ok"]]
    print(f"# {len(out) - len(bad)}/{len(out)} 个号读通"
          + (f"，🔴 {len(bad)} 个失败：{[r['email'] for r in bad]}" if bad else ""),
          file=sys.stderr)


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


def select_account(pool: list[dict], sel: str) -> list[dict]:
    """按 id 或 email 子串挑号。

    🔴 **纯数字一律只比 id，绝不去 email 里做子串匹配。**
    09-24 踩过：`--account 8` 想查 id8，却**同时命中 id3 `rmiglio582@gmail.com`**
    （邮箱里有 "8"），两个号的读数一起打出来 ⇒ 差一眼就把 id3 的
    `0.0/2000 已耗尽` 当成 id8 的额度，容量判断会整个反过来。
    ⇒ 数字选择器必须精确；要按邮箱找就用非数字子串。
    """
    if sel.isdigit():
        hit = [c for c in pool if str(c.get("id")) == sel]
        if not hit:
            ids = sorted(str(c.get("id")) for c in pool)
            sys.exit(f"池子里没有 id={sel}（现有 {ids}）。"
                     "\n⚠️ 纯数字只按 id 精确匹配；想按邮箱找请给非数字子串。")
        return hit
    hit = [c for c in pool if sel.lower() in (c.get("email") or "").lower()]
    if not hit:
        sys.exit(f"池子里没有匹配 {sel!r} 的账号")
    if len(hit) > 1:
        emails = [c.get("email") for c in hit]
        sys.exit(f"{sel!r} 命中 {len(hit)} 个号 {emails} —— 说具体点，"
                 "别让两个号的读数混在一起")
    return hit


def dump_rotated(a, pool: list[dict]) -> None:
    """把探完的凭据落盘（0600）。social 号的必经一步，见 refresh_token 的说明。"""
    if not a.out_rotated:
        if any(c.get("_rotated") for c in pool):
            print("\n🔴 refreshToken 已被本次探测轮转，但没给 --out-rotated ⇒ "
                  "新值只存在于服务端，你手上那份可能已经死了。"
                  "\n   ⇒ 重新导出账号，或下次带 --out-rotated 再探。", file=sys.stderr)
        return
    out = [{k: v for k, v in c.items() if k != "_rotated"} for c in pool]
    fd = os.open(a.out_rotated, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    rot = [c.get("email") for c in pool if c.get("_rotated")]
    print(f"\n# 凭据已写回 {a.out_rotated} (0600)"
          f"{'  ⚠️ 其中轮转过的：' + ', '.join(map(str, rot)) if rot else ''}",
          file=sys.stderr)
    print(f"# ⚠️ 里面是 live refreshToken，用完立刻 rm -f {a.out_rotated}",
          file=sys.stderr)


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
    p.add_argument("--account",
                   help="只查某个号：**纯数字=按 id 精确匹配**，否则按 email 子串"
                        "（子串命中多个直接报错，不让两个号的读数混在一起）。"
                        "缺省整池逐个查")
    p.add_argument("--no-proxy", action="store_true",
                   help="不走境外出口直连 AWS。⚠️ 这不是线上那条路，只用于对照")
    p.add_argument("--json", action="store_true",
                   help="吐原始响应。⚠️ `both --json` 是两个响应拼在一起 + 人类表头 ⇒ "
                        "**整体不是合法 JSON**，批量解析用 --summary-json")
    p.add_argument("--summary-json", action="store_true",
                   help="整池一个**合法 JSON 数组**：每号 tier/models/remaining/"
                        "exhausted/rotated + ok/error。批量验号用这个，"
                        "别在 shell 里 grep 人类表格（10-02 那次三条 grep 全写错，"
                        "20/20 打出 tier=[] models=0 err=0 像整批凭据都废了）")
    p.add_argument("--out-rotated", metavar="FILE",
                   help="把探完（可能已轮转）的凭据写回这个文件，0600。"
                        "social 号必须用它 —— 轮转后原 refreshToken 可能已死，"
                        "拿它去 kiro-pool.py add")
    a = p.parse_args()

    pool = load_pool(a.creds)
    # 🔴 machineId 必须在**任何刷新之前**钉死。派生式是 sha256(".../<refreshToken>")，
    # 而 social 刷新会轮转 refreshToken ⇒ 不钉的话同一个号的设备指纹会在探测中途漂掉
    # （UA 里换了指纹 = 换了台机器）。钉成「服务端此刻本来就会用的那个值」。
    for c in pool:
        c.setdefault("machineId", machine_id_of(c))
    if a.account:
        pool = select_account(pool, a.account)

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
        try:
            hit = cmd_has(pool, a.region, a.origin, opener, a.needles)
        finally:
            dump_rotated(a, pool)
        sys.exit(0 if hit else 1)

    if a.summary_json:
        try:
            cmd_summary_json(pool, a.region, a.origin, opener)
        finally:
            dump_rotated(a, pool)
        return

    exhausted_all = True
    try:
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
    finally:
        # finally 而不是末尾：探到一半抛异常时，**轮转已经发生在服务端了** ——
        # 这时候不落盘就等于把用户那份 refreshToken 弄丢。
        dump_rotated(a, pool)
    if a.command in ("quota", "both") and exhausted_all:
        sys.exit("\n🔴 整池全部耗尽 ⇒ 18 条 kiro-* 道会全挂。")



if __name__ == "__main__":
    main()
