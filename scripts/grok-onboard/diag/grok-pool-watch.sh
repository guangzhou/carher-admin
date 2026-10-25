#!/usr/bin/env bash
# grok-pool-watch.sh — 每分钟采一行 CSV，判 sub2api grok 池「整池没腿可选」的频率与成因。
#
#   ./grok-pool-watch.sh                      # 采到 END_HHMM（默认 1830），CSV+事件落当前目录
#   OUT=before END_HHMM=1830 ./grok-pool-watch.sh    # 改前
#   OUT=after  END_HHMM=2330 ./grok-pool-watch.sh    # 改后，两份 CSV 直接对比
#
# 为什么要它（2026-10-22）：「grok 又挂了」用 health 看不出来 —— health 显示「有腿在服务」，
# 而真相是每隔几分钟整池 503 两三分钟。只有**分钟级**把 服务数 / 没腿数 / 429 并排，
# 才看得出 503 紧跟在某个模型的 429 后面（那一轮因此定出 x.ai 每号每模型每秒 2 个的限制）。
#
# 每行：bj_min,served,no_leg,r429,r429_ma,<每条在服务的腿>,keys
#   served  = usage_logs 里真成功的请求数（唯一能证明「腿在服务」的量，schedulable 是坏尺子）
#   no_leg  = ops_error_logs 里 error_phase=routing（请求没出网，整池选不出腿）
#   r429_ma = 其中 multi-agent 的 429（本轮的触发源，留着做同族排查）
# no_leg >= FREEZE_MIN 的那一分钟记进事件文件，并带上前 3 分钟的 429 明细（因果同行）。
#
# 🔴 只读（全是 SELECT）。🔴 腿的列**现量**（取最近 2h 真发过请求的 account_id），不写死号：
#    写死 163/164 这种，换了号就恒读 0，像「这腿没流量」的假绿。
# ⚠️ 时间：`date` 用本机时区（= 北京）；库里 created_at 带 tz，用 at time zone 转。
set -u
cd "$(dirname "$0")"
END_HHMM="${END_HHMM:-1830}"
FREEZE_MIN="${FREEZE_MIN:-20}"
OUT="${OUT:-peak-watch}"
CSV="${OUT}.csv"
EV="${OUT}-events.log"
H198="${S2A_HOST:-cltx@10.68.13.198}"

q() {
  ssh -o ConnectTimeout=10 -o BatchMode=yes "${H198}" \
    'set -- $(sudo -n kubectl get pod -A -l app=sub2api-postgres --field-selector=status.phase=Running -o jsonpath="{.items[0].metadata.namespace} {.items[0].metadata.name}"); sudo -n kubectl -n $1 exec -i $2 -- psql -U sub2api -t -A -F, -f -' 2>/dev/null
}

# 现量在服务的腿：最近 2h 有 usage_logs 行的 grok 账号
LEGS=$(q <<'SQL' | tr -d ' '
select string_agg(distinct u.account_id::text, ',' order by u.account_id::text)
from usage_logs u join accounts a on a.id = u.account_id
where a.platform = 'grok' and a.deleted_at is null
  and u.created_at > now() - interval '2 hour';
SQL
)
[ -z "${LEGS}" ] && LEGS="(none)"
echo "legs with traffic in the last 2h: ${LEGS}"

# 每条腿一列
LEG_COLS=""
LEG_HDR=""
if [ "${LEGS}" != "(none)" ]; then
  IFS=','
  for id in ${LEGS}; do
    LEG_HDR="${LEG_HDR},leg${id}"
    LEG_COLS="${LEG_COLS},
 (select count(*) from usage_logs u where u.account_id=${id} and u.created_at>=w.s and u.created_at<w.s+interval '1 minute')"
  done
  unset IFS
fi

[ -s "${CSV}" ] || echo "bj_min,served,no_leg,r429,r429_ma${LEG_HDR},keys" > "${CSV}"

while :; do
  now=$(date +%H%M)
  [ "${now}" -ge "${END_HHMM}" ] && { echo "END ${now} reached ${END_HHMM}"; break; }
  row=$(q <<SQL
with w as (select date_trunc('minute', now()) - interval '1 minute' s)
select to_char(w.s at time zone 'Asia/Shanghai','HH24:MI'),
 (select count(*) from usage_logs u join accounts a on a.id=u.account_id where a.platform='grok' and u.created_at>=w.s and u.created_at<w.s+interval '1 minute'),
 (select count(*) from ops_error_logs e where e.platform='grok' and e.error_phase='routing' and e.created_at>=w.s and e.created_at<w.s+interval '1 minute'),
 (select count(*) from ops_error_logs e where e.platform='grok' and e.status_code=429 and e.created_at>=w.s and e.created_at<w.s+interval '1 minute'),
 (select count(*) from ops_error_logs e where e.platform='grok' and e.status_code=429 and coalesce(e.requested_model,e.model) ilike '%multi-agent%' and e.created_at>=w.s and e.created_at<w.s+interval '1 minute')${LEG_COLS},
 (select count(distinct u.api_key_id) from usage_logs u join accounts a on a.id=u.account_id where a.platform='grok' and u.created_at>=w.s and u.created_at<w.s+interval '1 minute')
from w;
SQL
)
  if [ -z "${row}" ]; then
    echo "$(date +%H:%M) QUERY_FAILED (ssh/psql returned nothing)" | tee -a "${EV}"
    sleep 60; continue
  fi
  echo "${row}" >> "${CSV}"
  no_leg=$(echo "${row}" | cut -d, -f3)
  if [ "${no_leg:-0}" -ge "${FREEZE_MIN}" ]; then
    ctx=$(q <<'SQL'
select string_agg(account_id::text||':'||coalesce(requested_model,model)||'@'||to_char(created_at at time zone 'Asia/Shanghai','HH24:MI:SS'), ' ')
from ops_error_logs where platform='grok' and status_code=429 and created_at > now() - interval '4 minute';
SQL
)
    echo "FREEZE ${row} | 429s in prior 3min: ${ctx:-none}" | tee -a "${EV}"
  fi
  sleep 60
done
