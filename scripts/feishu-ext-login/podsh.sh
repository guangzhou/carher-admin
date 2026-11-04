#!/bin/bash
# 用法: podsh.sh <本机脚本文件> [timeout]   —— 把脚本 base64 送进 her-236 pod 的 carher 容器里用 sh 执行
# 只读/写都靠脚本自己决定;本工具不做任何写操作
set -euo pipefail
F="$1"; T="${2:-240}"
B64=$(base64 < "$F" | tr -d '\n')
cd /Users/Liuguoxian/codes/carher-admin
scripts/jms ssh k8s-01 --timeout "$T" "P=\$(kubectl get pod -n carher -l user-id=236 -o name | head -1); echo '$B64' | base64 -d | kubectl exec -i -n carher \$P -c carher -- sh" < /dev/null
