# CarHer H75 批量升级问题复盘与防呆清单

更新时间：2026-06-04

## 结论摘要

本轮未升级 Her 已完成 H75 批量升级收敛，最终 K8s 健康结果如下：

| 项目 | 结果 | 说明 |
|---|---:|---|
| HerInstance 目标镜像/profile | 259/259 | `h75-runtime-fa244014-hermestest75-20260602` + `h75-openclaw` |
| Deployment 目标镜像 | 259/259 | `carher` 容器均为目标 H75 镜像 |
| Pod Ready | 259/259 | `carher-user` Pod 全部 `2/2 Running` |
| 最终升级 manifest | 0 | `manifest_count=0`，无剩余未达标实例 |
| 异常 Pod 扫描 | 0 | 无 `CrashLoopBackOff/Error/Pending/ImagePullBackOff/PostStartHookError` |
| 压测 | 未执行 | 用户明确要求不做压测，只做部署/健康/冒烟范围 |

本轮未改源代码，只修改 K8s/CRD/Deployment/runtime 配置与批量执行脚本。

## 本轮核心教训

1. 复杂升级必须拆成多个简单门禁：manifest、部署 patch、rollout、runtime env、config mount、pod 日志签名、最终 fleet scan。
2. 每出现一个新失败类型，必须暂停扩容，把修复固化进脚本，再扫描所有已升级实例是否存在同类问题。
3. 不要只相信 rollout 成功。Deployment 可以 rollout ready，但 runtime 仍可能因为 env、mount、postStart、配置 schema 在启动后失败。
4. K8s 服务器环境才是验证环境。本地 macOS/zsh 只能做触发和文件传输，不用于验证 shell 语义、runtime readiness。
5. 代表性 canary 只能证明镜像/主路径，不证明 fleet 配置完整。批量前后都必须做 fleet-level 扫描。

## 问题清单与解决方案

| 编号 | 问题现象 | 根因 | 解决办法 | 后续防呆 |
|---|---|---|---|---|
| P0-1 | `Invalid config ... agents.defaults: Unrecognized key: "llm"` | 部分 H75 Deployment 仍挂旧 `carher-base-config`，`$include` 引入旧 `carher-config.json`，与 H75/OpenClaw schema 不兼容 | 将 Deployment 的 `base-config` volume 统一指向 `carher-base-config-h75` | 脚本 `deployment_hardened()` 必须检查 `base-config == carher-base-config-h75`；升级前扫描旧 ConfigMap |
| P0-2 | Gateway 启动失败：`CARHER_GATEWAY_TOKEN is missing or empty` | 旧批量脚本漏注入 H75 runtime 必需 SecretRef | 补齐 `CARHER_GATEWAY_TOKEN`，来自 `carher-h75-runtime-secrets/CARHER_GATEWAY_TOKEN` | env gate 必须检查 gateway token、ACP token、Dify bootstrap token |
| P0-3 | 启动拒绝：`required secret env CARHER_PROD_KEY is missing` | H75 启动保护要求 `CARHER_REQUIRED_SECRET_ENVS=CARHER_PROD_KEY`，但脚本只保留了 `LITELLM_API_KEY`，没有派生 `CARHER_PROD_KEY` | 将 `CARHER_PROD_KEY` 设置为实例级 `LITELLM_API_KEY` 的同值 | env gate 必须检查 `CARHER_PROD_KEY == LITELLM_API_KEY` |
| P0-4 | `/data/.agents/skills/... Read-only file system` | 部分旧 Deployment 已有 `readOnly:true`，strategic merge patch 不能可靠删除数组项里的旧字段 | 对残留实例用 JSON Patch 精准替换 `volumeMounts`，将 H75 writable mounts 显式设为 `readOnly:false` | fleet scan 必须查所有 H75 writable mount 是否仍 `readOnly:true` |
| P1-1 | Admin pod 执行长批量任务 OOMKilled | 长 Python executor 跑在 `carher-admin` pod 内，影响 admin 进程资源 | 改为独立 K8s Job，使用 `carher-admin` serviceAccount 执行 | 所有长批量升级都用 Job，不在 admin pod 里长时间跑 |
| P1-2 | Job 初始无法拉镜像 | Job 未带 ACR imagePullSecrets | Job spec 加 `acr-secret` 和 `acr-vpc-secret` | Job 模板固化 imagePullSecrets |
| P1-3 | 批量 rollout 因 CPU 不足 Pending | 多实例升级如果 `maxSurge=1,maxUnavailable=0`，会额外调度新 pod，集群 CPU 容量不够 | 批量维护波次使用 `maxSurge=0,maxUnavailable=1`；`carher` CPU request 下调到 `50m` | wave policy 默认低 surge，并记录恢复策略 |
| P1-4 | `rm -rf /data/.openclaw/runtime-plugins` 报 `Device or resource busy` | H75 runtime-plugin 目录被单独挂载，启动脚本删除父目录时碰到 mount point | 增加 `prepare-h75-fastbin` initContainer，注入 `/carher-fastbin/rm` wrapper，跳过这个危险删除 | PATH gate 必须包含 `/carher-fastbin`；pod 中验证 wrapper 存在 |
| P1-5 | Hermes Feishu 依赖缺失 | 目标镜像/运行时需要 `lark_oapi`、`aiohttp_socks`，不能依赖手工热安装 | 增加 `copy-hermes-feishu-deps` initContainer，安装到 `/data/.openclaw/local/hermes-python-packages`，并设置 `PYTHONPATH` | fresh pod 重建后再验证 import；不能只验证旧 pod |
| P1-6 | Dify bootstrap 401 | 缺 `CARHER_DIFY_BOOTSTRAP_TOKEN` | 从 `carher-dify-bootstrap-token/token` 注入 | Dify env gate 固化 token、base URL、bootstrap URL |
| P1-7 | Dify/LLM 走公网 URL | 部分运行时配置继承旧公网 `https://litellm.carher.net/v1` 或 S3 endpoint | 运行时 env 使用 `http://litellm-proxy.carher.svc.cluster.local:4000/v1`；Dify 使用 `dify-nginx`、`dify-bootstrap` 内网地址 | 生成态配置检查禁止公网 URL |
| P2-1 | 本地 zsh 变量拆分导致批量目标列表错误 | macOS zsh 标量不会像 bash 一样按空格拆分 | 目标列表用 JSON/换行/脚本参数数组，不用本地 shell 隐式拆分 | 本地只做触发；K8s 侧 Python 读取 manifest |
| P2-2 | Job 参数误带 `--include-target-crd`，manifest 扩大到已升级实例 | 参数语义不清，可能重滚大量已达标实例 | 立即删除 Job，重新按默认未达标 manifest 执行 | 默认禁止 include target CRD；除非专项修复并明确只读扫描 |
| P2-3 | rollout 成功但旧 ReplicaSet 残留错误日志干扰判断 | K8s rollout 与 pod 观察存在时间窗口；旧 pod Terminating 仍可能显示 CrashLoop | 同时看 Deployment generation、新旧 pod、当前 ReplicaSet、最新 pod 日志 | 异常扫描需区分 current pod 与 terminating/old pod |

## H75 Deployment 必备参数

| 参数 | 目标值/来源 | 意义 | 校验方式 |
|---|---|---|---|
| `spec.image` | `h75-runtime-fa244014-hermestest75-20260602` | HerInstance 目标镜像 tag | HerInstance 与 Deployment 镜像一致 |
| `carher.io/runtime-profile` | `h75-openclaw` | 使用 H75 OpenClaw runtime profile | CRD annotation |
| deploy group | `beta-h75-<id>` | 每个 Her 独立灰度组，避免 stable 批量替换 | CRD 与 pod annotation |
| `base-config` volume | `carher-base-config-h75` | H75 兼容 base config，避免旧 schema | Deployment volume scan |
| `REDIS_URL` | `redis://carher-redis.carher.svc:6379` | group mode、tracked group、runtime 状态 | Deployment env + Redis probe |
| `OPENAI_BASE_URL` | `http://litellm-proxy.carher.svc.cluster.local:4000/v1` | K8s 内网 LiteLLM，不走外网 URL | env/config URL scan |
| `CARHER_PROD_KEY` | 等于实例 `LITELLM_API_KEY` | H75 推理路由必需 key | env equality check |
| `CARHER_GATEWAY_TOKEN` | `carher-h75-runtime-secrets` | Gateway SecretRef 必需 | env valueFrom check |
| `ANTHROPIC_AUTH_TOKEN` | `carher-h75-acp-secrets` | ACP/Hermes 相关鉴权 | env valueFrom check |
| `CARHER_DIFY_BOOTSTRAP_TOKEN` | `carher-dify-bootstrap-token/token` | Dify bootstrap 鉴权 | env valueFrom check |
| `CARHER_DIFY_BASE_URL` | `http://dify-nginx.dify.svc.cluster.local` | Dify 内网服务 | env/config URL scan |
| `CARHER_DIFY_BOOTSTRAP_URL` | `http://dify-bootstrap.dify.svc.cluster.local:5688/v1/bootstrap/carher-bot` | Dify lifecycle/bootstrap 内网服务 | env/config URL scan |
| `CARHER_RUNTIME_PLUGINS_REFRESH` | `0` | 避免启动期在线刷新带来不确定性 | env check |
| `FEISHU_GROUP_POLICY` | `open` | 支持 group-at/owner-at 原生群模式语义 | env + Redis group mode |
| `FEISHU_ALLOW_ALL_USERS` | `true` | 群管理模式下允许群成员触发 | env + Feishu smoke |
| H75 writable mounts | `readOnly:false` | skills/plugins/local/extensions 需要可写 | Deployment volumeMount scan |
| `prepare-h75-fastbin` | initContainer | 避免 runtime plugin mount 被误删 | initContainer scan |
| `copy-hermes-feishu-deps` | initContainer | 确保 Hermes Feishu deps fresh pod 可用 | initContainer + import probe |

## 标准执行流程

### 1. Manifest 阶段

- 生成目标 Her 清单，记录 UID、owner、app_id、bot_open_id、当前 image/group/profile、rollback 值。
- UID guard 必须开启，UID 不一致拒绝 patch。
- home channel 不猜测：没有真实 chat_id 只能做部署/健康验证，不能声称 Feishu 群 @ 通过。

### 2. Canary 阶段

- 选择有真实 home channel 的目标做 canary。
- 完整验证：rollout、env、base config、mount、OpenClaw health、Feishu WS、Dify 内网配置、Hermes deps。
- canary 修复必须写进脚本，再进入批量。

### 3. Batch 阶段

- 从 K8s Job 执行批量脚本，不在本地 macOS 验证 shell 语义。
- 每波 10 个左右，`maxSurge=0,maxUnavailable=1`。
- 每波结束后扫异常 pod、Deployment env、writable mounts、base-config。
- 新失败类型出现时暂停扩容，修复后扫描所有已升级实例。

### 4. 收尾阶段

- 最终 `manifest_count` 必须为 0。
- `carher-user` pods 必须全部 `2/2 Running`。
- 扫描无异常状态：`CrashLoopBackOff/Error/Pending/ImagePullBackOff/PostStartHookError`。
- 扫描无 H75 writable mount `readOnly:true`。
- 报告要区分：部署健康通过、Feishu 冒烟通过、未自测/无 home channel。

## 必须自动化的 fleet scans

| 扫描项 | 目的 | 失败处理 |
|---|---|---|
| `manifest_count` | 判断是否还有未达目标状态实例 | 非 0 则继续批量或解释跳过原因 |
| pod 异常状态 | 捕获 CrashLoop/Pending/PostStartHookError | 抓日志、冻结扩容、修脚本 |
| `base-config` ConfigMap | 防止旧 schema 引入 `llm` | patch 到 `carher-base-config-h75` |
| H75 writable mount 只读扫描 | 防止 skills/plugins 写入失败 | JSON Patch 替换 `volumeMounts` |
| H75 必需 env | 防止 gateway/secrets 启动失败 | patch env/valueFrom |
| 内网 URL 扫描 | 防止 Dify/LiteLLM 走公网或 S3 URL | patch env 和生成态配置 |
| initContainer 扫描 | 防止 deps/fastbin 修复不持久 | 补 `prepare-h75-fastbin`、`copy-hermes-feishu-deps` |
| Redis group-mode 扫描 | 防止 group-at/owner-at 行为不符合预期 | 只修有明确证据的群模式 |
| active engine 扫描 | 防止升级后停在 Hermes 导致群消息掉链 | 默认恢复 OpenClaw |

## 下次升级防呆清单

- 不从裸 ID 列表直接 patch，必须先 manifest。
- 不在本地 macOS/zsh 做 runtime 验证，只在 K8s 侧执行。
- 不把 canary 通过当成 fleet 通过。
- 不把 rollout ready 当成用户可见功能通过。
- 不自动开放新群为 `group-at`；必须由群主/owner 显式开启。
- 不猜 home channel；无 chat_id 的实例只能标记为 `not_self_tested`。
- 不做压测，除非用户明确要求。
- 不改源代码；若必须改源码，先冻结升级并重新走代码 review/镜像构建流程。
- 每个新失败类型必须沉淀为脚本检查项和文档条目。

## 本轮建议更新到 skill/flow 的规则

1. `deployment_hardened()` 必须检查 `base-config == carher-base-config-h75`。
2. H75 env gate 必须检查：
   - `CARHER_GATEWAY_TOKEN`
   - `ANTHROPIC_AUTH_TOKEN`
   - `CARHER_DIFY_BOOTSTRAP_TOKEN`
   - `CARHER_REQUIRED_SECRET_ENVS=CARHER_PROD_KEY`
   - `CARHER_PROD_KEY == LITELLM_API_KEY`
3. H75 writable mounts 必须显式 `readOnly:false`，不能依赖 strategic merge 删除旧字段。
4. 对数组字段需要删除/覆盖旧值时，使用 JSON Patch 精准替换，不能只靠 strategic merge。
5. 长批量任务必须用 K8s Job，带 `acr-secret`、`acr-vpc-secret`。
6. 默认批量 Job 禁止 `--include-target-crd`；该参数只允许专项修复并且先打印影响范围。
7. 每次修复一个实例后，立即做 fleet scan，确认同类问题没有扩散。

## 可复用命令样例

异常 Pod 扫描：

```bash
kubectl -n carher get pods -l app=carher-user --no-headers \
  | awk '$3 ~ /CrashLoopBackOff|Error|Pending|ImagePullBackOff|PostStartHookError/ {print}'
```

H75 writable mount 只读扫描：

```bash
kubectl -n carher get deploy -l app=carher-user -o json \
  | jq -r '.items[] | .metadata.name as $d |
    (.spec.template.spec.containers[] | select(.name=="carher") | .volumeMounts[]? |
    select((.name|test("^h75-(agent-skills|openclaw-local|runtime-plugins|openclaw-extensions|openclaw-skills|hermes-skills|hermes-opt-skills)$")) and (.readOnly==true)) |
    $d + " " + .name + " " + .mountPath)'
```

目标镜像 Deployment 统计：

```bash
TARGET='cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-fa244014-hermestest75-20260602'
kubectl -n carher get deploy -l app=carher-user -o json \
  | jq -r --arg t "$TARGET" '.items as $items |
    [$items|length, ($items | map(select(.spec.template.spec.containers[]? |
    select(.name=="carher" and .image==$t))) | length)] | @tsv'
```

最终 manifest 期望：

```text
manifest_count=0
```
