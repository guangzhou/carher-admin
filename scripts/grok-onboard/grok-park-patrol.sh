#!/bin/bash
# grok-park-patrol.sh -- 每分钟把被 sub2api park 掉的健康 grok 腿放回池子。
#
# 为什么需要它（2026-09-20 实测）：
#   sub2api 对**单条**上游 403 的反应是把整条腿 park 30 分钟。当天 12h 内上游只返了
#   18 条 403（成功 21020 条，占 0.086%），却换来 8 腿·小时的停机，并且从 09:00 起
#   制造了 8791 条 routing 503。13:17 手动 rescue 放开 10 条腿，**两分钟后只剩 1 条**
#   —— 放开的速度跟不上 park 的速度，手动救已经不成立。
#
# 只放探针实测 `200 usable` 的腿。⛔ 绝不放 `spending-limit` 那批：放出来只是多一条
#   腿吃 failover 再 403，09-20 就是这么把薄池子推成全池 503 的。
#
# 判据是 postgres 回读，不是端点的 200 —— clear-error / bulk-update 都属于
#   "返 200 可能没写" 那一类。rescue 自己带回读和收敛重试。
#
# 动了啥：只调 admin API 的 clear-error + bulk-update schedulable=true。
# 备份在哪：不改任何配置文件，无需备份。
# 怎么回滚：`sudo crontab -e` 删掉 BEGIN/END grok-park-patrol 那三行即可；
#           脚本本身无状态，删了就停，池子回到纯手动。

#
# 节奏（重要，改一处要改两处）：cron 只能到分钟，而 park 会在一分钟的间隔里把整池吃光
#   —— 13:26~13:30 每分钟跑一次时，池子在 11 条 ↔ 1 条之间来回跳，503 一直没停。
#   所以脚本自己在一分钟内循环 4 轮（SWEEPS × SLEEP_BETWEEN ≈ 60s），cron 每分钟起一次。
#   ⛔ 改 cron 的间隔必须同时改 SWEEPS，否则两轮会叠在一起（flock 会挡住，表现为"巡检不跑了"）。

set -uo pipefail

LOG=/var/log/grok-park-patrol.log
SCRIPT_DIR=/home/cltx/grok-onboard
PWFILE=/run/.s2apw-patrol
SWEEPS=4
SLEEP_BETWEEN=15

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

cleanup() { rm -f "$PWFILE"; }
trap cleanup EXIT

cd "$SCRIPT_DIR" || { log "FATAL cannot cd $SCRIPT_DIR"; exit 1; }

# 密码从 secret 现取，不落盘在家目录，跑完即删。
if ! kubectl -n litellm-dev get secret sub2api-secrets \
       -o jsonpath='{.data.ADMIN_PASSWORD}' 2>>"$LOG" | base64 -d > "$PWFILE"; then
  log "FATAL cannot read sub2api-secrets"
  exit 1
fi
chmod 600 "$PWFILE"

for sweep in $(seq 1 "$SWEEPS"); do
  [ "$sweep" -gt 1 ] && sleep "$SLEEP_BETWEEN"

  OUT=$(S2A_PW_FILE="$PWFILE" python3 sub2api-grok-onboard.py rescue --minutes 5 2>&1)
  RC=$?

  # 只在真放了腿、或者出错时写日志，避免日志被"无事可做"刷满。
  FREED=$(printf '%s' "$OUT" | sed -n 's/^=== freeing \(.*\) ===$/\1/p')
  STILL=$(printf '%s' "$OUT" | grep -c 'STILL HELD')
  OPEN=$(printf '%s' "$OUT" | grep -c 'parked=False')

  if [ "$RC" -ne 0 ] && [ -z "$FREED" ]; then
    # rescue 在"放了腿但仍被重新 park"时也返非 0，那是预期的（池子级 park 会立刻改写），
    # 不该当故障刷全量输出。只有连 freeing 段都没有才是真出错。
    log "sweep=$sweep rc=$RC -- rescue failed, full output follows"
    printf '%s\n' "$OUT" >> "$LOG"
  elif [ -n "$FREED" ]; then
    log "sweep=$sweep freed=$FREED ok=$OPEN still_held=$STILL"
  fi
done

exit 0
