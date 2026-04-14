---
name: carher-upgrade-compare
description: >-
  Compare a target carher branch/commit with the currently deployed version on K8s,
  produce a diff analysis, risk assessment, and step-by-step upgrade plan.
  Use when the user says "升级 carher"、"比较分支"、"upgrade carher"、"部署新版本",
  or provides a branch name + commit hash for the carher main program.
---

# CarHer 主程序升级比较 & 执行

每次升级 carher 主程序前，**必须先比较目标版本与线上版本**，给出改动分析和风险评估，
用户确认后再执行升级。

## 前置条件

- carher 主程序仓库在本地：`/Users/Liuguoxian/codes/carher`
- kubectl 可连接 K8s（参考 check-instance-status skill 中的 SSH 隧道）
- carher-admin 仓库在本地：`/Users/Liuguoxian/codes/carher-admin`（operator 代码）

## Step 1：获取线上版本

```bash
# 获取当前所有实例的 image tag 分布
kubectl get her -n carher -o jsonpath='{range .items[*]}{.spec.image}{"\n"}{end}' \
  | sort | uniq -c | sort -rn
```

解析 image tag 中的 commit hash。tag 格式通常为 `<描述>-<commit7>`，
例如 `upgrade-0402-8ef16fb` → commit `8ef16fb`；
`skills-two-layer-8045eb9e` → commit `8045eb9e`。

记录：
- `ONLINE_TAG`: 线上 image tag（可能有多个，取最多的那个）
- `ONLINE_COMMIT`: 对应的 git commit

## Step 2：获取目标版本

用户提供目标分支和 commit，例如 `origin/feat/skills-two-layer @ 8045eb9e59`。

```bash
cd /Users/Liuguoxian/codes/carher
git fetch origin
git log --oneline -1 <TARGET_COMMIT>
```

记录：
- `TARGET_BRANCH`: 目标分支
- `TARGET_COMMIT`: 目标 commit

## Step 3：生成改动对比

```bash
cd /Users/Liuguoxian/codes/carher

# commit 列表
git log --oneline <ONLINE_COMMIT>..<TARGET_COMMIT>

# 文件变更统计
git diff --stat <ONLINE_COMMIT>..<TARGET_COMMIT>

# 分类查看关键变更
git diff <ONLINE_COMMIT>..<TARGET_COMMIT> -- Dockerfile.carher docker/
git diff <ONLINE_COMMIT>..<TARGET_COMMIT> -- scripts/carher-entrypoint.sh start-user.sh
git diff <ONLINE_COMMIT>..<TARGET_COMMIT> -- extensions/
git diff <ONLINE_COMMIT>..<TARGET_COMMIT> -- docker/skills/ skills/
git diff <ONLINE_COMMIT>..<TARGET_COMMIT> -- docker/carher-config.json configs/
git diff <ONLINE_COMMIT>..<TARGET_COMMIT> -- docs/
```

## Step 4：影响分类

将每个变更分类到以下维度：

### 4.1 镜像变更（需要重新构建镜像）

| 变更 | 文件 | 影响 |
|------|------|------|
| Dockerfile 改动 | `Dockerfile.carher` | 必须在服务器上重新构建 |
| 入口脚本改动 | `scripts/carher-entrypoint.sh` | 包含在镜像内 |
| 扩展代码改动 | `extensions/**` | 包含在 pnpm build 产物内 |
| 源码改动 | `src/**`, `packages/**` | 包含在 pnpm build 产物内 |

### 4.2 K8s ConfigMap 变更（可热重载，不重启 Pod）

| 配置 | K8s 资源 | 更新方式 |
|------|----------|----------|
| `carher-config.json` | `carher-base-config` ConfigMap | `kubectl apply` |
| `shared-config.json5` | `carher-base-config` ConfigMap | `kubectl apply` |

对比当前 ConfigMap 与目标版本：
```bash
diff <(kubectl get configmap carher-base-config -n carher \
       -o jsonpath='{.data.carher-config\.json}' | python3 -m json.tool) \
     <(cd /Users/Liuguoxian/codes/carher && \
       git show <TARGET_COMMIT>:docker/carher-config.json | python3 -m json.tool)
```

### 4.3 Operator 变更（需要重新部署 operator）

检查 operator 的 pod spec 是否需要与新版 carher 对齐。
关键文件：`operator-go/internal/controller/reconciler.go`

常见需要同步的点：
- Volume / VolumeMount 变更（如新增/移除 PVC）
- 环境变量变更
- 端口变更
- 容器资源限制调整

**重要**：operator 变更会影响所有未来的 pod 创建。单实例灰度测试时可暂不更新 operator，
等全量推送时再一起更新。旧 operator 创建的 pod spec 只要不 break 新镜像就行。

### 4.4 PVC / 持久化数据变更

```bash
kubectl exec -n carher deploy/carher-100 -c carher -- ls -la /data/.openclaw/skills/
kubectl exec -n carher deploy/carher-100 -c carher -- ls -la /data/.agents/skills/
```

如果新版本的 skills 加载方式改变（如从插件路径改到 PVC），需要在部署前同步 PVC 内容。

### 4.5 飞书权限变更

```bash
git diff <ONLINE_COMMIT>..<TARGET_COMMIT> -- docs/her/her-feishu-bot-enterprise-deploy.md
```

如果权限数量变化，需要在飞书开放平台更新所有 bot 应用的权限。

## Step 5：风险评估

| 风险 | 含义 | 典型场景 |
|------|------|----------|
| 🔴 高 | 可能导致功能不可用 | Skills 加载路径变更、PVC 内容缺失 |
| 🟡 中 | 需要额外操作，不做会有问题 | Operator 代码需同步更新 |
| 🟢 低 | 向后兼容，自然生效 | 代码 bug fix、新增可选功能 |
| ⚪ 信息 | 仅文档/注释变更 | docs 更新 |

## Step 6：生成升级计划

输出格式：

```
## 升级摘要
- 线上: <ONLINE_TAG> @ <ONLINE_COMMIT> (<commit message>)
- 目标: <TARGET_BRANCH> @ <TARGET_COMMIT> (<commit message>)
- 间隔: N commits, M files, +X -Y lines

## 改动项
1. [风险] 改动描述 — 所需操作
2. ...

## 执行步骤（按顺序）
### Phase 0 ~ Phase 5（见下方"执行流程"）
```

## Step 7：用户确认后执行

**绝对不要跳过用户确认步骤。** 输出完整分析后，等待用户说"执行"或"开始升级"。

执行时参考以下 skills：
- `carher-deploy` — 镜像部署流程（CI/CD 或 Admin API）
- `carher-admin-deploy` — operator 部署流程（如需更新 operator）
- `hot-grayscale` — 灰度/零宕机部署

---

## 执行流程

### Phase 0：在服务器上构建新镜像

carher 主程序仓库在服务器 `/root/carher`。如果不存在需要先 clone。

```bash
GITHUB_TOKEN=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.github-token}' | base64 -d)

export SSHPASS='5ip0krF>qazQjcvnqc'

# 首次 clone（如已存在则跳过）
sshpass -e ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
  -p 1023 root@47.84.112.136 \
  "test -d /root/carher || git clone --branch <TARGET_BRANCH> \
   https://x-access-token:${GITHUB_TOKEN}@github.com/guangzhou/CarHer.git /root/carher"

# 切换到目标 commit
sshpass -e ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
  -p 1023 root@47.84.112.136 \
  "cd /root/carher && git fetch origin && git checkout <TARGET_COMMIT>"
```

**构建镜像**（耗时约 5-10 分钟，大部分时间在 pnpm install + build）：

```bash
# tag 格式：<描述>-<commit8>
TAG="<branch-slug>-$(echo <TARGET_COMMIT> | cut -c1-8)"
ACR_PUB="cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her"

sshpass -e ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
  -p 1023 root@47.84.112.136 \
  "cd /root/carher && nerdctl build -f Dockerfile.carher -t $ACR_PUB/carher:$TAG . 2>&1 | tail -30"
```

**已知问题**：Dockerfile 中 `curl -fsSL https://bun.sh/install | bash` 可能因
GitHub 下载 503 失败（服务器到 GitHub 不稳定）。解决方法：在服务器上给 Dockerfile
的 bun install 行加重试：

```bash
sshpass -e ssh ... "cd /root/carher && \
  sed -i 's#RUN curl -fsSL https://bun.sh/install | bash#RUN for i in 1 2 3 4 5; do curl -fsSL https://bun.sh/install | bash \&\& break || { echo \"Retry \$i...\"; sleep 10; }; done#' Dockerfile.carher"
```

**推送到 ACR**（镜像约 1.2 GiB，推送约 5-10 分钟）：

```bash
sshpass -e ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
  -p 1023 root@47.84.112.136 \
  "nerdctl push $ACR_PUB/carher:$TAG"
```

Push 用 Public endpoint，K8s Pod pull 时自动走 VPC endpoint（共享底层存储）。

### Phase 1：前置数据准备

#### 同步 Skills 到 PVC

如果 skills 加载方式有变更（如从插件 `"skills"` 声明改为全局 PVC），
**必须在新镜像部署前**把 skills 同步到 `carher-shared-skills` PVC。

使用 `kubectl apply` 创建临时 Pod（比 `kubectl run --overrides` 更可靠）：

```bash
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: skills-sync
  namespace: carher
spec:
  imagePullSecrets:
    - name: acr-secret
    - name: acr-vpc-secret
  restartPolicy: Never
  containers:
    - name: sync
      image: cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:<TAG>
      command: ["sh", "-c", "cp -rv /app/extensions/feishu-her/skills/* /skills/ && ls -la /skills/"]
      volumeMounts:
        - name: skills
          mountPath: /skills
  volumes:
    - name: skills
      persistentVolumeClaim:
        claimName: carher-shared-skills
EOF

# 等待完成
sleep 20 && kubectl logs skills-sync -n carher

# 清理
kubectl delete pod skills-sync -n carher --grace-period=0 --force
```

**验证**（通过任意运行中的实例检查 PVC 内容）：
```bash
kubectl exec -n carher deploy/carher-100 -c carher -- ls /data/.openclaw/skills/
```

#### 更新飞书权限（如需）

如果权限数量变化，需要在飞书开放平台对所有 bot 应用批量导入新权限 JSON。
这个操作是手动的，需要提醒用户。

### Phase 2：ConfigMap 更新（如需）

```bash
# 更新 carher-admin 仓库中的 k8s/base-config.yaml，然后 apply
kubectl apply -f k8s/base-config.yaml
```

ConfigMap 变更不重启 Pod，通过 config-reloader sidecar 热重载（~60s 生效）。

### Phase 3：Operator 更新（如需）

参考 `carher-admin-deploy` skill。operator 变更影响所有 pod 的创建/更新。

**灰度策略**：如果只是单实例测试，可暂不更新 operator。
旧 operator 只要不 break 新镜像（如多挂一个空 PVC 无影响）就行。
全量推送时再更新 operator。

### Phase 4：灰度部署

#### 单实例灰度

```bash
# 只更新一个实例（如 carher-1000）
kubectl patch her her-<ID> -n carher --type merge \
  -p '{"spec":{"image":"<TAG>"}}'

# 观察滚动更新（新 Pod → ReadinessGate 1/1 → 老 Pod Terminating）
kubectl get pod -n carher -l user-id=<ID> -o wide -w
```

#### 批量灰度

```bash
# Canary 组
kubectl get her -n carher --no-headers -o custom-columns='NAME:.metadata.name' \
  | sort -t- -k2 -n | head -20 \
  | xargs -I{} kubectl patch her {} -n carher --type merge \
    -p '{"spec":{"image":"<TAG>"}}'
```

#### 全量

通过 Admin API 的 deploy 接口，或批量 patch 所有 CRD。

### Phase 5：验证

#### 基础验证（部署后立即做）

```bash
# 1. Pod 状态
kubectl get pod -n carher -l user-id=<ID> -o wide
# 期望：2/2 Running, ReadinessGate 1/1

# 2. CRD 状态
kubectl get her her-<ID> -n carher \
  -o jsonpath='image={.spec.image} phase={.status.phase} ws={.status.feishuWS}'
# 期望：image=<TAG> phase=Running ws=Connected

# 3. Skills PVC 挂载
kubectl exec -n carher deploy/carher-<ID> -c carher -- ls /data/.openclaw/skills/
# 期望：所有 feishu-* skills 目录

# 4. 容器日志（无 ERROR）
kubectl logs deploy/carher-<ID> -n carher -c carher --tail=40
# 关键检查：
#   - "[ws] ws client ready" — 飞书 WS 连接成功
#   - "registered feishu_* tool" — 飞书工具注册成功
#   - 无 "Cannot find module" 类错误
```

#### 功能验证（通过 Admin API exec 或飞书实际对话）

```bash
API_KEY=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.admin-api-key}' | base64 -d)

# 检查 skills 是否被应用加载（通过 exec ls workspace skills 目录）
curl -s -X POST "https://admin.carher.net/api/instances/<ID>/exec" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"command":"ls /data/.openclaw/skills/"}' | jq

# 检查 feishu-her 插件配置
curl -s -X POST "https://admin.carher.net/api/instances/<ID>/exec" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"command":"cat /app/extensions/feishu-her/openclaw.plugin.json"}' | jq

# 检查新增的 ENV 或文件（按本次升级的具体改动）
curl -s -X POST "https://admin.carher.net/api/instances/<ID>/exec" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"command":"echo NPM_CONFIG_PREFIX=$NPM_CONFIG_PREFIX"}' | jq
```

#### 回滚（如验证失败）

```bash
# 单实例回滚到旧 image tag
kubectl patch her her-<ID> -n carher --type merge \
  -p '{"spec":{"image":"<OLD_TAG>"}}'
```

---

## Pitfalls（实战踩坑记录）

### 1. GitHub 下载 503

**场景**：Dockerfile 中 `curl -fsSL https://bun.sh/install | bash` 从 GitHub
releases 下载 Bun 时返回 503。

**原因**：阿里云服务器到 GitHub 连接不稳定。

**解决**：在服务器上用 sed 给 Dockerfile 的 bun install 加重试（见 Phase 0）。

### 2. CarHer 仓库是私有仓库

**场景**：服务器首次 clone CarHer 仓库需要认证。

**解决**：从 K8s Secret 获取 GitHub token：
```bash
GITHUB_TOKEN=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.github-token}' | base64 -d)
git clone https://x-access-token:${GITHUB_TOKEN}@github.com/guangzhou/CarHer.git
```

### 3. SSH 命令 tail 看不到构建进度

**场景**：`nerdctl build ... 2>&1 | tail -30` 在构建期间无输出，因为 tail 缓冲。

**建议**：后台执行并用 Await 工具等待 exit_code，或去掉 tail 直接看完整输出。
用 `--progress=plain` 可以看到更详细的构建步骤。

### 4. Skills PVC 为空导致功能丢失

**场景**：新版 feishu-her 插件移除了 `"skills": ["./skills"]` 声明，
skills 改从全局 PVC 加载。如果 PVC 为空，升级后 bot 丢失所有飞书技能。

**铁律**：**Skills PVC 同步必须在镜像部署之前完成。**

### 5. CRD status.message 历史残留

**场景**：CRD `status.message` 可能显示 "CrashLoopBackOff (restarts: 7)" 之类的
历史信息，但实际 Pod 运行正常（restarts=0）。

**判断**：以 Pod 实际状态为准，不要被 CRD status.message 误导。

### 6. 单实例灰度时 operator 不需要更新

**场景**：新版 carher 不再使用 dept-skills PVC，但 operator 仍会挂载它。

**影响**：旧 operator 多挂一个空 PVC（`/data/.agents/skills`），对新镜像无影响。
等全量推送时再更新 operator 即可。
