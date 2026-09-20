#!/usr/bin/env bash
# lane_seed_install.sh —— 把 188 上刚抓的 users.json 灌进 225 的 hostPath，给新 lane 用。
#
# 与 lane_seed_capture.sh 的分工：capture 只产出文件，**不碰生产面**；install 是改生产面的
# 那一步，单独脚本、单独判据（否则一个脚本里既抓又灌，出事分不清是哪一半坏的）。
#
# 路径为什么是「188 读 → 本机管道 → 225 写」：
#   · 官方 refresh-225.sh 走 188 → 198 → `kubectl cp` 进 pod（pod 的 /app/temp 就是 225 的
#     hostPath）。**新 lane 还没有 pod**，那条路走不通。
#   · 188 到 225 没有免密（实测 `Permission denied (publickey,password)`），不能直推。
#   ⇒ 只能经本机中转。用管道，**字节不落本地磁盘**（不 scp 到本地再上传）。
#
# 写前必备份：225 上已有的 users.json 挪到 /Data/backups/zerokey-seed-<N>-<ts>.json。
# 没有旧文件也照打一行「无旧文件」，别让「没备份」和「备份失败」长得一样。
#
# 用法：
#   bash scripts/zk-cursor-web/lane_seed_install.sh 135
#   bash scripts/zk-cursor-web/lane_seed_install.sh 135 136 137 138 139 140
#
# 判据（缺一条就算没灌成，别往下建 lane）：
#   ① 落地字节数 == 188 上的源文件字节数
#   ② 225 上能 JSON 解析，users 的 key == acct<N>（灌错号是最容易犯又最难发现的错）
#   ③ parsedFetch 里 cookie 非空、openai-sentinel-proof-token 在
#   ④ 【in-pod】容器 startedAt 晚于本次 seed 的 mtime，且 pod 里的 users key == acct<N>
#
# 第 ④ 条为什么必须有：容器 args 是
#   `cp /seed/users.json /app/temp/users.json && exec node ...`
# /app/temp 是 **emptyDir**，只在**启动那一刻**拷一次。所以「225 上 seed 是新的」
# 和「这条腿正在用新 seed」是两回事 —— 不重启就一直吃旧的，而且**毫无症状**：
# 请求照样 200，只是用的是过期 token，到期才突然全红。
#
# ⚠️ 判「拷到了没」**不能用 in-pod 的字节数或 mtime** —— zerokey 进程会持续回写
# /app/temp/users.json：实测 lane 176/191/193 的 in-pod 都是 54–57KB（seed 只有 ~20KB），
# 且三条腿的 mtime 全落在同一个 9 秒窗口里（=「刚刚」，与启动时刻无关）。
# 拿这两个量会得到**恒红（字节）+ 恒绿（mtime）**两种假读数。
# 唯一量得到 `cp` 那一刻的是容器 `state.running.startedAt`：晚于 seed mtime ⇒ 拷的是新的。
# 新 lane 还没有 pod 时这一条打「跳过」并说明原因，别让「没 pod」和「校验没过」同形。
set -uo pipefail

H188="cltx@10.68.13.188"
H198="cltx@10.68.13.198"
H225="cltx@10.68.13.225"
# -n：脚本主体是 for 循环，循环体里的 ssh 不加 -n 会吞掉后续输入（实测过一次：
# 把四个 lane 的校验只跑掉第一个就静默退出）。灌装那一步要用 stdin 收管道，单独不带 -n。
SSH="ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=25"
SSH_IN="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=25"
KC="sudo k3s kubectl -n litellm-product"
TS="$(date +%Y%m%d-%H%M%S)"

[ $# -ge 1 ] || { echo "usage: $0 <acct-N> [N2 ...]"; exit 2; }

rc_all=0
for N in "$@"; do
  echo "==================== acct-$N ===================="
  SRC="/Data/zkcaps/zkcap-$N/out/zerokey-users.json"
  DST="/Data/zerokey-sessions/zero-$N/users.json"

  SZ=$($SSH "$H188" "stat -c %s '$SRC' 2>/dev/null || echo 0")
  if [ "${SZ:-0}" -lt 1000 ]; then
    echo "❌ 188 上没有可用的源文件（${SRC}，size=${SZ}）—— 先跑 lane_seed_capture.sh $N"
    rc_all=1; continue
  fi
  echo "源: 188:$SRC  $SZ bytes"

  # 备份旧的（有就挪，没有就明说）
  $SSH "$H225" "sudo -n mkdir -p /Data/backups /Data/zerokey-sessions/zero-$N; \
    if [ -f '$DST' ]; then sudo -n cp -a '$DST' /Data/backups/zerokey-seed-$N-$TS-pre-install.json && \
      echo \"  备份旧 seed -> /Data/backups/zerokey-seed-$N-$TS-pre-install.json (\$(sudo -n stat -c %s '$DST') bytes)\"; \
    else echo '  无旧 seed（新号，不需要备份）'; fi"

  # 灌：字节走管道，不落本地磁盘。
  # 先落临时文件再比内容 —— **内容没变就不覆盖**，否则 mtime 每轮刷新，
  # 判据 ④ 结构性恒红：本脚本自己写的 mtime 永远晚于「上一次 pod 启动」。
  # （2026-09-20 踩过：新建腿 restart 完再跑一遍校验，三条腿全红，差值只有十几秒，
  #   红的不是"pod 里是旧副本"而是"我刚又写了一次"。）
  TMP="/tmp/seed-$N-$TS.json"
  $SSH "$H188" "cat '$SRC'" | $SSH_IN "$H225" "cat > '$TMP' && chmod 600 '$TMP'"
  CHANGED=$($SSH "$H225" "if sudo -n cmp -s '$TMP' '$DST' 2>/dev/null; then echo same; else sudo -n cp '$TMP' '$DST' && sudo -n chmod 600 '$DST' && echo written; fi; rm -f '$TMP'")
  if [ "$CHANGED" = "same" ]; then
    echo "  内容与现有 seed 一致，未覆盖（保留原 mtime，判据 ④ 才量得准）"
  else
    echo "  已写入（内容有变）"
  fi
  SEED_MT=$($SSH "$H225" "sudo -n stat -c %Y '$DST' 2>/dev/null || echo 0")

  # 判据三连（在 225 上就地验，不把内容传回来）
  $SSH_IN "$H225" "sudo -n python3 - '$DST' '$N' '$SZ'" <<'PY'
import json, sys
p, n, want = sys.argv[1], sys.argv[2], int(sys.argv[3])
raw = open(p, 'rb').read()
ok = True
if len(raw) != want:
    print("  ❌ 字节数不符: 落地 %d != 源 %d" % (len(raw), want)); ok = False
else:
    print("  ✅ 字节数一致 %d" % len(raw))
try:
    d = json.loads(raw)
except Exception as e:
    print("  ❌ JSON 解析失败: %s" % e); sys.exit(1)
keys = list((d.get("chatgpt") or {}).keys())
if keys == ["acct%s" % n]:
    print("  ✅ users key = %s" % keys)
else:
    print("  ❌ users key = %s，期望 ['acct%s'] —— 灌错号了" % (keys, n)); ok = False


def walk(o):
    if isinstance(o, dict):
        if "headers" in o and "body" in o:
            return o
        for v in o.values():
            r = walk(v)
            if r:
                return r
    return None


pf = walk(d) or {}
h = pf.get("headers") or {}
ck, sent = len(h.get("cookie") or ""), bool(h.get("openai-sentinel-proof-token"))
if ck > 1000 and sent:
    print("  ✅ parsedFetch: headers=%d cookie=%d 字节 sentinel=True" % (len(h), ck))
else:
    print("  ❌ parsedFetch 不完整: cookie=%d sentinel=%s" % (ck, sent)); ok = False
sys.exit(0 if ok else 1)
PY
  [ $? -ne 0 ] && rc_all=1

  # ④ in-pod：正在服务的那份是不是刚灌的这份
  DEP="zero-cursor-bpi-$N"
  POD=$($SSH "$H198" "$KC get pod -l app=$DEP -o jsonpath='{.items[0].metadata.name}' 2>/dev/null")
  if [ -z "$POD" ]; then
    echo "  ⏭  in-pod 判据跳过：还没有 $DEP 的 pod（新 lane 正常；灌完再 clone 建腿）"
  else
    # 容器启动时刻（cp 发生的唯一时刻）—— 不是文件 mtime，见文件头 ④ 的说明
    STARTED=$($SSH "$H198" "$KC get pod '$POD' -o jsonpath='{.status.containerStatuses[?(@.name==\"zerokey\")].state.running.startedAt}' 2>/dev/null")
    START_EPOCH=$(python3 -c "import sys,datetime;s=sys.argv[1];print(int(datetime.datetime.strptime(s,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).timestamp()) if s else 0)" "$STARTED")
    IN_KEY=$($SSH "$H198" "$KC exec '$POD' -c zerokey -- node -e 'console.log(Object.keys((require(\"/app/temp/users.json\").chatgpt)||{}).join(\",\"))' 2>/dev/null")
    if [ "${START_EPOCH:-0}" -eq 0 ]; then
      echo "  ❌ 读不到 $POD 的 zerokey 容器 startedAt（不是 Running？）—— 判不了拷没拷到，先看 pod 状态"
      rc_all=1
    elif [ "$START_EPOCH" -lt "${SEED_MT:-0}" ]; then
      echo "  ❌ 容器 startedAt=$STARTED 早于本次 seed（mtime=${SEED_MT}）—— pod 里那份是旧副本，"
      echo "     必须 $KC rollout restart deploy/$DEP 才算灌进去了（不重启毫无症状，到期才全红）"
      rc_all=1
    elif [ "$IN_KEY" != "acct$N" ]; then
      echo "  ❌ in-pod users key = '${IN_KEY:-none}'，期望 acct$N"
      rc_all=1
    else
      echo "  ✅ in-pod: 容器 startedAt=$STARTED 晚于 seed mtime=${SEED_MT}，key=$IN_KEY"
    fi
  fi
done

echo
[ $rc_all -eq 0 ] && echo "全部灌装通过。" || echo "⚠️ 有号没过判据 —— 不带病入池，先修那几号。"
exit $rc_all
