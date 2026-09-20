#!/usr/bin/env bash
# 跑在 198 上,从磁盘执行。重启前/后各跑一次,两次输出必须逐行相同。
#
# 判据 = 容器内 sha256。"ConfigMap 还挂着" 不是判据 —— 挂载存在不代表内容对,
# 共用 CM 的 lane 漂移就是这么藏起来的。
#
# 两类补丁,重启行为不同:
#   1) 38 个 ConfigMap subPath 挂载(含 sitecustomize.py) => 重启照旧挂,不会丢
#   2) postStart hook `python3 /patches/patch.py` 改写容器可写层里的
#      transformation.py                                 => 重启后重新打
#      ⚠️ patch.py 找不到锚点时只往 stderr 写 WARN 然后 exit 0,
#      所以"重启后它还在"必须靠 sha256 验,不能假定。
#
# 路径不写死: 从 volumeMounts 现读。上一版我猜 /app/callbacks 这种目录名,
# 实际是 39 个 subPath 单文件挂载,只扫出 1 个文件 —— 那就是个假绿。
# 另外 `find /app -newer /proc/1` 也漏掉了 transformation.py:
# postStart 几乎和容器同时跑,mtime 和 /proc/1 相同,而 -newer 是严格大于。
set -uo pipefail
NS=litellm-product
PODS=$(sudo kubectl -n $NS get pods -l app=litellm-proxy -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}')

PATHS=$(sudo kubectl -n $NS get deploy litellm-proxy \
  -o jsonpath='{range .spec.template.spec.containers[0].volumeMounts[*]}{.mountPath}{"\n"}{end}' | sort -u)
TRANSFORM=/app/.venv/lib/python3.13/site-packages/litellm/responses/litellm_completion_transformation/transformation.py

for P in $PODS; do
  echo "--- $P ---"
  sudo kubectl -n $NS exec -i "$P" -c litellm -- sh -c "
    for T in $(echo $PATHS) $TRANSFORM; do
      if [ -f \"\$T\" ]; then sha256sum \"\$T\"
      elif [ -d \"\$T\" ]; then
        # k8s atomic writer 的 ..2026_09_15_02_18_37.383/ 目录名每次重启都变,
        # 内容却一样。不归一化的话重启后 diff 必然假红。
        find -L \"\$T\" -type f ! -name '*.pyc' 2>/dev/null | sort \
          | while read F; do sha256sum \"\$F\"; done \
          | sed 's#/\\.\\.[0-9_.]*/#/<ts>/#'
      else echo \"MISSING  \$T\"
      fi
    done
    # postStart 那两处补丁的锚点标记还在不在(sha 之外的独立判据)
    for M in CARHER_PATCH_INPUT_NORMALIZE 'Ensure all messages are dicts'; do
      if grep -q \"\$M\" \"$TRANSFORM\" 2>/dev/null; then echo \"MARKER-OK   \$M\"
      else echo \"MARKER-GONE \$M\"; fi
    done
  " < /dev/null | sort -k2
done
