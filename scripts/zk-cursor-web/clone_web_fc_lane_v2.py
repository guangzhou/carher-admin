#!/usr/bin/env python3
"""
clone_web_fc_lane_v2.py — 把 cursor 网页额度线克隆到一个新账号(cursor-g-* 命名era)。

与 v1(clone_web_fc_lane.py,terra 冻结era)的区别:
  - v1 注册 `cursor-web-fc-N-terra{,-high,-max}`,slug=虚构 `openai/gpt-5.6-terra`(静默降级)。
    该批名字现已冻结(保留当调试工具),v1 保留为历史参照,不再用于新号。
  - v2 注册 **cursor-g-* 命名**,slug=**真身**(gpt-5-6 / -t-mini / -pro / -instant / gpt-5-5-thinking),
    双层:运维直连名 `cursor-g-<N>-5.6-sol…`(带账号号,钉单线,调试用) + 用户池别名
    `cursor-g-5.6-sol…`(无数字,WA 跨线负载+容灾)。
  - **xhigh 档已退役**(2026-08-24 实测:`xhigh→web max` 对 gpt-5-6 产不出正文,第三个虚构档)。
    v2 只出 sol(standard)/sol-high(extended)两个 effort 档,其余变体单档。

它做的事(全部幂等,已存在的 (model_name, id) 自动跳过):
  0. 刷新 zero-<N> web seed 的 authorization(见 §登录态)
  1. apply Deployment/Service zero-cursor-bpi-<N>(**从活着的参照 lane 克隆**,见 §别写死模板)
  2. [step3] /model/new 建 6 个**直连名** cursor-g-<N>-*(真身 slug,api_key 占位符必带)
  3. [step5] /model/new 把 6 个**池成员** deployment 挂进现有池别名 cursor-g-5.6-* / cursor-g-5.5
  4. [grant] /key/update 把 6 直连名 + 6 池别名并进目标 key(整表覆盖坑→读旧合并写回)
  5. [step6] --apply 时自动验收:临时 scoped key 打 6 直连名暗号 → 断言 200+回显 → 删 key。
     **验收是入池闸门,但判据是相对的**:失败项再打池别名当对照组(那时新 lane 还没入池,
     池里只有老 lane),只有"老 lane 出得了字而新 lane 出不了"才拦;池范围内既有的坏档
     (如 2026-08-31 实测的 `cursor-g-5.6-pro`,101/82/81 一律 output_tokens=0)不拦,单独立案。

═══ 别写死模板(2026-08-31 事故未遂)═══
本文件原来内嵌一份 08-24 定稿的 DEPLOY_TMPL。到 08-31,lane 82 的 env 已从 5 个长到 25 个
(ZK_PROTO_V2/ZK_HANDSHAKE/ZK_TOOL_DIET/ZK_WRITE_DIALECT/ZK_EMPTY_RETRY/ZK_STRIP_*/
ZK_CONV_PERSIST…),多一个 convcache hostPath,args 多 cp 两个 mcp_*.js。
拿老模板建新 lane = **新 lane 静默跑另一套行为**,而池一致性门只比 responses.js 的 sha256,
env 漂移它看不见。现在 spec 一律 `--ref-lane` 从集群里的活 lane 克隆,只换四类身份字段
(deploy/svc name、labels、ZK_USER、seed/cache hostPath 路径),换完还做残留自检。
⚠️ 顺带查过:101 与 82 **本来就不一致**(101 有 ZK_CHAIN_SRV=1、无 proto2 那一批;82 反之),
克隆前先想清楚要照哪条。

用法:
  # 复用 WS 线活 token(推荐,前提 A):先 dry-run 看计划(会打印将要写的 env 全表),再 --apply
  python3 clone_web_fc_lane_v2.py --acct 90 --key-alias cursor-liuguoxian04-5rub --live-from-ws
  python3 clone_web_fc_lane_v2.py --acct 90 --key-alias cursor-liuguoxian04-5rub --live-from-ws --apply
  # 换参照 lane:--ref-lane 101
  # 全新号(前提 B):先人工手抓 seed,再去掉 --live-from-ws 直接 --apply

═══ 登录态(本脚本最容易踩、也是最大价值点)═══
zero-<N> web seed(/Data/zerokey-sessions/zero-N/users.json)里 parsedFetch.authorization
那个 bearer 是手抓的、会失效(token 里的 exp ≠ 上游还认它;唯一判据是真去 sentinel 握手)。
  • **前提 A(--live-from-ws 可用)**:该号同时是 chatgpt-acct-N WS 线成员时,其 PVC
    /chatgpt-auth/auth.json 有一份带 refresh_token、会自动续的 OAuth access_token(同一账号),
    web 端点 /backend-api/f/conversation 接受它 → --live-from-ws 灌进 seed,无需手抓。
  • **前提 B(全新号、非 WS 成员)**:没有 WS 线可借 token → **首次必须人工手抓 web seed**
    (浏览器抓 parsedFetch 落 /Data/zerokey-sessions/zero-N/users.json),本脚本不做手抓,
    只在 --live-from-ws 时刷新。手抓后不加 --live-from-ws 直接 --apply 即可。

═══ 部署拓扑前提 ═══
  • **全部 lane 钉 standby 单 node**(nodeName: aiyjy-litellm-standby):web seed 是 hostPath,
    只在 standby(10.68.13.225)上;lane 必须调度到该 node 才读得到 seed。参照 lane 已钉死,克隆照抄。
  • live OAuth token 经 **ssh stdin** 传给 standby(绝不进命令行 argv/远端 shell 命令串),
    防 ps 泄漏(同 Step 0「sudo 密码泄漏」事故家族)。

前置:本脚本在【198 控制面】跑(能 kubectl + ssh standby)。standby=10.68.13.225 存 hostPath。
"""
import argparse, base64, json, re, subprocess, sys, textwrap, time

NS = "litellm-product"
STANDBY = "10.68.13.225"
SSH = ["sshpass", "-p", "Hn8#mKLp3QxZ", "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20"]
KUBECONFIG_EXPORT = "export KUBECONFIG=/home/cltx/.kube/config; "

# 载体 slug 表(2026-08-24 Step 9 两轮修正:litellm 层必须注册**点分载体**——litellm 的
# chat→responses 桥只认点分 gpt-5.4+ 名(is_model_gpt_5_4_plus 横杠全 False),Cursor 真实
# 流量全走 /v1/chat/completions,不桥接就掉进 lane chatgpt.js 坏路崩。真身由 bpi CM raw.js
# 的 ALIASES 映射(gpt-5.6-sol→gpt-5-6 等,新线共用同一 CM,映射自动生效)。
# ⚠️ effort 必须钉在 deployment(桥第二道门 reasoning_effort is not None 不能依赖客户端:
# Cursor 模型 parameters 为空时不发 effort)。base 档钉 medium(lane 映射→web standard=默认档)。
# xhigh 已退役。
# (variant_key, 直连名后缀, 池别名, litellm 载体 slug, reasoning_effort)
VARIANTS = [
    ("sol",      "5.6-sol",      "cursor-g-5.6-sol",      "openai/gpt-5.6-sol",      "medium"),
    ("sol-high", "5.6-sol-high", "cursor-g-5.6-sol-high", "openai/gpt-5.6-sol",      "high"),
    ("luna",     "5.6-luna",     "cursor-g-5.6-luna",     "openai/gpt-5.6-luna",     "medium"),
    ("pro",      "5.6-pro",      "cursor-g-5.6-pro",      "openai/gpt-5.6-pro",      "medium"),
    ("instant",  "5.6-instant",  "cursor-g-5.6-instant",  "openai/gpt-5.6-instant",  "medium"),
    ("v5.5",     "5.5",          "cursor-g-5.5",          "openai/gpt-5.5-thinking", "medium"),
]

COMMON = {
    "use_in_pass_through": False, "use_litellm_proxy": False,
    "use_chat_completions_api": False, "use_xai_oauth": False,
    "merge_reasoning_content_in_choices": False,
    "api_key": "sk-zerokey-web-noop",   # 假绿①:占位符必带,否则真流量 401
    "weight": 1,
}

# (原来这里有一份写死的 PATCH_CP —— 那串 cp 命令 08-31 已在 lane 82 上长出两条
#  mcp_*.js,写死必陈旧。现在 args 直接从活参照 lane 抄,见 build_lane_spec。)


def kubectl(argstr, stdin=None):
    cmd = SSH + ["cltx@10.68.13.198", KUBECONFIG_EXPORT + "kubectl -n %s %s" % (NS, argstr)]
    if stdin is not None:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    else:
        r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout, r.stderr, r.returncode


def proxy_pod():
    out, _, _ = kubectl("get pod -l app=litellm-proxy -o jsonpath={.items[0].metadata.name}")
    return out.strip()


def proxy_py(script):
    """在 litellm-proxy pod 里跑一段 python(base64 传,躲引号剥离)。"""
    b = base64.b64encode(script.encode()).decode()
    P = proxy_pod()
    out, err, rc = kubectl("exec %s -- sh -c 'printf %%s %s | base64 -d | python3 -'" % (P, b))
    return "\n".join(l for l in out.splitlines() if "sitecustomize" not in l), err


def api_base_of(acct):
    return "http://zero-cursor-bpi-%s.%s.svc.cluster.local:8201/v1" % (acct, NS)


# ── 登录态:live token 经 ssh stdin 传,绝不进 argv/命令串 ──
def refresh_seed_from_ws(acct):
    # ⚠️ 必须钉 Running:acct pod 常留着几个 Failed 的旧 ReplicaSet 残骸(acct-85 实测 2 Failed
    # + 1 Running),取 items[0] 会挑到 Failed 那个 → exec 出空串 → 报成"auth.json 读不出",
    # 看起来像"token 坏了"其实是挑错了 pod。
    A_out, _, _ = kubectl("get pod -l app=chatgpt-acct-%s --field-selector=status.phase=Running "
                          "-o jsonpath={.items[0].metadata.name}" % acct)
    A = A_out.strip()
    if not A:
        print("!! chatgpt-acct-%s 没有 Running 的 pod → 前提 A 不满足;"
              "走前提 B(手抓 seed),去掉 --live-from-ws" % acct)
        sys.exit(1)
    tok_out, tok_err, _ = kubectl("exec %s -- sh -c 'cat /chatgpt-auth/auth.json'" % A)
    if not tok_out.strip():
        print("!! acct-%s pod %s 上 /chatgpt-auth/auth.json 读出空(不是 token 坏,是没读到):%s"
              % (acct, A, (tok_err or "")[:200])); sys.exit(1)
    try:
        live = json.loads(tok_out)["access_token"]
    except Exception as e:
        print("!! 解析 acct-%s /chatgpt-auth/auth.json 失败:%s" % (acct, e)); sys.exit(1)
    # python -c 内联脚本(无秘密);token 只经 ssh stdin 进 remote python 的 sys.stdin
    py = (
        "import json,sys;"
        "live=sys.stdin.read().strip();"
        'p="/Data/zerokey-sessions/zero-%s/users.json";'
        "d=json.load(open(p));"
        'd["chatgpt"]["acct%s"]["parsedFetch"]["headers"]["authorization"]="Bearer "+live;'
        'json.dump(d,open(p,"w"),ensure_ascii=False,indent=2);'
        'print("seed authorization refreshed, head:",live[:12])'
    ) % (acct, acct)
    remote_cmd = (
        "set -e; "
        'SEED=/Data/zerokey-sessions/zero-%s/users.json; '
        'cp "$SEED" /Data/backups/zero-%s-users-pre-livetoken-$(date +%%Y%%m%%d-%%H%%M%%S).json 2>/dev/null || true; '
        "python3 -c '%s'"
    ) % (acct, acct, py)
    r = subprocess.run(SSH + ["cltx@%s" % STANDBY, remote_cmd], input=live, capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr[:300])


# ── 新 lane 的 spec 从**活着的参照 lane** 拷,不再用冻结模板 ──
# 2026-08-31 事故未遂:本文件原来内嵌一份 08-24 写死的 DEPLOY_TMPL(5 个 env),
# 而那时起 lane 82 已长到 25 个 env(ZK_PROTO_V2/ZK_HANDSHAKE/ZK_TOOL_DIET/
# ZK_WRITE_DIALECT/ZK_EMPTY_RETRY/ZK_STRIP_*/ZK_CONV_PERSIST…)、多一个 convcache
# hostPath、args 多 cp 两个 mcp_*.js。照模板建出来的新 lane 会**静默跑另一套行为**,
# 而池一致性门只比 responses.js 的 sha256,看不见 env 漂移。
# 教训同 [[feedback_manifest_prod_drift_apply_overwrites]]:仓库里的 manifest 会陈旧,
# 真相在集群里。所以这里改成读活参照 lane 的 spec,只换身份字段。
REF_DEPLOY_NAME = {"101": "zero-cursor-bpi"}   # 101 那条历史上没带号后缀


def _ref_deploy_name(ref):
    return REF_DEPLOY_NAME.get(str(ref), "zero-cursor-bpi-%s" % ref)


def build_lane_spec(ref, acct):
    """把参照 lane 的活 spec 克隆成新 lane。只改身份字段,其余(env/args/volume/资源)逐字段照抄。"""
    refname = _ref_deploy_name(ref)
    out, err, rc = kubectl("get deploy %s -o json" % refname)
    if rc != 0 or not out.strip():
        print("!! 读参照 lane %s 失败: %s" % (refname, (err or "")[:200])); sys.exit(1)
    d = json.loads(out)
    newname = "zero-cursor-bpi-%s" % acct
    labels = {"account": str(acct), "app": newname, "pool": "zerokey-cursor"}

    d.pop("status", None)
    d["metadata"] = {"name": newname, "namespace": NS, "labels": labels}
    sp = d["spec"]
    for k in ("revisionHistoryLimit", "progressDeadlineSeconds"):
        sp.pop(k, None)
    sp["replicas"] = 1
    sp["selector"] = {"matchLabels": {"app": newname}}
    tmpl = sp["template"]
    tmpl["metadata"] = {"labels": labels}
    tspec = tmpl["spec"]
    c = tspec["containers"][0]

    seen_user = None
    for e in c.get("env", []):
        if e.get("name") == "ZK_USER":
            seen_user = e.get("value"); e["value"] = "acct%s" % acct
    if seen_user != "acct%s" % ref:
        print("!! 参照 lane %s 的 ZK_USER=%r,与 --ref-lane %s 对不上,拒绝" % (refname, seen_user, ref))
        sys.exit(1)

    for v in tspec.get("volumes", []):
        hp = v.get("hostPath") or {}
        p = hp.get("path", "")
        m = re.match(r"^(/Data/zerokey-sessions/zero-)[^/]+?(-cache)?$", p)
        if m:
            hp["path"] = "%s%s%s" % (m.group(1), acct, m.group(2) or "")

    # 身份残留自检:参照 lane 的号不许漏在任何字段里(端口 8201 含 "82",所以只查带边界的形态)
    blob = json.dumps(d)
    for pat in ("zero-cursor-bpi-%s" % ref, "acct%s" % ref, "zero-%s" % ref, _ref_deploy_name(ref)):
        if pat in blob:
            print("!! 克隆后仍残留参照身份 %r,拒绝 apply(下面是残留上下文)" % pat)
            i = blob.index(pat); print("   …%s…" % blob[max(0, i - 80):i + 80]); sys.exit(1)

    svc = {"apiVersion": "v1", "kind": "Service",
           "metadata": {"name": newname, "namespace": NS, "labels": {"pool": "zerokey-cursor"}},
           "spec": {"type": "ClusterIP", "selector": {"app": newname},
                    "ports": [{"port": 8201, "targetPort": 8201, "protocol": "TCP"}]}}
    return d, svc


def describe_lane_spec(d):
    c = d["spec"]["template"]["spec"]["containers"][0]
    env = [(e["name"], e.get("value")) for e in c.get("env", [])]
    vols = [(v["name"], (v.get("hostPath") or {}).get("path") or list(v.keys())[-1])
            for v in d["spec"]["template"]["spec"].get("volumes", [])]
    return env, vols


def apply_deploy(acct, ref):
    d, svc = build_lane_spec(ref, acct)
    for obj in (d, svc):
        out, err, rc = kubectl("apply -f -", stdin=json.dumps(obj))
        print(out.strip() or err[:300])


def _register(acct, mode):
    """mode='direct' 建 cursor-g-<N>-* 直连名;mode='pool' 挂进 cursor-g-* 池别名。幂等跳过。"""
    ab = api_base_of(acct)
    rows = []
    for vkey, dsuffix, pool_name, slug, reff in VARIANTS:
        if mode == "direct":
            name = "cursor-g-%s-%s" % (acct, dsuffix)
            mid = "zerokey-cursor-g-%s-direct-%s" % (acct, vkey)
        else:
            name = pool_name
            mid = "zerokey-cursor-g-%s-%s" % (acct, vkey)
        lp = dict(COMMON, model=slug, api_base=ab)
        if reff:
            lp["reasoning_effort"] = reff
        rows.append({"model_name": name, "litellm_params": lp,
                     "model_info": {"id": mid, "mode": "chat"}})
    payload_json = json.dumps(rows)
    script = textwrap.dedent('''
        import os,json,urllib.request
        mk=os.environ["LITELLM_MASTER_KEY"]; BASE="http://localhost:4000"
        rows=json.loads(%r)
        def get(path):
            req=urllib.request.Request(BASE+path,headers={"Authorization":"Bearer "+mk})
            return json.loads(urllib.request.urlopen(req,timeout=30).read().decode())
        def post(path,payload):
            req=urllib.request.Request(BASE+path,data=json.dumps(payload).encode(),
                headers={"Authorization":"Bearer "+mk,"Content-Type":"application/json"})
            try:
                r=urllib.request.urlopen(req,timeout=30); return "OK"
            except urllib.error.HTTPError as e:
                b=e.read().decode()[:160]
                return "SKIP(exists)" if e.code in (400,409) and "already" in b.lower() else ("ERR %%d %%s"%%(e.code,b))
        # 幂等靠**先查已有 id**,不靠猜错误文案:重复注册时 /model/new 返的是
        # 500 "Failed to add model to db"(不含 already),按文案判会当成真错误报 6 个 ERR。
        # 只取 model_info.id —— litellm_params 里是加密凭据 blob,不拉。
        try:
            have={ (m.get("model_info") or {}).get("id") for m in get("/model/info").get("data",[]) }
            have.discard(None)
        except Exception as e:
            have=set(); print("WARN: /model/info 预查失败(%%s)-> 退回按错误文案判"%%e)
        ok=0
        for row in rows:
            mid=row["model_info"]["id"]
            r="SKIP(exists)" if mid in have else post("/model/new",row)
            if r in ("OK","SKIP(exists)"): ok+=1
            print(mid,"=>",r)
        print("SUMMARY: %%d/%%d ok"%%(ok,len(rows)))
    ''' % payload_json)
    out, _ = proxy_py(script)
    print(out)


def register_direct(acct):
    _register(acct, "direct")


def register_pool(acct):
    _register(acct, "pool")


def grant_key(acct, key_alias, names=None):
    """把指定模型名并进目标 key(读旧合并写回;/key/update 是整表覆盖,不合并必丢名)。
    names 省略时 = 6 直连名 + 6 池别名。"""
    if names is None:
        names = []
        for vkey, dsuffix, pool_name, slug, reff in VARIANTS:
            names.append("cursor-g-%s-%s" % (acct, dsuffix))
            names.append(pool_name)
    sql = ("SELECT token FROM \"LiteLLM_VerificationToken\" WHERE key_alias='%s';" % key_alias)
    b = base64.b64encode(sql.encode()).decode()
    out, _, _ = kubectl("exec -i litellm-db-0 -- sh -c "
        "'echo %s | base64 -d | psql -U $POSTGRES_USER -d $POSTGRES_DB -t -A -f -'" % b)
    tok = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not tok:
        print("!! key_alias %s 在 DB 查不到 token" % key_alias); return
    add_json = json.dumps(names)
    script = textwrap.dedent('''
        import os,json,urllib.request
        mk=os.environ["LITELLM_MASTER_KEY"]; BASE="http://localhost:4000"; tok="%s"
        add=json.loads(%r)
        info=json.load(urllib.request.urlopen(urllib.request.Request(
            BASE+"/key/info?key="+tok,headers={"Authorization":"Bearer "+mk}),timeout=30))["info"]
        cur=info.get("models") or []
        newm=sorted(set(cur)|set(add))
        r=urllib.request.Request(BASE+"/key/update",data=json.dumps({"key":tok,"models":newm}).encode(),
            headers={"Authorization":"Bearer "+mk,"Content-Type":"application/json"})
        try:
            resp=urllib.request.urlopen(r,timeout=30)
            print("KEY UPDATE",resp.status,"before=",len(cur),"after=",len(newm),
                  "added=",[m for m in add if m not in cur])
        except urllib.error.HTTPError as e: print("KEY UPDATE ERR",e.code,e.read().decode()[:200])
    ''' % (tok, add_json))
    out, _ = proxy_py(script)
    print(out)


def accept(acct):
    """自动验收:临时 scoped key 打 6 直连名暗号 → 断言 200+回显 → 删 key。禁 master(假绿③)。
    注:任一档回显为空是红旗(参 xhigh 退役教训)——直连名全是 standard/extended,均应出字。"""
    names = ["cursor-g-%s-%s" % (acct, d) for _, d, _, _, _ in VARIANTS]
    run = time.strftime("%H%M%S")
    names_json = json.dumps(names)
    script = textwrap.dedent('''
        import os,json,urllib.request
        mk=os.environ["LITELLM_MASTER_KEY"]; BASE="http://localhost:4000"; run="%s"
        names=json.loads(%r)
        def post(path,payload,key=None):
            req=urllib.request.Request(BASE+path,data=json.dumps(payload).encode(),
                headers={"Authorization":"Bearer "+(key or mk),"Content-Type":"application/json"})
            try:
                r=urllib.request.urlopen(req,timeout=180); return r.status,r.read().decode("utf8","replace")
            except urllib.error.HTTPError as e: return e.code,e.read().decode("utf8","replace")
        def outtext(raw):
            try:
                d=json.loads(raw); t=[]
                for o in d.get("output",[]):
                    for c in (o.get("content") or []):
                        if c.get("type") in ("output_text","text"): t.append(c.get("text",""))
                return " ".join(t)
            except Exception as ex: return "PARSE_ERR:"+str(ex)
        st,b=post("/key/generate",{"models":names,"max_budget":2,"key_alias":"tmp-clonechk-"+run,"duration":"1h"})
        key=json.loads(b)["key"]
        passed=0
        for i,n in enumerate(names):
            mk2="CLONECHK"+run+"-"+str(i)
            st,raw=post("/v1/responses",{"model":n,"input":"Reply with exactly this token and nothing else: "+mk2,"stream":False},key=key)
            ot=outtext(raw); ok=(st==200 and mk2 in ot); passed+=ok
            print("ACCEPT",n,"st="+str(st),"echo="+str(ok),"out="+repr(ot)[:60])
        dr=post("/key/delete",{"keys":[key]})
        print("tmpkey deleted",dr[0])
        print("RESULT: %%d/%%d PASS"%%(passed,len(names)))
    ''' % (run, names_json))
    out, _ = proxy_py(script)
    print(out)
    m = re.search(r"RESULT: (\d+)/(\d+) PASS", out or "")
    res = (int(m.group(1)), int(m.group(2))) if m else (0, len(names))
    failed = [ln.split()[1] for ln in (out or "").splitlines()
              if ln.startswith("ACCEPT ") and "echo=False" in ln]
    print("!! 还差两步人工(脚本替不了):①proxy WA 日志 grep group=cursor-g 看 MISS→HIT 钉同线;"
          "②lane pod zero-cursor-bpi-%s grep CLONECHK%s 确认流量真落新线。" % (acct, run))
    return res[0], res[1], failed


def pool_control(pool_names, shots=2):
    """对照组:同样的暗号打**池别名**。此刻新 lane 还没入池,所以池里只有老 lane
    → 这是天然对照组,回答的是"这个档在老 lane 上本来就出不了字吗"。

    为什么需要它(2026-08-31 acct-81 实证):验收拿绝对分数当闸门是错的。
    `cursor-g-5.6-pro` 在 101/82/81 三条线上一律 output_tokens=0(实测 2/2、2/2、3/3),
    是**池范围内既有的坏档**(同 xhigh 退役形态)。用绝对分数当闸,新 lane 会被一个
    跟它无关的老毛病永久挡在门外,而真正该做的是把坏档单独立案。
    **新 lane 的及格线 = 不比在池的老 lane 差**,不是"绝对全绿"。
    见 [[feedback_control_group_must_not_come_from_suspect_metric]]。"""
    run = time.strftime("%H%M%S")
    script = textwrap.dedent('''
        import os,json,urllib.request
        mk=os.environ["LITELLM_MASTER_KEY"]; BASE="http://localhost:4000"; run="%s"
        names=json.loads(%r); shots=%d
        def post(path,payload,key=None):
            req=urllib.request.Request(BASE+path,data=json.dumps(payload).encode(),
                headers={"Authorization":"Bearer "+(key or mk),"Content-Type":"application/json"})
            try:
                r=urllib.request.urlopen(req,timeout=180); return r.status,r.read().decode("utf8","replace")
            except urllib.error.HTTPError as e: return e.code,e.read().decode("utf8","replace")
        for n in names:
            anyok=False
            for i in range(shots):
                # 每发换 key:WA 是 key 级黏性,复用同一 key 只会考到同一条老 lane
                st,b=post("/key/generate",{"models":[n],"max_budget":2,"key_alias":"tmp-ctl-%%s-%%d"%%(run,i),"duration":"1h"})
                key=json.loads(b)["key"]
                tk="CTL"+run+"-"+str(i)
                st,raw=post("/v1/responses",{"model":n,"input":"Reply with exactly this token and nothing else: "+tk,"stream":False},key=key)
                t=[]
                try:
                    d=json.loads(raw)
                    for o in d.get("output",[]):
                        for c in (o.get("content") or []):
                            if c.get("type") in ("output_text","text"): t.append(c.get("text",""))
                except Exception: pass
                anyok = anyok or (st==200 and tk in " ".join(t))
                post("/key/delete",{"keys":[key]})
            print("CONTROL",n,"incumbent_ok="+str(anyok))
    ''' % (run, json.dumps(pool_names), shots))
    out, _ = proxy_py(script)
    print(out)
    return {ln.split()[1]: ("incumbent_ok=True" in ln)
            for ln in (out or "").splitlines() if ln.startswith("CONTROL ")}


def wait_models_propagated(names, timeout=240):
    """等**每一个** proxy 副本都认得这些 model_name 再验收。
    /model/new 只落在被打到的那个副本 + DB,其余副本靠轮询 DB 追(实测 20–37s)。
    不等就验收 = 打到没追上的副本 → 400 Invalid model name → 假红,还会把好 lane 挡在池外。
    (同 failover_drill.py 的 wait_deps 教训。)"""
    out, _, _ = kubectl("get pod -l app=litellm-proxy -o jsonpath={.items[*].metadata.name}")
    pods = out.split()
    if not pods:
        print("!! 查不到 litellm-proxy pod,跳过传播等待"); return False
    want_json = json.dumps(sorted(names))
    t0 = time.time()
    while True:
        lagging = []
        for p in pods:
            b = base64.b64encode(textwrap.dedent('''
                import os,json,urllib.request
                mk=os.environ["LITELLM_MASTER_KEY"]
                want=set(json.loads(%r))
                r=urllib.request.Request("http://localhost:4000/model/info",
                    headers={"Authorization":"Bearer "+mk})
                have={m["model_name"] for m in json.load(urllib.request.urlopen(r,timeout=20))["data"]}
                print("MISSING="+",".join(sorted(want-have)))
            ''' % want_json).encode()).decode()
            o, _, _ = kubectl("exec %s -- sh -c 'printf %%s %s | base64 -d | python3 -'" % (p, b))
            miss = ""
            for ln in (o or "").splitlines():
                if ln.startswith("MISSING="):
                    miss = ln[len("MISSING="):].strip()
            if miss:
                lagging.append("%s(%s)" % (p[-5:], miss.count(",") + 1))
        if not lagging:
            print("   传播完成:%d 个副本全部认得 %d 个新名(%.0fs)" % (len(pods), len(names), time.time() - t0))
            return True
        if time.time() - t0 > timeout:
            print("   !! 传播超时 %ds,仍落后:%s —— 验收结果不可信,当红处理" % (timeout, ", ".join(lagging)))
            return False
        print("   等副本追 DB… 落后:%s" % ", ".join(lagging))
        time.sleep(8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--acct", required=True, help="目标账号号,如 90")
    ap.add_argument("--key-alias", required=True, help="要授权的 key alias(DB 完整 alias)")
    ap.add_argument("--live-from-ws", action="store_true", help="用 chatgpt-acct-N WS PVC 活 token 刷 web seed(前提 A)")
    ap.add_argument("--ref-lane", default="82",
                    help="拿哪条**活着的** lane 当克隆参照(默认 82)。不再用写死模板——"
                         "池里各 lane 的 env 会各自长,写死必陈旧,见文件头注释。")
    ap.add_argument("--apply", action="store_true", help="真执行(默认 dry-run)")
    a = ap.parse_args()
    direct = ["cursor-g-%s-%s" % (a.acct, d) for _, d, _, _, _ in VARIANTS]
    pool = [p for _, _, p, _, _ in VARIANTS]
    print("=== clone web lane v2 -> acct-%s (ref lane=%s), grant key=%s, apply=%s ===" % (
        a.acct, a.ref_lane, a.key_alias, a.apply))
    if not a.apply:
        d, _svc = build_lane_spec(a.ref_lane, a.acct)
        env, vols = describe_lane_spec(d)
        print("[dry-run] 将执行(加 --apply 生效):")
        print("  0. %s刷 zero-%s web seed" % ("从 WS 活 token(前提 A)" if a.live_from_ws else "跳过(前提 B:需已人工手抓 seed)", a.acct))
        print("  1. apply deploy/svc zero-cursor-bpi-%s —— 从活 lane %s 逐字段克隆,只换身份字段:" % (a.acct, a.ref_lane))
        print("     env(%d 个,照抄参照 lane):" % len(env))
        for n, v in env:
            print("       %-24s = %s%s" % (n, v, "   <-- 身份字段已换" if n == "ZK_USER" else ""))
        print("     volumes: %s" % ", ".join("%s->%s" % (n, p) for n, p in vols))
        print("  2. [step3] 建 6 直连名: %s" % ", ".join(direct))
        print("  3. [grant] 6 直连名并进 key %s(读旧合并)" % a.key_alias)
        print("  4. [step6 = 池闸门] 临时真 key 验收 6 直连名(200+回显);失败项再打池别名当对照组——\n     只有\"老 lane 能出字而新 lane 不能\"才拦,池范围内的既有坏档不拦(单独立案)")
        print("  5. **验收全过才**[step5] 挂 6 池成员进别名: %s" % ", ".join(pool))
        print("     验收不过 → 新 lane 建了但不进池,用户面零影响,退出码 1")
        print("  slug 表(真身,无 xhigh): %s" % ", ".join("%s=%s%s" % (d, s, "/"+r if r else "") for _, d, _, s, r in VARIANTS))
        return
    if a.live_from_ws:
        print("--- step0: refresh web seed from WS live token ---"); refresh_seed_from_ws(a.acct)
    print("--- step1: apply deploy/svc (cloned from live lane %s) ---" % a.ref_lane); apply_deploy(a.acct, a.ref_lane)
    print("--- step3: register 6 direct names (WITH api_key placeholder) ---"); register_direct(a.acct)
    print("--- grant: merge 6 direct names into key ---")
    grant_key(a.acct, a.key_alias, direct)
    # ⚠️ 顺序是承重的:**先验收直连名,过了才让新 lane 进用户面的池**。
    # 反过来(先挂池后验收)= 一条没验过的 lane 直接接用户流量;失败虽有同发 failover 兜底
    # (见 project_cursor_pool_inrequest_failover_verified_2026_08_31),但那是兜底不是许可证。
    print("--- step6: auto-accept via temp scoped key (POOL GATE) ---")
    print("   先等 6 个直连名传播到所有 proxy 副本…")
    if not wait_models_propagated(direct):
        print("!! 传播没等到,拒绝在此基础上下验收结论(假红会把好 lane 挡在池外)。"); sys.exit(1)
    print("   等 lane pod ready…")
    kubectl("rollout status deploy/zero-cursor-bpi-%s --timeout=180s" % a.acct)
    passed, total, failed = accept(a.acct)
    if failed:
        # 绝对分数不是闸门。先拿池别名当天然对照组,分清"这条新 lane 坏"和"这个档本来就坏"。
        d2p = {"cursor-g-%s-%s" % (a.acct, d): p for _, d, p, _, _ in VARIANTS}
        ctl_names = [d2p[n] for n in failed if n in d2p]
        print("--- 对照组:同样的档打池别名(此刻池里只有老 lane)---")
        ctl = pool_control(ctl_names)
        lane_specific = [n for n in failed if ctl.get(d2p.get(n, ""), False)]
        preexisting = [n for n in failed if n not in lane_specific]
        if preexisting:
            print("   既有坏档(老 lane 上也出不了字,与本次克隆无关,需单独立案):%s"
                  % ", ".join(sorted(set(d2p[n] for n in preexisting if n in d2p))))
        if lane_specific:
            print("\n!! 新 lane 专属失败(老 lane 能出字、它不能):%s → **不挂池**。" % ", ".join(lane_specific))
            print("   新 lane 已建但只有直连名 cursor-g-%s-*,用户面零影响。" % a.acct)
            print("   排查完重跑本脚本(幂等),或回滚:kubectl delete deploy,svc zero-cursor-bpi-%s + "
                  "/model/delete 6 个 zerokey-cursor-g-%s-direct-*。" % (a.acct, a.acct))
            sys.exit(1)
        print("   → 失败项全部是既有坏档,新 lane 不比老 lane 差,放行入池。")
    print("--- step5: register 6 pool members (验收已过) ---"); register_pool(a.acct)
    print("--- grant: merge 6 pool aliases into key ---"); grant_key(a.acct, a.key_alias, pool)
    print("\n完成。收尾:跑 pool_consistency.py 确认新 lane 代码与 CM 一致。")
    print("回滚:/model/delete 12 个 zerokey-cursor-g-%s-* id;kubectl delete deploy,svc zero-cursor-bpi-%s。" % (a.acct, a.acct))


if __name__ == "__main__":
    main()
