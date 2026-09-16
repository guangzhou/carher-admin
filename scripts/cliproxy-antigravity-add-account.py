#!/usr/bin/env python3
"""给 198 的 cli-proxy-api 网关加 Google AI Pro(Antigravity)账号。

在 198 上跑（需要 sudo kubectl + 直连 Google 出网，两者 198 都有）：

    # 一个号
    sudo python3 cliproxy-antigravity-add-account.py authurl
    # 浏览器打开 → 授权 → 地址栏会跳到 localhost:54545 打不开，把整条 URL 复制回来
    sudo python3 cliproxy-antigravity-add-account.py exchange --code '<那条 URL 或纯 code>'
    sudo python3 cliproxy-antigravity-add-account.py install --creds /root/cliproxy-manifests/antigravity-<email>.json --apply

    # 一批号（100 个就走这条）：先逐个 exchange 攒凭据，再一次性装
    sudo python3 cliproxy-antigravity-add-account.py exchange --code '<URL1>'
    sudo python3 cliproxy-antigravity-add-account.py exchange --code '<URL2>'
    ...
    sudo python3 cliproxy-antigravity-add-account.py install --creds-dir /root/cliproxy-manifests --apply
    # ↑ 一次 Secret patch + **一次** rollout，不是每号一次

    # 平时看池子 / 回归
    sudo python3 cliproxy-antigravity-add-account.py list       # 只读，不发请求不花额度
    sudo python3 cliproxy-antigravity-add-account.py regress    # 打到 LiteLLM 用户面
    sudo python3 cliproxy-antigravity-add-account.py verify --only a@x.com   # 判单个号死活

**authurl 只用跑一次**：那条链接不绑账号（账号由浏览器登录态决定），
换号重点一次就多一条 callback URL。100 个号的真正瓶颈是 consent 页要真人点。

三层判据，别混用（2026-09-08 三层全跑过一遍）：
  * `verify --only <email>` = **个体**。直打上游，绕开网关池子。
  * `verify`（不带 --only）/ 网关 nonce = **池子整体还能服务**，不回答谁在服务。
  * `regress` = **用户真实入口**（LiteLLM entry + key 白名单 + strict 模式）。
    网关绿不代表用户面绿；反过来用户面绿也不代表每个号都健康。

设计纪律（和仓库里其它 onboard 脚本一致）：
  * install 默认 dry-run，--apply 才写；写前自动备份 Secret。
  * 每一步的判据都是「唯一 nonce 被原样回读」，不是 HTTP 200。
    坏端点上 :loadCodeAssist 和 /v1/models 都照样 200，是假绿。
  * exchange 阶段就把账号打通验证完，**没通过绝不进集群**。
  * 装完的判活**直打上游、逐号单独打**（借 cliproxy-antigravity-model-matrix.py），
    不打网关 —— 网关 round-robin 会把坏号的 403 悄悄重试到好号上，池子级 nonce 命中
    是**假绿**（2026-09-08 栽过：脚本对一个 100% 坏的号报了"✅ 通过"）。
  * regress 用**临时受限 key** 不用 master key（master 绕过白名单 = 没测白名单），
    且必带一个白名单外模型的反向对照。

加号只是扩容：**LiteLLM 的 entry 和 key 白名单一个字都不用动。**

背景见 skill `cliproxy-antigravity-ops`。
"""
import argparse
import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# ---- 常量：从运行中的 CLIProxyAPI 二进制里抠出来的，别凭记忆改 ----
CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
# client_secret 不进仓库（本仓 public）。198 上放 /root/cliproxy-manifests/antigravity-oauth-client-secret
# （600，内容就是那一行 GOCSPX-...），或用 env ANTIGRAVITY_CLIENT_SECRET 覆盖。
# 想重新取值：从运行中的 CLIProxyAPI 二进制里抠，别凭记忆填。
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


REDIRECT_URI = "http://localhost:54545/callback"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]
AUTH_EP = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_EP = "https://oauth2.googleapis.com/token"
# 判别变量是端点主机名。prod(cloudcode-pa) 对消费者订阅恒 429 且是误导文案。
DAILY = "https://daily-cloudcode-pa.googleapis.com"
# UA 的「族」是第二个判别变量（2026-09-08 定因）。网关默认发 hub 族
# `antigravity/hub/<ver> darwin/arm64`（桌面 App 的 UA，版本从 hub manifest 现拉，
# 拉不到用兜底 2.9.1）。**新注册的 Google 号在 hub 族上会 403 VALIDATION_REQUIRED
# "Verify your account to continue."，换成下面这个 UA 就 200。**
# 源码 `antigravityConfiguredUserAgent()` 会优先读凭据 JSON 里的 `user_agent` 字段，
# 所以修法是把能用的 UA 钉进凭据，不用改网关。
UA_WORKING = "antigravity/1.0.0 windows/amd64"
UA_HUB_FALLBACK = "antigravity/hub/2.9.1 darwin/arm64"
HUB_MANIFEST = ("https://antigravity-hub-auto-updater-974169037036.us-central1.run.app"
                "/manifest/latest-arm64-mac.yml")
DEFAULT_PROJECT = "aicode-consumers"

NS = "litellm-dev"
SECRET = "cliproxy-secrets"
DEPLOY = "cli-proxy-api"
MANIFEST_DIR = "/root/cliproxy-manifests"
GATEWAY = "http://127.0.0.1:31882"
KUBECFG = "/etc/rancher/k3s/k3s.yaml"

# LiteLLM 侧（用户真实入口）。加号本身不需要动 LiteLLM，但**回归要打到这一层**——
# 网关绿只证明网关绿。`regress` 子命令会临时建一把受限 key 打完就删。
LITELLM = "http://127.0.0.1:30402"
LITELLM_NS = "litellm-product"
LITELLM_SECRET = "litellm-secrets"
# 第三个故意选一个「多数号的上游清单里没有」的模型：它能同时证明
# (号×模型) 冷却 + 换号重试这条兜底真的在工作，而不只是证明池子里有健康号。
LITELLM_PROBE_MODELS = ["ag-gemini-3.1-pro", "claude-ag-opus-4.6", "ag-gemini-3.8-flash"]
LITELLM_DENY_MODEL = "chatgpt-gpt-5.6-sol"   # 反向对照：白名单外，期望 401/403

# ⚠️ **这个数是判据的一部分，不是随手填的上限。**
# gemini/claude 这些推理模型会先烧一大截 thinking token（实测 41~133）才吐正文，
# `max_tokens` 给小了，正文被截断 ⇒ nonce 回读不到 ⇒ **一条好腿被判成红**。
# 2026-09-09 之前这里写的是 64，离 09-09 实测过的假红阈值（50 截出 `'S4'`）只差一点，
# 属于随时会咬人的潜伏尺子缺陷。别再往下调。
MAX_TOKENS = 2000

# 同事 key 上**客户端真正打的公开名** → 真实组。这层 per-key `aliases` 改写是独立的一跳，
# 只测真实组名的回归**测不到它**。{公开名: 真实组}
PUBLIC_ALIASES = {"gemini-3.8-flash": "ag-gemini-3.8-flash"}


def discover_litellm_ag_models(master_key):
    """向 LiteLLM 现拉 `ag-*` / `claude-ag-*` 组名，别写死——Google 一直改名。"""
    st, r = http_json(LITELLM + "/v1/models", headers={"Authorization": "Bearer " + master_key})
    if st != 200:
        sys.exit("拉 LiteLLM 模型清单失败 (%s): %s" % (st, json.dumps(r)[:200]))
    return sorted(m["id"] for m in r.get("data", [])
                  if m.get("id", "").startswith(("ag-", "claude-ag-")))


def kubectl_ns(ns, *args, check=True):
    cmd = ["kubectl", "--kubeconfig", KUBECFG, "-n", ns] + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit("kubectl failed: %s\n%s" % (" ".join(cmd), p.stderr))
    return p.stdout


def kubectl(*args, check=True):
    return kubectl_ns(NS, *args, check=check)


def http_json(url, body=None, headers=None, timeout=120, form=False):
    data = None
    hdr = dict(headers or {})
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
            return e.code, {"raw": raw}


def nonce():
    return "NONCE-%d" % random.randint(10 ** 8, 10 ** 9)


# --------------------------------------------------------------- authurl
def cmd_authurl(args):
    state = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    url = AUTH_EP + "?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",       # 必须，否则复授权拿不到 refresh_token
        "state": state,
    })
    print("用要接入的 Google 账号打开下面这条链接授权：\n")
    print(url)
    print("\n授权后浏览器会跳到 http://localhost:54545/callback?... 并显示打不开——这是正常的。")
    print("把地址栏那整条 URL 复制下来，喂给 exchange 子命令即可。")
    print("\n💡 攒 100 个号时不用一个号跑一次 authurl：**同一条链接可以反复用**，")
    print("   浏览器里换 Google 账号（或用不同 profile / 无痕窗）再点一次授权即可，")
    print("   每次会拿到一条新的 callback URL。code 一次性、账号由登录态决定，")
    print("   所以链接本身不绑账号。真正的瓶颈是 consent 页必须真人点，脚本代替不了。")


# -------------------------------------------------------------- exchange
def parse_code(raw):
    raw = raw.strip()
    if raw.startswith("http"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        if "error" in q:
            sys.exit("回调 URL 里带的是错误：%s" % q["error"][0])
        if "code" not in q:
            sys.exit("回调 URL 里没有 code 参数")
        return q["code"][0]
    return raw


def jwt_email(id_token):
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("email")


def hub_user_agent():
    """网关实际会发的 UA。拉不到 manifest 就用它自己的兜底版本。"""
    try:
        with urllib.request.urlopen(HUB_MANIFEST, timeout=15) as r:
            v = re.search(r"version:\s*([0-9.]+)", r.read().decode()).group(1)
        return "antigravity/hub/%s darwin/arm64" % v
    except Exception:
        return UA_HUB_FALLBACK


def generate_probe(access_token, project, ua, model="gemini-pro-agent"):
    """唯一 nonce 打 daily :generateContent，返回 (命中, http, 报文片段)。"""
    n = nonce()
    code, body = http_json(
        DAILY + "/v1internal:generateContent",
        {"model": model, "project": project,
         "request": {"contents": [{"role": "user",
                                   "parts": [{"text": "Reply with exactly: " + n}]}]}},
        {"Authorization": "Bearer " + access_token, "User-Agent": ua},
    )
    return (n in json.dumps(body)), code, json.dumps(body)[:300]


def cmd_exchange(args):
    code = parse_code(args.code)
    st, tok = http_json(TOKEN_EP, {
        "client_id": CLIENT_ID, "client_secret": client_secret(),
        "code": code, "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }, form=True)
    if st != 200 or "refresh_token" not in tok:
        sys.exit("换 token 失败 (%s): %s\n"
                 "常见原因：code 只能用一次 / 授权链接漏了 prompt=consent" % (st, json.dumps(tok)[:400]))

    email = jwt_email(tok["id_token"]) if tok.get("id_token") else None
    if not email:
        sys.exit("id_token 里没解出 email，无法确定凭据文件名")
    print("账号 = %s" % email)

    at = tok["access_token"]
    # project_id：先试默认桶，nonce 不中就留空让网关自己 FetchAntigravityProjectID。
    hub_ua = hub_user_agent()
    ok, hc, detail = generate_probe(at, DEFAULT_PROJECT, hub_ua)
    project = DEFAULT_PROJECT
    if not ok:
        ok2, hc2, d2 = generate_probe(at, "", hub_ua)
        if ok2:
            project, ok, hc, detail = "", True, hc2, d2
            print("默认 project_id 不通、留空可用 → project_id 留空")

    # 必须**用网关真正会发的 UA** 探。拿别的 UA 探是假绿：
    # hub 族对新注册的号会 403 VALIDATION_REQUIRED，而其它 UA 照样 200。
    pinned_ua = None
    if ok:
        print("daily 端点实测通过 ✅  project_id=%r  UA=%s" % (project, hub_ua))
    else:
        print("网关默认 UA (%s) 没通过 (http=%s)：%s" % (hub_ua, hc, detail))
        ok3, hc3, d3 = generate_probe(at, project, UA_WORKING)
        if ok3:
            pinned_ua = UA_WORKING
            print("换 UA=%s 通过 ✅ → 把 user_agent 钉进凭据，网关会优先用它。" % UA_WORKING)
        else:
            print("换 UA=%s 也没通过 (http=%s)：%s" % (UA_WORKING, hc3, d3))
            if not args.force:
                sys.exit("这个号在两种 UA 下都不能真生成，拒绝生成凭据。硬来请加 --force。")

    creds = {
        "type": "antigravity", "email": email, "project_id": project,
        "access_token": at, "refresh_token": tok["refresh_token"],
        "expires_in": tok.get("expires_in", 3599), "timestamp": 0,
        "expired": "2020-01-01T00:00:00Z", "disabled": False,
    }
    if pinned_ua:
        creds["user_agent"] = pinned_ua
    out = args.out or os.path.join(MANIFEST_DIR, "antigravity-%s.json" % email)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(creds, f)
    os.chmod(out, 0o600)
    print("凭据已写入 %s (600)" % out)
    print("下一步：install --creds %s --apply" % out)


# --------------------------------------------------------------- install
def secret_key_for(email):
    """Secret 的 key 名不能带 @，所以落地成 antigravity-<slug>.json；
    initContainer 会照 JSON 里的 email 字段还原成真实文件名。"""
    return "antigravity-%s.json" % re.sub(r"[^A-Za-z0-9._-]", "-", email)


def cmd_install(args):
    paths = list(args.creds or [])
    if args.creds_dir:
        import glob as _glob
        paths += [p for p in sorted(_glob.glob(os.path.join(args.creds_dir, "antigravity-*.json")))
                  if p not in paths]
    if not paths:
        sys.exit("没有凭据文件可装（--creds / --creds-dir 都空）")

    cur = json.loads(kubectl("get", "secret", SECRET, "-o", "json"))
    existing = sorted(k for k in cur["data"] if k.startswith("antigravity-"))
    print("Secret 现有凭据 key: %d 个" % len(existing))

    # key -> (b64, email)；同一批里也要查重
    todo, emails = {}, {}
    for p in paths:
        try:
            creds = json.load(open(p))
        except Exception as e:
            sys.exit("读不了 %s: %s" % (p, e))
        if creds.get("type") != "antigravity" or not creds.get("refresh_token"):
            print("  跳过 %s（不是 antigravity 凭据）" % os.path.basename(p))
            continue
        email = creds["email"]
        key = secret_key_for(email)

        # 查重必须按 JSON 里的 email，不能按 key 名：老号的 key 是遗留名
        # antigravity-auth.json，和本脚本生成的 antigravity-<slug>.json 不同名，
        # 光比 key 名会把同一个账号装成两份。同账号两个 listener 会互相抢 token。
        dup = None
        for k in existing:
            if k == key:
                continue
            try:
                other = json.loads(base64.b64decode(cur["data"][k]))
            except Exception:
                continue
            if other.get("email") == email:
                dup = k
                break
        if dup:
            # 显式 --creds 点名 = 明确意图，硬拒；--creds-dir 扫目录 = 顺带扫到，跳过。
            # （目录里就躺着老号那份遗留名的凭据，硬拒会让批量流程根本跑不起来。）
            msg = ("账号 %s 已经以 key %s 存在于 Secret 里（遗留 key 名）。\n"
                   "要重新授权就先删掉那个 key，别装成两份。" % (email, dup))
            if p in (args.creds or []):
                sys.exit("拒绝：" + msg)
            print("  = 跳过 %s（已以 key %s 装过）" % (email, dup))
            continue
        if email in emails.values():
            sys.exit("拒绝：这一批里 %s 出现了两次" % email)

        new_b64 = base64.b64encode(json.dumps(creds).encode()).decode()
        if cur["data"].get(key) == new_b64:
            print("  = %s 已在 Secret 里且逐字节相同，跳过" % email)
            continue
        if key in cur["data"]:
            print("  ⚠️ %s 同名 key 已存在，将被覆盖（等同于重新授权该账号）" % email)
        else:
            print("  + %s → key %s" % (email, key))
        todo[key] = new_b64
        emails[key] = email

    if not todo:
        print("\n没有需要写入的变更。")
        return
    print("\n本次将写入 %d 个账号，之后 **一次** rollout。" % len(todo))

    if not args.apply:
        print("[dry-run] 未做任何改动。确认无误后加 --apply。")
        return

    bak = os.path.join(MANIFEST_DIR, "secret-backup-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(bak, "w") as f:
        f.write(json.dumps(cur))
    os.chmod(bak, 0o600)
    print("Secret 已备份到 %s" % bak)

    kubectl("patch", "secret", SECRET, "--type", "merge", "-p", json.dumps({"data": todo}))

    # 回读：写进去 ≠ 生效，逐字节比对
    back = json.loads(kubectl("get", "secret", SECRET, "-o", "json"))
    for k, v in todo.items():
        if back["data"].get(k) != v:
            sys.exit("回读不一致，key=%s 没写对。备份在 %s" % (k, bak))
    print("Secret 回读逐字节一致 ✅ (%d 个)" % len(todo))

    kubectl("rollout", "restart", "deploy/" + DEPLOY)
    kubectl("rollout", "status", "deploy/" + DEPLOY, "--timeout=300s")

    seed = kubectl("logs", pod_name(), "-c", "seed")
    print("--- initContainer seed 日志 ---")
    print(seed.rstrip()[-1500:])
    missing = [e for e in emails.values() if e not in seed]
    if missing:
        sys.exit("seed 日志里没有 %s，凭据没被铺进 /runtime/auths" % missing)

    verify(expect_emails=sorted(emails.values()))


# ---------------------------------------------------------------- verify
def client_key():
    with open(os.path.join(MANIFEST_DIR, ".keys")) as f:
        return f.read().split("CLIENT_API_KEY=")[1].split()[0]


def pod_name():
    return kubectl("get", "pod", "-l", "app=" + DEPLOY,
                   "-o", "jsonpath={.items[0].metadata.name}").strip()


def pod_sh(script):
    return kubectl("exec", pod_name(), "-c", DEPLOY, "--", "sh", "-c", script)


def nonce_burst(models, n=3):
    """返回 {model: 命中数}。判据只认 nonce 原样回读。"""
    ck = client_key()
    out = {}
    for m in models:
        hit = 0
        for _ in range(n):
            x = nonce()
            st, body = http_json(
                GATEWAY + "/v1/chat/completions",
                {"model": m, "messages": [{"role": "user", "content": "Reply with exactly: " + x}],
                 "max_tokens": MAX_TOKENS},
                {"Authorization": "Bearer " + ck})
            if x in json.dumps(body):
                hit += 1
            else:
                out.setdefault("_err", json.dumps(body)[:220])
        out[m] = hit
    return out


def per_account_proof(emails):
    """逐号单独证明它自己能真生成——**直打上游，不经过网关**。

    为什么不打网关：网关是 round-robin，一个坏号 403/404 之后会被换掉，
    请求悄悄落到健康号上照样 nonce 命中 —— 池子级判活是**假绿**
    （2026-09-08 实测：一个 100% 坏的新号，池子回归报"✅ 通过"）。
    旧版靠 `mv` 把其它号挪出 /runtime/auths 做隔离，100 个号扛不住，
    且会打断在途的真实请求。改成直接拿每个号自己的 refresh_token 打 daily 端点。
    """
    mm = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "cliproxy-antigravity-model-matrix.py")
    if not os.path.exists(mm):
        print("⚠️ 找不到 %s，跳过逐号验证（这一步是防假绿的，别长期缺）" % mm)
        return
    p = subprocess.run([sys.executable, mm, "-n", "2",
                        "--models", "gemini-pro-agent,claude-opus-4-6-thinking",
                        "--only", ",".join(emails)],
                       capture_output=True, text=True)
    print(p.stdout.rstrip())
    if p.returncode != 0:
        sys.exit("逐号验证未通过（上面是明细）。\n"
                 "常见原因：新注册的号在网关默认的 hub 族 UA 上会 403 VALIDATION_REQUIRED，\n"
                 "修法是往凭据 JSON 里加 \"user_agent\": \"%s\"（exchange 会自动探）。" % UA_WORKING)


def verify(expect_emails=None, all_models=False, n=4):
    ck = client_key()
    st, models = http_json(GATEWAY + "/v1/models", headers={"Authorization": "Bearer " + ck})
    names = [m["id"] for m in models.get("data", [])]
    print("网关 /v1/models: %d 个 —— %s" % (len(names), ", ".join(names[:4]) + " ..."))

    if expect_emails:
        print("\n--- 逐号验证（直打上游，%d 个号）---" % len(expect_emails))
        per_account_proof(expect_emails)

    if all_models:
        # ⚠️ 全量扫会连图像模型一起打，而图像有独立小配额且 quotaInfo 不上报。
        #    所以 --all 也把它排掉，要探图像只能显式点名。
        probe = [m for m in names if "image" not in m]
        n = 1
        print("\n--- 全池全模型回归（%d 个模型 × 1 发，走网关）---" % len(probe))
    else:
        probe = [m for m in ["gemini-pro-agent", "claude-opus-4-6-thinking"] if m in names]
        print("\n--- 全池回归（走网关，证明池子整体没坏）---")
    res = nonce_burst(probe, n=n)
    fails = [m for m in probe if res[m] < n]
    for m in probe:
        print("  %s nonce %-28s %d/%d" % ("✅" if res[m] == n else "❌", m, res[m], n))
    if fails:
        sys.exit("回归未通过: %s  %s" % (fails, res.get("_err", "")))

    # ⚠️ 这个计数只是线索不是判据：网关对 (号 × 模型) 做 60s 冷却后会换号重试，
    #    所以「有 404/403 行 + 用户面 100% 通」是**正常形态**，不是故障。
    #    各号模型清单本来就不一样（33 vs 27 实测），缺的那些必然留下 404 行。
    nbad = kubectl("logs", pod_name(), "-c", DEPLOY).count("upstream execution failed")
    print("  网关 upstream 失败日志行数: %d（不是判据，见上面注释）" % nbad)

    print("\n✅ 通过。网关按 round-robin 在多个账号间轮。")
    print("提醒：加号只是扩容，LiteLLM 的 entry 和 key 白名单不用动。")
    print("最后补一刀用户面：`regress`（临时受限 key 打 LiteLLM + 反向对照）。")


# ------------------------------------------------------------------ list
def cmd_list(args):
    """池子现状：只读 Secret 解码打印，不发一个请求，也不碰额度。

    加号之前先跑这个 —— 它能一眼看出「这个号是不是已经装过了」（按 email，
    不是按 key 名）、「哪些号钉了 user_agent」。
    """
    cur = json.loads(kubectl("get", "secret", SECRET, "-o", "json"))
    keys = sorted(k for k in cur["data"] if k.startswith("antigravity-"))
    print("Secret %s/%s 里共 %d 个 antigravity 凭据：\n" % (NS, SECRET, len(keys)))
    print("%-34s %-40s %-18s %s" % ("email", "secret key", "project_id", "user_agent"))
    legacy = []
    for k in keys:
        try:
            c = json.loads(base64.b64decode(cur["data"][k]))
        except Exception as e:
            print("%-34s %-40s  ❌ 解不开: %s" % ("?", k, e))
            continue
        email = c.get("email", "?")
        ua = c.get("user_agent") or "(网关默认 hub 族)"
        flag = "" if c.get("refresh_token") else "  ⚠️ 没有 refresh_token"
        if c.get("disabled"):
            flag += "  ⚠️ disabled=true"
        if k != secret_key_for(email):
            legacy.append((email, k))
        print("%-34s %-40s %-18s %s%s" % (email, k, c.get("project_id") or "(空)", ua, flag))
    if legacy:
        print("\n遗留 key 名（和本脚本的命名规则不一致，查重必须按 email 而不是 key 名）：")
        for e, k in legacy:
            print("  %s → %s（本脚本会叫 %s）" % (e, k, secret_key_for(e)))
    print("\n盘上凭据文件（%s）：" % MANIFEST_DIR)
    import glob as _glob
    for p in sorted(_glob.glob(os.path.join(MANIFEST_DIR, "antigravity-*.json"))):
        try:
            e = json.load(open(p)).get("email", "?")
        except Exception:
            e = "?"
        print("  %-52s %s" % (os.path.basename(p), e))
    print("\n提醒：Secret 是运行时真相，盘上文件只是 exchange 的产物。两边不一致时以 Secret 为准。")


# ---------------------------------------------------------------- regress
def cmd_regress(args):
    """**用户面**回归：临时建一把受限 LiteLLM key，打完就删。

    为什么必须打到 LiteLLM 这层：网关绿只证明网关绿。用户真实走的是
    LiteLLM entry → svc → 网关，中间任何一段（entry 的 api_base、key 白名单、
    strict 模式）坏了，网关侧的回归一律看不见。

    为什么用临时 key 而不是 master key：master key 绕过白名单，拿它测等于
    不测白名单。这里同时打一个**白名单外**的模型做反向对照 —— 只有
    「该通的通 + 该拒的拒」两边都对，这把尺子才算没坏。
    """
    mk = base64.b64decode(kubectl_ns(
        LITELLM_NS, "get", "secret", LITELLM_SECRET,
        "-o", "jsonpath={.data.LITELLM_MASTER_KEY}")).decode().strip()
    if args.models:
        models = args.models.split(",")
    elif getattr(args, "all", False):
        models = discover_litellm_ag_models(mk)
        print("从 LiteLLM 现拉到 %d 条 ag-* entry（别写死清单，Google 一直改名）" % len(models))
    else:
        models = LITELLM_PROBE_MODELS
    # ⭐ 客户端可见的公开别名（同事 key 上真正打的名字）必须一起测。
    #    它和真实组名是**两条不同的路**：公开名要靠 per-key `aliases` 改写才落得到组上，
    #    只测真实组名 ⇒ 别名少写一半也全绿。2026-09-09 那 19 把 blocked key 就是
    #    「models 里有名字、aliases 里没有」⇒ 解封即 400，而只测组名的尺子看不见。
    models = models + [m for m in PUBLIC_ALIASES if m not in models]

    st, r = http_json(LITELLM + "/key/generate",
                      {"models": models, "aliases": dict(PUBLIC_ALIASES),
                       "key_alias": "tmp-ag-regress-%d" % random.randint(10**6, 10**7),
                       "duration": "20m", "max_budget": 1.0},
                      {"Authorization": "Bearer " + mk})
    if st != 200 or "key" not in r:
        sys.exit("临时 key 建不出来 (%s): %s" % (st, json.dumps(r)[:300]))
    tk = r["key"]
    print("临时受限 key 已建（20 分钟过期，结束会删）：models=%s" % models)

    try:
        bad = []
        for m in models:
            x = nonce()
            hs, hb = http_json(
                LITELLM + "/v1/chat/completions",
                {"model": m, "messages": [{"role": "user", "content": "Reply with exactly: " + x}],
                 "max_tokens": MAX_TOKENS},
                {"Authorization": "Bearer " + tk})
            hit = x in json.dumps(hb)
            print("  %s %-28s http=%s %s" % ("✅" if hit else "❌", m, hs,
                                             "" if hit else json.dumps(hb)[:200]))
            if not hit:
                bad.append(m)

        # 反向对照：白名单外的模型必须被拒。少了这一腿，"全绿"可能只是白名单没生效。
        ds, db = http_json(
            LITELLM + "/v1/chat/completions",
            {"model": LITELLM_DENY_MODEL, "messages": [{"role": "user", "content": "hi"}],
             "max_tokens": 8},
            {"Authorization": "Bearer " + tk})
        ok_deny = ds in (401, 403)
        print("  %s 反向对照 %-19s http=%s（期望 401/403）"
              % ("✅" if ok_deny else "❌", LITELLM_DENY_MODEL, ds))
        if not ok_deny:
            bad.append("反向对照没被拒——白名单可能根本没在拦")
    finally:
        ds2, _ = http_json(LITELLM + "/key/delete", {"keys": [tk]},
                           {"Authorization": "Bearer " + mk})
        print("临时 key 已删 (http=%s)" % ds2)

    if bad:
        sys.exit("\n❌ 用户面回归未通过: %s" % bad)
    print("\n✅ 用户面回归通过（%d 个模型 + 反向对照）。" % len(models))
    print("注意：这里绿**不代表每个号都健康** —— 池子会换号。判个体用 verify --only。")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("authurl", help="打印 OAuth 授权链接")

    e = sub.add_parser("exchange", help="用回调 URL/code 换 refresh_token 并实测账号")
    e.add_argument("--code", required=True, help="整条回调 URL，或纯 code")
    e.add_argument("--out", help="凭据落盘路径，默认 %s/antigravity-<email>.json" % MANIFEST_DIR)
    e.add_argument("--force", action="store_true", help="daily 实测没过也照样生成凭据")

    i = sub.add_parser("install", help="把凭据装进 Secret 并回归（可一次装多个）")
    i.add_argument("--creds", action="append", help="凭据文件；可重复给多次")
    i.add_argument("--creds-dir", help="目录下所有 antigravity-*.json 一次装完（100 个号走这条）")
    i.add_argument("--apply", action="store_true", help="不加就是 dry-run")

    v = sub.add_parser("verify", help="只跑回归，不改任何东西")
    v.add_argument("--only", metavar="EMAILS",
                   help="逗号分隔，逐号直打上游验证这些号（不再挪文件，不打断在途请求）")
    v.add_argument("--all", action="store_true",
                   help="网关侧扫**全部**模型各 1 发（默认只打 2 个代表模型 ×4）。"
                        "图像模型自动排除——它有独立小配额且不上报")

    sub.add_parser("list", help="只读打印池子现状（Secret 解码），不发任何请求")

    r = sub.add_parser("regress", help="用户面回归：临时受限 LiteLLM key + 反向对照，打完就删")
    r.add_argument("--models", help="逗号分隔，默认 %s" % ",".join(LITELLM_PROBE_MODELS))
    r.add_argument("--all", action="store_true",
                   help="向 LiteLLM 现拉全部 ag-*/claude-ag-* entry 一起测（别写死清单）。"
                        "无论加不加，公开别名 %s 都会被一并测到" % list(PUBLIC_ALIASES))

    args = ap.parse_args()
    {"authurl": cmd_authurl, "exchange": cmd_exchange,
     "install": cmd_install, "list": cmd_list, "regress": cmd_regress,
     "verify": lambda a: verify(a.only.split(",") if a.only else None,
                                all_models=a.all)}[args.cmd](args)


if __name__ == "__main__":
    main()
