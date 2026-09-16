#!/usr/bin/env python3
"""逐账号 × 逐模型量一张真实可用性矩阵（198 上跑）。

**这把尺子的形状是照 CLIProxyAPI v7.2.153 源码 1:1 重建的**，不是我拍的：

  internal/runtime/executor/antigravity_executor_request.go  geminiToAntigravity()
    body 里除了 model/project/request 之外，网关还会塞
      "userAgent": "antigravity"
      "requestType": "agent"（模型名含 image 则 "image_gen"）
      "requestId":  "agent-<uuid>"（image 是 "image_gen/<ms>/<uuid>/12"）
      "request.sessionId": "-<int64>"（按首条 user 文本 sha256 推的稳定 id；image 不塞）
    并且会 **删掉** request.safetySettings。
  UA 走 misc.AntigravityRequestUserAgent(auth.user_agent)，
    凭据里没钉 user_agent 就是 hub 族 `antigravity/hub/<ver> darwin/arm64`。

⚠️ 2026-09-08 的教训：少塞这四个字段，`gemini-3.8-flash-high` 这类模型会回 404
`Requested entity was not found`，看起来像"这个号没有这个模型"——**是尺子坏了**。
判据永远是唯一 nonce 原样回读，不是 HTTP 200 也不是 404 的字面意思。

用法（198）：
    sudo python3 cliproxy-antigravity-model-matrix.py            # 全部号 × 全部模型
    sudo python3 cliproxy-antigravity-model-matrix.py -n 3       # 每格打 3 发
    sudo python3 cliproxy-antigravity-model-matrix.py --quota    # 只拉配额表，一发生成都不打
    sudo python3 cliproxy-antigravity-model-matrix.py --only a@x.com --quota   # 先看清单再决定探什么

⛔ **2026-09-08 第二个更贵的教训：UA 版本号会改变模型目录，量清单时它是自变量。**
同一个老号、同一时刻，只换 UA：

  | UA                          | fetchAvailableModels | 打 gemini-3.8-flash-high |
  |-----------------------------|----------------------|--------------------------|
  | `antigravity/1.0.0 windows/amd64` | **27 个，无 3.8/3.7-flash** | **404** |
  | `antigravity/2.12.2 windows/amd64`（及 hub/2.12.2 mac、hub/2.12.2 win）| **33 个，有** | **200** |

我一开始拿 `1.0.0` 量新号、拿 hub `2.12.2` 量老号，于是把「老客户端只拿到旧目录」
误读成「新号没有这个模型」——**一次改了两个变量（账号 + UA），把差异全归给了账号。**
真相：**没有任何账号缺 3.8-flash**。所以量清单时 UA 必须**跨号钉成同一个**，
要比较账号就只准动账号这一个变量。

新号的真实形状是**账号级校验没过**（不是目录差异）：任何能看到 33 个模型的 UA 上
都回 403 `VALIDATION_REQUIRED / Verify your account to continue.`，
details 里给 `validation_url`（`support.google.com/accounts?p=al_alert`，
即"异常活动请验证身份"），而 `loadCodeAssist` 明确返 `paidTier: g1-pro-tier`
—— **订阅是好的，是号没验证**。钉 `1.0.0` 只是绕开了这道校验并被降级到旧目录，
是止血不是修好。修好 = 真人点 validation_url 走完验证，然后**摘掉钉的 user_agent**。

⚠️ **2026-09-09 补的第三处尺子缺陷：per-auth `model_aliases` 必须跟着解析。**
钉了 `1.0.0` 的号够不到 `gemini-3.8-flash-high` 这个**名字**（`-high/-medium/-low`
只在 33 目录里，`-tiered` 才是两份目录都有的通用名），已在 auth JSON 里加了

    "model_aliases": [{"name": "gemini-3.8-flash-tiered",
                       "alias": "gemini-3.8-flash-high"}, ...]

网关会按 `alias`→`name` 改写后再发上游。**本脚本原先直接拿客户端名打上游，
于是对这两个号量出 404，看起来像「这条腿没有 3.8」——又是尺子坏了，不是腿坏了。**
现在按各号的 `model_aliases` 解析后再探，输出里会标 `(alias→真名)`。
要量「账号原生有没有这个名字」而不是「当前配置能不能服务」，加 `--no-alias`。
"""
import argparse
import base64
import binascii
import glob
import hashlib
import json
import os
import random
import struct
import sys
import time
import urllib.parse
import urllib.request
import uuid

CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
# client_secret 不进仓库（本仓 public）。198 上 /root/cliproxy-manifests/antigravity-oauth-client-secret
# （600，一行 GOCSPX-...），或 env ANTIGRAVITY_CLIENT_SECRET 覆盖。
CLIENT_SECRET_FILE = os.environ.get(
    "ANTIGRAVITY_CLIENT_SECRET_FILE",
    "/root/cliproxy-manifests/antigravity-oauth-client-secret")


def client_secret():
    v = os.environ.get("ANTIGRAVITY_CLIENT_SECRET", "").strip()
    if v:
        return v
    try:
        with open(CLIENT_SECRET_FILE) as fh:
            v = fh.read().strip()
    except OSError as e:
        sys.exit("拿不到 OAuth client_secret：%s\n"
                 "  给 %s 写入那一行，或 export ANTIGRAVITY_CLIENT_SECRET=..." % (e, CLIENT_SECRET_FILE))
    if not v:
        sys.exit("%s 是空的" % CLIENT_SECRET_FILE)
    return v


TOKEN_EP = "https://oauth2.googleapis.com/token"
DAILY = "https://daily-cloudcode-pa.googleapis.com"
UA_HUB_FALLBACK = "antigravity/hub/2.9.1 darwin/arm64"
HUB_MANIFEST = ("https://antigravity-hub-auto-updater-974169037036.us-central1.run.app"
                "/manifest/latest-arm64-mac.yml")
MANIFEST_DIR = "/root/cliproxy-manifests"

# 网关 `-local-model` 下内置的 11 个模型（= LiteLLM 那 12 条 entry 的上游）
MODELS = [
    "gemini-pro-agent", "gemini-3.1-pro-low",
    "gemini-3.8-flash-high", "gemini-3.7-flash-high", "gemini-3.6-flash-high",
    "gemini-3-flash", "gemini-3.1-flash-lite",
    "gpt-oss-120b-medium", "claude-opus-4-6-thinking", "claude-sonnet-4-6",
]
# ⚠️ 图像模型**默认不探**，要 --with-image 才探。它有一个独立的、极小的、
#    `quotaInfo.remainingFraction` **完全不上报**的图像配额：2026-09-08 我拿它当
#    普通模型探了十来发，两个号双双打空，报
#    `429 You have exhausted your capacity on this model. Your quota will reset
#    after 1h21m3s`（另一个号 4h25m6s），而同一时刻 quotaInfo 还显示 0.976/0.996。
#    也就是说**探这个模型 = 花掉用户真正稀缺的额度**，量具不许默认这么干。
IMAGE_MODELS = ["gemini-3.1-flash-image"]


def http_json(url, body=None, headers=None, timeout=180, form=False):
    hdr = dict(headers or {})
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            hdr.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            data = json.dumps(body).encode()
            hdr.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:400]}
    except Exception as e:
        return 0, {"raw": "%s: %s" % (type(e).__name__, e)}


def hub_user_agent():
    try:
        with urllib.request.urlopen(HUB_MANIFEST, timeout=15) as r:
            import re
            return "antigravity/hub/%s darwin/arm64" % re.search(
                r"version:\s*([0-9.]+)", r.read().decode()).group(1)
    except Exception:
        return UA_HUB_FALLBACK


def refresh(creds):
    st, tok = http_json(TOKEN_EP, {
        "client_id": CLIENT_ID, "client_secret": client_secret(),
        "refresh_token": creds["refresh_token"], "grant_type": "refresh_token",
    }, form=True)
    if st != 200 or "access_token" not in tok:
        return None, "refresh 失败 %s %s" % (st, json.dumps(tok)[:200])
    return tok["access_token"], None


def stable_session_id(text):
    """照抄 generateStableSessionID：首条 user 文本 sha256 前 8 字节 & 0x7FFF...。"""
    h = hashlib.sha256(text.encode()).digest()
    return "-%d" % (struct.unpack(">Q", h[:8])[0] & 0x7FFFFFFFFFFFFFFF)


def gateway_body(model, project, text):
    """1:1 复刻 geminiToAntigravity() 产出的 body。"""
    is_image = "image" in model
    body = {
        "model": model,
        "userAgent": "antigravity",
        "requestType": "image_gen" if is_image else "agent",
        "request": {"contents": [{"role": "user", "parts": [{"text": text}]}]},
    }
    if project:
        body["project"] = project
    if is_image:
        body["requestId"] = "image_gen/%d/%s/12" % (int(time.time() * 1000), uuid.uuid4())
    else:
        body["requestId"] = "agent-" + str(uuid.uuid4())
        body["request"]["sessionId"] = stable_session_id(text)
    return body


def probe(at, ua, project, model):
    n = "NONCE-%d" % random.randint(10 ** 8, 10 ** 9)
    text = "Reply with exactly: " + n
    st, body = http_json(DAILY + "/v1internal:generateContent",
                         gateway_body(model, project, text),
                         {"Authorization": "Bearer " + at, "User-Agent": ua})
    raw = json.dumps(body)
    return (n in raw), st, raw[:260]


def fetch_models(at, ua, project):
    return http_json(DAILY + "/v1internal:fetchAvailableModels",
                     {"project": project} if project else {},
                     {"Authorization": "Bearer " + at, "User-Agent": ua})


def alias_map(creds):
    """per-auth `model_aliases` → {客户端名: 上游真名}。

    照 CLIProxyAPI 的语义：条目里 `name` 是**上游真名**，`alias` 是**客户端可见名**，
    网关收到 alias 后改写成 name 再发上游。只对该号生效，优先于全局 oauth-model-alias。
    """
    out = {}
    for e in creds.get("model_aliases") or []:
        if isinstance(e, dict) and e.get("name") and e.get("alias"):
            out[e["alias"]] = e["name"]
    return out


def load_accounts():
    out = []
    for f in sorted(glob.glob(os.path.join(MANIFEST_DIR, "antigravity-*.json"))):
        try:
            c = json.load(open(f))
        except Exception:
            continue
        if c.get("type") != "antigravity" or not c.get("refresh_token"):
            continue
        out.append((f, c))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=2, help="每格打几发（默认 2）")
    ap.add_argument("--quota", action="store_true", help="只拉 fetchAvailableModels 配额表")
    ap.add_argument("--models", help="逗号分隔，只探这些模型")
    ap.add_argument("--only", help="逗号分隔的 email，只探这些号")
    ap.add_argument("--with-image", action="store_true",
                    help="连图像模型一起探。⚠️ 图像配额极小且 quotaInfo 不上报，"
                         "探几发就会把用户的真实额度打空，别随手加")
    ap.add_argument("--ua", metavar="UA",
                    help="所有号强制用同一个 UA（**跨号比模型清单必须加这个**：UA 版本号"
                         "本身会改变目录，1.0.0 只给 27 个、2.12.2 给 33 个。不加就按各号"
                         "凭据里钉的 UA 走——那是量「按当前配置能不能服务」，不是量账号）")
    ap.add_argument("--no-alias", action="store_true",
                    help="不解析 per-auth model_aliases，直接拿客户端名打上游。"
                         "这是量「账号原生有没有这个名字」，**不是**量「当前配置能不能服务」")
    ap.add_argument("--json", metavar="PATH", help="把结构化结果落盘，供报告引用")
    args = ap.parse_args()

    hub = hub_user_agent()
    if args.ua:
        print("⚠️ 已强制所有号用 UA=%s（覆盖凭据里钉的），这是**跨号比清单**模式" % args.ua)
    models = args.models.split(",") if args.models else (
        MODELS + IMAGE_MODELS if args.with_image else MODELS)
    accts = load_accounts()
    if args.only:
        want = set(e.strip() for e in args.only.split(","))
        accts = [(p, c) for p, c in accts if c.get("email") in want]
        missing = want - {c.get("email") for _, c in accts}
        if missing:
            sys.exit("在 %s 里没找到这些号的凭据：%s" % (MANIFEST_DIR, sorted(missing)))
    if not accts:
        sys.exit("在 %s 里没找到任何 antigravity-*.json" % MANIFEST_DIR)
    print("网关默认 UA = %s" % hub)

    results = {}
    upstream = {}
    structured = {}
    broken = []
    for path, c in accts:
        email = c.get("email", os.path.basename(path))
        # 凭据里钉了 user_agent 就用它——网关就是这么选的；--ua 强制统一以便跨号对比
        ua = args.ua or ((c.get("user_agent") or "").strip() or hub)
        project = (c.get("project_id") or "").strip()
        amap = {} if args.no_alias else alias_map(c)
        print("\n=== %s  (project=%r, UA=%s) ===" % (email, project, ua))
        if amap:
            print("  per-auth model_aliases: %s" % ", ".join(
                "%s→%s" % (a, n) for a, n in sorted(amap.items())))

        at, err = refresh(c)
        if err:
            print("  ❌ " + err)
            broken.append((email, "refresh"))
            continue

        st, ml = fetch_models(at, ua, project)
        # ⚠️ 注意：网关拉模型表用的是 hub UA（sdk/cliproxy/antigravity_models.go:53，
        #    **不读凭据里的 user_agent**），所以钉了 UA 的号这里可能仍 403。
        #    不影响生成，因为跑的是 -local-model 内置清单。
        if st == 200 and isinstance(ml, dict):
            # ⚠️ `models` 是 **dict**（modelId -> 详情），不是 list。当成 list 遍历会拿到
            #    一堆字符串然后 AttributeError。
            ms = ml.get("models") or {}
            dep = set(ml.get("deprecatedModelIds") or [])
            print("  fetchAvailableModels: %d 个模型（deprecated %d 个）" % (len(ms), len(dep)))
            upstream[email] = ms
            for mid in models:
                real = amap.get(mid, mid)
                tag = "" if real == mid else "  (alias→%s)" % real
                if real not in ms:
                    print("    %-34s ⛔ 上游清单里没有这个名字%s" % (mid, tag))
                    continue
                d = ms[real]
                q = d.get("quotaInfo") or {}
                print("    %-34s remaining=%s reset=%s win=%s/%s%s%s" % (
                    mid, q.get("remainingFraction"), q.get("resetTime", "?"),
                    d.get("maxTokens"), d.get("maxOutputTokens"),
                    "  ⚠️DEPRECATED" if real in dep else "", tag))
        else:
            print("  fetchAvailableModels: http=%s %s" % (st, json.dumps(ml)[:160]))
        if args.quota:
            continue

        row = {}
        for m in models:
            real = amap.get(m, m)
            tag = "" if real == m else " (→%s)" % real
            hits, last, lat = 0, "", []
            for _ in range(args.n):
                t0 = time.time()
                ok, code, detail = probe(at, ua, project, real)
                lat.append(time.time() - t0)
                if ok:
                    hits += 1
                else:
                    last = "http=%s %s" % (code, detail)
            row[m] = (hits, last)
            structured.setdefault(email, {})[m] = {
                "upstream": real, "hits": hits, "n": args.n,
                "latency_s": [round(x, 2) for x in lat], "last_error": last[:300],
            }
            flag = "✅" if hits == args.n else ("⚠️" if hits else "❌")
            print("  %s %-30s %d/%d  %.1fs%s %s" % (
                flag, m, hits, args.n, sum(lat) / len(lat), tag, last[:130]))
            # 判失败要分两种：
            #   * 上游清单里**有**这个模型却 0 命中 → 真故障，退出码要红
            #   * 上游清单里**没有**（各号清单不一样，实测 33 vs 27）→ 只是能力差异，
            #     网关会按 (号×模型) 冷却 60s 后换号，用户看不到，不算故障
            if hits == 0:
                known = upstream.get(email)
                if known is None or real in known:
                    broken.append((email, m))
        results[email] = row

    if results and not args.quota:
        print("\n=== 汇总（命中/发数）===")
        emails = list(results)
        print("%-30s %s" % ("model", "  ".join("%-22s" % e for e in emails)))
        for m in models:
            print("%-30s %s" % (m, "  ".join(
                "%-22s" % ("%d/%d" % (results[e][m][0], args.n)) for e in emails)))
        dead = [m for m in models if all(results[e][m][0] == 0 for e in emails)]
        partial = [m for m in models
                   if any(results[e][m][0] == 0 for e in emails) and m not in dead]
        print("\n全账号都不通: %s" % (dead or "无"))
        print("部分账号不通: %s" % (partial or "无"))
        if partial:
            print("  ⚠️ 别急着说「这个号没有这个模型」——**先排掉 UA**："
                  "各号 UA 不同时清单本来就不同（1.0.0=27 个 / 2.12.2=33 个）。"
                  "加 `--ua 'antigravity/2.12.2 windows/amd64'` 跨号统一再量一遍；"
                  "统一 UA 后还差，才是账号差异。403 VALIDATION_REQUIRED 是**账号没过验证**，"
                  "不是没这个模型。")
        # ⚠️ "全账号都不通" **不等于** "该下线"。2026-09-08 栽过：图像模型两个号双双
        #    0 命中，脚本当时的文案写的是"该考虑下线"，而现场是**我自己刚把额度探空了**。
        #    所以凡是 0 命中且报文里带 exhausted/RESOURCE_EXHAUSTED 的，先自查这一轮打了多少发。
        if dead:
            spent = [m for m in dead if any(
                ("exhaust" in results[e][m][1].lower() or "429" in results[e][m][1])
                for e in emails)]
            if spent:
                print("  ⚠️ 其中 %s 的失败报文里带 429/exhausted ——" % spent)
                print("     **先自查是不是我这一轮的探针把额度打空的**（图像/语音这类按次限量的"
                      "配额 quotaInfo 根本不上报），确认不是再谈下线。")
            print("  下线前还要过一道：拿这个模型走网关 + 走 LiteLLM 各打一发，"
                  "用户面也不通才算真不可用。")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "n": args.n,
                       "forced_ua": args.ua, "alias_resolved": not args.no_alias,
                       "accounts": structured}, fh, ensure_ascii=False, indent=1)
        print("\n结构化结果已落盘: %s" % args.json)

    if broken:
        print("\n❌ 真故障（上游清单里有这个模型却 0 命中，或 refresh 失败）:")
        for e, m in broken:
            print("   %s / %s" % (e, m))
        sys.exit(1)
    print("\n✅ 没有真故障。")


if __name__ == "__main__":
    main()
