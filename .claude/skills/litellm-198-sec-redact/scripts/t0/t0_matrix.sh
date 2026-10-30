#!/bin/bash
# 在 198 上跑 sec_redact 全链 T0 的三种模式（ON / SHADOW / DISABLED 阳性对照）+ SpendLogs 落库检查。
# 只动本地 docker 容器 t0sec-*，不碰集群。
set -uo pipefail
cd /home/cltx/t0-sec-redact
MIP=$(sudo -n docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' t0sec-mock)
SECRET_TAIL='a1B2c3D4e5F6g7H8i9J0kLmNoPqRsTuV'

restart_with() {  # $1 = 额外 env（空 = 正常模式）
  sed -i 's/ -e SEC_REDACT_DISABLED=1//; s/ -e SEC_REDACT_SHADOW=1//' run_t0.sh
  if [ -n "${1}" ]; then
    sed -i "s/-e LITELLM_MASTER_KEY=sk-t0master/-e LITELLM_MASTER_KEY=sk-t0master -e ${1}/" run_t0.sh
  fi
  sudo -n docker rm -f t0sec-litellm >/dev/null 2>&1
  for _ in $(seq 1 30); do sudo -n docker inspect t0sec-litellm >/dev/null 2>&1 || break; sleep 1; done
  sudo -n bash -c "$(grep 'name t0sec-litellm' run_t0.sh)" >/dev/null
  for _ in $(seq 1 72); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:14100/health/liveliness)" = 200 ] && return 0
    sleep 5
  done
  echo "litellm not ready"; return 1
}

spendlogs() {
  sudo -n docker exec -i t0sec-db psql -U litellm -d litellm -At <<SQL
select case when proxy_server_request::text like '%[密钥凭证脱敏]%' then 'body=redacted'
            when proxy_server_request::text like '%${SECRET_TAIL}%' then 'body=RAW'
            else 'body=no-key' end, count(*)
from "LiteLLM_SpendLogs" where call_type not like '/%' group by 1;
SQL
}

clear_spendlogs() {
  sudo -n docker exec t0sec-db psql -U litellm -d litellm -qAt -c 'delete from "LiteLLM_SpendLogs"'
}

echo "### MODE=ON"
restart_with "" && clear_spendlogs
python3 t0_sec_redact.py --base http://127.0.0.1:14100 --master sk-t0master --mock "http://${MIP}:9999" | grep -E 'FAIL|/[0-9]+ PASS'
echo "proxy errors: $(sudo -n docker logs t0sec-litellm 2>&1 | grep -cE 'sec_redact: ERROR|Traceback')"
echo "raw secret in proxy log: $(sudo -n docker logs t0sec-litellm 2>&1 | grep -c "${SECRET_TAIL}")"
sleep 40; echo "SpendLogs (ON):"; spendlogs

echo "### MODE=SHADOW"
restart_with "SEC_REDACT_SHADOW=1"
python3 t0_sec_redact.py --base http://127.0.0.1:14100 --master sk-t0master --mock "http://${MIP}:9999" --shadow | grep -E 'FAIL|/[0-9]+ PASS'
echo "shadow audit lines: $(sudo -n docker logs t0sec-litellm 2>&1 | grep -c 'action=shadow')"

echo "### MODE=DISABLED (positive control: must FAIL)"
restart_with "SEC_REDACT_DISABLED=1" && clear_spendlogs
python3 t0_sec_redact.py --base http://127.0.0.1:14100 --master sk-t0master --mock "http://${MIP}:9999" | grep -E '/[0-9]+ PASS'
sleep 40; echo "SpendLogs (DISABLED):"; spendlogs

restart_with ""
echo "### done (left in MODE=ON)"
