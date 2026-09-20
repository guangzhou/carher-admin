---
name: k3s-198-node-expand
description: 198 K3s 集群扩节点 + litellm-proxy 迁移减压完整 SOP。四段闭环：离线 join（hostname 撞名/无外网/独立 taint）→ 出口 preflight（对照组同码判据）→ 金丝雀（镜像/CNI/上游四验证）→ 软亲和滚动迁移（tolerations 整表替换坑/etp=Cluster 前提）。2026-08-25 加入 242 并迁 proxy 实证，198 内存 -12Gi。
---

# 198 K3s 扩节点 + litellm-proxy 迁移 SOP

> 实证来源：2026-08-25 把 10.68.13.242 加入集群（`aiyjy-litellm-242`）并把 4 个
> litellm-proxy 副本全部迁离 198 master，零中断，198 内存 65Gi→52.4Gi。
> 凭据见 memory `project_198_k3s_node_242_joined` / `reference_198_direct_ssh`。

## 适用范围

- ✅ 给 198 K3s 集群（v1.30.4+k3s1，master=198，data-dir=/Data/rancher）加 worker 节点
- ✅ 把 litellm-proxy（或其他**无 openai/google 出口依赖**的服务）迁到新节点
- ❌ chatgpt-acct pod 迁移 —— acct 需直连 chatgpt.com，无 openai 出口的节点**永远不能**承载 acct（迁 225 走 `project_198_acct_migrate_to_225_static_pv` 的静态 PV 流程）
- ❌ 带 local-path PVC 的有状态服务（litellm-db 等）—— PV 钉死在原节点，迁移=搬数据，另行设计

## 四段流程（每段有独立脚本，顺序不可跳）

### Phase 1 — 离线 join：`scripts/k3s-198-join-node.sh`

```bash
P198='<198密码>' NEW_PASS='<新机密码>' bash scripts/k3s-198-join-node.sh 10.68.13.X [node_name]
```

必知坑（脚本已内置规避，但排障时要知道）：

| 坑 | 形态 | 规避 |
|---|---|---|
| hostname 撞名 | 内网机器出厂 hostname 可能就是 `AIYJY-litellm`，小写化后顶掉 master 节点名 | systemd unit 写死 `--node-name`，**此参数永不可删** |
| 无外网 | get.k3s.io 打不通（000） | k3s 二进制从 198 scp 中转 + sha256 校验；镜像走 daocloud mirror + 198:5000 本地 registry（registries.yaml 照抄 225） |
| token 路径 | `/var/lib/rancher/k3s/server/node-token` 不存在 | 198 data-dir 是 **/Data/rancher**，token 在 `/Data/rancher/server/node-token` |
| join 即被调度 | 新节点一 Ready 调度器就可能排 prod pod 上去 | 带 `dedicated=new-node:NoSchedule` taint + `role=new-node` label 加入，验证后再放行 |
| 数据落盘 | 系统盘通常只有 38G | data-dir 用 `/Data`（500G 数据盘），同集群惯例 |

验收：`kubectl get node <name>` Ready + taint 在位 + 节点上 0 pod。

### Phase 2 — 出口 preflight：`scripts/litellm-198-node-egress-preflight.sh`

scp 到新机跑一遍，再在 198 跑同一份。**判据 = 两边逐行同码**（横向对照，
不许单边解读 404/307；memory `feedback_http200_wrong_result_needs_horizontal_control`）。

核心认知：**litellm-proxy 本身不直连 openai/google/chatgpt.com**。它的外呼面 =
wangsu 网关 + kuaihuiai + openrouter + 飞书 webhook + 内网 188:4130，全部国内可达。
chatgpt 出口在 acct pod（不迁）。所以"新节点无 openai 出口"不阻塞 proxy 迁移 ——
但这句话每次都要用 preflight 实测，不许引用本 SKILL 当证据（CM 会加新上游）。

CM 上游变了就同步更新脚本端点清单：
`kubectl -n litellm-product get cm litellm-config -o yaml | grep api_base | sort -u`

### Phase 3 — 金丝雀：`scripts/litellm-198-proxy-node-canary.sh`

```bash
sudo bash litellm-198-proxy-node-canary.sh new-node
```

host 出口通 ≠ Pod 出口通（CNI SNAT 是另一条路径）。金丝雀用 **prod 同款镜像**
钉到新节点，四验证：镜像拉取 / Pod 内出口 / 跨节点 VXLAN（redis+db）/ acct svc。

⚠️ 测 acct svc 必须先 `kubectl get endpoints` 挑**非空**的：connection refused
大概率是该 acct 本来就 scale=0（kube-proxy REJECT 下线号），不是节点网络故障 ——
242 迁移时 acct-100 拒连虚惊一场就是这个（脚本已自动挑活号）。

### Phase 4 — 滚动迁移：`scripts/litellm-198-proxy-migrate-offmaster.sh`

```bash
sudo bash litellm-198-proxy-migrate-offmaster.sh
```

设计决策（改脚本前先读）：

1. **软亲和不是硬亲和**：preferred weight=100 → 实测足以把 4 副本全推离 198
   （空节点资源打分叠加），但 standby+新节点全挂时仍可回落 198，容灾不牺牲。
2. **tolerations 整表替换坑**：tolerations 是无 patchMergeKey 的 list，
   strategic patch 会整体覆盖 —— patch 里必须把已有 standby toleration 写全，
   只写新增项 = 静默删掉旧的 = proxy 从 225 被驱逐。
3. **198 清零的前提**：`litellm-proxy-nodeport` 是 `externalTrafficPolicy: Cluster`，
   nginx→127.0.0.1:30402 由 kube-proxy 跨节点转发。若有人改成 Local，198 必须留 pod。
4. 滚动沿用 maxSurge=0/maxUnavailable=1，一次一个，符合零中断纪律。

验收（缺一不算完）：
- 4/4 Ready 0 restart，198 上 0 个 proxy
- 入口冒烟 `:30402/health/liveliness` 连续 200（浅探针，只证入口链路活着）
- **新节点 pod 日志有真实流量**（`--since=3m` 数请求行）—— liveliness 200 不等于在接客
- `kubectl top nodes` 看 198 内存实际下降

## 放行新节点跑其他负载

taint 保持 `dedicated=new-node:NoSchedule`，给目标 workload 加 toleration 逐个放行
（参考 225 的 `dedicated=standby` 模式）。**不要**直接去 taint —— 会让任意 pod
（包括需要 openai 出口的）飘上去。

## 相关

- memory：`project_198_k3s_node_242_joined`（242 现状+凭据）/ `198-full-topology`（集群全景）
- skill：`chatgpt-acct-close-wait-restart` / `litellm-fix-or-feature`（proxy 改动纪律）
