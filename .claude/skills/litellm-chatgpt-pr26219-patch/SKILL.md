---
name: litellm-chatgpt-pr26219-patch
version: 1.0.0
description: >-
  修复 LiteLLM #25429 — ChatGPT Pro 池子 `chatgpt/*` provider 在 `/v1/responses`
  端点返回 `response.completed.output=null` (流式) 或 `output=[]` (非流式)，导致客户端撞
  `TypeError: 'NoneType' object is not iterable` / `'ResponsesAPIResponse' object
  has no attribute 'output'` / HTTP 400。涵盖：诊断决策树、池子端 PR #26219 patch
  应用、Build ACR image + K8s rollout、188 docker container 热 patch、OpenAI Python
  SDK 流式 parser None-safe 一行 fix、升级时检测旧 bug 是否回归。
  Use when 用户报 `chatgpt-gpt-5.x` 调用空响应 / `Non-retryable error (HTTP None)`
  / `'NoneType' object is not iterable` / `'ResponsesAPIResponse' object has no
  attribute 'output'` / `Unknown items in responses API response: []` / Codex CLI
  / Codex Desktop / hermes / Cursor 用 ChatGPT model 报错；或要升级 LiteLLM 池子
  镜像版本时验证 bug 是否仍存在。
metadata:
  requires:
    bins: ["jms", "kubectl", "docker", "python3", "nerdctl"]
  related_docs:
    - docs/litellm-chatgpt-pr26219-fix.md
---

# 修复 LiteLLM #25429 ChatGPT 池子 `/v1/responses` 路径

> ⚠️ **bug 截至 LiteLLM v1.85.2 仍未在 main release 修复**。PR #26219 (2026-04-22) 仅 merge 到 staging branch `litellm_oss_staging_04_21_2026`，**任何升级前必须 grep 验证 fix 是否还在**。

## 触发条件 / 症状字典

立刻进入本 skill 的关键词：

- `❌ Non-retryable error (HTTP None): 'NoneType' object is not iterable`
- `TypeError: 'NoneType' object is not iterable` 调用 chatgpt-* model
- LiteLLM 返回 `{"error":{"message":"...'ResponsesAPIResponse' object has no attribute 'output'..."}}`
- `Unknown items in responses API response: []`
- chatgpt-gpt-5.5 / 5.4 / 5.3-codex / 5.3-codex-spark 调用空响应
- Codex CLI / Codex Desktop App 调 carher 池子时持续报错
- hermes（hermestest-14）调 ChatGPT Pro 失败
- 升级 LiteLLM 后 chatgpt provider 突然不可用

## 快速诊断（30 秒）

```bash
# 1. 测内层池子单 container 的 /v1/responses
docker exec litellm-chatgpt-2 /app/.venv/bin/python -c "
import urllib.request as u, json, os
key = os.environ['LITELLM_MASTER_KEY']
data = json.dumps({'model':'chatgpt-gpt-5.5','input':[{'role':'user','content':'reply PONG'}],'store':False}).encode()
req = u.Request('http://localhost:4000/v1/responses', data=data,
                headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'})
r = json.loads(u.urlopen(req,timeout=30).read())
out = r.get('output') or []
print(f'out_len={len(out)} tok={(r.get(\"usage\") or {}).get(\"output_tokens\")}')
"

# 结果判读：
#   out_len=1 tok>0  → 池子已 patched，问题在客户端
#   out_len=0 tok>0  → 池子未 patched (#25429 命中)，跳到「池子修复」章
```

```bash
# 2. 检测池子是否已 patched (PR #26219 标记)
docker exec litellm-chatgpt-2 grep -c streamed_output_items \
  /app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py
# 期望：>= 3（patched）；0 = 未 patched

kubectl exec -n carher chatgpt-acct-7-XXXXX -- grep -c streamed_output_items \
  /app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py
# 同上
```

## 池子修复决策树

```
诊断显示池子未 patched
        │
        ├─ aliyun K8s (5 × chatgpt-acct-{7..11}) → Build ACR image + rollout (永久持久)
        │
        └─ 188 docker (5 × litellm-chatgpt-{2..6}) → docker cp + restart (restart 持久)
                                                     ↑ 永久化需进 image build
```

## Patch 文件准备（一次性，复用给两个集群）

### 1) 拉 base file

```bash
# 任选一个 unpatched 容器拉 base 文件
docker exec litellm-chatgpt-2 cat /app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py \
  > /tmp/chatgpt-transformation-base.py

# 或者从阿里云 pod 拉（同 sha256，两边 base 文件一致）
kubectl cp -n carher chatgpt-acct-7-XXXXX:/app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py \
  /tmp/chatgpt-transformation-base.py
```

### 2) 写 idempotent patch script

```python
# /tmp/apply-26219-patch.py
import sys
from pathlib import Path
p = Path(sys.argv[1])
src = p.read_text()
if "streamed_output_items" in src:
    print("[SKIP] already patched"); sys.exit(0)
src = src.replace("from typing import Any, Optional", "from typing import Any, Dict, Optional")
src = src.replace(
    "        completed_response = None\n        error_message = None\n        for chunk in body_text.splitlines():",
    "        completed_response = None\n        error_message = None\n        streamed_output_items: Dict[int, dict] = {}\n        for chunk in body_text.splitlines():")
src = src.replace(
    '            event_type = parsed_chunk.get("type")\n            if event_type == ResponsesAPIStreamEvents.RESPONSE_COMPLETED:',
    '            event_type = parsed_chunk.get("type")\n            if event_type == ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE:\n                item = parsed_chunk.get("item")\n                output_index = parsed_chunk.get("output_index")\n                if isinstance(item, dict):\n                    try:\n                        index = int(output_index)\n                    except (TypeError, ValueError):\n                        index = len(streamed_output_items)\n                    streamed_output_items[index] = item\n                continue\n            if event_type == ResponsesAPIStreamEvents.RESPONSE_COMPLETED:')
src = src.replace(
    '                if isinstance(response_payload, dict):\n                    response_payload = dict(response_payload)\n                    if "created_at" in response_payload:',
    '                if isinstance(response_payload, dict):\n                    response_payload = dict(response_payload)\n                    if not response_payload.get("output") and streamed_output_items:\n                        response_payload["output"] = [\n                            item for _, item in sorted(streamed_output_items.items())\n                        ]\n                    if "created_at" in response_payload:')
for m in ["from typing import Any, Dict, Optional", "streamed_output_items: Dict[int, dict] = {}",
          "ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE", 'if not response_payload.get("output") and streamed_output_items:']:
    if m not in src:
        print(f"[FAIL] missing marker: {m!r}"); sys.exit(1)
p.write_text(src)
print(f"[OK] patched {p}")
```

```bash
# 应用
cp /tmp/chatgpt-transformation-base.py /tmp/chatgpt-transformation-patched.py
python3 /tmp/apply-26219-patch.py /tmp/chatgpt-transformation-patched.py
```

## 路径 A: 188 docker 池子热 patch（restart 持久）

```bash
TS=$(date +%Y%m%d-%H%M%S)
for C in litellm-chatgpt-2 litellm-chatgpt-3 litellm-chatgpt-4 litellm-chatgpt-5 litellm-chatgpt-6; do
  echo "=== $C ==="
  docker exec $C cp /app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py /tmp/transformation-orig-$TS.py
  docker cp /tmp/chatgpt-transformation-patched.py $C:/app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py
  docker cp /tmp/chatgpt-transformation-patched.py $C:/app/litellm/llms/chatgpt/responses/transformation.py
  COUNT=$(docker exec $C grep -c streamed_output_items /app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py)
  echo "  marker=$COUNT (expect 5)"
  docker restart $C
  sleep 8
done

# 验证
for PORT in 4002 4003 4004 4005 4006; do
  C=litellm-chatgpt-$(echo "scale=0; $PORT-4000" | bc)
  echo "--- $C ---"
  docker exec $C /app/.venv/bin/python -c "
import urllib.request as u, json, os
key = os.environ['LITELLM_MASTER_KEY']
data = json.dumps({'model':'chatgpt-gpt-5.5','input':[{'role':'user','content':'reply PONG'}],'store':False}).encode()
req = u.Request('http://localhost:4000/v1/responses', data=data, headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'})
r = json.loads(u.urlopen(req,timeout=30).read())
out = r.get('output') or []
text = (out[0].get('content',[{}])[0].get('text','')[:20] if out and isinstance(out[0],dict) else '')
print(f'  out_len={len(out)} text={text!r}')
"
done
```

**回滚命令**（每个容器 `/tmp/transformation-orig-<TS>.py`）：

```bash
docker exec litellm-chatgpt-N cp /tmp/transformation-orig-<TS>.py /app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py
docker restart litellm-chatgpt-N
```

## 路径 B: 阿里云 K8s 池子 Build + Rollout（永久持久）

**重要前提**：

- `liuguoxian` ACR 账号对 `her/litellm-acct` 和 `her/litellm-proxy` 无 push 权限。**必须 retag 到 `her/carher-admin` 才能 push**。
- `chatgpt-acct-{7..11}` deployment **原本没设 imagePullSecrets**（公网 ghcr 是 anonymous 可拉），切到 ACR 私有 image 时必须同时加。

### B1: Build & Push

```bash
# 上传 patched file
scripts/jms scp /tmp/chatgpt-transformation-patched.py k8s-work-227:/root/chatgpt-transformation-patched.py

# Build on k8s-work-227
scripts/jms ssh k8s-work-227 'bash -s' <<'EOF'
TS=$(date +%Y%m%d-%H%M%S)
TAG=litellm-acct-v1.85.0.pr26219-$TS
ACR=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com
IMG=$ACR/her/carher-admin:$TAG   # 注意：必须用 her/carher-admin repo

WORK=/root/litellm-pr26219-build
rm -rf $WORK && mkdir -p $WORK && cd $WORK
cp /root/chatgpt-transformation-patched.py transformation.py

cat > Dockerfile <<DOCKER
FROM ghcr.io/berriai/litellm:v1.85.0
COPY transformation.py /app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py
COPY transformation.py /app/litellm/llms/chatgpt/responses/transformation.py
LABEL patch.id="PR-26219-minimal" patch.date="$TS"
DOCKER

nerdctl build --namespace k8s.io -t $IMG .
nerdctl push --namespace k8s.io $IMG
echo "BUILT: $IMG"
EOF
```

### B2: Rollout 5 deployments

```bash
# 把 step B1 输出的 IMG 填入下面
IMG=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-admin:litellm-acct-v1.85.0.pr26219-<TS>

for ACCT in chatgpt-acct-7 chatgpt-acct-8 chatgpt-acct-9 chatgpt-acct-10 chatgpt-acct-11; do
  echo "=== $ACCT ==="
  # 同时 set image + 加 imagePullSecrets（原 deployment 没设）
  kubectl patch deploy $ACCT -n carher --type='strategic' \
    -p "{\"spec\":{\"template\":{\"spec\":{\"imagePullSecrets\":[{\"name\":\"acr-vpc-secret\"}],\"containers\":[{\"name\":\"litellm\",\"image\":\"$IMG\"}]}}}}"
  kubectl rollout status deploy/$ACCT -n carher --timeout=120s
done

# 验证：直接打 pod 测试 /v1/responses
for ACCT in chatgpt-acct-7 chatgpt-acct-8 chatgpt-acct-9 chatgpt-acct-10 chatgpt-acct-11; do
  POD=$(kubectl get pods -n carher --no-headers | grep "^$ACCT" | grep Running | awk '{print $1}' | head -1)
  R=$(kubectl exec -n carher "$POD" -- /app/.venv/bin/python -c "
import urllib.request as u, json, os
key = os.environ['LITELLM_MASTER_KEY']
data = json.dumps({'model':'chatgpt-gpt-5.5','input':[{'role':'user','content':'reply PONG'}],'store':False}).encode()
req = u.Request('http://localhost:4000/v1/responses', data=data, headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'})
r = json.loads(u.urlopen(req,timeout=30).read())
out = r.get('output') or []
print(f'out_len={len(out)} tok={(r.get(\"usage\") or {}).get(\"output_tokens\")}')
")
  printf '  %-20s | %s\n' "$ACCT" "$R"
done
```

**回滚命令**：

```bash
for ACCT in chatgpt-acct-7 chatgpt-acct-8 chatgpt-acct-9 chatgpt-acct-10 chatgpt-acct-11; do
  kubectl set image deploy/$ACCT litellm=ghcr.io/berriai/litellm:v1.85.0 -n carher
  kubectl rollout status deploy/$ACCT -n carher --timeout=120s
done
```

## 路径 C: 客户端 OpenAI Python SDK None-safe（仅 hermes 类客户端需要）

只在客户端用 `client.responses.stream()` context manager 时需要（hermestest-14 是典型）。Codex CLI / Codex Desktop / Cursor 不依赖这一行。

```bash
docker exec hermestest-14 python3 -c "
p = '/opt/hermes/.venv/lib/python3.13/site-packages/openai/lib/_parsing/_responses.py'
src = open(p).read()
if 'response.output or []' in src:
    print('[SKIP] already patched')
else:
    src = src.replace('for output in response.output:', 'for output in response.output or []:', 1)
    open(p,'w').write(src)
    print('[OK] patched')
"
docker restart hermestest-14
```

**测试**：

```bash
docker exec --user hermes -e HOME=/opt/data/.hermes -e HERMES_HOME=/opt/data/.hermes hermestest-14 \
  /opt/hermes/.venv/bin/hermes -z 'reply with exactly the single word PONG' \
  -m chatgpt-gpt-5.5 --provider chatgpt-pro
# 期望：PONG
```

## 升级 LiteLLM 时的回归检测

**任何升级 LiteLLM 池子镜像前/后，必须跑这个检测**，不然 #25429 静默回归你不会立刻发现（症状是 HTTP 200 假成功）：

```bash
# 升级前/后都跑
docker exec <new-litellm-container> grep -c streamed_output_items \
  /app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py

# 0 = 没 patch，需要重 apply PR #26219
# >=3 = 已 patch，正常

# 端到端验证（最权威）
docker exec <new-litellm-container> /app/.venv/bin/python -c "
import urllib.request as u, json, os
key = os.environ['LITELLM_MASTER_KEY']
data = json.dumps({'model':'chatgpt-gpt-5.5','input':[{'role':'user','content':'PONG?'}],'store':False}).encode()
req = u.Request('http://localhost:4000/v1/responses', data=data, headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'})
r = json.loads(u.urlopen(req,timeout=30).read())
out_present = bool(r.get('output'))
tok = (r.get('usage') or {}).get('output_tokens', 0)
ok = out_present and tok > 0
print(f'  HEALTHY={ok}  output_present={out_present}  tok={tok}')
"
```

升级后**必须看到 `HEALTHY=True`**，否则池子又坏了。

## 已知坑

1. **K8s container in-place restart 重置 writable layer**：跟 docker 不一样，`kill -TERM 1` 让 container restart，下次启动用 image rootfs，writable layer 不保留。**K8s 必须 build patch image**。
2. **`her/litellm-acct` repo 无 push 权限**：`liuguoxian` ACR 账号对 `her/litellm-*` 无权 push（`insufficient_scope`），必须 retag 到 `her/carher-admin`。
3. **`chatgpt-acct-*` deployment 没设 `imagePullSecrets`**：切到 ACR 私有 image 时必须同时加 `acr-vpc-secret`。
4. **`/v1/responses` 跟 `/v1/chat/completions` 是两条 code path**：v1.85.x 在两个端点上 bug 表现不同，诊断时分别测两个。
5. **池子层 patch 不等于客户端 patch**：PR #26219 修非流式聚合；OpenAI Python SDK 流式 parser 的 None-safe 是**独立 bug**，hermes 这种 stream context 客户端需要双 patch。
6. **`main-stable` rolling tag**：188 docker 用的 `main-stable` digest 可能跟 ACR 上的 `v1.85.0` 不一样。检测时以**实际 `streamed_output_items` 标记**为准，不以 image tag 名字为准。
7. **carher-runtime `.env` & `config.yaml` 强制 sync**：每次容器启动 entrypoint 重写两个文件，所以手动 sed 改 hermes 配置/key 在容器重启时丢失。要持久改 hermes 配置必须同时改 `/opt/carher-runtime/templates/hermes-config.carher-pro.yaml`。
8. **hermes 跨 transport 切换会留 chatcmpl-* ID 在 session history**：切 transport 后 session 老 history 里 `chatcmpl-*` 格式 message ID 被发到 Codex backend → 400 `Invalid input[1].id`。修复后必须 end 当前 session：

```sql
-- 在 hermes state.db 上跑
UPDATE sessions SET ended_at = strftime('%s','now'), end_reason = 'transport_swap' WHERE ended_at IS NULL;
UPDATE sessions SET billing_provider=NULL, billing_base_url=NULL, billing_mode=NULL
  WHERE started_at > strftime('%s','now','-2 hours');
```

9. **运行时 patch 不抗容器重建**（2026-05-28 实测踩爆）：客户端 SDK patch + 手动 sed 的 config/.env，遇到 `docker compose down/up` 或 image 升级全丢。`docker restart` 才保留。要持久改 hermes endpoint/key 必须改 3 个 source of truth（不是改容器内文件）：
   - key → `/Data/CarHer/docker/users/14.env`（entrypoint 用它重写 .env）
   - env URL → `compose.cicd-14.yaml` 的 `environment:` 块（优先级 > env_file，会覆盖 14.env）
   - provider base_url → 容器内 `/opt/carher-runtime/templates/hermes-config.carher-pro.yaml`（entrypoint sync 源）
   - SDK patch 永久化：烧进 `carher-runtime` image 或 `dual-entrypoint.sh` 加 idempotent `grep -q 'response.output or' || sed ...`
   - 完整复盘 + mermaid 图见 [docs/litellm-chatgpt-pr26219-fix.md](../../../docs/litellm-chatgpt-pr26219-fix.md) 「容器重建事件复盘」章节

10. **docker compose down 会 untag image**：`carher-runtime:dev` 是本地 tag，down 后原 image 进 `<untagged>` 列表，`up` 尝试 pull → denied。先 `docker images -a | grep <digest>` 找回原 image `docker tag` 回去，再用 `--pull never` up。**别误用 hermestest-75 的 image**（那是 openclaw-only，没 `/opt/hermes`）。

## 相关 issue / PR

- [LiteLLM #25429](https://github.com/BerriAI/litellm/issues/25429) — bug report (2026-04-09)
- [LiteLLM PR #26219](https://github.com/BerriAI/litellm/pull/26219) — fix (merged 2026-04-22 to **staging branch only**)
- 其他报这个 bug 的 PR：#25403 / #26075 / #27562 / #27374（都没合）

## 相关文档 / skill

- [docs/litellm-chatgpt-pr26219-fix.md](../../../docs/litellm-chatgpt-pr26219-fix.md) — 全集群修复过程的完整记录
- [litellm-ops](../litellm-ops/SKILL.md) — LiteLLM 通用运维（升级、DB、性能）
- [litellm-pro-ops](../../.claude/skills/litellm-pro-ops/SKILL.md) — 198 prod 环境运维（关于 patch.N 镜像构建流程）
- [chatgpt-pool-aliyun-canary](../../.claude/skills/chatgpt-pool-aliyun-canary/SKILL.md) — 阿里云 chatgpt-acct 池子架构
