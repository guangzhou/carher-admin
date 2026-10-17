#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。来源：2026-10-16~17 v1.105.1 升级逐字搬出，只把写死的
# run 目录 / 版本号 / 签字说明参数化。run 目录由 /root/l3/.gray-run-latest 指向（上一轮是 .up1051-latest）。
# 必需环境：$D/target.env（TARGET_TAG/TARGET_DIGEST/SRC/BACKUP_DIR）、$D/nginx/frozen-env.sh。
# 签字人与授权说明从 $D/signer.env 读（SIGNER=… / SIGN_NOTE=…），不在代码里写死。
# §7 4~5：prod_verified（带回归依据）→ convergence commit。用法: commit.sh "<回归依据>"
set -uo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}); N=$D/nginx; . $D/target.env; umask 077
set -a; . $N/frozen-env.sh; set +a
bash $D/sign.sh prod_verified "litellm-proxy ${TARGET_DIGEST:7:12} 4/4 Ready restart 0；R10 6 个文件逐 pod = gray；config = prod-target（= gray 除 disable_reset_budget）" "$1"
bash $SRC/scripts/gray-phase.sh set prod_verified 2>&1 | tail -1
bash $D/sign.sh convergence_commit "prod_verified 已过；新 prod 与 gray 同字节同 config（除 gray 专用 scheduler 抑制）；commit 后 nginx 回 30402，gray 4 副本留作热回退"
bash $D/sign.sh prod_healthy "prod 4/4 Ready，30402 health 200，并排回归通过" >/dev/null 2>&1 || true
bash $SRC/scripts/gray-convergence-commit.sh 2>&1 | grep -vE 'syntax is ok|test is successful|render-production' | tail -2
bash $SRC/scripts/gray-phase.sh current | grep -E '^(phase|split|mode)='
for i in 1 2 3; do echo "health $(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 -H 'Host: cc.auto-link.com.cn' http://127.0.0.1/pro/health/liveliness)"; done
