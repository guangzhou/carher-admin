#!/bin/bash
# litellm-198-t0-fullchain.sh — 在 198 上起「全 prod callback 链」的 T0 litellm 栈
#
# 为什么必须全链(2026-08-20/21 两次实证):
# - 流式管线 bug 的 T0 用 2-callback 精简链会**假阴性**:守卫在精简链里恰好是
#   最内层有效,prod 29-callback 链里不在最内层照崩(responses mock choke)。
# - monkey-patch 类改动(如 budget_notice 的 _virtual_key_max_budget_check
#   软拦截)与其它 callback 的 patch 有加载顺序交互,只有全链能验。
#
# 纪律:callback 文件**全部 dump 自 prod CM**(不覆盖 prod 上别人打的补丁,
# 不把 canary 版带进来),只替换显式指定的待验文件。
#
# 用法(在 198 host 上以 sudo 跑):
#   sudo bash litellm-198-t0-fullchain.sh /tmp/budget_notice.py [more.py ...]
#   # 就绪后打 http://127.0.0.1:14000,master key sk-t0master
#   # 回归套件: python3 k8s/litellm-callbacks/tests/t0_budget_notice.py
#   # 收尾: sudo docker rm -f t0-litellm t0-mock t0-redis t0-db; sudo docker network rm t0bn
#
# 依赖(198 上已存在): /home/cltx/t0-budget-notice/mock_upstream.py,
# 本地 registry 127.0.0.1:5000 的 postgres/redis/litellm-carher 镜像。
set -euo pipefail

NS=litellm-product
CM=litellm-callbacks
WORKDIR=/home/cltx/t0-budget-notice
IMAGE=127.0.0.1:5000/litellm-carher:vanilla-v1.90.2.capacity.sse-fix-bare-20260711-122004

cd "$WORKDIR"
D=callbacks-prod
rm -rf "$D" && mkdir -p "$D"

# 1. dump prod CM 全部 key(全链的唯一真源)
kubectl -n "$NS" get cm "$CM" -o json | python3 -c '
import json, sys, os
data = json.load(sys.stdin)["data"]
for k, v in data.items():
    open(os.path.join("callbacks-prod", k), "w").write(v)
print(f"dumped {len(data)} files from prod CM")
'

# 2. 替换待验文件(必须已在 CM 里存在同名 key,防拼错静默新增)
for f in "$@"; do
  base=$(basename "$f")
  test -f "$D/$base" || { echo "FATAL: $base 不在 prod CM 里,拒绝替换"; exit 1; }
  cp "$f" "$D/$base"
  echo "replaced $base: $(md5sum "$D/$base" | cut -d' ' -f1)"
done

# 3. 全链 config:callbacks 列表实时取自 prod litellm-config(不许硬编码,
#    prod 加了新 callback 这里自动跟上) + T0 专用 model_list
kubectl -n "$NS" get cm litellm-config -o go-template='{{index .data "config.yaml"}}' \
  | python3 -c '
import sys, re
text = sys.stdin.read()
m = re.search(r"^  callbacks:\n((?:  - .+\n)+)", text, re.M)
assert m, "callbacks list not found in prod config"
print("""general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
router_settings:
  redis_host: redis
  redis_port: 6379
litellm_settings:
  timezone: Asia/Shanghai
  drop_params: true
  callbacks:""")
print(m.group(1), end="")
print("""model_list:
  - model_name: mock-gpt
    litellm_params:
      model: openai/mock-gpt
      api_base: http://mock:9999/v1
      api_key: sk-mock
      input_cost_per_token: 0.00001
      output_cost_per_token: 0.00001
  - model_name: mock-gpt-5.3
    litellm_params:
      model: openai/mock-gpt
      api_base: http://mock:9999/v1
      api_key: sk-mock
      input_cost_per_token: 0.00001
      output_cost_per_token: 0.00001""")
' > config-full.yaml
echo "config-full.yaml callbacks: $(grep -c '^  - ' config-full.yaml) entries"

# 4. 起栈(callback 逐文件挂 /app/,与 prod 同布局;sitecustomize 同目录自动生效)
MOUNTS=""
for f in "$D"/*.py; do
  MOUNTS="$MOUNTS -v $PWD/$f:/app/$(basename "$f"):ro"
done

docker network create t0bn 2>/dev/null || true
docker rm -f t0-db t0-redis t0-mock t0-litellm 2>/dev/null || true
docker run -d --name t0-db --network t0bn --network-alias db \
  -e POSTGRES_USER=litellm -e POSTGRES_PASSWORD=litellm -e POSTGRES_DB=litellm \
  127.0.0.1:5000/postgres:16-prod-71e27bf
docker run -d --name t0-redis --network t0bn --network-alias redis \
  127.0.0.1:5000/redis:7-alpine-qualification
docker run -d --name t0-mock --network t0bn --network-alias mock \
  -v "$PWD/mock_upstream.py:/mock.py:ro" python:3.11-slim python /mock.py
sleep 8
docker run -d --name t0-litellm --network t0bn -p 14000:4000 \
  -e DATABASE_URL=postgresql://litellm:litellm@db:5432/litellm \
  -e LITELLM_MASTER_KEY=sk-t0master \
  -e "BUDGET_NOTICE_KEY_PREFIXES=claude-code-,cursor-" \
  -e BUDGET_FAMILY_ENABLED=1 \
  -v "$PWD/config-full.yaml:/app/config.yaml:ro" \
  $MOUNTS \
  "$IMAGE" --config /app/config.yaml --port 4000

echo "waiting for litellm readiness..."
for i in $(seq 1 90); do
  if curl -sf -o /dev/null -H "Authorization: Bearer sk-t0master" http://127.0.0.1:14000/health/readiness; then
    echo "READY after ${i}s  →  http://127.0.0.1:14000  (master key sk-t0master)"; exit 0
  fi
  sleep 1
done
echo "NOT READY after 90s"; docker logs t0-litellm 2>&1 | tail -40; exit 1
