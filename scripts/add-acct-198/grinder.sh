#!/bin/bash
# grinder.sh (scripts/add-acct-198/) — 批量 OAuth 轮换 grinder (198 K3s litellm-product pool)
# ────────────────────────────────────────────────────────────────────────────
# 用途: 把一批 ChatGPT 订阅号端到端接入 198 池, retry-until-success。
#   phase1.5 login(+可选二次OTP) + device flow 各需一次 mail.com 取码成功, 靠 3-IP
#   轮换重试撞过 CF Turnstile + 取码时序。每号最多 10 次尝试。
#
# 由 acct-86..99 实战沉淀 (2026-07-21)。核心固化点见 skill add-chatgpt-acct-198 v2.1:
#   - oauth.py 已含 9 个修复(真键入/主题过滤/设备二次OTP真键入/callback停留 等)
#   - 密码+登录OTP 号 toggle off 时先跑 run-enable-codex-toggle-188.sh 再来 grinder(两步法 #10)
#
# ── creds 来源(不硬编码明文进仓库) ──
#   CSV 文件, 每行: acct_num,email,mail_pw,chatgpt_pw
#   路径由 GRIND_CREDS 指定(默认 /tmp/grind-creds.csv)。'#' 开头行忽略。
#   例:
#     93,RosaliaParkskno@mail.com,jqcI0h5XA2L,UAl13a5FDSa3
#     95,MannLaraptu@mail.com,kzK3Z9xW71vO,LMCpJzibCQhP
#
# ── 用法 ──
#   GRIND_ACCTS="93 94 95" GRIND_CREDS=/tmp/grind-creds.csv \
#     nohup bash scripts/add-acct-198/grinder.sh > /tmp/grind-main.log 2>&1 &
#   # 监控: tail -f /tmp/grind-main.log  ← 读本地文件, 别穿 jms 隧道盯屏(隧道频繁2min超时)
#   # 成败判定: 每号一行 "auth_valid=1/0"; 全部完成写 "GRINDER DONE ok=..."
#   # 卡点根因才 docker logs(远端): j8 "docker logs \$CID"
#
# ── 环境要求 ──
#   - jms 别名 AIYJY-litellm(198 kube host) + JSZX-AI-03(188 docker host, patchright 跑这)
#   - 188 上: /Data/chatgpt-auth/re-oauth.sh + docker image playwright/python:v1.60.0-noble
#   - LITELLM_MK_198 用于收尾 smoke(按需改)
# ────────────────────────────────────────────────────────────────────────────
set -uo pipefail

: "${LITELLM_MK_198:=sk-pro-litellm-ce077e2b0721bb419a633e4d}"
CREDS_CSV="${GRIND_CREDS:-/tmp/grind-creds.csv}"
NS=litellm-product
IMAGE=mcr.microsoft.com/playwright/python:v1.60.0-noble
REPO="${GRIND_REPO:-$HOME/codes/carher-admin}"
cd "$REPO" || { echo "FATAL: repo not found $REPO"; exit 1; }
[ -s "$CREDS_CSV" ] || { echo "FATAL: creds csv missing/empty: $CREDS_CSV"; exit 1; }

# jms 重试包装(过滤 189 瞬态 Permission denied 抖动)
jr(){ local o; for t in 1 2 3 4 5 6; do o=$(jms ssh AIYJY-litellm "$1" 2>&1); echo "$o"|grep -q "Permission denied (password" || { echo "$o"; return 0; }; sleep 3; done; echo "$o"; }
j8(){ local o; for t in 1 2 3 4 5 6; do o=$(jms ssh JSZX-AI-03 "$1" 2>&1); echo "$o"|grep -q "Permission denied (password" || { echo "$o"; return 0; }; sleep 3; done; echo "$o"; }
# manifest apply 带重试 + presence 校验。裸 `cat|jms ssh kubectl apply` 无重试, jms 189 抖动
# (Connection reset/TLS)命中时静默丢 deploy → 号只注册 router 无 pod → smoke 500, 随后
# quota-rebalance 摘 entry 变 400(实证 2026-07-22 acct-102/110)。返回 0=确认 deploy 存在。
japply(){ local f="$1" dep="$2" t; for t in 1 2 3 4 5 6; do cat "$f" | jms ssh AIYJY-litellm "kubectl apply -f -" >/dev/null 2>&1; jr "kubectl -n $NS get deploy $dep -o jsonpath='{.metadata.name}' 2>/dev/null" | grep -q "^$dep$" && return 0; sleep 3; done; return 1; }

# CSV 查字段
csv_field(){ awk -F, -v n="$1" -v c="$2" '$1==n{print $c}' "$CREDS_CSV" | head -1; }
email_for(){ csv_field "$1" 2; }
mailpw_for(){ csv_field "$1" 3; }
gptpw_for(){ csv_field "$1" 4; }
# 3-IP egress 轮换: att%3==1→236:17890(美), ==2→236:17891(美), ==0→188直连(日)
egress_for(){ case $(( $1 % 3 )) in 1) echo 'socks5://10.68.13.236:17890';; 2) echo 'socks5://10.68.13.236:17891';; 0) echo '';; esac; }

# 同步最新 oauth.py + toggle.py 到 188(修复随之传播)
cat scripts/chatgpt-onboard/chatgpt-litellm-oauth.py    | jms ssh JSZX-AI-03 "cat > /tmp/chatgpt-litellm-oauth.py"    2>/dev/null
cat scripts/chatgpt-onboard/chatgpt-enable-codex-toggle.py | jms ssh JSZX-AI-03 "cat > /tmp/chatgpt-enable-codex-toggle.py" 2>/dev/null

ACCTS="${GRIND_ACCTS:-$(awk -F, '/^[0-9]/{print $1}' "$CREDS_CSV" | tr '\n' ' ')}"
MAXATT="${GRIND_MAX_ATT:-10}"
OK=""
for N in $ACCTS; do
  EMAIL=$(email_for "$N"); GPW=$(gptpw_for "$N"); MPW=$(mailpw_for "$N"); GOT=0
  TSEC=$(csv_field "$N" 5)   # 第5列=飞书表「2FA密钥」(base32), 无则空
  [ -n "$EMAIL" ] || { echo "!!!! acct-$N no creds in $CREDS_CSV, skip"; continue; }
  # 单引号包裹: 密码含 & $ % # ! * 等 shell 元字符, re-oauth.sh 用 `source <(... declare)` 读取,
  # 不引号会把 & 当后台符、$x 当变量展开 → 密码被撕碎 (见 feedback_creds_single_quote_dollar)。
  # 值本身若含单引号需转义, 目前批次无单引号密码。
  # totp_secret: 带 2FA 的号密码后落 /mfa-challenge, 邮箱永不来码, 必须本地算 TOTP。
  # 不写这行 → 该号必卡在 authenticator 页 (见 feedback_chatgpt_onboard_totp_2fa_support)。
  {
    printf "email='%s'\nmail_pw='%s'\nchatgpt_pw='%s'\n" "$EMAIL" "$MPW" "$GPW"
    [ -n "$TSEC" ] && printf "totp_secret='%s'\n" "$TSEC"
  } > /tmp/creds-$N.txt
  CLINES=$(grep -c . /tmp/creds-$N.txt)
  for t in 1 2 3 4 5; do cat /tmp/creds-$N.txt | jms ssh JSZX-AI-03 "mkdir -p /Data/chatgpt-auth/acct-$N && cat > /Data/chatgpt-auth/acct-$N/.creds && chmod 600 /Data/chatgpt-auth/acct-$N/.creds && grep -c . /Data/chatgpt-auth/acct-$N/.creds" 2>&1 | grep -q "^$CLINES$" && break; sleep 3; done
  # 确保 deployment 存在(以 acct-86 为模板)。apply 走 japply(重试+presence 校验),
  # 建不成就 skip 该号防"半接入"(router 有 entry 但无 pod)。
  if ! jr "kubectl -n $NS get deploy chatgpt-acct-$N -o jsonpath='{.metadata.name}' 2>/dev/null" | grep -q "^chatgpt-acct-$N$"; then
    sed "s/acct-86/acct-$N/g; s/account: \"86\"/account: \"$N\"/g" k8s/chatgpt-acct-86.yaml > k8s/chatgpt-acct-$N.yaml
    japply k8s/chatgpt-acct-$N.yaml chatgpt-acct-$N || { echo "!!!! acct-$N deploy apply 失败(jms 抖动?) — skip 防半接入, 稍后重跑该号"; continue; }
  fi

  TOGGLE_TRIED=0
  # 已有有效 token(例如由官方 codex CLI device-auth 拿到, 人工绑定过 toggle)时
  # 跳过 OAuth 直接 finalize, 否则重跑 OAuth 会把好 token 覆盖掉。
  PRE=$(j8 "python3 -c 'import json;print(1 if len(json.load(open(\"/tmp/auth-acct-$N.json\")).get(\"access_token\",\"\"))>1000 else 0)' 2>/dev/null" | tr -dc 0-9)
  if [ "${PRE:-0}" = "1" ]; then
    echo "  ✓ acct-$N 已有有效 token (access_len>1000) → 跳过 OAuth, 直接 finalize"
    GOT=1
  fi
  for att in $(seq 1 "$MAXATT"); do
    [ "$GOT" = "1" ] && break
    PX=$(egress_for $att); LBL=$([ -n "$PX" ] && echo "$PX" || echo "188-JP")
    echo "===== acct-$N attempt $att egress=$LBL $(date +%H:%M:%S) ====="
    j8 'docker ps --filter ancestor='"$IMAGE"' -q | xargs -r docker kill >/dev/null 2>&1'
    # 每次 attempt 结束把日志/截图归档成 .att<N> 再清空; 否则下一 attempt 一 rm 就把
    # 上一次的失败证据全毁了(诊断时只能干瞪眼, acct-112 实证)。
    j8 "[ -s /tmp/oauth-acct-$N.log ] && cp -f /tmp/oauth-acct-$N.log /tmp/oauth-acct-$N.log.prev 2>/dev/null; [ -d /tmp/screenshots-acct-$N ] && { rm -rf /tmp/screenshots-acct-$N.prev; cp -a /tmp/screenshots-acct-$N /tmp/screenshots-acct-$N.prev 2>/dev/null; }; docker run --rm -v /tmp:/t busybox rm -rf /t/auth-acct-$N.json /t/oauth-acct-$N.log /t/screenshots-acct-$N >/dev/null 2>&1; rm -f /tmp/oauth-acct-$N.log /tmp/auth-acct-$N.json; rm -rf /tmp/screenshots-acct-$N; setsid bash -c 'OAUTH_PROXY=$PX MAIL_OTP_PROVIDER=mailcom GEN_ONLY=1 bash /Data/chatgpt-auth/re-oauth.sh acct-$N 2>&1 | stdbuf -oL tee /tmp/oauth-acct-$N.log' </dev/null >/dev/null 2>&1 & disown; sleep 2; echo launched"
    for w in $(seq 1 54); do
      sleep 10
      V=$(j8 "python3 -c 'import json;print(1 if len(json.load(open(\"/tmp/auth-acct-$N.json\")).get(\"access_token\",\"\"))>1000 else 0)' 2>/dev/null" | tr -dc 0-9)
      [ "${V:-0}" = "1" ] && break
      CT=$(j8 "docker ps --filter ancestor=$IMAGE -q | wc -l" | tr -dc 0-9)
      [ "${CT:-0}" = "0" ] && [ "$w" -gt 3 ] && break
    done
    echo "  -> acct-$N att $att egress=$LBL auth_valid=${V:-0}"
    [ "${V:-0}" = "1" ] && { GOT=1; break; }
    # 两步法自动触发(skill #10): 连续 ≥2 次失败 且从没开过 toggle → 疑似 toggle off, 先开 toggle
    # 注意: toggle docker 必须在 188(j8)上跑, 不是本地; toggle py 已在开头上传到 188 /tmp
    if [ "$att" -ge 2 ] && [ "$TOGGLE_TRIED" = 0 ]; then
      # 检测源用持久的 tee 日志 /tmp/oauth-acct-N.log, 不用易失的 docker logs $STUCK:
      # OAuth 容器 --rm, 退出即删, docker logs 常拿到空 → 两步法从不触发(acct-112 实证)。
      # consent-disabled 时 python 打印 "Enable device code authorization" 到该 tee 日志。
      if j8 "grep -qE 'toggle: false . false|aria_disabled=true|Enable device code|consent Continue disabled' /tmp/oauth-acct-$N.log 2>/dev/null && echo Y" | grep -q Y; then
        echo "  [两步法] acct-$N 疑似 Codex toggle off → 在 188 内联跑独立 toggle enable (mouse-box-center 才点得动 radix switch)"
        # toggle 也走 3-IP 轮换重试: 登录 chatgpt.com 同样会被 CF 概率拦(acct-112 实证),
        # 单跑一次撞 CF 就废。最多 3 次, 命中 RESULT=ENABLED / aria-checked=true 即停。
        # 密码用 sed 去掉外层单引号(.creds 现单引号包裹, cut -f2- 会连引号读进来 → 密码错)。
        for tg in 1 2 3; do
          TGPX=$(egress_for $tg)
          echo "  [两步法] toggle attempt $tg egress=$([ -n "$TGPX" ] && echo "$TGPX" || echo 188-JP)"
          TGOUT=$(j8 "docker ps --filter ancestor=$IMAGE -q | xargs -r docker kill >/dev/null 2>&1
sd=\$(mktemp -d /tmp/tgl-$N-XXXX)
unq(){ grep -E \"^\$1=\" /Data/chatgpt-auth/acct-$N/.creds|head -1|cut -d= -f2-|sed -E \"s/^'(.*)'\\\$/\\\\1/\"; }
pw=\$(unq chatgpt_pw); mpw=\$(unq mail_pw); em=\$(unq email)
printf '%s' \"\$pw\">\$sd/p.txt; printf '%s' \"\$mpw\">\$sd/m.txt
docker run --rm -v /tmp/chatgpt-enable-codex-toggle.py:/work/script.py:ro \
  -v \$sd/p.txt:/run/chatgpt_pw.txt:ro -v \$sd/m.txt:/run/mail_pw.txt:ro -v \$sd:/work/screenshots \
  -e CHATGPT_EMAIL=\$em -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt -e MAIL_PW_FILE=/run/mail_pw.txt \
  -e SCREENSHOT_DIR=/work/screenshots -e ACTION=enable-codex-toggle -e MAIL_OTP_PROVIDER=mailcom \
  -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e OAUTH_PROXY=$TGPX -e DISPLAY=:99 \
  -e TOTP_SECRET='$TSEC' $IMAGE \
  bash -c 'Xvfb :99 -screen 0 1440x1000x24 >/dev/null 2>&1 & sleep 1 && pip install patchright==1.60.0 -q --root-user-action=ignore >/dev/null 2>&1 && python3 /work/script.py' 2>&1 | grep -E 'RESULT|ENABLED|FAILED|aria-checked=true'
rm -rf \$sd")
          echo "$TGOUT" | sed 's/^/    tgl> /'
          echo "$TGOUT" | grep -qE 'RESULT=ENABLED|aria-checked=true' && { echo "  [两步法] toggle 开启成功 (att $tg)"; break; }
          sleep 3
        done
        TOGGLE_TRIED=1
      fi
    fi
    sleep 3
  done
  [ "$GOT" != "1" ] && { echo "!!!! acct-$N FAILED after $MAXATT (查远端 docker logs 卡点)"; continue; }

  echo "===== acct-$N finalize ====="
  # auth.json 经 188→本地→198 中转, hostPath busybox 写 PVC(local-path node-bound)
  j8 "cat /tmp/auth-acct-$N.json" > /tmp/auth-acct-$N.json
  cat /tmp/auth-acct-$N.json | jms ssh AIYJY-litellm "cat > /tmp/acct${N}stage.json" 2>/dev/null
  jr "kubectl -n $NS scale deploy chatgpt-acct-$N --replicas=0; for i in \$(seq 1 20); do [ \"\$(kubectl -n $NS get pod -l app=chatgpt-acct-$N --no-headers 2>/dev/null|wc -l)\" = 0 ] && break; sleep 3; done"
  NODE=$(jr "PV=\$(kubectl -n $NS get pvc chatgpt-acct-$N-auth -o jsonpath='{.spec.volumeName}'); kubectl get pv \$PV -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]}'"|tail -1)
  OVR="{\"spec\":{\"nodeName\":\"$NODE\",\"restartPolicy\":\"Never\",\"volumes\":[{\"name\":\"a\",\"persistentVolumeClaim\":{\"claimName\":\"chatgpt-acct-$N-auth\"}},{\"name\":\"h\",\"hostPath\":{\"path\":\"/tmp/acct${N}stage.json\",\"type\":\"File\"}}],\"containers\":[{\"name\":\"x\",\"image\":\"busybox\",\"command\":[\"sh\",\"-c\",\"cp /h/src /a/auth.json; echo RESULT=\$(wc -c < /a/auth.json)\"],\"volumeMounts\":[{\"name\":\"a\",\"mountPath\":\"/a\"},{\"name\":\"h\",\"mountPath\":\"/h/src\"}]}]}}"
  jr "kubectl -n $NS run cp$N-h --restart=Never --image=busybox --overrides='$OVR' >/dev/null 2>&1; kubectl -n $NS wait --for=condition=Ready pod/cp$N-h --timeout=30s >/dev/null 2>&1; sleep 3; kubectl -n $NS logs cp$N-h 2>/dev/null|grep RESULT; kubectl -n $NS delete pod cp$N-h --force --grace-period=0 >/dev/null 2>&1"
  jr "kubectl -n $NS scale deploy chatgpt-acct-$N --replicas=1; kubectl -n $NS rollout status deploy/chatgpt-acct-$N --timeout=150s"
  # 兜底: pod 首启若抢在 hostPath 写入前, 容器会自发 device_code 把有效 auth.json 覆写成
  # 空壳 {device_code_requested_at}(实证 acct-98)。scale=1 后校验 pod 内 auth, 空壳则重写 PVC
  # 再重启, 最多 2 轮。PVC 预置有效 token 后容器就不再触发 device_code。
  for vfix in 1 2; do
    sleep 12
    VPOD=$(jr "kubectl -n $NS get pod -l app=chatgpt-acct-$N -o jsonpath='{.items[0].metadata.name}' 2>/dev/null" | tail -1)
    [ -z "$VPOD" ] && { sleep 8; continue; }
    PODTOK=$(jr "kubectl -n $NS exec $VPOD -- python3 -c \"import json;print(len(json.load(open('/chatgpt-auth/auth.json')).get('access_token','')))\" 2>/dev/null" | tr -dc 0-9)
    if [ "${PODTOK:-0}" -gt 1000 ]; then echo "  ✓ acct-$N pod auth 有效 (access_len=$PODTOK)"; break; fi
    echo "  ⚠ acct-$N pod auth 被覆写成空壳 (access_len=${PODTOK:-0}) — 第 $vfix 轮重写 PVC"
    jr "kubectl -n $NS scale deploy chatgpt-acct-$N --replicas=0; for i in \$(seq 1 20); do [ \"\$(kubectl -n $NS get pod -l app=chatgpt-acct-$N --no-headers 2>/dev/null|wc -l)\" = 0 ] && break; sleep 3; done"
    jr "kubectl -n $NS run cp$N-hv --restart=Never --image=busybox --overrides='$OVR' >/dev/null 2>&1; kubectl -n $NS wait --for=condition=Ready pod/cp$N-hv --timeout=30s >/dev/null 2>&1; sleep 3; kubectl -n $NS logs cp$N-hv 2>/dev/null|grep RESULT; kubectl -n $NS delete pod cp$N-hv --force --grace-period=0 >/dev/null 2>&1"
    jr "kubectl -n $NS scale deploy chatgpt-acct-$N --replicas=1; kubectl -n $NS rollout status deploy/chatgpt-acct-$N --timeout=150s"
  done
  # 注册进 quota-rebalance POOL_ACCOUNTS + state.json HEALTHY + resume_acct(6模型)
  j8 "grep -q '\"acct-$N\":' /home/cltx/quota-rebalance.py || sed -i '/\"acct-79\": {\"port\": 4079/a\\    \"acct-$N\": {\"port\": 40$N, \"location\": \"198\"},' /home/cltx/quota-rebalance.py"
  j8 "python3 -c 'import json,pathlib,time;p=pathlib.Path(\"/home/cltx/.chatgpt-quota/state/state.json\");d=json.loads(p.read_text());a=d.setdefault(\"acct-$N\",{});a.update({\"tier\":\"HEALTHY\",\"paused\":False,\"manual_offline\":False,\"consecutive_401\":0,\"consecutive_probe_err\":0,\"probe_err_alerted\":False,\"restore_at\":0,\"cause\":None,\"ts\":int(time.time())});p.write_text(json.dumps(d,indent=2,ensure_ascii=False))'"
  j8 "set -a; source /home/cltx/.chatgpt-quota/env; set +a; python3 -c \"import importlib.util,sys;spec=importlib.util.spec_from_file_location('qr','/home/cltx/quota-rebalance.py');qr=importlib.util.module_from_spec(spec);sys.modules['qr']=qr;spec.loader.exec_module(qr);print('resume:',qr.resume_acct('acct-$N',qr.POOL_ACCOUNTS['acct-$N']))\"" | grep -E "resume|resumed"
  OK="$OK $N"
done

echo "===== final rollout ====="
jr "kubectl -n $NS rollout restart deploy/litellm-proxy; for i in \$(seq 1 100); do R=\$(kubectl -n $NS get deploy litellm-proxy -o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null); [ \"\$R\" = 4/4 ] && break; sleep 5; done"
for N in $OK; do for M in gpt-5.5 gpt-5.6-sol; do R=$(jr "curl -sS -m40 -o /dev/null -w '%{http_code}' https://cc.auto-link.com.cn/pro/v1/chat/completions -H 'Authorization: Bearer $LITELLM_MK_198' -H 'Content-Type: application/json' -d '{\"model\":\"chatgpt-acct-$N-$M\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}'" | grep -oE '^[0-9]{3}$'); echo "chatgpt-acct-$N-$M -> HTTP $R"; done; done
echo "GRINDER DONE ok=$OK $(date +%H:%M:%S)"
