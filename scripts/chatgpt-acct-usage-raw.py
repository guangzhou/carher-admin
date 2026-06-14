#!/usr/bin/env python3
"""
chatgpt-acct-usage-raw.py — 拉 ChatGPT Pro 账号的 /codex/usage 原始数据

输出两张表：
  1. 主 5h / 周配额 + spark 子配额 + credits
  2. 异常账号清单（401 token_invalidated / 403 Cloudflare / no auth.json）

数据源：
  https://chatgpt.com/backend-api/codex/usage  + 各账号 auth.json 的 access_token

字段映射（实测，2026-05-19）：
  rate_limit.primary_window.limit_window_seconds   = 18000  → 5h 窗口
  rate_limit.secondary_window.limit_window_seconds = 604800 → 7d 周窗口
  *.used_percent                                   = 整数 0-100，直接是 %（不是 messages 数）
  *.reset_after_seconds                            = 当前窗口距重置剩余秒
  additional_rate_limits[i].limit_name             = 子配额名（如 "GPT-5.3-Codex-Spark"）
  additional_rate_limits[i].rate_limit.*           = 同主结构（per-model 独立池）
  code_review_rate_limit                           = null 或同结构（GitHub @Codex 触发）
  credits.has_credits                              = 是否买过额外信用额度

运行方式：
  本地一键   ./scripts/chatgpt-acct-usage.sh [--json] [--retry N]
  单机直跑   USAGE_ACCOUNT_SPECS='[...]' python3 /tmp/chatgpt-acct-usage-raw.py

可选环境变量：
  USAGE_RETRY=3   遇 Cloudflare 403 / 网络异常时重试次数（默认 3）
  USAGE_HTTP_TIMEOUT=10  单次 /codex/usage HTTP 超时秒数（默认 10）
  USAGE_JSON=1    输出 raw JSON 数组而非表格
  USAGE_ALL=1     主表也显示异常账号（默认只显示健康账号，异常单独列）
  USAGE_ACCOUNT_SPECS='[{"name":"acct-2","auth_path":"/.../auth.json","source":"188"}]'
  USAGE_ACCOUNT_SPECS_FILE=/tmp/specs.json
  USAGE_RENDER_INPUT=/tmp/results.json  渲染已采集的 JSON，不再发 HTTP
"""
import json, os, sys, time, urllib.request, urllib.error

def discover_accounts():
    if os.environ.get("USAGE_ACCOUNT_SPECS_FILE"):
        with open(os.environ["USAGE_ACCOUNT_SPECS_FILE"]) as f:
            os.environ["USAGE_ACCOUNT_SPECS"] = f.read()
    if os.environ.get("USAGE_ACCOUNT_SPECS"):
        specs = json.loads(os.environ["USAGE_ACCOUNT_SPECS"])
        return [
            (
                s["name"],
                s.get("auth_path") or (f"/Data/chatgpt-auth/{s['name']}/auth.json"),
                s.get("container") or f"litellm-chatgpt-{s['name'].split('-')[1]}",
                s.get("source") or "unknown",
            )
            for s in specs
        ]

    found = set()
    base = "/Data/chatgpt-auth"
    if os.path.isdir(base):
        for name in os.listdir(base):
            if name.startswith("acct-") and name.split("-", 1)[1].isdigit():
                found.add(int(name.split("-", 1)[1]))

    accounts = []
    for n in sorted(found):
        container = "litellm-chatgpt" if n == 1 else f"litellm-chatgpt-{n}"
        accounts.append((f"acct-{n}", f"/Data/chatgpt-auth/acct-{n}/auth.json", container, "local"))
    return accounts


ACCOUNTS = discover_accounts()
# acct-1 没注册到 prod 轮询，"no auth.json" 是预期状态而非异常
EXPECTED_MISSING = {"acct-1"}

RETRY = int(os.environ.get("USAGE_RETRY", "3"))
HTTP_TIMEOUT = int(os.environ.get("USAGE_HTTP_TIMEOUT", "10"))
WANT_JSON = os.environ.get("USAGE_JSON", "") == "1"
SHOW_ALL = os.environ.get("USAGE_ALL", "") == "1"
RENDER_INPUT = os.environ.get("USAGE_RENDER_INPUT", "")

USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
UA = "codex_cli_rs/0.30.0 (Linux; x86_64)"


def fmt_eta(secs):
    if secs is None or secs == "":
        return "—"
    h, m = divmod(int(secs) // 60, 60)
    d, h = divmod(h, 24)
    if d: return f"{d}d{h}h"
    if h: return f"{h}h{m}m"
    return f"{m}m"


def fetch_usage(tok, aid):
    """带 retry 调用 /codex/usage。403 重试；401 立即返回（token 失效不会自愈）。"""
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {tok}",
        "ChatGPT-Account-ID": aid,
        "Originator": "codex_cli_rs",
        "User-Agent": UA,
    })
    last_err = None
    for attempt in range(1, RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read()), None
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode(errors="ignore")
            last_err = (e.code, body)
            if e.code == 401:
                return None, last_err  # token 失效不重试
            if attempt < RETRY:
                time.sleep(2 * attempt)
        except Exception as e:
            last_err = (-1, str(e)[:200])
            if attempt < RETRY:
                time.sleep(2 * attempt)
    return None, last_err


def load_auth(p):
    if not os.path.exists(p):
        return None, "no auth.json"
    try:
        d = json.load(open(p))
    except Exception as e:
        return None, f"parse error: {e}"
    tok = d.get("access_token", "")
    aid = d.get("account_id") or d.get("accountId") or ""
    if not tok:
        return None, "auth.json missing access_token"
    return (tok, aid), None


# ── 主流程 ──────────────────────────────────────────────

ACCT_META = {name: {"auth_path": auth_path, "container": container, "source": source}
             for name, auth_path, container, source in ACCOUNTS}

if RENDER_INPUT:
    with open(RENDER_INPUT) if RENDER_INPUT != "-" else sys.stdin as f:
        packed = json.load(f)
    results = []
    for item in packed:
        name = item["acct"]
        ACCT_META.setdefault(name, {
            "auth_path": item.get("auth_path", ""),
            "container": item.get("container", ""),
            "source": item.get("source", ""),
        })
        results.append((name, item.get("source", ""), item.get("ok", False), item.get("usage"), item.get("error") or ""))
else:
    results = []  # [(name, source, auth_ok, usage_dict_or_none, err_str)]
    for name, auth_path, container, source in ACCOUNTS:
        auth, err = load_auth(auth_path)
        if err:
            results.append((name, source, False, None, err))
            continue
        tok, aid = auth
        usage, err = fetch_usage(tok, aid)
        if err:
            code, body = err
            if code == 401:
                results.append((name, source, False, None, "401 token_invalidated"))
            elif code == 403:
                results.append((name, source, False, None, f"403 Cloudflare after {RETRY} retries"))
            else:
                results.append((name, source, False, None, f"HTTP {code}: {body[:60]}"))
            continue
        results.append((name, source, True, usage, ""))


def acct_num(name):
    return int(name.split("-", 1)[1])


def source_group(source):
    return (source or "").split(":", 1)[0]


results.sort(key=lambda item: (source_group(item[1]), acct_num(item[0])))

if WANT_JSON:
    out = []
    for name, source, ok, usage, err in results:
        meta = ACCT_META.get(name, {})
        out.append({
            "acct": name,
            "source": source or meta.get("source"),
            "auth_path": meta.get("auth_path"),
            "container": meta.get("container"),
            "ok": ok,
            "error": err or None,
            "usage": usage,
        })
    print(json.dumps(out, indent=2, ensure_ascii=False))
    sys.exit(0)


# ── 主表（默认只显示健康账号，USAGE_ALL=1 则全显示）─────────────

hdr = ["acct", "source", "plan", "email", "5h%", "5h reset", "wk%", "wk reset",
       "spark 5h%", "spark wk%", "credits", "status"]
rows = []
for name, source, ok, usage, err in results:
    if not ok:
        if not SHOW_ALL:
            continue
        rows.append([name, source or "—", "—", err, "—", "—", "—", "—", "—", "—", "—", "❌"])
        continue
    plan = usage.get("plan_type", "?")
    email = usage.get("email", "")
    rl = usage.get("rate_limit", {}) or {}
    pw = rl.get("primary_window", {}) or {}
    sw = rl.get("secondary_window", {}) or {}
    add = usage.get("additional_rate_limits", []) or []
    spark = next((a for a in add if "Spark" in a.get("limit_name", "")), None)
    s_pw = (spark or {}).get("rate_limit", {}).get("primary_window", {}) or {}
    s_sw = (spark or {}).get("rate_limit", {}).get("secondary_window", {}) or {}
    cred = usage.get("credits") or {}
    has_cred = cred.get("has_credits", False)
    limit_reached = rl.get("limit_reached", False)

    def pct(w):
        v = w.get("used_percent")
        return "—" if v is None else f"{v}%"

    rows.append([
        name, source or "—", plan, email or "",
        pct(pw), fmt_eta(pw.get("reset_after_seconds")),
        pct(sw), fmt_eta(sw.get("reset_after_seconds")),
        pct(s_pw), pct(s_sw),
        "yes" if has_cred else "no",
        "⚠ LIMIT REACHED" if limit_reached else "✅",
    ])

if rows:
    w = [max(len(str(r[i])) for r in [hdr] + rows) for i in range(len(hdr))]
    def line(r):
        print("  ".join(str(r[i]).ljust(w[i]) for i in range(len(r))))
    line(hdr); print("  ".join("-" * x for x in w))
    for r in rows: line(r)
else:
    print("(主表为空：所有账号都异常)")


# ── 异常区（独立成块，每条带具体修复命令）────────────────────

bad = [(n, src, e) for n, src, ok, _, e in results if not ok]
unexpected_bad = [(n, src, e) for n, src, e in bad if not (n in EXPECTED_MISSING and "no auth.json" in e)]

if bad:
    n_unexpected = len(unexpected_bad)
    n_expected = len(bad) - n_unexpected
    parts = []
    if n_unexpected:
        parts.append(f"需处理 {n_unexpected} 个")
    if n_expected:
        parts.append(f"预期跳过 {n_expected} 个")
    print(f"\n异常账号（{', '.join(parts)}）：")
    for name, src, err in bad:
        meta = ACCT_META.get(name, {})
        container = meta.get("container", "?")
        auth_path = meta.get("auth_path", "")
        auth_dir = os.path.dirname(auth_path) if auth_path else "?"
        if name in EXPECTED_MISSING and "no auth.json" in err:
            print(f"  {name:<8}  {src:<24}  {err}")
            print(f"             ↳ 预期状态：legacy slot，prod 没注册它做轮询，跳过即可")
        elif "401" in err:
            print(f"  {name:<8}  {src:<24}  {err}")
            print(f"             ↳ refresh token 失效；在该账号实际服务位置重新 OAuth")
            print(f"               auth_dir={auth_dir} container={container}")
        elif "403" in err:
            print(f"  {name:<8}  {src:<24}  {err}")
            print(f"             ↳ Cloudflare 偶发拦截（authed API 也会撞），过几分钟重跑")
            print(f"               或本次调高重试：USAGE_RETRY=5 ./scripts/chatgpt-acct-usage.sh")
        elif "no auth.json" in err:
            print(f"  {name:<8}  {src:<24}  {err}")
            print(f"             ↳ 还没 OAuth；走 'OAuth 首次授权' 流程（见 SKILL.md）后 auth.json 落到 {auth_dir}/")
        elif "parse error" in err or "missing access_token" in err:
            print(f"  {name:<8}  {src:<24}  {err}")
            print(f"             ↳ auth.json 损坏：检查 {auth_path}；可能需重新 OAuth")
        elif "probe_channel_eof" in err:
            print(f"  {name:<8}  {src:<24}  {err}")
            print(f"             ↳ 探针通道异常，不等于账号失效；先复跑脚本或用 Pod 内 /codex/usage + 服务请求交叉验证")
        else:
            print(f"  {name:<8}  {src:<24}  {err}")
            print(f"             ↳ 未识别异常，先看该账号实际服务位置的 {container} 日志")


# ── 容量摘要 ─────────────────────────────────────────────

healthy_rows = [r for r in rows if r[-1] == "✅"]
ok_count = sum(1 for _, _, ok, _, _ in results if ok)
expected_missing = sum(1 for n, _, ok, _, e in results
                       if not ok and n in EXPECTED_MISSING and "no auth.json" in e)
print(f"\n健康账号：{ok_count} / {len(results) - expected_missing}（预期 slot 已扣除 {expected_missing} 个）")
if healthy_rows:
    def parse_pct(s):
        return int(s.rstrip('%')) if s and s != "—" and s.endswith("%") else None
    pcts_5h = [parse_pct(r[4]) for r in healthy_rows]
    pcts_wk = [parse_pct(r[6]) for r in healthy_rows]
    pcts_5h = [v for v in pcts_5h if v is not None]
    pcts_wk = [v for v in pcts_wk if v is not None]
    max_5h = max(pcts_5h, default=0)
    max_wk = max(pcts_wk, default=0)
    print(f"最高 5h%：{max_5h}%   最高 wk%：{max_wk}%")
    if max_5h >= 50:
        print("  ⚠ 有账号 5h% ≥ 50%，考虑短期降流量或扩账号")
    if max_wk >= 70:
        print("  ⚠ 有账号 wk% ≥ 70%，本周容量紧张")
