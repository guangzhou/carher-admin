#!/usr/bin/env bash
# OWUI 镜像升级。默认 dry-run(只做检查),--apply 才动。
#
#   owui-upgrade.sh v0.11.5            # 检查: 拉镜像进本地 registry / 镜像里 pypatch 挂点 / 残留 Pod
#   owui-upgrade.sh v0.11.5 --apply --announced  # 🔴 Recreate 单副本 ⇒ 站点硬中断 ~1~2 分钟
#   ... --set-env K=V [--set-env K=V]   # 需要同时改的 env,跟换镜像合成一次重启
# --announced = 已经跟用户说过「会断 1~2 分钟」。不带它 --apply 直接拒(这句话我漏说过三次)。
#
# 为什么要查 pypatch 挂点: PVC 上 pypatch/sitecustomize.py 换掉的是三个模块属性,
# 新版改名/挪位置后补丁会静默失效(不报错,只是截断不再生效)。
# 连接: 默认 ssh key + sudo -n; 设了 P198 才走密码(见 _198.sh)。
# 验收里的 gate audit 会发一条非流式 chat —— 10-24 v0.12.3 就是它抓到反代裸 gzip, 别删。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/_198.sh"

NEW=""; APPLY=0; ANNOUNCED=0; ENVS=()
while [ $# -gt 0 ]; do
  case $1 in
    --apply) APPLY=1; shift ;;
    --announced) ANNOUNCED=1; shift ;;
    --set-env) ENVS+=("$2"); shift 2 ;;
    v*) NEW=$1; shift ;;
    *) sed -n '2,13p' "$0"; exit 2 ;;
  esac
done
[ -n "$NEW" ] || { sed -n '2,13p' "$0"; exit 2; }

fail=0
chk() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi; }

echo "== 当前"
r198 "kubectl get deploy open-webui -n open-webui -o jsonpath='{.spec.template.spec.containers[0].image} strategy={.spec.strategy.type} replicas={.spec.replicas}'; echo"
curl -sS --max-time 15 http://10.68.13.198:30880/api/version 2>/dev/null || true; echo

echo "== 拉镜像到 198 本地 registry(不影响线上)"
# 不用 docker pull/tag/push: 198 docker 29 推多架构镜像必失败, 原因见那个脚本头注释
"$HERE/../../../../scripts/host-198-registry-mirror.sh" \
  "ghcr.nju.edu.cn/open-webui/open-webui:$NEW" "127.0.0.1:5000/open-webui:$NEW" \
  || { echo "FAIL 拉/推镜像失败(版本号写错?)"; exit 3; }

echo "== 新镜像里的 pypatch 挂点 ($NEW) —— 直接 grep 要上线的那份文件"
HOOKS=$(r198 "docker run --rm --entrypoint sh 127.0.0.1:5000/open-webui:$NEW -c '
cd /app/backend/open_webui
grep -c \"^def convert_output_to_messages\" utils/misc.py
grep -cE \"^    convert_output_to_messages,\" utils/middleware.py
grep -c \"^def get_source_context\" utils/middleware.py
grep -c \"OAUTH_PROVIDERS\\[.feishu.\\]\" config.py
grep -c \"provider.get(.name., name)\" main.py' 2>/dev/null || true" | tr '\n' ' ')
echo "hits: $HOOKS"
read -r -a H <<< "$HOOKS"
NAMES=("misc.convert_output_to_messages" "middleware 从 misc 导入 convert_output" "middleware.get_source_context" "config.OAUTH_PROVIDERS['feishu']" "main 用 provider.get('name') 出按钮名")
for i in 0 1 2 3 4; do chk "${NAMES[$i]}" "[ ${H[$i]:-0} -ge 1 ]"; done

echo "== 残留 Pod(Recreate 会被非 Running 的老 Pod 永久卡在 0 副本)"
r198 "kubectl get pod -n open-webui -l app=open-webui --no-headers"
# phase=Succeeded/Failed 的已退出 Pod 不接流量, --apply 时自动删(不算违反「禁删服务中 Pod」);
# 其他非 Running(Pending/CrashLoop 等)才算真问题, 拦住。
DEAD=$(r198 "kubectl get pod -n open-webui -l app=open-webui --field-selector=status.phase!=Running,status.phase!=Pending -o name" | tr '\n' ' ')
STUCK=$(r198 "kubectl get pod -n open-webui -l app=open-webui --field-selector=status.phase=Pending -o name | wc -l | tr -d ' '")
[ -n "${DEAD// }" ] && echo "NOTE 已退出 Pod(--apply 时删): $DEAD"
chk "无 Pending 的 open-webui Pod" "[ '$STUCK' = 0 ]"

[ $fail -eq 0 ] || { echo "检查不过,停。挂点没了先改 pypatch/sitecustomize.py。"; exit 3; }
if [ $APPLY -ne 1 ]; then echo "(dry-run; 先跟用户说「会断 1~2 分钟」, 再加 --apply --announced)"; exit 0; fi
[ $ANNOUNCED -eq 1 ] || { echo "拒绝: 先告诉用户站点会断 1~2 分钟, 再带 --announced 重跑"; exit 2; }
[ -n "${DEAD// }" ] && r198 "kubectl delete -n open-webui $DEAD"

TS=$(date +%Y%m%dT%H%M%S); BK=/Data/owui-ops/backup-$TS-upgrade-$NEW
echo "== 备份 → 198:$BK + PVC webui.db.bak-$TS"
r198 "set -e; mkdir -p $BK
kubectl get deploy open-webui -n open-webui -o yaml > $BK/open-webui-deploy.yaml
kubectl exec -n open-webui deploy/open-webui -- python3 -c \"import sqlite3; s=sqlite3.connect('/app/backend/data/webui.db'); d=sqlite3.connect('/app/backend/data/webui.db.bak-$TS'); s.backup(d); d.close(); print('db backup ok')\""

echo "== 发布(pause → set image/env → resume,只重启一次)  $(date +%T)"
r198 "set -e
kubectl rollout pause deployment/open-webui -n open-webui
kubectl set image -n open-webui deployment/open-webui open-webui=127.0.0.1:5000/open-webui:$NEW
${ENVS[*]:+kubectl set env -n open-webui deployment/open-webui ${ENVS[*]}}
kubectl rollout resume deployment/open-webui -n open-webui
kubectl rollout status deployment/open-webui -n open-webui --timeout=600s"
echo "恢复 $(date +%T)"

echo "== 验收"
V=$(curl -sS --max-time 15 http://10.68.13.198:30880/api/version || true); echo "$V"
chk "/api/version = ${NEW#v}" "echo '$V' | grep -q '\"${NEW#v}\"'"
LOG=$(r198 "kubectl logs -n open-webui deploy/open-webui | grep -E 'pypatch final|Traceback' | tail -3")
echo "$LOG"
chk "pypatch final 三个 True" "echo \"\$LOG\" | grep -q 'misc.convert=True middleware.convert=True middleware.get_source_context=True'"
chk "启动无 Traceback" "! echo \"\$LOG\" | grep -q Traceback"
"$HERE/owui-model-gate-audit.sh" || fail=1
[ $fail -eq 0 ] && echo "UPGRADE PASS" || {
  echo "UPGRADE FAIL — 先看是不是反代/上游的事(10-24 非流式 0x8b 是反代, 不是 OWUI), 别急着回滚。"
  echo "  回滚 = set image 回 $BK/open-webui-deploy.yaml 里的旧 tag; 🔴 新版跑过 DB 迁移的话还要恢复 PVC webui.db.bak-$TS"
  exit 1; }
