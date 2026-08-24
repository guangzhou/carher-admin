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
  1. apply Deployment/Service zero-cursor-bpi-<N>(照 bpi 模板,只换 name/ZK_USER/seed 路径)
  2. [step3] /model/new 建 6 个**直连名** cursor-g-<N>-*(真身 slug,api_key 占位符必带)
  3. [step5] /model/new 把 6 个**池成员** deployment 挂进现有池别名 cursor-g-5.6-* / cursor-g-5.5
  4. [grant] /key/update 把 6 直连名 + 6 池别名并进目标 key(整表覆盖坑→读旧合并写回)
  5. [step6] --apply 时自动验收:临时 scoped key 打 6 直连名暗号 → 断言 200+回显 → 删 key

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
    只在 standby(10.68.13.225)上;lane 必须调度到该 node 才读得到 seed。模板已钉死。
  • live OAuth token 经 **ssh stdin** 传给 standby(绝不进命令行 argv/远端 shell 命令串),
    防 ps 泄漏(同 Step 0「sudo 密码泄漏」事故家族)。

用法:
  # 复用 WS 线活 token(推荐,前提 A):先 dry-run 看计划,再 --apply
  python3 clone_web_fc_lane_v2.py --acct 90 --key-alias cursor-liuguoxian04-5rub --live-from-ws
  python3 clone_web_fc_lane_v2.py --acct 90 --key-alias cursor-liuguoxian04-5rub --live-from-ws --apply
  # 全新号(前提 B):先人工手抓 seed,再:
  python3 clone_web_fc_lane_v2.py --acct 90 --key-alias cursor-liuguoxian04-5rub --apply

前置:本脚本在【198 控制面】跑(能 kubectl + ssh standby)。standby=10.68.13.225 存 hostPath。
"""
import argparse, base64, json, subprocess, sys, textwrap, time

NS = "litellm-product"
STANDBY = "10.68.13.225"
SSH = ["sshpass", "-p", "Hn8#mKLp3QxZ", "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20"]
KUBECONFIG_EXPORT = "export KUBECONFIG=/home/cltx/.kube/config; "

# 真身 slug 表(2026-08-24 model_slug 铁证;xhigh 已退役,不在此表)。
# (variant_key, 直连名后缀, 池别名, 真身 slug, reasoning_effort)
VARIANTS = [
    ("sol",      "5.6-sol",      "cursor-g-5.6-sol",      "openai/gpt-5-6",          None),
    ("sol-high", "5.6-sol-high", "cursor-g-5.6-sol-high", "openai/gpt-5-6",          "high"),
    ("luna",     "5.6-luna",     "cursor-g-5.6-luna",     "openai/gpt-5-6-t-mini",   None),
    ("pro",      "5.6-pro",      "cursor-g-5.6-pro",      "openai/gpt-5-6-pro",      None),
    ("instant",  "5.6-instant",  "cursor-g-5.6-instant",  "openai/gpt-5-6-instant",  None),
    ("v5.5",     "5.5",          "cursor-g-5.5",          "openai/gpt-5-5-thinking", None),
]

COMMON = {
    "use_in_pass_through": False, "use_litellm_proxy": False,
    "use_chat_completions_api": False, "use_xai_oauth": False,
    "merge_reasoning_content_in_choices": False,
    "api_key": "sk-zerokey-web-noop",   # 假绿①:占位符必带,否则真流量 401
    "weight": 1,
}

# bpi 模板要 cp 的 patch 文件(照 deploy/zero-cursor-bpi args 逐字节)
PATCH_CP = ("mkdir -p /app/temp /app/routes /app/core/chatgpt /app/config /app/lib/engine && "
    "cp /seed/users.json /app/temp/users.json && "
    "cp /patch/zerokey-serve-codex.js /app/zerokey-serve-codex.js && "
    "cp /patch/chatgpt.js /app/routes/chatgpt.js && cp /patch/raw.js /app/routes/raw.js && "
    "cp /patch/web-tools.js /app/routes/web-tools.js && cp /patch/cursor.js /app/routes/cursor.js && "
    "cp /patch/images.js /app/routes/images.js && cp /patch/responses.js /app/routes/responses.js && "
    "cp /patch/api.js /app/core/chatgpt/api.js && cp /patch/constants.js /app/config/constants.js && "
    "cp /patch/tool-defs.js /app/lib/engine/tool-defs.js && cp /patch/instructions.md /app/lib/engine/instructions.md && "
    "cp /patch/stream.js /app/lib/engine/stream.js && exec node /app/zerokey-serve-codex.js")


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
    A_out, _, _ = kubectl("get pod -l app=chatgpt-acct-%s -o jsonpath={.items[0].metadata.name}" % acct)
    A = A_out.strip()
    if not A:
        print("!! chatgpt-acct-%s pod 不存在 → 前提 A 不满足;走前提 B(手抓 seed),去掉 --live-from-ws" % acct)
        sys.exit(1)
    tok_out, _, _ = kubectl("exec %s -- sh -c 'cat /chatgpt-auth/auth.json'" % A)
    try:
        live = json.loads(tok_out)["access_token"]
    except Exception as e:
        print("!! 读 acct-%s /chatgpt-auth/auth.json 失败:%s" % (acct, e)); sys.exit(1)
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


DEPLOY_TMPL = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: zero-cursor-bpi-{n}
  namespace: {ns}
  labels: {{account: "{n}", app: zero-cursor-bpi-{n}, pool: zerokey-cursor}}
spec:
  replicas: 1
  strategy: {{type: Recreate}}
  selector: {{matchLabels: {{app: zero-cursor-bpi-{n}}}}}
  template:
    metadata:
      labels: {{account: "{n}", app: zero-cursor-bpi-{n}, pool: zerokey-cursor}}
    spec:
      containers:
      - name: zerokey
        image: docker.io/library/zerokey-codex:latest
        imagePullPolicy: IfNotPresent
        command: ["sh","-c"]
        args:
        - {args}
        env:
        - {{name: PORT, value: "8201"}}
        - {{name: ZK_USER, value: acct{n}}}
        - {{name: ZK_DEFAULT_MODEL, value: gpt-5-5}}
        - {{name: CODEX_TOKEN_DIR}}
        - {{name: ZK_INLINE_MAX, value: "125000"}}
        ports: [{{containerPort: 8201, protocol: TCP}}]
        readinessProbe:
          httpGet: {{path: /health, port: 8201, scheme: HTTP}}
          initialDelaySeconds: 5
          periodSeconds: 10
        resources:
          limits: {{cpu: 500m, memory: 256Mi}}
          requests: {{cpu: 50m, memory: 64Mi}}
        volumeMounts:
        - {{mountPath: /seed, name: seed, readOnly: true}}
        - {{mountPath: /app/temp, name: temp}}
        - {{mountPath: /patch, name: patch, readOnly: true}}
        - {{mountPath: /codex-tokens, name: codex-tokens, readOnly: true}}
      dnsConfig: {{nameservers: ["1.1.1.1","8.8.8.8"]}}
      dnsPolicy: None
      nodeName: aiyjy-litellm-standby
      volumes:
      - {{name: seed, hostPath: {{path: /Data/zerokey-sessions/zero-{n}, type: Directory}}}}
      - {{name: temp, emptyDir: {{}}}}
      - {{name: patch, configMap: {{name: zk-cursor-bpi-patch, defaultMode: 420}}}}
      - {{name: codex-tokens, hostPath: {{path: /Data/codex-tokens, type: Directory}}}}
---
apiVersion: v1
kind: Service
metadata: {{name: zero-cursor-bpi-{n}, namespace: {ns}, labels: {{pool: zerokey-cursor}}}}
spec:
  type: ClusterIP
  ports: [{{port: 8201, targetPort: 8201, protocol: TCP}}]
  selector: {{app: zero-cursor-bpi-{n}}}
"""


def apply_deploy(acct):
    y = DEPLOY_TMPL.format(n=acct, ns=NS, args=json.dumps(PATCH_CP))
    out, err, rc = kubectl("apply -f -", stdin=y)
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
        def post(path,payload):
            req=urllib.request.Request(BASE+path,data=json.dumps(payload).encode(),
                headers={"Authorization":"Bearer "+mk,"Content-Type":"application/json"})
            try:
                r=urllib.request.urlopen(req,timeout=30); return "OK"
            except urllib.error.HTTPError as e:
                b=e.read().decode()[:160]
                return "SKIP(exists)" if e.code in (400,409) and "already" in b.lower() else ("ERR %%d %%s"%%(e.code,b))
        ok=0
        for row in rows:
            r=post("/model/new",row)
            if r in ("OK","SKIP(exists)"): ok+=1
            print(row["model_info"]["id"],"=>",r)
        print("SUMMARY: %%d/%%d ok"%%(ok,len(rows)))
    ''' % payload_json)
    out, _ = proxy_py(script)
    print(out)


def register_direct(acct):
    _register(acct, "direct")


def register_pool(acct):
    _register(acct, "pool")


def grant_key(acct, key_alias):
    """把 6 直连名 + 6 池别名并进目标 key(读旧合并写回;整表覆盖坑)。"""
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
    print("!! 还差两步人工(脚本替不了):①proxy WA 日志 grep group=cursor-g 看 MISS→HIT 钉同线;"
          "②lane pod zero-cursor-bpi-%s grep CLONECHK%s 确认流量真落新线。" % (acct, run))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--acct", required=True, help="目标账号号,如 90")
    ap.add_argument("--key-alias", required=True, help="要授权的 key alias(DB 完整 alias)")
    ap.add_argument("--live-from-ws", action="store_true", help="用 chatgpt-acct-N WS PVC 活 token 刷 web seed(前提 A)")
    ap.add_argument("--apply", action="store_true", help="真执行(默认 dry-run)")
    a = ap.parse_args()
    direct = ["cursor-g-%s-%s" % (a.acct, d) for _, d, _, _, _ in VARIANTS]
    pool = [p for _, _, p, _, _ in VARIANTS]
    print("=== clone web lane v2 -> acct-%s, grant key=%s, apply=%s ===" % (a.acct, a.key_alias, a.apply))
    if not a.apply:
        print("[dry-run] 将执行(加 --apply 生效):")
        print("  0. %s刷 zero-%s web seed" % ("从 WS 活 token(前提 A)" if a.live_from_ws else "跳过(前提 B:需已人工手抓 seed)", a.acct))
        print("  1. apply deploy/svc zero-cursor-bpi-%s(钉 standby 单 node)" % a.acct)
        print("  2. [step3] 建 6 直连名: %s" % ", ".join(direct))
        print("  3. [step5] 挂 6 池成员进别名: %s" % ", ".join(pool))
        print("  4. [grant] 并 12 名进 key %s(读旧合并)" % a.key_alias)
        print("  5. [step6] 临时真 key 验收 6 直连名(200+回显;空回显=红旗见 xhigh 教训)")
        print("  slug 表(真身,无 xhigh): %s" % ", ".join("%s=%s%s" % (d, s, "/"+r if r else "") for _, d, _, s, r in VARIANTS))
        return
    if a.live_from_ws:
        print("--- step0: refresh web seed from WS live token ---"); refresh_seed_from_ws(a.acct)
    print("--- step1: apply deploy/svc ---"); apply_deploy(a.acct)
    print("--- step3: register 6 direct names (WITH api_key placeholder) ---"); register_direct(a.acct)
    print("--- step5: register 6 pool members ---"); register_pool(a.acct)
    print("--- grant: merge 12 names into key ---"); grant_key(a.acct, a.key_alias)
    print("--- step6: auto-accept via temp scoped key ---"); accept(a.acct)
    print("\n完成。回滚:/model/delete 12 个 zerokey-cursor-g-%s-* id;kubectl delete deploy,svc zero-cursor-bpi-%s。" % (a.acct, a.acct))


if __name__ == "__main__":
    main()
