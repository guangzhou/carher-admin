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
set -uo pipefail

H188="cltx@10.68.13.188"
H225="cltx@10.68.13.225"
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=25"
TS="$(date +%Y%m%d-%H%M%S)"

[ $# -ge 1 ] || { echo "usage: $0 <acct-N> [N2 ...]"; exit 2; }

rc_all=0
for N in "$@"; do
  echo "==================== acct-$N ===================="
  SRC="/Data/zkcaps/zkcap-$N/out/zerokey-users.json"
  DST="/Data/zerokey-sessions/zero-$N/users.json"

  SZ=$($SSH "$H188" "stat -c %s '$SRC' 2>/dev/null || echo 0")
  if [ "${SZ:-0}" -lt 1000 ]; then
    echo "❌ 188 上没有可用的源文件（$SRC，size=$SZ）—— 先跑 lane_seed_capture.sh $N"
    rc_all=1; continue
  fi
  echo "源: 188:$SRC  $SZ bytes"

  # 备份旧的（有就挪，没有就明说）
  $SSH "$H225" "sudo -n mkdir -p /Data/backups /Data/zerokey-sessions/zero-$N; \
    if [ -f '$DST' ]; then sudo -n cp -a '$DST' /Data/backups/zerokey-seed-$N-$TS-pre-install.json && \
      echo \"  备份旧 seed -> /Data/backups/zerokey-seed-$N-$TS-pre-install.json (\$(sudo -n stat -c %s '$DST') bytes)\"; \
    else echo '  无旧 seed（新号，不需要备份）'; fi"

  # 灌：字节走管道，不落本地磁盘
  $SSH "$H188" "cat '$SRC'" | $SSH "$H225" "sudo -n tee '$DST' >/dev/null && sudo -n chmod 600 '$DST'"

  # 判据三连（在 225 上就地验，不把内容传回来）
  $SSH "$H225" "sudo -n python3 - '$DST' '$N' '$SZ'" <<'PY'
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
done

echo
[ $rc_all -eq 0 ] && echo "全部灌装通过。" || echo "⚠️ 有号没过判据 —— 不带病入池，先修那几号。"
exit $rc_all
