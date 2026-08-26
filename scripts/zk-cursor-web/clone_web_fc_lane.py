#!/usr/bin/env python3
"""
clone_web_fc_lane.py — 把 cursor-web-fc 网页额度线克隆到一个新账号。

背景：网页额度线（Cursor /v1/responses → LiteLLM → zero-cursor-bpi:8201 → chatgpt.com
/backend-api/f/conversation → 网页订阅额度）原本只有 acct101 一条（deploy zero-cursor-bpi）。
本脚本把它逐字节克隆到另一个 acct-N，产出一条独立的 zero-cursor-bpi-<N> 线 + 三档模型
+ 授权给指定 key。2026-08-24 用 acct-82 实战跑通。

它做四件事（全部幂等，可重复跑）：
  1. 用【acct-N 的活登录态】刷新 zero-<N> web seed 的 authorization 头（关键见 §登录态）
  2. apply Deployment/Service zero-cursor-bpi-<N>（照 bpi 模板，只换 name/ZK_USER/seed 路径）
  3. /model/new 建三档 cursor-web-fc-<N>-terra{,-high,-max}（**必带 api_key 占位符**）
  4. /key/update 把三个模型追加到目标 key 的 models

═══ 登录态（本脚本最容易踩、也是最大价值点）═══
zero-<N> web seed（/Data/zerokey-sessions/zero-N/users.json）里 parsedFetch.authorization
那个 bearer 是**手抓的、会失效**（token 里的 exp ≠ 上游还认它；唯一判据是真去 sentinel 握手）。
若该号同时是 chatgpt-acct-N WS 线成员，它的 PVC /chatgpt-auth/auth.json 里有一份
**带 refresh_token + offline_access、会自动续**的 OAuth access_token（同一个账号）——
把这份活 token 灌进 web seed 的 authorization 头即可，无需重新手抓。--live-from-ws 即走这条路。

用法：
  # 复用 WS 线的活 token（推荐，前提：acct-N 是 WS 线成员）
  python3 clone_web_fc_lane.py --acct 82 --key-alias cursor-liuguoxian04-5rub --live-from-ws --apply
  # 仅演练（默认 dry-run，不写任何东西）
  python3 clone_web_fc_lane.py --acct 82 --key-alias cursor-liuguoxian04-5rub --live-from-ws

前置：本脚本在【198 控制面】跑（能 kubectl + ssh standby）。standby=10.68.13.225 存 hostPath。
"""
import argparse, base64, json, subprocess, sys, textwrap

NS = "litellm-product"
STANDBY = "10.68.13.225"
SSH = ["sshpass", "-p", "Hn8#mKLp3QxZ", "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20"]
KUBECONFIG_EXPORT = "export KUBECONFIG=/home/cltx/.kube/config; "
# bpi 模板要 cp 的 13 个 patch 文件（照 deploy/zero-cursor-bpi args 逐字节）
PATCH_CP = ("mkdir -p /app/temp /app/routes /app/core/chatgpt /app/config /app/lib/engine && "
    "cp /seed/users.json /app/temp/users.json && "
    "cp /patch/zerokey-serve-codex.js /app/zerokey-serve-codex.js && "
    "cp /patch/chatgpt.js /app/routes/chatgpt.js && cp /patch/raw.js /app/routes/raw.js && "
    "cp /patch/web-tools.js /app/routes/web-tools.js && cp /patch/cursor.js /app/routes/cursor.js && "
    "cp /patch/images.js /app/routes/images.js && cp /patch/responses.js /app/routes/responses.js && "
    "cp /patch/api.js /app/core/chatgpt/api.js && cp /patch/constants.js /app/config/constants.js && "
    "cp /patch/tool-defs.js /app/lib/engine/tool-defs.js && cp /patch/instructions.md /app/lib/engine/instructions.md && "
    "cp /patch/stream.js /app/lib/engine/stream.js && exec node /app/zerokey-serve-codex.js")


def sh(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print("CMD FAILED:", " ".join(cmd[:3]), "...\n", r.stderr[:400], file=sys.stderr)
        sys.exit(1)
    return r.stdout


def kubectl(argstr, stdin=None):
    """kubectl over ssh 到 198。argstr 是 kubectl 之后的部分。"""
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
    """在 litellm-proxy pod 里跑一段 python（base64 传，躲引号剥离）。"""
    b = base64.b64encode(script.encode()).decode()
    P = proxy_pod()
    out, err, rc = kubectl("exec %s -- sh -c 'printf %%s %s | base64 -d | python3 -'" % (P, b))
    return "\n".join(l for l in out.splitlines() if "sitecustomize" not in l), err


def refresh_seed_from_ws(acct):
    """把 chatgpt-acct-N PVC 里的活 access_token 灌进 zero-N web seed 的 authorization 头。"""
    A_out, _, _ = kubectl("get pod -l app=chatgpt-acct-%s -o jsonpath={.items[0].metadata.name}" % acct)
    A = A_out.strip()
    if not A:
        print("!! chatgpt-acct-%s pod 不存在，无法 --live-from-ws；请手抓 web seed" % acct); sys.exit(1)
    tok_out, _, _ = kubectl("exec %s -- sh -c 'cat /chatgpt-auth/auth.json'" % A)
    try:
        live = json.loads(tok_out)["access_token"]
    except Exception as e:
        print("!! 读 acct-%s /chatgpt-auth/auth.json 失败：%s" % (acct, e)); sys.exit(1)
    # 到 standby 改 seed（带备份）
    remote = textwrap.dedent("""
        set -e
        SEED=/Data/zerokey-sessions/zero-%s/users.json
        cp $SEED /Data/backups/zero-%s-users-pre-livetoken-$(date +%%Y%%m%%d-%%H%%M%%S).json 2>/dev/null || true
        LIVE='%s' python3 - <<'PYEOF'
        import json,os
        p="/Data/zerokey-sessions/zero-%s/users.json"
        d=json.load(open(p)); uv=d["chatgpt"]["acct%s"]
        uv["parsedFetch"]["headers"]["authorization"]="Bearer "+os.environ["LIVE"]
        json.dump(d,open(p,"w"),ensure_ascii=False,indent=2)
        print("seed authorization refreshed, token head:",os.environ["LIVE"][:16])
        PYEOF
    """ % (acct, acct, live, acct, acct))
    r = subprocess.run(SSH + ["cltx@%s" % STANDBY, remote], capture_output=True, text=True)
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


def add_models(acct):
    api_base = "http://zero-cursor-bpi-%s.%s.svc.cluster.local:8201/v1" % (acct, NS)
    script = textwrap.dedent('''
        import os,json,urllib.request
        mk=os.environ["LITELLM_MASTER_KEY"]; BASE="http://localhost:4000"
        def post(path,payload):
            req=urllib.request.Request(BASE+path,data=json.dumps(payload).encode(),
                headers={"Authorization":"Bearer "+mk,"Content-Type":"application/json"})
            try:
                r=urllib.request.urlopen(req,timeout=30); return r.status
            except urllib.error.HTTPError as e: return (e.code,e.read().decode()[:200])
        ab="%s"
        for suffix,extra in [("",{}),("-high",{"reasoning_effort":"high"}),("-max",{"reasoning_effort":"xhigh"})]:
            name="cursor-web-fc-%s-terra"+suffix
            mid="zerokey-cursor-web-fc-%s-terra"+suffix
            lp={"model":"openai/gpt-5.6-terra","api_key":"sk-zerokey-web-noop","api_base":ab,
                "use_chat_completions_api":False,"use_in_pass_through":False,"use_litellm_proxy":False,
                "use_xai_oauth":False,"merge_reasoning_content_in_choices":False}
            lp.update(extra)
            print("ADD",name,post("/model/new",{"model_name":name,"litellm_params":lp,"model_info":{"id":mid,"mode":"chat"}}))
    ''' % (api_base, acct, acct))
    out, _ = proxy_py(script)
    print(out)


def grant_key(acct, key_alias):
    # 拿 key 完整 token（DB alias→token）再 /key/update 追加三个模型
    sql = ("SELECT token FROM \"LiteLLM_VerificationToken\" WHERE key_alias='%s';" % key_alias)
    b = base64.b64encode(sql.encode()).decode()
    out, _, _ = kubectl("exec -i litellm-db-0 -- sh -c "
        "'echo %s | base64 -d | psql -U $POSTGRES_USER -d $POSTGRES_DB -t -A -f -'" % b)
    tok = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not tok:
        print("!! key_alias %s 在 DB 查不到 token" % key_alias); return
    script = textwrap.dedent('''
        import os,json,urllib.request
        mk=os.environ["LITELLM_MASTER_KEY"]; BASE="http://localhost:4000"; tok="%s"
        info=json.load(urllib.request.urlopen(urllib.request.Request(
            BASE+"/key/info?key="+tok,headers={"Authorization":"Bearer "+mk}),timeout=30))["info"]
        cur=info.get("models") or []
        add=["cursor-web-fc-%s-terra","cursor-web-fc-%s-terra-high","cursor-web-fc-%s-terra-max"]
        newm=sorted(set(cur)|set(add))
        r=urllib.request.Request(BASE+"/key/update",data=json.dumps({"key":tok,"models":newm}).encode(),
            headers={"Authorization":"Bearer "+mk,"Content-Type":"application/json"})
        try:
            resp=urllib.request.urlopen(r,timeout=30)
            print("KEY UPDATE",resp.status,"added=",[m for m in add if m not in cur])
        except urllib.error.HTTPError as e: print("KEY UPDATE ERR",e.code,e.read().decode()[:200])
    ''' % (tok, acct, acct, acct))
    out, _ = proxy_py(script)
    print(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--acct", required=True, help="目标账号号，如 82")
    ap.add_argument("--key-alias", required=True, help="要授权的 key alias（DB 里的完整 alias，如 cursor-liuguoxian04-5rub）")
    ap.add_argument("--live-from-ws", action="store_true", help="用 chatgpt-acct-N WS PVC 里的活 token 刷 web seed")
    ap.add_argument("--apply", action="store_true", help="真执行（默认 dry-run）")
    a = ap.parse_args()
    print("=== clone web-fc lane -> acct-%s, grant key=%s, apply=%s ===" % (a.acct, a.key_alias, a.apply))
    if not a.apply:
        print("[dry-run] 将：(1)%s刷 seed (2)apply zero-cursor-bpi-%s (3)建三档模型 (4)授权 key。加 --apply 执行。"
              % ("从 WS 活 token " if a.live_from_ws else "跳过", a.acct))
        return
    if a.live_from_ws:
        print("--- step1: refresh web seed from WS live token ---"); refresh_seed_from_ws(a.acct)
    print("--- step2: apply deploy/svc ---"); apply_deploy(a.acct)
    print("--- step3: add 3 models (WITH api_key placeholder) ---"); add_models(a.acct)
    print("--- step4: grant key ---"); grant_key(a.acct, a.key_alias)
    print("\n完成。验证（真 key 路径，别用 master key —— 那是假绿）：建临时 key 打 /v1/responses，"
          "看 HTTP 200+暗号回显，并 grep zero-cursor-bpi-%s pod 日志 conversation/200 OK。" % a.acct)


if __name__ == "__main__":
    main()
