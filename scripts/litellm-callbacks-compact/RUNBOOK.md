# gateway 压缩（#25 Phase B）激活 runbook

> 前提：Phase 2（WS 增量全池）观察窗收口后再激活——外层 proxy 变更与 acct 观察不叠加。
> 变更面：litellm-callbacks CM 的 `chatgpt_responses_normalize.py` 一个 key + proxy 滚动重启。
> 默认行为零变化：灰度名单 `_COMPACT_ALIASES={"compact-canary-01"}`，名单外 key 一律不碰。

## 激活步骤（在 198 上）

```bash
export KUBECONFIG=/home/cltx/.kube/config
# 0. 备份现行 CM（整体）
kubectl -n litellm-product get cm litellm-callbacks -o yaml > ~/compact-rollout/cm-backup-$(date +%Y%m%d-%H%M%S).yaml
# 1. 用 patched 文件替换该 key（scp 本目录 chatgpt_responses_normalize.patched.py 到 198:/tmp/）
kubectl -n litellm-product create cm litellm-callbacks --from-file=chatgpt_responses_normalize.py=/tmp/chatgpt_responses_normalize.patched.py \
  --dry-run=client -o yaml | kubectl -n litellm-product patch cm litellm-callbacks --patch-file /dev/stdin   # 或用 python 读旧CM合并后 apply
# （更稳妥：python 读旧 CM json，仅替换该 key，kubectl apply）
# 2. 滚动重启 proxy（零中断，历史 4/4 验证过）
kubectl -n litellm-product rollout restart deploy/litellm-proxy && kubectl -n litellm-product rollout status deploy/litellm-proxy
# 3. 建 canary key：/key/generate alias=compact-canary-01（照 enc-canary-01 模式，budget $10）
# 4. 验证：canary key 发 >2MB 历史请求 → proxy 日志出现
#    counts={'compact_items_omitted': N, 'compact_kb_before': ..., 'compact_kb_after': ...}
#    （warning 级，kubectl logs 可见）；答案语义连续；SpendLogs 该请求计费 tokens 显著下降。
```

## 判据（灰度期）
- 带宽：canary 会话请求体从 MB 级降到 ~1MB 内（SpendLogs octet_length）
- 计费：同会话每轮 tokens 显著下降（=桶容量收益）
- 质量：抽查答案连续性。v2 结构（官方 harness 同款）："忘事"面收窄到陈旧工具输出的
  内容体——用户消息全保、调用配对全保、每个截断处留占位（含原长度+开头 512 字符）
- 零新增 400（不删项不破配对，结构风险为零；11/11 单测）

## 回滚
- 名单摘 alias（改 CM 该行 + restart）；或整 CM 从备份 apply + restart。秒级到分钟级。

## 扩灰度
`_COMPACT_ALIASES` 加真实 key alias（先挑发超巨会话的：cursor-youxun/cursor-zhuge/claude-code-ff550a8b 等，见 task #25 metadata）→ restart。
