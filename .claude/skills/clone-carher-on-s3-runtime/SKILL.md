---
name: clone-carher-on-s3-runtime
description: >-
  Clone or create a CarHer Her instance on the S3 on-prem Docker server using the
  carher-runtime architecture (compose.runtime.yaml + hermestest-N container).
  Use when adding a new instance directly to S3 (NOT K8s), cloning an existing
  on-S3 instance with full memory/skills/data, or swapping the Feishu bot binding.
  Replaces the deprecated start-user.sh single-script flow. For K8s clone, use
  clone-instance-memory instead. For migrating S3→K8s, use migrate-s3-to-k8s.
---

# 在 S3 (carher-runtime) 上克隆 / 新建 her 实例

> ⚠️ **方向警告**：项目整体在迁 S3 → K8s（见 `cloud-migration-it-handover.md`）。
> 优先用 K8s + Admin Dashboard 创建，仅在用户明确要求"在 S3 上创建"时才用本 skill。
> Admin Dashboard **不会**跟踪 S3 实例。

## 一、先放掉的迷思

会让你浪费时间的过时认知：

| 看上去对，其实错 | 现实（2026-05+） |
|---|---|
| `start-user.sh --id=N` 创建 | 已被 `start-user.sh.bak-20260420-194431` 弃用 |
| 容器名 `carher-N` | 实际叫 `hermestest-N`（内部 env 仍是 carher-N） |
| `/Data/CarHer/deploy/carher-N/compose.yaml`（carher-core 镜像，localhost:5001） | 这套**不在跑**——保留但失效 |
| 端口公式 BASE+{1,2,3,4,5} | **BASE+{1,3,4,5,6}**——少 +2、多 +6（A2A） |
| cloudflared 用 systemd + 动态 DNS | Docker 容器 + 静态 `config.yml` ingress |
| `docker exec cloudflared cloudflared tunnel route dns ...` | 容器缺 `cert.pem`，必须在 **host 上**跑 |
| 用 `alpine` 做 volume copy helper | S3 不通 Docker Hub，alpine 拉不下来 |
| `cp -a` 直接拷 `/var/lib/docker/volumes/` | JMS cltx 没 NOPASSWD sudo，读不到 |

## 二、架构与文件布局

S3 主机上一个 her 实例由 **3 棵目录树** + 2 类 volume 拼出来：

```
/Data/carher-runtime/deploy/carher-N/
  compose.runtime.yaml      ← 端口、env、volume 引用、image=carher-runtime:dev
  openclaw.runtime.json5    ← 飞书 overlay（appId / botOpenId / oauthRedirectUri / dm.allowFrom）

/Data/CarHer/deploy/carher-N/
  secrets.env               ← FEISHU_APP_SECRET（被 compose env_file 引用，跨树）

/Data/hermestest/deploy/carher-N/
  data-hermes/              ← bind 到容器 /opt/data（HERMES_DATA_DIR：sqlite/sessions/cron/SOUL.md）
  data-engine/              ← bind 到容器 /data/.engine

docker volumes:
  carher-N-home             ← /data
  carher-N-data             ← /data/.openclaw（记忆/skills/会话——大头）
```

镜像：`carher-runtime:dev`（本地构建，不在 registry 里；其它候选 `hermestest:dev`、`hermestest:dual`）。

## 三、端口公式 & 域名映射

```
BASE = 29000 + (UID - 1) × 10
```

| 域名 | host port | container port | 说明 |
|---|---|---|---|
| 内网网关 | BASE+1 | 18789 | gateway HTTP（healthcheck 命中此端口） |
| `s3-uN-proxy.carher.net` | BASE+3 | 8000 | "proxy"（命名反直觉，历史包袱） |
| `s3-uN-fe.carher.net` | BASE+4 | 8080 | "fe"（同上） |
| `s3-uN-auth.carher.net` | BASE+5 | 18891 | OAuth 回调 |
| 内网 A2A | BASE+6 | 18800 | 跨实例 A2A |

> **fe/proxy 端点 502 是正常的**：`carher-runtime:dev` 镜像只对外暴露 gateway + OAuth，不带 Live Frontend / WS Proxy。`s3-u75-fe`/`s3-u75-proxy` 在生产也是 502。**只用 auth=400 作为路由通验证**。

UID=1001 → host ports `39001 / 39003 / 39004 / 39005 / 39006`。

## 四、JMS 访问

所有命令都通过堡垒机：

```bash
scripts/jms ssh JSZX-AI-03 "<command>"          # S3 主机
scripts/jms scp local.txt JSZX-AI-03:/tmp/      # 上传
scripts/jms list                                 # 资产一览
```

cltx 账号在 S3 上 **没有 NOPASSWD sudo**，但所有 `/Data/*` 目录都是 cltx:cltx → 不需要 sudo 写。
docker 命令直接以 cltx 跑（cltx 在 docker 组）。
`/var/lib/docker/volumes/` 仍要 sudo → 不要走这条路。

## 五、执行流程

### Phase 0 — Discovery（必跑，状态偏差检测）

```bash
NEW_ID=1001            # 替换
SRC_ID=75              # 克隆源；新建则按需选

scripts/jms ssh JSZX-AI-03 "
  docker ps --format '{{.Names}}|{{.Status}}' | grep -E 'hermestest|cloudflared'
  echo ---
  ls -la /Data/carher-runtime/deploy/carher-${SRC_ID}/
  echo ---
  cat /Data/carher-runtime/deploy/carher-${SRC_ID}/compose.runtime.yaml | head -15
  echo ---ports-busy---
  ss -tln 2>/dev/null | awk '\$4 ~ /:(${BASE_NEW_PORTS_REGEX})\$/{print}'
  echo ---csv---
  awk -F, 'NR==1 || \$1==\"${SRC_ID}\"' /Data/CarHer/docker/users.csv
"
```

确认：
- 源实例 `hermestest-${SRC_ID}` 在跑
- 源 compose.runtime.yaml 存在
- 新 5 个 host port（BASE+{1,3,4,5,6}）都空闲
- CSV 里有/没有源行

### Phase 1 — 决定克隆模式

| 模式 | appId/secret | 数据 | bot_open_id | 用途 |
|---|---|---|---|---|
| **Full clone（同 bot）** | 同源 | 全拷 | 同源 | ⚠️ 会和源抢飞书 WSS。仅适合"马上要 down 源" |
| **Bot swap（换 bot）** | 新 | 全拷 | 应换（用户没给则保留+留警告） | 新人接手老 her；本 skill 默认场景 |
| **Fresh new（不克隆数据）** | 新 | 不拷，空 volume | 新 | 真新用户。考虑改走 K8s |

如果是 Bot swap，需要用户提供：
- 新 `app_id`（必填）
- 新 `app_secret`（必填）
- 新 `bot_open_id`（可选——没给的话保留源值，留警告）

### Phase 2 — 建目录、Volume、克隆 3 份配置

```bash
scripts/jms ssh JSZX-AI-03 "
set -euo pipefail
NEW_ID=${NEW_ID}; SRC_ID=${SRC_ID}

# 1) 目录（cltx 拥有，无需 sudo）
mkdir -p /Data/carher-runtime/deploy/carher-\${NEW_ID}
mkdir -p /Data/CarHer/deploy/carher-\${NEW_ID}
mkdir -p /Data/hermestest/deploy/carher-\${NEW_ID}/{data-hermes,data-engine}

# 2) Named volumes
docker volume create carher-\${NEW_ID}-home >/dev/null
docker volume create carher-\${NEW_ID}-data >/dev/null

# 3) 端口字符串预备
SRC_BASE=\$((29000 + (SRC_ID-1)*10))   # 例 75 → 29740
NEW_BASE=\$((29000 + (NEW_ID-1)*10))   # 例 1001 → 39000

# 4) compose.runtime.yaml（5 端口 + 容器名 + 别名 + 域名 + ID）
sed \
  -e \"s|carher-\${SRC_ID}|carher-\${NEW_ID}|g\" \
  -e \"s|hermestest-\${SRC_ID}|hermestest-\${NEW_ID}|g\" \
  -e \"s|\$((SRC_BASE+1))|\$((NEW_BASE+1))|g\" \
  -e \"s|\$((SRC_BASE+3))|\$((NEW_BASE+3))|g\" \
  -e \"s|\$((SRC_BASE+4))|\$((NEW_BASE+4))|g\" \
  -e \"s|\$((SRC_BASE+5))|\$((NEW_BASE+5))|g\" \
  -e \"s|\$((SRC_BASE+6))|\$((NEW_BASE+6))|g\" \
  -e \"s|s3-u\${SRC_ID}-|s3-u\${NEW_ID}-|g\" \
  /Data/carher-runtime/deploy/carher-\${SRC_ID}/compose.runtime.yaml \
  > /Data/carher-runtime/deploy/carher-\${NEW_ID}/compose.runtime.yaml

# 5) openclaw.runtime.json5
sed \
  -e \"s|carher-\${SRC_ID}|carher-\${NEW_ID}|g\" \
  -e \"s|s3-u\${SRC_ID}-|s3-u\${NEW_ID}-|g\" \
  /Data/carher-runtime/deploy/carher-\${SRC_ID}/openclaw.runtime.json5 \
  > /Data/carher-runtime/deploy/carher-\${NEW_ID}/openclaw.runtime.json5

# 6) secrets.env
sed -e \"s|carher-\${SRC_ID}|carher-\${NEW_ID}|g\" \
  /Data/CarHer/deploy/carher-\${SRC_ID}/secrets.env \
  > /Data/CarHer/deploy/carher-\${NEW_ID}/secrets.env
"
```

**Bot swap 场景额外做**（在新生成文件上 sed appId/secret/botOpenId）：

```bash
scripts/jms ssh JSZX-AI-03 "
NEW_ID=${NEW_ID}
OLD_APPID=cli_xxxOLD; NEW_APPID=cli_xxxNEW
OLD_SECRET=oldSECRET ; NEW_SECRET=newSECRET
# bot_open_id 用户没给就跳过这两行
OLD_BOT_OPEN_ID=ou_xxxOLD; NEW_BOT_OPEN_ID=ou_xxxNEW

CR=/Data/carher-runtime/deploy/carher-\${NEW_ID}
SE=/Data/CarHer/deploy/carher-\${NEW_ID}/secrets.env

sed -i \"s|\${OLD_APPID}|\${NEW_APPID}|g\" \$CR/compose.runtime.yaml \$CR/openclaw.runtime.json5
sed -i \"s|\${OLD_SECRET}|\${NEW_SECRET}|g\" \$SE
[ -n \"\${NEW_BOT_OPEN_ID:-}\" ] && sed -i \"s|\${OLD_BOT_OPEN_ID}|\${NEW_BOT_OPEN_ID}|g\" \$CR/compose.runtime.yaml \$CR/openclaw.runtime.json5
"
```

> **bot_open_id 没换的副作用**：群里 @ 自我识别会错（容器以为自己是 SRC bot），@ 新 bot 时可能不响应或重复响应。后续拿到再 sed + restart。

### Phase 3 — 数据克隆（如果是 clone-data 模式）

⚠️ **必须先停源容器**（SQLite WAL 一致性）。18 GB 数据 cp -a 用时约 20 分钟（小文件多）。

```bash
scripts/jms ssh JSZX-AI-03 "
set -euo pipefail
NEW_ID=${NEW_ID}; SRC_ID=${SRC_ID}

docker stop hermestest-\${SRC_ID}

# carher-runtime:dev 有 ENTRYPOINT，必须 --entrypoint 覆盖
docker run --rm --entrypoint /bin/sh \
  -v carher-\${SRC_ID}-home:/src-home:ro \
  -v carher-\${NEW_ID}-home:/dst-home \
  -v carher-\${SRC_ID}-data:/src-data:ro \
  -v carher-\${NEW_ID}-data:/dst-data \
  -v /Data/hermestest/deploy/carher-\${SRC_ID}:/src-bind:ro \
  -v /Data/hermestest/deploy/carher-\${NEW_ID}:/dst-bind \
  carher-runtime:dev -c '
    set -e
    cp -a /src-home/. /dst-home/
    cp -a /src-data/. /dst-data/
    cp -a /src-bind/data-hermes/. /dst-bind/data-hermes/
    cp -a /src-bind/data-engine/. /dst-bind/data-engine/
    du -sh /dst-home /dst-data /dst-bind/data-hermes /dst-bind/data-engine
  '

docker start hermestest-\${SRC_ID}
"
```

> 不要用 `alpine` —— S3 上 docker hub 不通。本地有 `carher-runtime:dev` / `hermestest:dev` 就够了。
> 不要忘 `--entrypoint /bin/sh` —— `carher-runtime:dev` 的 ENTRYPOINT 会接管命令，你 `sh -c '...'` 会被吞掉，看到 `Missing config. Run openclaw setup` 报错就是这个。

### Phase 4 — Cloudflared ingress + DNS

cloudflared 在 Docker 容器里跑、用静态 ingress；DNS CNAME 必须从 host 注册。

```bash
scripts/jms ssh JSZX-AI-03 "
set -euo pipefail
NEW_ID=${NEW_ID}
NEW_BASE=\$((29000 + (NEW_ID-1)*10))

# 1) 编辑 host 上的 cloudflared 配置（在 catch-all 之前插 3 段）
CFG=/tmp/cloudflared-docker-config.yml
cp \$CFG \$CFG.bak.\$(date +%s)
python3 - <<PY
from pathlib import Path
cfg = Path('${CFG}')
text = cfg.read_text()
marker = '- service: http_status:404'
new_id = ${NEW_ID}; base = ${NEW_BASE}
block = f'''  # User {new_id}
  - hostname: s3-u{new_id}-fe.carher.net
    service: http://host.docker.internal:{base+4}
  - hostname: s3-u{new_id}-proxy.carher.net
    service: http://host.docker.internal:{base+3}
  - hostname: s3-u{new_id}-auth.carher.net
    service: http://host.docker.internal:{base+5}
  '''
if f's3-u{new_id}-' not in text:
  cfg.write_text(text.replace(marker, block + marker, 1))
PY

# 2) 重启 cloudflared 让新 ingress 生效
docker restart cloudflared
sleep 3

# 3) DNS CNAME 注册——必须在 host 上跑，cloudflared 容器缺 cert.pem
cloudflared tunnel route dns --overwrite-dns carher-s3 s3-u\${NEW_ID}-auth.carher.net
cloudflared tunnel route dns --overwrite-dns carher-s3 s3-u\${NEW_ID}-fe.carher.net
cloudflared tunnel route dns --overwrite-dns carher-s3 s3-u\${NEW_ID}-proxy.carher.net
"
```

Tunnel 信息：name `carher-s3`，id `750fb00c-6572-4d7c-bed4-60c1a9c3107f`，cert 在 `/home/cltx/.cloudflared/cert.pem`。

### Phase 5 — users.csv 行（可选但推荐）

CSV **不被 carher-runtime 容器消费**——只是 host 工具（start.sh 的 knownBots 同步）和人工查询用。`HERMESTEST_KNOWN_BOTS` 是在 compose 里硬编码的，加 CSV 行不会传播给已跑的其它容器。

```bash
scripts/jms ssh JSZX-AI-03 "
CSV=/Data/CarHer/docker/users.csv
cp \$CSV \$CSV.bak.\$(date +%s)
# 列：id,姓名,模型,appId,appSecret,owner,provider,备注,allow_from,bot_open_id
echo \"${NEW_ID},${NEW_NAME},opus,${NEW_APPID},${NEW_SECRET},,anthropic,${NEW_NOTE},,${NEW_BOT_OPEN_ID:-}\" >> \$CSV
"
```

### Phase 6 — 启动 + 验证

```bash
scripts/jms ssh JSZX-AI-03 "
NEW_ID=${NEW_ID}
cd /Data/carher-runtime/deploy/carher-\${NEW_ID}
docker compose -f compose.runtime.yaml -p carher-\${NEW_ID} up -d

# 等 healthy（gateway 起来即 healthy；feishu WSS 可能再过 30s 才连上）
for i in \$(seq 1 18); do
  s=\$(docker inspect --format '{{.State.Health.Status}}' hermestest-\${NEW_ID} 2>/dev/null)
  echo \"[\$((i*5))s] health=\$s\"
  [ \"\$s\" = \"healthy\" ] && break
  sleep 5
done

# 关键日志
docker logs hermestest-\${NEW_ID} 2>&1 | grep -E 'agent model|ws client ready|listening on'
"

# 外部探活（auth 必须 400/4xx；fe/proxy 502 是预期）
NEW_ID=${NEW_ID}
curl -sS -o /dev/null -w "auth=%{http_code}\n" "https://s3-u${NEW_ID}-auth.carher.net/feishu/oauth/callback"
```

通过标准：
- `agent model: openrouter/anthropic/claude-opus-4.7`（或源对应模型）
- `[ws] ws client ready`
- `auth=400`（缺参数 = tunnel + OAuth 后端都通）
- 飞书向新 bot 发 DM 能收到回复（人工）

## 六、克隆数据后的预期噪音

**`[tools] message failed: Request failed with status code 400` 大量出现** —— 源实例的 cron / 定时任务 / 记忆里硬编码了**源 bot 所在的群 / open_id**（如 `oc_773046...`、`ou_3104...`），新 bot 不在这些群里 → Feishu 拒绝。

止血选项（按需）：
1. 不管，让它们 fail（最简单）
2. 在新容器里清掉 cron：`docker exec hermestest-${NEW_ID} sh -c 'rm -rf /opt/data/cron/* /data/.openclaw/cron/*'` 然后重启
3. 在 openclaw 配置里禁掉 cron 实体（需要看 `/data/.openclaw/agents/*` 里的 schedule 配置）

## 七、回滚

```bash
scripts/jms ssh JSZX-AI-03 "
NEW_ID=${NEW_ID}
cd /Data/carher-runtime/deploy/carher-\${NEW_ID}
docker compose -f compose.runtime.yaml -p carher-\${NEW_ID} down

# DNS：cloudflared 没有原子 unroute，去 cloudflare dashboard 手动删 CNAME，
# 或重新 overwrite 到一个不存在的 tunnel name。

# Volume + bind：确认无价值再删
docker volume rm carher-\${NEW_ID}-home carher-\${NEW_ID}-data
rm -rf /Data/hermestest/deploy/carher-\${NEW_ID}
rm -rf /Data/carher-runtime/deploy/carher-\${NEW_ID}
rm -rf /Data/CarHer/deploy/carher-\${NEW_ID}

# users.csv：找最新一份 .bak.* 还原或手 sed 删行
"
```

## 八、相关 skill

- `migrate-s3-to-k8s` —— 反向流程：S3 → K8s
- `clone-instance-memory` —— K8s 侧 Admin API 克隆
- `verify-fix-callback-dns` —— DNS / cloudflared 路由排错
- `k8s-via-bastion` —— JMS 用法详解
- `add-instances` —— K8s 批量新建（首选路径）
