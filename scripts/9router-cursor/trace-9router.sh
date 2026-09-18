#!/bin/sh
# 9router 侧日志量具 —— 判"上游到底回没回"、"工具链有没有在死转"。
#
# 在 198 上跑(有 kubectl)。经 jms:
#   jms scp scripts/9router-cursor/trace-9router.sh 10.68.13.198:/tmp/tmp_trace.sh
#   jms ssh 10.68.13.198 'sh /tmp/tmp_trace.sh'
#
# ✅ 好尺子:
#   POST_count == DONE_count      每个请求都拿到了结束帧
#   restarts=0                    没崩
#   declin_count=0                没有 decline 通道在报错
#   sessions_history_count=0      ★关键★ 非 0 = 模型在"找不到我的任务"里死转
#                                 (read SKILL.md → sessions_history → read → ...)
#   📊 DONE <ms> · IN <n> · OUT <n> 且 OUT>0 且客户端 body 非空
#
# ⛔ 第三条坏尺子:POST=0 一律读成"没有流量"。账号池全腿冷却时,请求在选号阶段
#   (chat.js:234)就 return 了,而 `▶ POST` 是 chatCore(chat.js:268)才打 ⇒ POST 也是 0。
#   两种情况处置完全相反,所以本脚本用 all_locked_count 把它们分开。
#
# ⛔ 坏尺子(已作废,别再拿它判红):
#   `iu.field 13 raw(0B)` / `stream ended by server`
#   健康 pod 上实测 6 行 raw(0B) 与 4 行成功 DONE 共存 ⇒ 零判别力。
#   见 memory feedback_cursor_agent_raw0b_is_benign_judge_by_done_out。
#
# ⛔ 另一条坏尺子:pod Running / 容器 healthy。掐流时它照样绿。

set -eu
NS="${NS:-litellm-product}"
TAIL="${TAIL:-4000}"

POD="$(kubectl -n "$NS" get pod -l app=9router -o jsonpath='{.items[0].metadata.name}')"
echo "NS=$NS"
echo "POD=$POD"
echo "IMAGE=$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.spec.containers[0].image}')"
kubectl -n "$NS" get pod "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}{"\n"}' | sed 's/^/restarts=/'

L="$(kubectl -n "$NS" logs "$POD" --tail="$TAIL" 2>/dev/null || true)"

P=$(echo "$L" | grep -c '▶ POST' || true)
D=$(echo "$L" | grep -c '📊 DONE' || true)
echo "POST_count=$P"
echo "DONE_count=$D"
echo "OUT0_count=$(echo "$L" | grep -c 'OUT 0' || true)"
E=$(echo "$L" | grep -c '✗ ERROR' || true)
FO=$(echo "$L" | grep -c 'NEXT ACCOUNT' || true)
STALL=$(echo "$L" | grep -c 'cursor stall' || true)
echo "ERROR_count=$E"
echo "failover_count=$FO   # 换腿次数;>0 不是故障,是账号池在干活"
echo "stall_count=$STALL   # 首帧死线触发次数(pool-20260918a 起)"
# ★ 池子空掉的形状:这条路在 chat.js:234 选号失败后**直接 return**,
#   而 `▶ POST` 是 chatCore(chat.js:268)才打的 ⇒ **一行 POST 都不会有**。
#   拿 POST 计数去判,会读成 NO_TRAFFIC(=「你的请求没走到 9router」),完全带错方向。
LOCKED=$(echo "$L" | grep -c 'accounts locked for' || true)
NOMORE=$(echo "$L" | grep -c 'No more accounts available' || true)
NOCRED=$(echo "$L" | grep -c 'No active credentials for provider' || true)
echo "all_locked_count=$LOCKED   # 全腿都在冷却窗口里(客户端收到带 reset 时间的 503)"
echo "no_more_accts_count=$NOMORE # 本轮把所有腿都换过一遍还是不行"
echo "no_active_cred_count=$NOCRED # 该 provider 一条活腿都没配(不是冷却,是没号)"
echo "declin_count=$(echo "$L" | grep -ci 'declin' || true)"
echo "sessions_history_count=$(echo "$L" | grep -c 'sessions_history' || true)"
echo "raw0B_count=$(echo "$L" | grep -c 'raw(0B)' || true)   # 仅供参考,不是判据"
echo "FMT_lines=$(echo "$L" | grep -c 'FMT:' || true)         # 确认 translator 走的哪条:openai→cursor"

echo "--- last 30 relevant ---"
echo "$L" | grep -E '▶ POST|📊 DONE|mcp_args|declin|ERR|FMT:|NEXT ACCOUNT|accounts locked for|No more accounts|No active credentials' | tail -30

echo
# ⚠️ 判据已从 `POST==DONE` 改成 `POST == DONE+ERROR`(2026-09-18,pool-20260918a)。
#    账号池的首帧死线会把"挂死的腿"变成一次真失败并换腿 ⇒ 一次**成功**的换腿必然留下
#    POST(坏腿) + ✗ERROR 504 + POST(好腿) + DONE。拿旧的 POST==DONE 去判,
#    每换一次腿都会读成 STALLED ——那是尺子过期,不是死转。
if [ "$P" -eq 0 ] && [ "$((LOCKED + NOCRED))" -gt 0 ]; then
  echo "VERDICT=POOL_EMPTY — 请求到了,但选号阶段就被挡住(locked=$LOCKED no_cred=$NOCRED),"
  echo "                   所以一行 ▶POST 都没有。⛔ 这不是 NO_TRAFFIC。"
  echo "                   locked>0 = 等锁到期或加号;no_cred>0 = 这个 provider 没有活腿。"
elif [ "$P" -eq 0 ]; then
  echo "VERDICT=NO_TRAFFIC — 这个窗口里没有请求打进来。先确认你的请求真的走到了 9router(别在本机发)。"
  echo "                   ⚠️ 也要看一眼上面 all_locked_count:全腿冷却时同样 POST=0。"
elif [ "$P" -eq "$((D + E))" ]; then
  if [ "$FO" -gt 0 ]; then
    echo "VERDICT=OK  (POST==DONE+ERROR;发生过 $FO 次换腿,客户端仍被兜住)"
  else
    echo "VERDICT=OK  (POST==DONE)"
  fi
else
  echo "VERDICT=STALLED  POST=$P DONE=$D ERROR=$E — 有请求既没结束帧也没错误行"
fi
