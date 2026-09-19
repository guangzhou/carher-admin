#!/bin/bash
# 把一个 chatgpt-acct 装回 198 litellm-product 循环池(串行,一号一跑)。
# chatgpt-pool-198-pause.sh 的反向操作。
#
#   P198='..' P188='..' ./chatgpt-pool-198-rejoin.sh <N> [/path/to/auth-acct-<N>.json]
#
# 给了 auth 文件 = 刚重烧完 OAuth,要换盘上的凭证;不给 = 只是之前被暂停,盘上凭证还能用。
#
# ## 前置:身份审计不许跳
#
# 换 auth 前必须确认 **这份 auth 的 chatgpt_account_id == 该 N 的 PVC 里原来那个**。
# 编号↔账号的映射历史上漂过(acct-173/174 的邮箱记在 163/164 名下),
# 装错 = 两个 pod 共用一个 refresh_token 互相作废。见 feedback_creds_audit_before_rerun_join_by_email。
#
# ## 三个坑(2026-09-12 装 175~194 时逐个踩出来的)
#
# ① **缺 `chatgpt-images-handler` 挂载 ⇒ 一 scale 起来必 CrashLoop。**
#    共享 CM `chatgpt-pool-config` 的 `custom_handler` 指向 `chatgpt_images.chatgpt_images_llm`,
#    那模块**不在镜像里**,是另一个 CM 以 subPath 挂成 `/app/chatgpt_images.py`。
#    handler 上线前建的老 deploy 全缺这个卷。报错 `ImportError: Could not import chatgpt_images_llm`
#    **长得像镜像问题**(我第一反应是刚钉的 digest 干的,回退旧 tag 照样崩才定位到卷)。
#    ⇒ 本脚本 D 段无条件补卷 + 顺手把 image 钉成 acct-82 的 digest。
#
# ② **`AUTO_SCALE_ON_PAUSE=0` 让 `resume_acct()` 的 scale=1 整段变 no-op,但它照样注册 arms。**
#    `quota-rebalance.py` 的 `scale_deploy()` 开头就 `if not AUTO_SCALE_ON_PAUSE: return True`,
#    而 188 的 `.chatgpt-quota/env` 正是 `AUTO_SCALE_ON_PAUSE=0`。
#    于是它以为"scale+等 ready"成功了,在 **0 副本、endpoint 无 IP** 时把 8 条 arms 写进 router。
#    ⇒ 正确顺序 = 自己 scale=1 + 等 endpoint 真有 IP + 叶子流式跑通,**再**调 resume_acct(H 段)。
#
# ③ **cron 的读-改-写会吞掉刚写的 state.json** ⇒ I 段写完必须跨一轮(≥6min)复查。
#
# ## 两个 shell 层的坑
#
# - glob 和 `<` 重定向由**非 root 那层 shell** 展开 ⇒ 必须 `sudo -S sh -c 'ls -d ...'`,
#   否则静默 Permission denied / 空结果,然后误报"找不到 PVC 目录"。
# - `resume_acct` 必须传**内联 meta dict**:`POOL_ACCOUNTS` 只有 15 条,查表必 KeyError。
#   location=198 时 `acct_api_base()` 走 svc 名,`port` 字段实际不用但要有。
set -u
N="${1:?用法: chatgpt-pool-198-rejoin.sh <N> [auth.json]}"
SRC="${2:-}"
NS=litellm-product
M198=10.68.13.198
M188=10.68.13.188
POOLKEY=${POOLKEY:?需要 export POOLKEY=<198 prod master key>；不再内置默认值}
# 铁律:acct 镜像永远 == acct-82 的 digest(feedback_all_acct_images_must_match_acct82_by_digest)
IMG=${ACCT_IMG:-127.0.0.1:5000/litellm-carher@sha256:580707d72770b0b5f6a65fe4cf68e2688f4bc6b5a91737317b37ff351e53202c}

: "${P198:?env P198 required}"
: "${P188:?env P188 required}"
s198() { sshpass -p "$P198" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 cltx@$M198 "$@"; }
s188() { sshpass -p "$P188" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 cltx@$M188 "$@"; }
kx()   { s198 "echo '$P198' | sudo -S sh -c '$1' 2>/dev/null"; }
fail() { echo "!!!! acct-$N $1"; exit 1; }

echo "== acct-$N 回池"

# A~C 换 auth(只在给了文件时做)
if [ -n "$SRC" ]; then
  LSZ=$(wc -c < "$SRC" | tr -d ' ')
  [ "${LSZ:-0}" -gt 1000 ] || fail "本地 auth 文件异常 ($LSZ B)"
  sshpass -p "$P198" scp -o StrictHostKeyChecking=no -q "$SRC" cltx@$M198:/tmp/stage-$N.json || fail "scp 失败"
  RSZ=$(s198 "wc -c < /tmp/stage-$N.json" | tr -d ' ')
  [ "$RSZ" = "$LSZ" ] || fail "中转文件大小不符 198=$RSZ 本地=$LSZ"
  kx "kubectl -n $NS scale deploy chatgpt-acct-$N --replicas=0" >/dev/null
  for i in $(seq 1 20); do
    C=$(kx "kubectl -n $NS get pod -l app=chatgpt-acct-$N --no-headers" | grep -c chatgpt-acct)
    [ "${C:-0}" = 0 ] && break; sleep 3
  done
  # PV 是 local-path 且落在控制节点 198 本机,scale=0 后直接 cp,不用 busybox hostPath pod
  PVDIR=$(kx "ls -d /Data/rancher/storage/*_${NS}_chatgpt-acct-$N-auth" | tail -1)
  [ -n "$PVDIR" ] || fail "找不到 PVC 目录"
  kx "cp /tmp/stage-$N.json $PVDIR/auth.json; chmod 644 $PVDIR/auth.json" >/dev/null
  WSZ=$(kx "wc -c < $PVDIR/auth.json" | tr -d ' ')
  [ "$WSZ" = "$LSZ" ] || fail "PVC 写入未确认 ($WSZ / 应为 $LSZ) — deploy 仍 0 副本,人工复核"
  kx "rm -f /tmp/stage-$N.json" >/dev/null
  echo "  ✓ PVC 已写入 (${WSZ}B)"
fi

# D 补 imghandler 卷 + 钉 digest(坑①)
PATCH=$(cat <<JSON | tr -d '\n'
{"spec":{"template":{"spec":{
 "volumes":[{"name":"imghandler","configMap":{"name":"chatgpt-images-handler","defaultMode":420}},
            {"name":"config","configMap":{"name":"chatgpt-pool-config","defaultMode":420}},
            {"name":"auth","persistentVolumeClaim":{"claimName":"chatgpt-acct-$N-auth"}}],
 "containers":[{"name":"litellm","image":"$IMG",
   "volumeMounts":[{"name":"imghandler","mountPath":"/app/chatgpt_images.py","subPath":"chatgpt_images.py"},
                   {"name":"config","mountPath":"/app/config.yaml","subPath":"config.yaml"},
                   {"name":"auth","mountPath":"/chatgpt-auth"}]}]
}}}}
JSON
)
# 注意:kx() 自己就套了一层单引号,这里必须直接走 s198 + '\'' 转义
s198 "echo '$P198' | sudo -S sh -c 'kubectl -n $NS patch deploy chatgpt-acct-$N --type=strategic -p '\''$PATCH'\''' 2>/dev/null" \
  | grep -q -e patched -e unchanged || fail "patch 失败"
echo "  ✓ imghandler 卷 + digest 已就位"

# E 起 pod + 等 endpoint 真有 IP(坑②:这步不能交给 resume_acct)
kx "kubectl -n $NS scale deploy chatgpt-acct-$N --replicas=1" >/dev/null
kx "kubectl -n $NS rollout status deploy/chatgpt-acct-$N --timeout=240s" | tail -1
EP=""
for i in $(seq 1 30); do
  EP=$(kx "kubectl -n $NS get endpoints chatgpt-acct-$N -o jsonpath='{.subsets[*].addresses[*].ip}'" | tr -d ' ')
  [ -n "$EP" ] && break; sleep 4
done
[ -n "$EP" ] || fail "pod 起来了但 endpoint 一直没 IP — 未注册 router,人工复核"
echo "  ✓ pod ready endpoint=$EP"

# F pod 内 auth 复核(防被覆写成空壳)
ALEN=$(kx "P=\$(kubectl -n $NS get pod -l app=chatgpt-acct-$N -o jsonpath={.items[0].metadata.name}); kubectl -n $NS exec \$P -- python3 -c \"import json;print(\\\"ALEN=\\\"+str(len(json.load(open(\\\"/chatgpt-auth/auth.json\\\")).get(\\\"access_token\\\",\\\"\\\"))))\"" \
  | grep -oE 'ALEN=[0-9]+' | tail -1 | cut -d= -f2)
[ "${ALEN:-0}" -gt 1000 ] || fail "pod 内 auth 无效 access_len=${ALEN:-0}"
echo "  ✓ pod auth 有效 access_len=$ALEN"

# G 叶子级真实推理 —— 判死活只认这一把(feedback_disk_authjson_token_is_stale_not_death_proof)
#   · 要叶子**自己的** key:母 router 的 key 打叶子回 400 `No connected db.`
#   · 叶子模型名带 `chatgpt-` 前缀:裸 `gpt-5.6-terra` 回 400 Invalid model name
#   · 必须流式 + 唯一 nonce + 断言 response.completed,否则响应缓存直接回绿不碰上游
MK=$(kx "kubectl -n $NS get secret chatgpt-pool-master-key -o jsonpath={.data.LITELLM_MASTER_KEY}" | tr -d ' \n' | base64 -d)
[ ${#MK} -gt 20 ] || fail "取不到叶子 master key"
NONCE=$(date +%s%N)
GOT=$(s198 "curl -sS -m 150 -N http://$EP:4000/v1/responses -H 'Authorization: Bearer $MK' -H 'Content-Type: application/json' -d '{\"model\":\"chatgpt-gpt-5.6-terra\",\"stream\":true,\"input\":[{\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"say OK-$NONCE and nothing else\"}]}]}'" \
  | grep -c 'response\.completed')
[ "${GOT:-0}" -ge 1 ] || fail "叶子流式推理没跑通 — 已起 pod 但未入 router"
echo "  ✓ 叶子真实推理通过"

# H 注册 arms(必须在 E/G 之后)
s188 "set -a; source /home/cltx/.chatgpt-quota/env; set +a; python3 -c \"
import importlib.util,sys
spec=importlib.util.spec_from_file_location('qr','/home/cltx/quota-rebalance.py')
qr=importlib.util.module_from_spec(spec); sys.modules['qr']=qr; spec.loader.exec_module(qr)
print('RESUME_CREATED=', qr.resume_acct('acct-$N', {'port': 40$N, 'location': '198'}))
\"" 2>&1 | grep -E 'RESUME_CREATED|resumed|failed' | tail -3

# I state.json 置 HEALTHY(不写这步 = 在池里但被标 manual_offline,governor 下轮就摘)
s188 "python3 -c \"
import json,pathlib,time
p=pathlib.Path('/home/cltx/.chatgpt-quota/state/state.json')
d=json.loads(p.read_text()); a=d.setdefault('acct-$N',{})
a.update({'tier':'HEALTHY','paused':False,'manual_offline':False,'consecutive_401':0,
          'consecutive_probe_err':0,'probe_err_alerted':False,'restore_at':0,'cause':None,
          'ts':int(time.time())})
p.write_text(json.dumps(d,indent=2,ensure_ascii=False))
print('  STATE_OK',a['tier'],a['paused'],a['manual_offline'])
\"" 2>&1 | grep STATE_OK

# J readback
CNT=$(s198 "curl -s -H 'Authorization: Bearer $POOLKEY' http://127.0.0.1:30402/pro/v1/model/info | python3 -c \"
import json,sys
ids={(e.get('model_info') or {}).get('id','') for e in json.load(sys.stdin).get('data',[])}
print(len([i for i in ids if i.startswith('chatgpt-acct-$N-')]))\"")
echo "  ✓ router 中 acct-$N unique arms = $CNT"
echo "== acct-$N DONE  ⚠️ 隔 ≥6min 复查一次 state 有没有被 cron 翻回去(坑③)"
