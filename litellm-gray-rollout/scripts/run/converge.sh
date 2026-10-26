#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。来源：2026-10-16~17 v1.105.1 升级逐字搬出，只把写死的
# run 目录 / 版本号 / 签字说明参数化。run 目录由 /root/l3/.gray-run-latest 指向（上一轮是 .up1051-latest）。
# 必需环境：$D/target.env（TARGET_TAG/TARGET_DIGEST/SRC/BACKUP_DIR）、$D/nginx/frozen-env.sh。
# 签字人与授权说明从 $D/signer.env 读（SIGNER=… / SIGN_NOTE=…），不在代码里写死。
# §7 收敛：split=100 稳定后，prod 原地升级到 gray 同字节，然后 commit。任何一步不过 ⇒ 停（nginx 仍在 gray，prod 不动或可 rollback）。
set -uo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}); N=$D/nginx; . $D/target.env; P=$D/prod-target; G=$D/gray; umask 077
set -a; . $N/frozen-env.sh; set +a
A=/etc/nginx/gray-route/active; CHART=$(ls $D/chart3/*.tgz)
die(){ echo "STOP: $*"; exit 1; }
[ "$(awk -F= '$1=="split"{print $2}' $A/state.env)" = 100 ] || die "split != 100"
[ -z "$(grep -vE '^\s*(#|$)' $A/force-prod.map)" ] || die "force-prod 非空"
GO=$(kubectl -n litellm-product get deploy litellm-proxy-guarded-old -o jsonpath='{.status.readyReplicas}/{.spec.replicas}'); [ "$GO" = 2/2 ] || die "guarded-old $GO"
BASIS_STABLE="$1"
bash $D/sign.sh convergence_stable "$BASIS_STABLE"
bash $D/sign.sh convergence_bridge "guarded-old ${GO_VERSION:-上一版} $GO Ready，30406 liveliness $(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:30406/health/liveliness)"
bash $D/sign.sh convergence_control_plane "三车道有效 router ${ROUTER_COUNTS:?先量三车道 /router/settings 条数+hash} 同 hash（gray vs prod 逐项相同）；DB 已迁移 ${DB_MIGRATIONS:?生产迁移后条数} 条，旧版读写已验" "gray/guarded-old scheduler disabled，prod 持后台任务；收敛窗口远离 16:00 UTC 额度重置" "收敛期 /pro/ 全到 gray（同库）"
bash $D/sign.sh convergence_bypass_disposition "ClusterIP litellm-proxy 集群内消费者经 RollingUpdate maxUnavailable=0 + preStop 30s + grace 600 不断服" "NetworkPolicy 与 live 相同（10.42.0-3.0/24、10.68.13.0/24、10.68.10.37/32、SNAT /32）"
bash $SRC/scripts/gray-convergence-prepare.sh 2>&1 | grep -vE 'syntax is ok|test is successful|render-production' | tail -2
sleep 120
n=$(awk -v cut="$(date -d '100 seconds ago' +%Y-%m-%dT%H:%M:%S)" '{split($1,a,"=");if(substr(a[2],1,19)<cut)next;if($0~/upstream=127.0.0.1:30402/)c++}END{print c+0}' /var/log/nginx/cc-auto-link.gray.log)
g=$(awk -v cut="$(date -d '100 seconds ago' +%Y-%m-%dT%H:%M:%S)" '{split($1,a,"=");if(substr(a[2],1,19)<cut)next;if($0~/upstream=127.0.0.1:30405/)c++}END{print c+0}' /var/log/nginx/cc-auto-link.gray.log)
echo "after prepare: nginx->30402=$n nginx->30405=$g"
[ "$n" -eq 0 ] || die "nginx 仍有请求到 30402 ($n)"
bash $D/sign.sh prod_zero "convergence mode=1 后 100 秒：nginx→30402 = 0，→30405 = ${g}；force-prod 空" "旧 prod 余下仅 ClusterIP 集群内消费者（非 nginx），RollingUpdate maxUnavailable=0 不断服"
bash $SRC/scripts/gray-phase.sh set prod_offline_upgrading 2>&1 | tail -1
TS=$(date -u +%Y%m%dT%H%M%SZ); B=$P/backup-$TS; mkdir -p $B
helm -n litellm-product get values litellm-product-proxy -o yaml > $B/helm-values.yaml
helm -n litellm-product get manifest litellm-product-proxy > $B/helm-manifest.yaml
kubectl -n litellm-product get deploy litellm-proxy -o yaml > $B/deploy.yaml
kubectl -n litellm-product get svc litellm-proxy litellm-proxy-nodeport -o yaml > $B/svc.yaml
echo "prod helm rev before: $(helm -n litellm-product history litellm-product-proxy --max 1 -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)[-1]["revision"])')  backup=$B"
helm -n litellm-product upgrade litellm-product-proxy $CHART --values $P/prod-target-values.yaml --reset-values 2>&1 | grep -E 'REVISION|STATUS|Error'
kubectl -n litellm-product rollout status deploy/litellm-proxy --timeout=2400s | tail -1 || die "prod rollout 未完成（nginx 仍全在 gray）"
sleep 30
# R10：逐 pod 字节 = gray
S=/app/.venv/lib/python3.13/site-packages/litellm
want=$(kubectl -n litellm-product exec $(kubectl -n litellm-product get po -l app=litellm-proxy-gray --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}') -c litellm -- sh -c "sha256sum $S/litellm_core_utils/streaming_handler.py $S/proxy/pass_through_endpoints/streaming_handler.py $S/proxy/pass_through_endpoints/llm_provider_handlers/anthropic_passthrough_logging_handler.py /app/register_pricing.py $S/proxy/auth/auth_checks.py $S/proxy/proxy_server.py | cut -c1-12 | tr '\n' ' '")
bad=0
for p in $(kubectl -n litellm-product get po -l app=litellm-proxy --field-selector=status.phase=Running -o json | python3 -c 'import json,sys;print(" ".join(i["metadata"]["name"] for i in json.load(sys.stdin)["items"] if not i["metadata"].get("deletionTimestamp")))'); do
  got=$(kubectl -n litellm-product exec $p -c litellm -- sh -c "sha256sum $S/litellm_core_utils/streaming_handler.py $S/proxy/pass_through_endpoints/streaming_handler.py $S/proxy/pass_through_endpoints/llm_provider_handlers/anthropic_passthrough_logging_handler.py /app/register_pricing.py $S/proxy/auth/auth_checks.py $S/proxy/proxy_server.py | cut -c1-12 | tr '\n' ' '")
  img=$(kubectl -n litellm-product get po $p -o jsonpath='{.spec.containers[0].image}')
  [ "$got" = "$want" ] && [[ $img == *${TARGET_DIGEST#sha256:}* ]] && echo "  R10 $p OK" || { echo "  R10 $p BAD got=$got img=$img"; bad=1; }
done
[ $bad = 0 ] || die "R10 prod 字节 ≠ gray"
echo "R10 want=$want"
IP=$(kubectl -n litellm-product get deploy litellm-proxy -o jsonpath='{.spec.template.spec.containers[0].image}')
GW=$A/workload.env
g_chart=$(awk -F= '$1=="release_litellm-product-gray_chart_package_sha256"{print $2}' $GW); g_values=$(awk -F= '$1=="release_litellm-product-gray_values_sha256"{print $2}' $GW); g_image=$(awk -F= '$1=="release_litellm-product-gray_image_digest"{print $2}' $GW)
o_chart=$(awk -F= '$1=="release_litellm-product-guarded-old_chart_package_sha256"{print $2}' $GW); o_values=$(awk -F= '$1=="release_litellm-product-guarded-old_values_sha256"{print $2}' $GW); o_image=$(awk -F= '$1=="release_litellm-product-guarded-old_image_digest"{print $2}' $GW)
bash $SRC/scripts/gray-workload-pin.sh --reason "section 7: litellm-proxy upgraded in place to ${TARGET_DIGEST:7:12} (= gray bytes)" \
  --release litellm-product-gray --chart-package-sha256 $g_chart --values-sha256 $g_values --image-digest $g_image \
  --release litellm-product-proxy --chart-package-sha256 $(sha256sum $CHART | cut -d' ' -f1) --values-sha256 $(sha256sum $P/prod-target-values.yaml | cut -d' ' -f1) --image-digest ${IP#*@} \
  --release litellm-product-guarded-old --chart-package-sha256 $o_chart --values-sha256 $o_values --image-digest $o_image \
  --shape-evidence $P/prod-pod-spec-shape.json 2>&1 | grep -E '^workload_checksum'
echo "prod pods: $(kubectl -n litellm-product get po -l app=litellm-proxy --no-headers | awk '{print $2,$4}' | sort | uniq -c | tr '\n' ' ')"
echo "30402 health: $(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:30402/health/liveliness)"
echo "READY_FOR_PROD_VERIFIED (run regression against 30402, then sign prod_verified + commit)"
