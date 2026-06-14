#!/bin/bash
# migrate-cm-to-db.sh — Day 0 一次性脚本：把 198 prod ConfigMap 里的 chatgpt-* 模型迁到 DB
#
# 背景：实测 LiteLLM v1.84.0 PATCH /model/{id}/update 对 ConfigMap-loaded 模型返回 404
#       自动调度（quota-rebalance.py PATCH rpm）的前提是模型在 DB 里
#       本脚本把现网 4 个 chatgpt-* model_list 条目从 ConfigMap 迁到 DB（admin API /model/new）
#
# 用法：./scripts/migrate-cm-to-db.sh
#
# 步骤：
#   1. 备份 ConfigMap 到 /root/cm-backup-YYYY-MM-DD.yaml
#   2. snapshot LiteLLM Postgres
#   3. 通过 /model/new 注册 acct-1 的 4 个 deployment 到 DB
#      (model_info.id = chatgpt-acct-1-{gpt-5.5/gpt-5.4/gpt-5.3-codex/gpt-5.3-codex-spark})
#   4. 验证 DB 里 4 行已就位
#   5. **手工步骤**：编辑 ConfigMap 删除 chatgpt-* 条目（脚本不自动改 yaml，避免误改）
#   6. **手工步骤**：rollout LiteLLM
#   7. 流量回归测试
#
# 前置条件：
#   - 188 上 acct-1 docker 容器跑在端口 4000（现网零中断）
#   - 198 prod ConfigMap general_settings.store_model_in_db: true（已开）

set -euo pipefail

ACCT="acct-1"
PORT=4000
DATE=$(date +%F)
LB="http://localhost:30402"

echo "========================================="
echo "Day 0 ConfigMap → DB 迁移"
echo "  目标：把 ConfigMap 里 4 个 chatgpt-* 条目迁到 DB"
echo "  日期：$DATE"
echo "========================================="
echo ""

# ---- Step 1: 备份 ConfigMap ----
echo "==[1/7]== 备份 ConfigMap 到 198:/root/cm-backup-$DATE.yaml"
jms ssh AIYJY-litellm "kubectl get cm -n litellm-product litellm-config -o yaml > /root/cm-backup-$DATE.yaml && wc -l /root/cm-backup-$DATE.yaml"
echo "  ✅ ConfigMap 备份完成"

# ---- Step 2: snapshot LiteLLM Postgres ----
echo ""
echo "==[2/7]== 备份 LiteLLM Postgres 到 198:/root/db-backup-$DATE.sql"
jms ssh AIYJY-litellm "kubectl exec -n litellm-product litellm-db-0 -- \
  pg_dump -U litellm litellm > /root/db-backup-$DATE.sql && wc -l /root/db-backup-$DATE.sql"
echo "  ✅ DB 备份完成"

# ---- Step 3: 注册 acct-1 的 4 个 deployment 到 DB ----
echo ""
echo "==[3/7]== 通过 /model/new 注册 acct-1 的 4 个 chatgpt-* deployment 到 DB"
MK=$(jms ssh AIYJY-litellm "kubectl get secret litellm-secrets -n litellm-product \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d")

for model in chatgpt-gpt-5.5 chatgpt-gpt-5.4 chatgpt-gpt-5.3-codex chatgpt-gpt-5.3-codex-spark; do
  MID="chatgpt-${ACCT}-${model#chatgpt-}"
  echo "  注册 model_name=$model id=$MID api_base=http://10.68.13.188:$PORT"
  jms ssh AIYJY-litellm "curl -fsS -X POST $LB/model/new \
    -H 'Authorization: Bearer $MK' -H 'Content-Type: application/json' \
    -d '{
      \"model_name\": \"$model\",
      \"litellm_params\": {
        \"model\": \"openai/$model\",
        \"api_base\": \"http://10.68.13.188:$PORT\",
        \"api_key\": \"sk-chatgpt-188-$ACCT\"
      },
      \"model_info\": {\"id\":\"$MID\",\"mode\":\"responses\"}
    }'" >/dev/null
done
echo "  ✅ 4 个 deployment 已注册"

# ---- Step 4: 验证 DB ----
echo ""
echo "==[4/7]== 验证 DB LiteLLM_ProxyModelTable 真有这 4 行"
ROWS=$(jms ssh AIYJY-litellm "kubectl exec -n litellm-product litellm-db-0 -- \
  psql -U litellm -d litellm -t -c \
  \"SELECT count(*) FROM \\\"LiteLLM_ProxyModelTable\\\" WHERE model_info->>'id' LIKE 'chatgpt-acct-1-%';\"" | xargs)
[[ "$ROWS" == "4" ]] && echo "  ✅ DB 中找到 4 条记录" || { echo "  ❌ DB 中只有 $ROWS 条（期望 4）"; exit 1; }

# ---- Step 5: 提示手工编辑 ConfigMap ----
echo ""
echo "==[5/7]== 【需手工】编辑 ConfigMap 删除 chatgpt-* 条目"
echo ""
echo "  现在请打开新终端，执行："
echo "    jms ssh AIYJY-litellm 'kubectl edit cm -n litellm-product litellm-config'"
echo ""
echo "  在 model_list: 段下，删除以下 4 个 chatgpt-* 条目（保留 wangsu / claude / 其他）："
echo "    - model_name: chatgpt-gpt-5.5"
echo "    - model_name: chatgpt-gpt-5.4"
echo "    - model_name: chatgpt-gpt-5.3-codex"
echo "    - model_name: chatgpt-gpt-5.3-codex-spark"
echo ""
echo "  保留 router_settings.fallbacks 中的 chatgpt-* 引用（fallback 配置仍需要）"
echo ""
read -p "  ConfigMap 编辑完成后按 Enter 继续..." DUMMY

# ---- Step 6: 提示手工 rollout ----
echo ""
echo "==[6/7]== 【需手工】rollout LiteLLM 让 ConfigMap 生效"
echo ""
echo "  请在新终端执行："
echo "    jms ssh AIYJY-litellm 'kubectl -n litellm-product rollout restart deploy/litellm-proxy && \\"
echo "                            kubectl -n litellm-product rollout status deploy/litellm-proxy'"
echo ""
read -p "  rollout 完成后按 Enter 继续..." DUMMY

# ---- Step 7: 流量回归 ----
echo ""
echo "==[7/7]== 流量回归 — 验证 chatgpt-gpt-5.5 仍可用 + model_id 切到 DB-managed"
echo ""

echo "  (a) /model/info 看 chatgpt-gpt-5.5 deployment 数和 id"
jms ssh AIYJY-litellm "curl -sS $LB/model/info -H 'Authorization: Bearer $MK' \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
ms = [x for x in d[\"data\"] if x[\"model_name\"]==\"chatgpt-gpt-5.5\"]
print(f\"  chatgpt-gpt-5.5 deployments: {len(ms)}\")
for m in ms:
    print(f\"    id={m[\\\"model_info\\\"][\\\"id\\\"]} api_base={m[\\\"litellm_params\\\"].get(\\\"api_base\\\",\\\"?\\\")}\")
'"

echo ""
echo "  (b) SSE smoke test"
echo "    需要一把有 chatgpt-gpt-5.5 权限的 prod key"
echo "    手动跑（替换 \$PROD_KEY）："
echo "    curl -sS -N https://cc.auto-link.com.cn/pro/v1/chat/completions \\"
echo "      -H \"Authorization: Bearer \$PROD_KEY\" \\"
echo "      -d '{\"model\":\"chatgpt-gpt-5.5\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"stream\":true}' | head -5"

echo ""
echo "  (c) 看 SpendLogs 确认 model_id 切到 chatgpt-acct-1-gpt-5.5"
echo "    跑完 (b) 后等 30s，然后："
echo "    jms ssh AIYJY-litellm 'kubectl exec litellm-db-0 -n litellm-product -- \\"
echo "      psql -U litellm -d litellm -c \"SELECT \\\\\"startTime\\\\\", \\\\\"model_id\\\\\", \\\\\"model\\\\\" FROM \\\\\"LiteLLM_SpendLogs\\\\\" \\"
echo "      WHERE \\\\\"startTime\\\\\" > NOW() - INTERVAL '\\''5 min'\\'' AND \\\\\"model\\\\\"='\\''chatgpt-gpt-5.5'\\'' ORDER BY \\\\\"startTime\\\\\" DESC LIMIT 5;\"'"

echo ""
echo "========================================="
echo "🎉 ConfigMap → DB 迁移完成"
echo ""
echo "下一步："
echo "  1. 跑 add-chatgpt-account.sh 加 acct-2 / acct-3"
echo "  2. 部署 quota-rebalance cron"
echo "  3. Stage 0 沙盒灰度"
echo ""
echo "回滚方案（万一出问题）："
echo "  - 恢复 ConfigMap：kubectl apply -f /root/cm-backup-$DATE.yaml"
echo "  - 恢复 DB：psql -U litellm -d litellm < /root/db-backup-$DATE.sql"
echo "  - rollout: kubectl -n litellm-product rollout restart deploy/litellm-proxy"
echo "========================================="
