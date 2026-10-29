#!/usr/bin/env bash
# myai-198-key-sync.sh —— 让 243 myai 的 LITELLM_ADMIN_KEY 跟上 198 生产现役 master key。
#
# 为什么有这个脚本：2026-10-28 09:41Z 198 master key 轮换到 secret/litellm-secrets-r2，
# myai 的 dashboard-secrets 里还是旧 key ⇒ 拿 198 用量全部 403，看板 primary 停更约 8 小时，没人报警。
#
# 用法（在本机跑，需要能 ssh 198 和 243）:
#   HOST243_PW='...' scripts/myai-198-key-sync.sh check   # 只比 sha256 前缀，不改；不一致 exit 1
#   HOST243_PW='...' scripts/myai-198-key-sync.sh apply   # 不一致时：先从 243 实打新 key → 备份 secret → patch → 逐个 rollout
#
# 全程不打印 key：只在 ssh stdin 里传，比较用 sha256 前 12 位。
# 新 key 要先从 243 打 cc.auto-link.com.cn/pro 的 /model/info 和 /spend/logs/v2 都 200 才会写入。
set -euo pipefail

MODE="${1:-check}"
case "$MODE" in check|apply) ;; *) echo "用法: $0 check|apply" >&2; exit 2 ;; esac
PW="${HOST243_PW:-${MYAI_SSH_PASSWORD:-}}"
[ -n "$PW" ] || { echo "需要环境变量 HOST243_PW" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
H198="${HOST198:-cltx@10.68.13.198}"
H243="${HOST243_ADDR:-cltx@10.68.13.243}"
NS=ai-token-dashboard
SSHO=(-o ConnectTimeout=30 -o ClearAllForwardings=yes -o StrictHostKeyChecking=no)

TMP="$(mktemp)"; chmod 600 "$TMP"; trap 'rm -f "$TMP"' EXIT

# 1) 198 现役 key（跟着 deploy envFrom 找，并在 198 本机实打 /model/info）
ssh "${SSHO[@]}" "$H198" 'bash -s' < "$HERE/litellm-198-master-key.sh" > "$TMP"
printf '\n' >> "$TMP"
NEW_H=$(head -1 "$TMP" | tr -d '\n' | shasum -a 256 | cut -c1-12)
# 阴性对照用：MYAI_KEYSYNC_FAKE_DRIFT=1 时假装 198 换了 key，只对 check 生效
[ "${MYAI_KEYSYNC_FAKE_DRIFT:-}" = 1 ] && [ "$MODE" = check ] && NEW_H="fake00000000"
[ "$(head -1 "$TMP" | tr -d '\n' | wc -c | tr -d ' ')" -ge 20 ] || { echo "STOP 198 取到的 key 太短，放弃" >&2; exit 3; }

r243() { SSHPASS="$PW" sshpass -e ssh "${SSHO[@]}" "$H243" "$@"; }
retry243() { local i; for i in 1 2 3; do r243 "$@" && return 0; sleep 5; done; return 1; }

CUR_H=$(retry243 "kubectl -n $NS get secret dashboard-secrets -o jsonpath='{.data.LITELLM_ADMIN_KEY}' | base64 -d | sha256sum | cut -c1-12")
echo "198 现役 key sha=${NEW_H}  myai 当前 sha=${CUR_H}"
if [ "$NEW_H" = "$CUR_H" ]; then echo "OK 一致，无需处理"; exit 0; fi
if [ "$MODE" = check ]; then echo "DRIFT myai 的 key 不是 198 现役 key（跑 apply 修）"; exit 1; fi

# 2) 从 243 实打（myai 用的就是这个地址）
CODES=$(retry243 'IFS= read -r K; [ ${#K} -ge 20 ] || { echo "000 000"; exit 0; }; S=$(date -u -d "-2 min" "+%Y-%m-%d+%H%%3A%M%%3A%S"); E=$(date -u "+%Y-%m-%d+%H%%3A%M%%3A%S");
  a=$(curl -s -o /dev/null -w "%{http_code}" -m 30 https://cc.auto-link.com.cn/pro/model/info -H "Authorization: Bearer $K");
  b=$(curl -s -o /dev/null -w "%{http_code}" -m 30 "https://cc.auto-link.com.cn/pro/spend/logs/v2?start_date=$S&end_date=$E&page=1&page_size=1" -H "Authorization: Bearer $K");
  echo "$a $b"' < "$TMP")
echo "新 key 从 243 实打: model/info+spend/logs/v2 = ${CODES}"
[ "$CODES" = "200 200" ] || { echo "STOP 新 key 从 243 打不通，不写入" >&2; exit 4; }

# 3) 先等同步安全窗口，再写 key：写完马上要重启，同步发布阶段拿着长事务锁，这时换 pod 会被 liveness 杀。
#    必须在写之前等：写完再失败，下次 apply 看到 sha 一致就不会再重启，新 key 永远没加载。
"$HERE/myai-wait-sync-idle.sh" 2400 || { echo "STOP 等不到同步安全窗口，什么都没改" >&2; exit 5; }
# 3b) 备份 → patch → 回读
# 🔴 key 经 stdin 传：sshpass + stdin 偶发传成空（10-29 一次 cat > file 落了 0 字节）⇒ 远端先校验长度+sha 再写
r243 "set -e; IFS= read -r K; [ \${#K} -ge 20 ] || { echo 'STOP 远端收到的 key 为空/过短，未写入' >&2; exit 7; };
  [ \$(printf %s \"\$K\" | sha256sum | cut -c1-12) = $NEW_H ] || { echo 'STOP 远端收到的 key 与 198 不一致，未写入' >&2; exit 7; };
  B=/Data/migration/dashboard-secrets.pre-keysync.\$(date -u +%Y%m%dT%H%M%S).yaml;
  kubectl -n $NS get secret dashboard-secrets -o yaml > \$B; chmod 600 \$B; echo backup=\$B;
  E=\$(printf %s \"\$K\" | base64 -w0);
  kubectl -n $NS patch secret dashboard-secrets --type=json -p \"[{\\\"op\\\":\\\"replace\\\",\\\"path\\\":\\\"/data/LITELLM_ADMIN_KEY\\\",\\\"value\\\":\\\"\$E\\\"}]\" >/dev/null;
  echo after=\$(kubectl -n $NS get secret dashboard-secrets -o jsonpath='{.data.LITELLM_ADMIN_KEY}' | base64 -d | sha256sum | cut -c1-12)" < "$TMP"

# 4) 逐个 rollout（worker 同时起会在 apply_usage_schema 互锁），放到 243 后台，免得 ssh 断开带断。
#    主看板 ai-token-dashboard 10-29 起 0 副本，不重启（rollout restart 会等一个不存在的 pod）。
r243 "nohup bash -c 'for d in ai-token-dashboard-chat usage-realtime-worker usage-sync-worker; do echo == \$d; kubectl -n $NS rollout restart deploy/\$d && kubectl -n $NS rollout status deploy/\$d --timeout=300s; sleep 40; done; echo DONE' > /tmp/myai-keysync-rollout.log 2>&1 &"
for _ in $(seq 1 40); do
  sleep 15
  out=$(retry243 'cat /tmp/myai-keysync-rollout.log' 2>/dev/null || true)
  grep -q DONE <<<"$out" && break
done
echo "$out" | grep -E "^==|successfully|DONE|error" || true
grep -q DONE <<<"$out" || { echo "STOP rollout 10 分钟没跑完，看 243 /tmp/myai-keysync-rollout.log" >&2; exit 5; }

# 5) 验收：新 pod 连 198 不再 403
sleep 30
N403=$(retry243 "kubectl -n $NS logs deploy/usage-realtime-worker --since=1m | grep -c 'cc.auto-link.com.cn.* 403' || true")
echo "realtime worker 近 1 分钟 198 403 次数=${N403}"
[ "${N403:-1}" = 0 ] || { echo "STOP 换完仍有 403" >&2; exit 6; }
echo "OK 已同步"
