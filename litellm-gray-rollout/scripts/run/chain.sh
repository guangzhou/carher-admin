#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。来源：2026-10-16~17 v1.105.1 升级逐字搬出，只把写死的
# run 目录 / 版本号 / 签字说明参数化。run 目录由 /root/l3/.gray-run-latest 指向（上一轮是 .up1051-latest）。
# 必需环境：$D/target.env（TARGET_TAG/TARGET_DIGEST/SRC/BACKUP_DIR）、$D/nginx/frozen-env.sh。
# 签字人与授权说明从 $D/signer.env 读（SIGNER=… / SIGN_NOTE=…），不在代码里写死。
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest})
bash $D/step.sh 50 5 && bash $D/step.sh 100 30
echo "CHAIN EXIT rc=$? at $(date -u +%FT%TZ)"
grep -E '^(phase|split)=' /etc/nginx/gray-route/active/state.env
bash $D/obs.sh 30
